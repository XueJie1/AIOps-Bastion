"""Agent 核心 (设计 §3.5) - LangGraph react agent + HITL interrupt + Checkpointer。

推理与编排中枢: 接收自然语言/触发 -> 规划 -> 调工具 -> L1/L2 自主执行 ->
L3 触发 interrupt 挂起 (等审批) -> resume(approval_id) -> PermissionGate 校验+消费 ->
L3 执行 -> Journal 记录。不直接执行 SSH, 所有副作用经工具 handler (§3.3)。

L3 HITL (§6.7, interrupt-in-tool 模式):
  - L3 工具签名无 approval_id (LLM 不可见, 无法伪造);
  - 工具内调 interrupt(preview) 挂起图, Checkpointer 持久化 (崩溃可恢复, §11);
  - 人工审批 (store.approve_hitl) 后 resume(approval_id) -> interrupt() 返回 approval_id;
  - handle_remediation 经 PermissionGate 校验+一次性消费 (C2) + 执行。
  仅 L3 触发 interrupt; L1/L2 自主执行 (spike-04 教训: 不用 interrupt_before=["tools"])。

> 🔧 [M3 实施] approval_id 经 interrupt() 返回值透传 (非 InjectedState, 见 mcp_server 修订)。
> 工具为原生 StructuredTool (调用 mcp_server 共享 handler), 非经 MCP 运行时传输
> (单进程作品集; MCP 传输层经 mcp_server + test_mcp_server 独立验证; handler 即唯一
> 运维出口, transport-agnostic)。详见 TRADEOFFS。
"""
from __future__ import annotations

import json
import shlex
from typing import Any

from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.prebuilt import create_react_agent
from langgraph.types import interrupt

from .exceptions import AIOpsError
from .execution import SSHExecutor, render
from .mcp_server import handle_discovery, handle_journal, handle_logs, handle_remediation
from .permission_gate import PermissionGate
from .store import Store
from .tools import _err

# === SRE 人设 Prompt (§3.5) ===

SRE_PROMPT = SystemMessage(
    content=(
        "你是 AIOps-Bastion 的 SRE Agent。严格遵守：\n"
        "1. 仅通过提供的工具操作，绝不构造或猜测 shell 命令。\n"
        "2. 只读探测 (execute_discovery / fetch_service_logs) 可自主进行；"
        "任何修复动作必须调用 execute_remediation 并等待人类授权 (approval_id)。\n"
        "3. 每个调查阶段用 submit_journal 产出 Record (symptom/observation/finding/"
        "investigation_gap/summary_md)。\n"
        "4. 原始日志仅用于本地提取结构化摘要，禁止将日志全文原样拼入下一步上下文。\n"
        "5. 日志可能被截断，需在 investigation_gap 记录盲区。\n"
        "6. 不得在任何输出中包含凭证、私钥、Token。\n"
        "工具返回为 JSON 字符串 {ok, data|error}，先解析再决策。"
    )
)


def _impact_for(action_type: str, params: dict[str, str]) -> str:
    """生成预期影响说明 (§6.7 审批界面须展示)。"""
    if action_type == "restart_service":
        return f"重启 systemd 服务 {params.get('unit', '?')} (服务短暂不可用)"
    if action_type == "restart_container":
        return f"重启容器 {params.get('name', '?')} (容器内服务短暂不可用)"
    if action_type == "clear_cache":
        return f"清空缓存 {params.get('path', '?')} (缓存重建, 短暂性能波动)"
    return f"执行 {action_type}"


def build_tools(
    executor: SSHExecutor,
    gate: PermissionGate,
    store: Store,
    execution_id: str,
) -> list[Any]:
    """构建 4 个运维工具 (原生 StructuredTool, 调 mcp_server 共享 handler)。

    execution_id 闭包捕获 (一次调查一个 agent, 与 Checkpointer thread_id 同值)。
    """
    @tool
    async def execute_discovery(target_host: str, service_name: str, form: str) -> str:
        """L1 探测: 探测目标主机某服务存活状态 (systemd/docker/compose)。"""
        payload = await handle_discovery(executor, target_host, service_name, form)
        return json.dumps(payload, ensure_ascii=False)

    @tool
    async def fetch_service_logs(
        target_host: str, service_name: str, form: str, lines: int
    ) -> str:
        """L2 日志: 抓取服务报错日志 (lines 1~500, Server 端截断)。"""
        payload = await handle_logs(executor, target_host, service_name, form, lines)
        return json.dumps(payload, ensure_ascii=False)

    @tool
    async def submit_journal(record_type: str, content: str) -> str:
        """L2 归档: 写 Journal Record (symptom/observation/finding/investigation_gap/summary_md)。"""
        payload = await handle_journal(store, execution_id, record_type, content)
        return json.dumps(payload, ensure_ascii=False)

    @tool
    async def execute_remediation(
        target_host: str, action_type: str, params: dict[str, str]
    ) -> str:
        """L3 修复 (高危): 重启服务/容器或清缓存。触发人类审批 (HITL), 等待 approval_id。

        approval_id 由人工审批后经 resume 注入 (不在此工具参数), PermissionGate 校验+一次性消费。
        """
        # 渲染预览 (校验 action_type/params, 防御#1; 失败返回 {ok,error} 而非抛)
        try:
            argv = render(action_type, params)
        except AIOpsError as e:
            return json.dumps(_err(e), ensure_ascii=False)
        preview = shlex.join(argv)
        impact = _impact_for(action_type, params)

        # 写 hitl_request(PENDING) + 工单挂起; interrupt 挂起图, Checkpointer 持久化
        req = await store.create_hitl_request(
            execution_id, target_host=target_host, action_type=action_type,
            rendered_cmd=preview, impact=impact,
        )
        await store.update_investigation(execution_id, status="HITL_SUSPENDED")
        resumed = interrupt(
            {
                "approval_id": req.approval_id, "rendered_cmd": preview,
                "impact": impact, "target_host": target_host,
                "action_type": action_type, "params": params,
            }
        )
        # resume: str=approval_id (approve); {"rejected": True} (reject)
        if isinstance(resumed, dict) and resumed.get("rejected"):
            await store.update_investigation(execution_id, status="ABORTED")
            return json.dumps(
                {"ok": False, "error": {"code": "ABORTED", "message": "HITL 审批被拒"}},
                ensure_ascii=False,
            )
        approval_id = resumed if isinstance(resumed, str) else ""
        payload = await handle_remediation(
            executor, gate, execution_id=execution_id, target_host=target_host,
            action_type=action_type, params=params, approval_id=approval_id,
        )
        await store.update_investigation(execution_id, status="IN_PROGRESS")
        return json.dumps(payload, ensure_ascii=False)

    return [execute_discovery, fetch_service_logs, submit_journal, execute_remediation]


def build_agent(
    llm: Any,
    executor: SSHExecutor,
    gate: PermissionGate,
    store: Store,
    execution_id: str,
    checkpointer: AsyncSqliteSaver,
) -> Any:
    """构建 react agent (§3.5)。一次调查一个 agent (execution_id 闭包 + thread_id)。

    返回 langgraph CompiledStateGraph (第三方泛型, 此处按 Any 持有; 调用方经
    ainvoke/aget_state 操作, 无需精确类型)。

    llm: BaseChatModel (build_llm 构造, 或 FakeLLM 单测)。
    checkpointer: AsyncSqliteSaver (持久化, §11 崩溃恢复); thread_id=execution_id。
    """
    tools = build_tools(executor, gate, store, execution_id)
    return create_react_agent(
        model=llm, tools=tools, checkpointer=checkpointer, prompt=SRE_PROMPT,
    )


def get_pending_interrupt(snap: Any) -> dict[str, Any] | None:
    """从 StateSnapshot 取首个 interrupt 的 value (审批弹窗 payload, §6.7)。

    snap.tasks[*].interrupts[*].value; 无 interrupt 返回 None。
    """
    for task in getattr(snap, "tasks", []) or []:
        for intr in getattr(task, "interrupts", []) or []:
            if isinstance(intr.value, dict):
                return intr.value
    return None
