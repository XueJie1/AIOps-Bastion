"""Agent 全图测试 (§3.5/§6.7/§6.8, interrupt-in-tool 模式)。

FakeLLM 驱动 react agent 全图, 无网/无真 LLM:
  - L1 探测 -> L2 日志 -> L3 interrupt (断言 rendered_cmd 在 interrupt 值) ->
    resume(approval_id) -> 消费 -> L3 执行恰好 1 次 (不重放);
  - reject -> ABORTED, L3 未执行;
  - approval_id 复用 -> HITL_REJECTED (C2);
  - 注入参数 (元字符 unit) -> 拒绝。

标 @pytest.mark.crash (interrupt/resume 崩溃恢复语义, §6.8/§11)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from aiops_bastion.agent import build_agent, get_pending_interrupt
from aiops_bastion.exceptions import HITLRejectedError
from aiops_bastion.execution import ExecResult
from aiops_bastion.llm import FakeLLM
from aiops_bastion.permission_gate import PermissionGate
from aiops_bastion.store import InMemoryStore
from tests.fakes import FakeSSHExecutor

pytestmark = pytest.mark.crash

# === fixtures ===

@pytest.fixture
async def checkpointer(tmp_path: Path) -> AsyncSqliteSaver:
    """AsyncSqliteSaver (持久化, §11)。from_conn_string 是 async context manager。"""
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "agent.sqlite")) as cp:
        yield cp


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def executor() -> FakeSSHExecutor:
    return FakeSSHExecutor(
        responses={"node-a": ExecResult(exit_code=0, stdout="active")},
        default_stdout="active", default_exit=0,
    )


@pytest.fixture
def gate(store: InMemoryStore) -> PermissionGate:
    return PermissionGate(store)


def _cfg(execution_id: str) -> dict:
    return {"configurable": {"thread_id": execution_id}}


# === happy path: L1 -> L2 -> L3 interrupt -> approve -> resume -> 执行 1 次 ===

async def test_full_loop_l3_interrupt_resume_executes_once(
    store: InMemoryStore, executor: FakeSSHExecutor, gate: PermissionGate,
    checkpointer: AsyncSqliteSaver,
) -> None:
    await store.create_investigation("exec-1")
    llm = FakeLLM(script=[
        # 1. L1 探测
        {"tool_calls": [{"name": "execute_discovery",
                         "args": {"target_host": "node-a", "service_name": "nginx", "form": "systemd"}}]},
        # 2. L2 日志
        {"tool_calls": [{"name": "fetch_service_logs",
                         "args": {"target_host": "node-a", "service_name": "nginx", "form": "systemd", "lines": 50}}]},
        # 3. L3 修复 -> 触发 interrupt
        {"tool_calls": [{"name": "execute_remediation",
                         "args": {"target_host": "node-a", "action_type": "restart_service",
                                  "params": {"unit": "nginx"}}}]},
        # 4. resume 后 -> 完成
        "调查完成, nginx 已重启",
    ])
    app = build_agent(llm, executor, gate, store, "exec-1", checkpointer)
    cfg = _cfg("exec-1")

    # Phase 1: 跑到 L3 interrupt
    await app.ainvoke({"messages": [("user", "node-a nginx 挂了, 排查并修复")]}, config=cfg)
    snap = await app.aget_state(cfg)
    # react agent 在 tools 节点前 interrupt (interrupt-in-tool 触发)
    assert snap.next  # 挂起中

    intr = get_pending_interrupt(snap)
    assert intr is not None, "L3 应触发 interrupt"
    assert "approval_id" in intr
    assert "rendered_cmd" in intr
    assert "systemctl restart nginx" in intr["rendered_cmd"]
    assert intr["target_host"] == "node-a"

    # 工单状态 -> HITL_SUSPENDED
    inv = await store.get_investigation("exec-1")
    assert inv is not None
    assert inv.status == "HITL_SUSPENDED"

    # Phase 2: 审批 + resume
    await store.approve_hitl(intr["approval_id"], decided_by="alice")
    await app.ainvoke(Command(resume=intr["approval_id"]), config=cfg)

    snap2 = await app.aget_state(cfg)
    assert not snap2.next   # END

    # L3 恰好执行 1 次 (不重放)
    rem_calls = [c for c in executor.calls if c.method == "remediation"]
    assert len(rem_calls) == 1
    # approval_id 已消费 (CONSUMED, C2)
    req = await store.get_hitl_request(intr["approval_id"])
    assert req is not None
    assert req.status == "CONSUMED"

    # 最后一条消息是 LLM 完成语
    msgs = snap2.values.get("messages", [])
    assert msgs and "完成" in msgs[-1].content


# === reject 路径 -> ABORTED, L3 未执行 ===

async def test_reject_aborts_no_execution(
    store: InMemoryStore, executor: FakeSSHExecutor, gate: PermissionGate,
    checkpointer: AsyncSqliteSaver,
) -> None:
    await store.create_investigation("exec-2")
    llm = FakeLLM(script=[
        {"tool_calls": [{"name": "execute_remediation",
                         "args": {"target_host": "node-a", "action_type": "restart_service",
                                  "params": {"unit": "nginx"}}}]},
        "审批被拒, 调查中止",
    ])
    app = build_agent(llm, executor, gate, store, "exec-2", checkpointer)
    cfg = _cfg("exec-2")

    await app.ainvoke({"messages": [("user", "重启 node-a nginx")]}, config=cfg)
    snap = await app.aget_state(cfg)
    intr = get_pending_interrupt(snap)
    assert intr is not None

    # reject: resume({"rejected": True})
    await app.ainvoke(Command(resume={"rejected": True}), config=cfg)
    snap2 = await app.aget_state(cfg)
    assert not snap2.next

    # L3 未执行
    rem_calls = [c for c in executor.calls if c.method == "remediation"]
    assert len(rem_calls) == 0
    # 工单 ABORTED
    inv = await store.get_investigation("exec-2")
    assert inv is not None
    assert inv.status == "ABORTED"
    # hitl_request 仍 PENDING (未审批, 未消费)
    req = await store.get_hitl_request(intr["approval_id"])
    assert req is not None
    assert req.status == "PENDING"


# === C2: approval_id 复用被拒 ===

async def test_approval_id_reuse_rejected(
    store: InMemoryStore, executor: FakeSSHExecutor, gate: PermissionGate,
    checkpointer: AsyncSqliteSaver,
) -> None:
    """resume 后 approval_id 已消费 (CONSUMED); 二次消费同一 id -> HITLRejectedError。"""
    await store.create_investigation("exec-3")
    llm = FakeLLM(script=[
        {"tool_calls": [{"name": "execute_remediation",
                         "args": {"target_host": "node-a", "action_type": "restart_service",
                                  "params": {"unit": "nginx"}}}]},
        "完成",
    ])
    app = build_agent(llm, executor, gate, store, "exec-3", checkpointer)
    cfg = _cfg("exec-3")

    await app.ainvoke({"messages": [("user", "重启 nginx")]}, config=cfg)
    snap = await app.aget_state(cfg)
    intr = get_pending_interrupt(snap)
    assert intr is not None
    await store.approve_hitl(intr["approval_id"], decided_by="alice")
    await app.ainvoke(Command(resume=intr["approval_id"]), config=cfg)

    # 首次消费成功
    req = await store.get_hitl_request(intr["approval_id"])
    assert req is not None and req.status == "CONSUMED"

    # C2: 直接经 PermissionGate 二次消费 -> HITLRejectedError
    with pytest.raises(HITLRejectedError, match="状态非 APPROVED|已消费"):
        await gate.validate_and_consume(
            intr["approval_id"], execution_id="exec-3", target_host="node-a",
            action_type="restart_service",
        )


# === 注入: 元字符 unit 被拒 (IDENT_RE 防御#1 在 agent 路径) ===

async def test_metachar_in_unit_rejected(
    store: InMemoryStore, executor: FakeSSHExecutor, gate: PermissionGate,
    checkpointer: AsyncSqliteSaver,
) -> None:
    """L3 unit 含元字符 -> render 拒绝 (CommandValidationError), 工具返回 {ok,error}。"""
    await store.create_investigation("exec-4")
    llm = FakeLLM(script=[
        {"tool_calls": [{"name": "execute_remediation",
                         "args": {"target_host": "node-a", "action_type": "restart_service",
                                  "params": {"unit": "nginx; rm -rf /"}}}]},
        # render 拒绝后, 工具返回 error; Agent 应结束 (脚本耗尽)
    ])
    app = build_agent(llm, executor, gate, store, "exec-4", checkpointer)
    cfg = _cfg("exec-4")

    await app.ainvoke({"messages": [("user", "重启 nginx")]}, config=cfg)
    snap = await app.aget_state(cfg)

    # 元字符 unit -> render 抛 -> 工具返回 {ok:false,VALIDATION_ERROR}
    # 无 approval_id 消费, 无 L3 执行
    rem_calls = [c for c in executor.calls if c.method == "remediation"]
    assert len(rem_calls) == 0
    # 没有产生 PENDING hitl_request (render 在 create_hitl_request 之前失败)
    # (不挂起, react agent 继续到脚本耗尽 -> END)
    assert not snap.next or get_pending_interrupt(snap) is None


# === L1/L2 自主执行 (不触发 interrupt) ===

async def test_l1_l2_autonomous_no_interrupt(
    store: InMemoryStore, executor: FakeSSHExecutor, gate: PermissionGate,
    checkpointer: AsyncSqliteSaver,
) -> None:
    """仅 L1/L2 -> 全程不 interrupt, 直接 END (spike-04: 不用 interrupt_before=tools)。"""
    await store.create_investigation("exec-5")
    llm = FakeLLM(script=[
        {"tool_calls": [{"name": "execute_discovery",
                         "args": {"target_host": "node-a", "service_name": "nginx", "form": "systemd"}}]},
        "探测完成, 服务正常, 无需修复",
    ])
    app = build_agent(llm, executor, gate, store, "exec-5", checkpointer)
    cfg = _cfg("exec-5")

    result = await app.ainvoke({"messages": [("user", "查 nginx 状态")]}, config=cfg)
    snap = await app.aget_state(cfg)
    assert not snap.next   # END
    assert get_pending_interrupt(snap) is None
    # L1 执行
    disc_calls = [c for c in executor.calls if c.method == "readonly"]
    assert len(disc_calls) == 1
    # 无 L3
    assert not any(c.method == "remediation" for c in executor.calls)
    # 工单非 HITL_SUSPENDED
    inv = await store.get_investigation("exec-5")
    assert inv is not None
    assert inv.status == "PENDING"
    msgs = result.get("messages", [])
    assert msgs and "正常" in msgs[-1].content


# === submit_journal 工具可调用 ===

async def test_submit_journal_works(
    store: InMemoryStore, executor: FakeSSHExecutor, gate: PermissionGate,
    checkpointer: AsyncSqliteSaver,
) -> None:
    await store.create_investigation("exec-6")
    llm = FakeLLM(script=[
        {"tool_calls": [{"name": "submit_journal",
                         "args": {"record_type": "symptom", "content": "nginx 异常"}}]},
        "已记录",
    ])
    app = build_agent(llm, executor, gate, store, "exec-6", checkpointer)
    cfg = _cfg("exec-6")
    await app.ainvoke({"messages": [("user", "记录症状")]}, config=cfg)
    recs = await store.list_records("exec-6")
    assert len(recs) == 1
    assert recs[0].record_type == "symptom"
    assert recs[0].content == "nginx 异常"


# === 工具结果经 _extract_text 解析 (spike-02 分层) ===

async def test_tool_result_parseable_as_json(
    store: InMemoryStore, executor: FakeSSHExecutor, gate: PermissionGate,
    checkpointer: AsyncSqliteSaver,
) -> None:
    """工具返回 JSON 字符串, Agent (及测试) 可 json.loads 得 {ok,data}。"""
    await store.create_investigation("exec-7")
    llm = FakeLLM(script=[
        {"tool_calls": [{"name": "execute_discovery",
                         "args": {"target_host": "node-a", "service_name": "nginx", "form": "systemd"}}]},
        "ok",
    ])
    app = build_agent(llm, executor, gate, store, "exec-7", checkpointer)
    cfg = _cfg("exec-7")
    result = await app.ainvoke({"messages": [("user", "查状态")]}, config=cfg)
    # 找 ToolMessage (工具返回)
    tool_msgs = [m for m in result.get("messages", [])
                 if getattr(m, "type", "") == "tool"]
    assert tool_msgs
    parsed = json.loads(tool_msgs[-1].content)
    assert parsed["ok"] is True
    assert parsed["data"]["status"] == "active"
