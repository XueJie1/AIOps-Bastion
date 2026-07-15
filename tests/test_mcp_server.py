"""MCP Server in-process 测试 (§3.3/§10.4)。

经 mcp.shared.memory.create_connected_server_and_client_session 加载 MCP Server,
断言 4 工具契约 ({ok,data|error} JSON, spike-02 分层) + L3 缺 approval_id -> HITL_REJECTED。
无 stdio 子进程, 全在事件循环内 (§10.4 可测试性)。

注: session 经回调式 helper 持有 (entry+__aexit__ 同任务), 避免 pytest-asyncio 跨任务
cancel scope 报错 (spike-02 单 async 函数模式的等价)。
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.memory import create_connected_server_and_client_session

from aiops_bastion.execution import ExecResult
from aiops_bastion.mcp_server import build_server, extract_text
from aiops_bastion.permission_gate import PermissionGate
from aiops_bastion.store import InMemoryStore
from tests.fakes import FakeSSHExecutor


async def _with_session(
    executor: FakeSSHExecutor,
    store: InMemoryStore,
    execution_id: str,
    fn: Callable[[Any, list], Awaitable[Any]],
) -> Any:
    """回调式持有 in-process session: entry+exit 同任务, 避免 anyio 跨任务报错。

    fn(session, tools) -> 在 session 上下文内执行, 返回结果。
    """
    gate = PermissionGate(store)
    server = build_server(executor, gate, store, execution_id)
    async with create_connected_server_and_client_session(server) as sess:
        await sess.initialize()
        tools = await load_mcp_tools(sess)
        return await fn(sess, tools)


async def _call_tool(tools: list, name: str, args: dict) -> dict:
    """从已加载 tools 调指定工具, 提取内层 {ok,data|error} JSON。"""
    tool = next((t for t in tools if t.name == name), None)
    assert tool is not None, f"工具 {name} 未加载"
    raw = await tool.ainvoke(args)
    return json.loads(extract_text(raw))


def _default_executor() -> FakeSSHExecutor:
    return FakeSSHExecutor(
        responses={"node-a": ExecResult(exit_code=0, stdout="active")},
        default_stdout="active", default_exit=0,
    )


# === list_tools ===

async def test_list_tools() -> None:
    store = InMemoryStore()
    await store.create_investigation("exec-1")

    async def body(_sess, tools):
        return sorted(t.name for t in tools)

    names = await _with_session(_default_executor(), store, "exec-1", body)
    assert names == ["execute_discovery", "execute_remediation", "fetch_service_logs", "submit_journal"]


# === L1 execute_discovery 契约 ===

async def test_execute_discovery_contract() -> None:
    store = InMemoryStore()
    await store.create_investigation("exec-1")

    async def body(_sess, tools):
        return await _call_tool(tools, "execute_discovery", {
            "target_host": "node-a", "service_name": "nginx", "form": "systemd",
        })

    payload = await _with_session(_default_executor(), store, "exec-1", body)
    assert payload["ok"] is True
    assert payload["data"]["status"] == "active"   # exit 0 -> active
    assert payload["data"]["target_host"] == "node-a"


async def test_execute_discovery_rejects_metachar() -> None:
    """注入对抗在 MCP 路径同样生效 (IDENT_RE 防御#1)。"""
    store = InMemoryStore()
    await store.create_investigation("exec-1")

    async def body(_sess, tools):
        return await _call_tool(tools, "execute_discovery", {
            "target_host": "node-a", "service_name": "nginx; rm -rf /", "form": "systemd",
        })

    payload = await _with_session(_default_executor(), store, "exec-1", body)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "VALIDATION_ERROR"


# === L2 fetch_service_logs 契约 ===

async def test_fetch_service_logs_contract() -> None:
    store = InMemoryStore()
    await store.create_investigation("exec-1")

    async def body(_sess, tools):
        return await _call_tool(tools, "fetch_service_logs", {
            "target_host": "node-a", "service_name": "nginx", "form": "systemd", "lines": 100,
        })

    payload = await _with_session(_default_executor(), store, "exec-1", body)
    assert payload["ok"] is True
    assert "logs" in payload["data"]
    assert "truncated" in payload["data"]


async def test_fetch_service_logs_lines_bounds() -> None:
    """lines 超 500 -> VALIDATION_ERROR (§5.3)。"""
    store = InMemoryStore()
    await store.create_investigation("exec-1")

    async def body(_sess, tools):
        return await _call_tool(tools, "fetch_service_logs", {
            "target_host": "node-a", "service_name": "nginx", "form": "systemd", "lines": 99999,
        })

    payload = await _with_session(_default_executor(), store, "exec-1", body)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "VALIDATION_ERROR"


# === L2 submit_journal 契约 ===

async def test_submit_journal_writes_record() -> None:
    store = InMemoryStore()
    await store.create_investigation("exec-1")

    async def body(_sess, tools):
        return await _call_tool(tools, "submit_journal", {
            "record_type": "observation", "content": "nginx active",
        })

    payload = await _with_session(_default_executor(), store, "exec-1", body)
    assert payload["ok"] is True
    assert payload["data"]["record_type"] == "observation"
    # Record 写入 Store
    recs = await store.list_records("exec-1")
    assert len(recs) == 1
    assert recs[0].record_type == "observation"
    assert recs[0].content == "nginx active"


# === L3 execute_remediation 契约 ===

async def test_execute_remediation_missing_approval() -> None:
    """L3 缺 approval_id -> HITL_REJECTED (防御性, §3.3)。"""
    store = InMemoryStore()
    await store.create_investigation("exec-1")

    async def body(_sess, tools):
        return await _call_tool(tools, "execute_remediation", {
            "target_host": "node-a", "action_type": "restart_service", "params": {"unit": "nginx"},
        })

    payload = await _with_session(_default_executor(), store, "exec-1", body)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "HITL_REJECTED"


async def test_execute_remediation_pending_not_approved() -> None:
    """approval_id 存在但未审批 (PENDING) -> HITL_REJECTED (C2 断言3)。"""
    store = InMemoryStore()
    await store.create_investigation("exec-2")
    req = await store.create_hitl_request(
        "exec-2", target_host="node-a", action_type="restart_service",
        rendered_cmd="systemctl restart nginx", impact="重启",
    )

    async def body(_sess, tools):
        return await _call_tool(tools, "execute_remediation", {
            "target_host": "node-a", "action_type": "restart_service",
            "params": {"unit": "nginx"}, "approval_id": req.approval_id,
        })

    payload = await _with_session(_default_executor(), store, "exec-2", body)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "HITL_REJECTED"


async def test_execute_remediation_approved_executes_and_consumes() -> None:
    """approval_id APPROVED -> 执行 + 一次性消费 (C2 断言1: 二次复用被拒)。"""
    store = InMemoryStore()
    await store.create_investigation("exec-3")
    executor = FakeSSHExecutor(
        responses={"node-a": ExecResult(exit_code=0, stdout="restarted")}, default_exit=0,
    )
    req = await store.create_hitl_request(
        "exec-3", target_host="node-a", action_type="restart_service",
        rendered_cmd="systemctl restart nginx", impact="重启",
    )
    await store.approve_hitl(req.approval_id, decided_by="alice")

    results: list[dict] = []

    async def body(_sess, tools):
        args = {"target_host": "node-a", "action_type": "restart_service",
                "params": {"unit": "nginx"}, "approval_id": req.approval_id}
        results.append(await _call_tool(tools, "execute_remediation", args))
        results.append(await _call_tool(tools, "execute_remediation", args))   # C2 二次

    await _with_session(executor, store, "exec-3", body)

    # 首次 -> ok, 执行
    assert results[0]["ok"] is True
    assert results[0]["data"]["exit_code"] == 0
    assert any(c.method == "remediation" for c in executor.calls)
    # C2: 二次 -> HITL_REJECTED (已 CONSUMED)
    assert results[1]["ok"] is False
    assert results[1]["error"]["code"] == "HITL_REJECTED"


async def test_unknown_tool_rejected() -> None:
    """白名单外工具名 -> VALIDATION_ERROR (§4.3, 直接调 session.call_tool)。"""
    store = InMemoryStore()
    await store.create_investigation("exec-1")

    async def body(sess, _tools):
        result = await sess.call_tool("rm", {"path": "/"})
        text = extract_text(result.content)
        return json.loads(text)

    payload = await _with_session(_default_executor(), store, "exec-1", body)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "VALIDATION_ERROR"
