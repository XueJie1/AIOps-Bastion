"""MCP Server (设计 §3.3) - in-process, 暴露运维工具的 JSON-RPC 接口。

所有 SSH 副作用经 MCP 工具发生 (§3.3 职责与边界)。in-process 加载经
mcp.shared.memory.create_connected_server_and_client_session, 绕过 stdio 子进程
边界, 供单进程作品集与 CI (§10.4)。

工具与权限分级 (§4.2):
  - execute_discovery  (L1 探测): 委托 tools.execute_discovery (M2)
  - fetch_service_logs (L2 日志): 委托 tools.fetch_service_logs (M2, Server 端截断)
  - submit_journal     (L2 归档): 写 Record 到 Store (§6.5)
  - execute_remediation (L3 修复): PermissionGate 校验 + 一次性消费 (C2) + 执行

统一返回 (§5): TextContent(text=<{ok,data|error} JSON>)。外层 MCP content block,
内层 {ok,data} JSON (spike-02 分层)。

L3 interrupt (§6.7): 不在 MCP Server 侧 (子进程模型下不可行, §3.3 [P1-4]),
而在 Agent 侧 LangGraph interrupt()。MCP execute_remediation 仅做"给定 approval_id
即校验+执行"的原语 (防御性: 缺 approval_id -> HITL_REJECTED)。interrupt/审批决策
由 agent.py 的原生 L3 工具持有 (MCP-loaded 工具无法调 langgraph interrupt)。

> 🔧 [M3 实施] 修改说明: approval_id 透传经 interrupt() 返回值 (Pattern 2,
> L3-only interrupt 自动), 非设计 §3.3 推荐的 InjectedState (方案 A)。
> 二者均使 approval_id 不进 LLM 可见 schema (L3 工具签名无 approval_id 字段),
> 安全语义一致; interrupt-in-tool 更简单且消除 InjectedState 在 resume 重注入的
> 不确定性 (langgraph 1.2.7 行为)。详见 agent.py + 设计 §3.3 修订。
"""
from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types
from mcp.server import Server

from .exceptions import AIOpsError, HITLRejectedError
from .execution import SSHExecutor
from .permission_gate import PermissionGate
from .store import Record, Store
from .tools import _err, _ok, execute_discovery, fetch_service_logs

# === Schema (§5.2/§5.3/§5.6, 对齐 M2 tools.py) ===

DISCOVERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["target_host", "service_name", "form"],
    "properties": {
        "target_host": {"type": "string", "description": "目标主机 host_id (IDENT_RE)"},
        "service_name": {"type": "string", "description": "服务名 (IDENT_RE)"},
        "form": {"type": "string", "enum": ["systemd", "docker", "compose"]},
    },
}

LOGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["target_host", "service_name", "form", "lines"],
    "properties": {
        "target_host": {"type": "string"},
        "service_name": {"type": "string"},
        "form": {"type": "string", "enum": ["systemd", "docker"]},
        "lines": {"type": "integer", "description": "1~500 (build_logs_cmd 强制, §5.3)"},
    },
}

JOURNAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["record_type", "content"],
    "properties": {
        "record_type": {
            "type": "string",
            "enum": ["symptom", "observation", "finding", "investigation_gap", "summary_md"],
        },
        "content": {"type": "string", "maxLength": 16000},
    },
}

REMEDIATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["target_host", "action_type", "params"],
    "properties": {
        "target_host": {"type": "string"},
        "action_type": {"type": "string", "enum": ["restart_service", "restart_container", "clear_cache"]},
        "params": {"type": "object"},
        "approval_id": {"type": "string", "description": "resume 时注入; PermissionGate 校验"},
    },
}


# === 工具 handler (transport-agnostic, 被 MCP @call_tool 与 agent 原生工具共用) ===

async def handle_discovery(
    executor: SSHExecutor, target_host: str, service_name: str, form: str
) -> dict[str, Any]:
    """L1 探测: 委托 tools.execute_discovery (M2, 含校验+状态映射)。"""
    return await execute_discovery(executor, target_host, service_name, form)


async def handle_logs(
    executor: SSHExecutor, target_host: str, service_name: str, form: str, lines: int
) -> dict[str, Any]:
    """L2 日志: 委托 tools.fetch_service_logs (M2, Server 端截断)。"""
    return await fetch_service_logs(executor, target_host, service_name, form, lines)


async def handle_journal(
    store: Store, execution_id: str, record_type: str, content: str
) -> dict[str, Any]:
    """L2 归档: 写 Journal Record 到 Store (§6.5)。best-effort, 不阻塞状态流转。"""
    try:
        await store.add_record(
            execution_id, Record(record_type=record_type, content=content),  # type: ignore[arg-type]
        )
    except Exception as e:   # noqa: BLE001 - journal best-effort, 不阻塞
        return {"ok": False, "error": {"code": "INTERNAL", "message": f"journal 写入失败: {e}"}}
    return _ok({"execution_id": execution_id, "record_type": record_type})


async def handle_remediation(
    executor: SSHExecutor,
    gate: PermissionGate,
    *,
    execution_id: str,
    target_host: str,
    action_type: str,
    params: dict[str, str],
    approval_id: str,
) -> dict[str, Any]:
    """L3 修复原语: PermissionGate 校验+一次性消费 (C2) + 执行。

    approval_id 须非空 (Agent 侧 interrupt 已确保 resume 后注入); 缺失 -> HITL_REJECTED
    (防御性, §3.3)。executor.run_remediation 内部仍校验 approval_id 存在 (第二道)。
    """
    if not approval_id:
        return _err(HITLRejectedError("L3 修复缺 approval_id (PermissionGate 授权失败)"))
    try:
        await gate.validate_and_consume(
            approval_id, execution_id=execution_id,
            target_host=target_host, action_type=action_type,
        )
        result = await executor.run_remediation(
            target_host, action_type, params, approval_id=approval_id,
        )
    except AIOpsError as e:
        return _err(e)
    return _ok({
        "target_host": target_host, "action_type": action_type,
        "exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr,
    })


# === MCP content block 编解码 (spike-02 分层) ===

def _text(payload: dict[str, Any]) -> list[mcp_types.TextContent]:
    """{ok,data|error} dict -> MCP TextContent 列表 (外层 block, 内层 JSON)。"""
    return [mcp_types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def extract_text(result: Any) -> str:
    """从 MCP 工具返回取首个 text block 的 text 字段 (spike-02)。

    Agent 读取工具结果时用: extract_text(result) -> json.loads -> {ok,data}。
    """
    if isinstance(result, list) and result:
        block = result[0]
        if isinstance(block, dict):
            return str(block.get("text", ""))
        return str(getattr(block, "text", block))
    if isinstance(result, str):
        return result
    return str(result)


# === in-process Server 构建 ===

def build_server(
    executor: SSHExecutor,
    gate: PermissionGate,
    store: Store,
    execution_id: str,
) -> Server:
    """构建 in-process MCP Server (§3.3/§10.4)。

    execution_id 绑定到本 server (一次调查一个 server/session, 与 Agent 同生命周期,
    spike-02 约束)。submit_journal / execute_remediation 的 execution_id 来自此绑定。
    """
    server: Server = Server("aiops-bastion")

    @server.list_tools()   # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(name="execute_discovery", description="L1 探测服务存活状态",
                           inputSchema=DISCOVERY_SCHEMA),
            mcp_types.Tool(name="fetch_service_logs", description="L2 抓取服务日志 (Server 端截断)",
                           inputSchema=LOGS_SCHEMA),
            mcp_types.Tool(name="submit_journal", description="L2 写 Journal Record",
                           inputSchema=JOURNAL_SCHEMA),
            mcp_types.Tool(name="execute_remediation",
                           description="L3 修复 (高危, 需 approval_id, PermissionGate 校验)",
                           inputSchema=REMEDIATION_SCHEMA),
        ]

    @server.call_tool()   # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
        if name == "execute_discovery":
            payload = await handle_discovery(
                executor, arguments["target_host"], arguments["service_name"], arguments["form"],
            )
            return _text(payload)
        if name == "fetch_service_logs":
            payload = await handle_logs(
                executor, arguments["target_host"], arguments["service_name"],
                arguments["form"], arguments["lines"],
            )
            return _text(payload)
        if name == "submit_journal":
            payload = await handle_journal(
                store, execution_id, arguments["record_type"], arguments["content"],
            )
            return _text(payload)
        if name == "execute_remediation":
            payload = await handle_remediation(
                executor, gate, execution_id=execution_id,
                target_host=arguments["target_host"], action_type=arguments["action_type"],
                params=arguments.get("params", {}), approval_id=arguments.get("approval_id", ""),
            )
            return _text(payload)
        # 白名单外工具 -> 拒绝 (§4.3)
        return _text({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": f"unknown tool {name}"}})

    return server
