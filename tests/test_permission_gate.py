"""PermissionGate 测试 (§3.3/§6.7, 含 C2 一次性消费 §4.3 验收)。

C2 四断言 (§4.3 "L3 approval_id 一次性消费"):
  1. 二次消费被拒;
  2. 过期拒;
  3. 未审批 (PENDING) 拒;
  4. 归属不匹配拒。

标 @pytest.mark.injection (C2 属 §4.3 注入/授权验收项)。
"""
from __future__ import annotations

import pytest

from aiops_bastion.exceptions import HITLRejectedError
from aiops_bastion.permission_gate import PermissionGate
from aiops_bastion.store import InMemoryStore, SqliteStore

pytestmark = pytest.mark.injection


@pytest.fixture(params=["memory", "sqlite"])
async def gate(request: pytest.FixtureRequest, tmp_path) -> PermissionGate:
    """两种后端的 PermissionGate (共用契约测试)。"""
    store = InMemoryStore() if request.param == "memory" else SqliteStore(tmp_path / "gate.sqlite")
    try:
        yield PermissionGate(store)
    finally:
        # InMemoryStore 无 aclose; SqliteStore 需关闭
        aclose = getattr(store, "aclose", None)
        if aclose is not None:
            await aclose()


async def _make_approved(gate: PermissionGate, **kw) -> str:
    """创建一个 PENDING -> APPROVED 的 hitl_request, 返回 approval_id。"""
    store = gate._store
    req = await store.create_hitl_request(
        execution_id=kw.get("execution_id", "exec-1"),
        target_host=kw.get("target_host", "node-a"),
        action_type=kw.get("action_type", "restart_service"),
        rendered_cmd=kw.get("rendered_cmd", "systemctl restart nginx"),
        impact=kw.get("impact", "重启 nginx 服务"),
    )
    await store.approve_hitl(req.request_id, decided_by="alice")
    return req.request_id


# === C2 断言 1: 二次消费被拒 ===

async def test_c2_consume_twice_rejected(gate: PermissionGate) -> None:
    approval_id = await _make_approved(gate)

    # 首次消费成功
    req = await gate.validate_and_consume(
        approval_id, execution_id="exec-1", target_host="node-a",
        action_type="restart_service",
    )
    assert req.status == "CONSUMED"
    assert req.consumed_at is not None

    # C2: 二次消费同一 approval_id -> HITL_REJECTED
    with pytest.raises(HITLRejectedError, match="状态非 APPROVED|已消费"):
        await gate.validate_and_consume(
            approval_id, execution_id="exec-1", target_host="node-a",
            action_type="restart_service",
        )


# === C2 断言 2: 过期拒 ===

async def test_c2_expired_rejected(gate: PermissionGate) -> None:
    # 用负 ttl 直接造一个"创建即过期"的 PENDING, 再 approve (approve 不查过期,
    # 过期由 PermissionGate consume 时校验), 模拟创建后 30min 内未及时消费。
    store = gate._store
    req = await store.create_hitl_request(
        execution_id="exec-1", target_host="node-a",
        action_type="restart_service",
        rendered_cmd="systemctl restart nginx", impact="重启",
        ttl_minutes=-5,   # expires_at 已在过去 -> 已过期
    )
    await store.approve_hitl(req.request_id, decided_by="alice")
    assert req.is_expired()

    with pytest.raises(HITLRejectedError, match="过期"):
        await gate.validate_and_consume(
            req.request_id, execution_id="exec-1", target_host="node-a",
            action_type="restart_service",
        )

    # 过期未消费 -> approval_id 仍 APPROVED (未被 consume), 真正置 EXPIRED 交 Recovery Sweep
    stored = await store.get_hitl_request(req.request_id)
    assert stored is not None
    assert stored.status == "APPROVED"


# === C2 断言 3: 未审批 (PENDING) 拒 ===

async def test_c2_pending_rejected(gate: PermissionGate) -> None:
    store = gate._store
    req = await store.create_hitl_request(
        execution_id="exec-1", target_host="node-a",
        action_type="restart_service",
        rendered_cmd="systemctl restart nginx", impact="重启",
    )
    # 未 approve, 仍 PENDING
    assert req.status == "PENDING"

    with pytest.raises(HITLRejectedError, match="非 APPROVED"):
        await gate.validate_and_consume(
            req.request_id, execution_id="exec-1", target_host="node-a",
            action_type="restart_service",
        )


# === C2 断言 4: 归属不匹配拒 ===

async def test_c2_mismatch_rejected(gate: PermissionGate) -> None:
    approval_id = await _make_approved(gate)

    # execution_id 不符
    with pytest.raises(HITLRejectedError, match="归属不匹配"):
        await gate.validate_and_consume(
            approval_id, execution_id="other-exec", target_host="node-a",
            action_type="restart_service",
        )
    # target_host 不符
    with pytest.raises(HITLRejectedError, match="归属不匹配"):
        await gate.validate_and_consume(
            approval_id, execution_id="exec-1", target_host="other-host",
            action_type="restart_service",
        )
    # action_type 不符
    with pytest.raises(HITLRejectedError, match="归属不匹配"):
        await gate.validate_and_consume(
            approval_id, execution_id="exec-1", target_host="node-a",
            action_type="restart_container",
        )

    # 归属不匹配时不应消费 (CONSUMED 兜底: approval_id 仍 APPROVED, 可被正确归属消费)
    req = await gate._store.get_hitl_request(approval_id)
    assert req is not None
    assert req.status == "APPROVED"


# === 不存在的 approval_id ===

async def test_nonexistent_rejected(gate: PermissionGate) -> None:
    with pytest.raises(HITLRejectedError, match="不存在"):
        await gate.validate_and_consume(
            "does-not-exist", execution_id="exec-1", target_host="node-a",
            action_type="restart_service",
        )


# === REJECTED 不可消费 ===

async def test_rejected_not_consumable(gate: PermissionGate) -> None:
    store = gate._store
    req = await store.create_hitl_request(
        execution_id="exec-1", target_host="node-a",
        action_type="restart_service",
        rendered_cmd="systemctl restart nginx", impact="重启",
    )
    await store.reject_hitl(req.request_id, decided_by="bob")

    with pytest.raises(HITLRejectedError, match="非 APPROVED"):
        await gate.validate_and_consume(
            req.request_id, execution_id="exec-1", target_host="node-a",
            action_type="restart_service",
        )


# === 正常路径: 消费后状态变 CONSUMED ===

async def test_happy_path_consumes(gate: PermissionGate) -> None:
    approval_id = await _make_approved(gate)
    req = await gate.validate_and_consume(
        approval_id, execution_id="exec-1", target_host="node-a",
        action_type="restart_service",
    )
    assert req.status == "CONSUMED"
    assert req.consumed_at is not None
    # Store 侧也确认 CONSUMED
    stored = await gate._store.get_hitl_request(approval_id)
    assert stored is not None
    assert stored.status == "CONSUMED"
