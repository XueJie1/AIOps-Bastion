"""
Spike 04 — 端到端 HITL 闭环 (集成 01+02+03)

假设: 真 LLM 驱动 Agent 选 L3 工具 -> interrupt 挂起 -> resume ->
      MCP 工具执行一次 (PermissionGate 校验 approval_id)。

架构:
  - DeepSeek (deepseek-chat) 作为 Agent LLM
  - MCP in-process Server 提供 execute_discovery (L1) + restart_service (L3)
  - LangGraph create_react_agent + checkpointer (SqliteSaver)
  - interrupt_before=["tools"] 模拟 HITL (注: 真实实现应仅 L3 中断, spike 验证机制本身)
  - resume 注入 approval_id, 模拟 PermissionGate 校验

PASS: DeepSeek 正确选 restart_service -> interrupt 挂起 -> resume ->
      MCP 工具执行一次 -> 图完成; L3 exec 计数=1
FAIL: 定位是 LLM 选错 / interrupt 不挂起 / MCP 校验失败 哪一环

注: MCP session 须与 Agent 同生命周期 (02 发现), 整个流程在一个 async 上下文内。
"""
import os
import sys
import json
import asyncio
import sqlite3
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from mcp.server import Server
import mcp.types as mcp_types
from mcp.shared.memory import create_connected_server_and_client_session
from langchain_mcp_adapters.tools import load_mcp_tools


# ---------- 副作用计数 (跨进程, 验证 L3 不重复执行) ----------
COUNTER_FILE = Path(tempfile.gettempdir()) / "spike04_counter.json"
CHECKPOINTER_DB = Path(tempfile.gettempdir()) / "spike04_checkpoints.sqlite"
THREAD_ID = "spike-04-thread"


def _read_counter() -> dict:
    if COUNTER_FILE.exists():
        return json.loads(COUNTER_FILE.read_text())
    return {"discovery_calls": 0, "l3_exec_calls": 0}


def _write_counter(d: dict) -> None:
    COUNTER_FILE.write_text(json.dumps(d))


def reset_counter() -> None:
    _write_counter({"discovery_calls": 0, "l3_exec_calls": 0})


# ---------- MCP Server (in-process) ----------
def build_server() -> Server:
    server = Server("spike-04")

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name="execute_discovery",
                description="探测目标主机某服务的存活状态。form 取值 systemd/docker/compose。调用此工具查明服务是否 active。",
                inputSchema={
                    "type": "object",
                    "required": ["target_host", "service_name", "form"],
                    "properties": {
                        "target_host": {"type": "string"},
                        "service_name": {"type": "string"},
                        "form": {"type": "string", "enum": ["systemd", "docker", "compose"]},
                    },
                },
            ),
            mcp_types.Tool(
                name="restart_service",
                description="重启目标主机上的 systemd 服务 (L3 高危操作, 需 HITL 审批)。",
                inputSchema={
                    "type": "object",
                    "required": ["target_host", "unit"],
                    "properties": {
                        "target_host": {"type": "string"},
                        "unit": {"type": "string"},
                        "approval_id": {"type": "string", "description": "resume 时注入, PermissionGate 校验"},
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[mcp_types.TextContent]:
        c = _read_counter()
        if name == "execute_discovery":
            c["discovery_calls"] += 1
            _write_counter(c)
            return [mcp_types.TextContent(type="text", text=json.dumps({
                "ok": True,
                "data": {"target_host": arguments.get("target_host"),
                         "service_name": arguments.get("service_name"),
                         "status": "inactive", "detail": "service is down"},
            }))]
        if name == "restart_service":
            # PermissionGate: 校验 approval_id (spike 简化: 仅检查存在)
            approval_id = arguments.get("approval_id")
            if not approval_id:
                return [mcp_types.TextContent(type="text", text=json.dumps({
                    "ok": False, "error": {"code": "HITL_REJECTED", "message": "missing approval_id"},
                }))]
            c["l3_exec_calls"] += 1
            _write_counter(c)
            return [mcp_types.TextContent(type="text", text=json.dumps({
                "ok": True,
                "data": {"target_host": arguments.get("target_host"),
                         "unit": arguments.get("unit"), "exit_code": 0},
            }))]
        return [mcp_types.TextContent(type="text", text=json.dumps({
            "ok": False, "error": {"code": "VALIDATION_ERROR", "message": f"unknown tool {name}"}}))]

    return server


# ---------- 提取工具结果 ----------
def _extract_text(result) -> str:
    if isinstance(result, list) and result:
        block = result[0]
        if isinstance(block, dict):
            return block.get("text", "")
        return getattr(block, "text", str(block))
    if isinstance(result, str):
        return result
    return str(result)


def _extract_tool_messages_from_ai(state) -> list:
    """从 react agent 状态里找待执行的 tool_call。"""
    messages = state.get("messages", [])
    last_ai = None
    for m in reversed(messages):
        # 找最近一个带 tool_calls 的 AI 消息
        if hasattr(m, "tool_calls") and m.tool_calls:
            last_ai = m
            break
        if isinstance(m, dict) and m.get("tool_calls"):
            last_ai = m
            break
    if last_ai is None:
        return []
    if hasattr(last_ai, "tool_calls"):
        return last_ai.tool_calls
    return last_ai.get("tool_calls", [])


async def run_e2e():
    reset_counter()
    if CHECKPOINTER_DB.exists():
        CHECKPOINTER_DB.unlink()

    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key or api_key.startswith("sk-your"):
        print("FAIL: 未配置 DEEPSEEK_API_KEY")
        sys.exit(2)

    print("=" * 60)
    print("Spike 04: 端到端 HITL 闭环")
    print("=" * 60)

    llm = ChatOpenAI(
        model="deepseek-chat",
        api_key=api_key,
        base_url="https://api.deepseek.com/v1",
        temperature=0,
        max_tokens=1000,
    )

    server = build_server()

    # MCP session 须与 Agent 同生命周期
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        tools = await load_mcp_tools(session)
        print(f"MCP 工具加载: {[t.name for t in tools]}")

        # 注: create_react_agent 是异步路径, 须用 AsyncSqliteSaver (同步 SqliteSaver 不支持 async)
        # 这是相对 01 的关键差异: 01 手写同步图用 SqliteSaver 跑通; react agent 异步须 AsyncSqliteSaver
        async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINTER_DB)) as checkpointer:
            # 注: create_react_agent 用 interrupt_before=["tools"] 在工具执行前挂起
            # 真实实现: 应仅在 restart_service (L3) 时 interrupt, 非 L1/L2; spike 验证机制本身
            app = create_react_agent(
                model=llm,
                tools=tools,
                checkpointer=checkpointer,
                interrupt_before=["tools"],
                prompt=(
                    "你是 AIOps-Bastion 的 SRE Agent。严格遵守："
                    "1. 仅通过提供的 MCP 工具操作。"
                    "2. 任何修复动作必须调用 restart_service 并等待人类授权。"
                    "节点A (node-a) 上的 nginx 服务挂了，必须修复："
                    "先用 execute_discovery 查状态，确认 inactive 后必须用 restart_service 重启它（L3 高危，需等待审批）。"
                    "不能只探测不修复。"
                ),
            )

            config = {"configurable": {"thread_id": THREAD_ID}}

            # ---------- PHASE 1: 首次跑, 应在工具执行前 interrupt ----------
            print("\n--- PHASE 1: 首次执行, Agent 决策 + interrupt ---")
            result = await app.ainvoke({"messages": [("user", "node-a 的 nginx 挂了, 排查并修复")]}, config=config)
            # 注: AsyncSqliteSaver 须用 aget_state (异步), 同步 get_state 会报 InvalidStateError
            snap = await app.aget_state(config)
            print(f"  状态 next: {snap.next}")
            pending_tools = _extract_tool_messages_from_ai(snap.values)
            print(f"  待执行 tool_calls: {pending_tools}")

            if not pending_tools:
                print("FAIL: Agent 未发出 tool_call")
                print(f"  messages: {result.get('messages')}")
                return False

            # ---------- PHASE 2: 模拟人工审批, resume ----------
            print("\n--- PHASE 2: resume (注入 approval_id), 模拟 HITL approve ---")
            result2 = await app.ainvoke(Command(resume={"approval_id": "approval-04-001"}), config=config)
            snap2 = await app.aget_state(config)
            print(f"  resume 后 next: {snap2.next}")

            # ---------- PHASE 3: 继续到完成 (Agent 多轮决策) ----------
            print("\n--- PHASE 3: 继续 Agent 循环到完成 ---")
            for i in range(8):
                if not snap2.next:  # END
                    print(f"  第 {i+1} 轮: 已到达 END")
                    break
                print(f"  第 {i+1} 轮: 仍有 next={snap2.next}, 继续 resume")
                result2 = await app.ainvoke(Command(resume={"approval_id": f"approval-04-{i+2:03d}"}), config=config)
                snap2 = await app.aget_state(config)
                pending = _extract_tool_messages_from_ai(snap2.values)
                print(f"    待执行: {pending}, next: {snap2.next}")

    # ---------- 断言 ----------
    print("\n" + "=" * 60)
    print("验证结果")
    print("=" * 60)
    final_counter = _read_counter()
    print(f"最终计数器: {final_counter}")

    failures = []
    # execute_discovery 应被调用 (L1 探测)
    if final_counter["discovery_calls"] < 1:
        failures.append(f"FAIL: discovery_calls 期望 >=1, 实际 {final_counter['discovery_calls']}")
    # restart_service 应被调用恰好 1 次 (L3 执行一次, 不重复)
    if final_counter["l3_exec_calls"] != 1:
        failures.append(f"FAIL: l3_exec_calls 期望 1, 实际 {final_counter['l3_exec_calls']}")

    print("-" * 40)
    if failures:
        for f in failures:
            print(f)
        print("\n>>> 04 结论: FAIL")
        sys.exit(1)

    print(f"discovery_calls={final_counter['discovery_calls']} (L1 探测执行) ✓")
    print(f"l3_exec_calls={final_counter['l3_exec_calls']} (L3 执行一次, 不重复) ✓")
    print("DeepSeek 正确选 restart_service (L3) ✓")
    print("interrupt + resume 闭环成立 ✓")
    print("\n>>> 04 结论: PASS")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(run_e2e())
