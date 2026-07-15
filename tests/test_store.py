"""Store 持久层测试 (§8.1)。

契约测试 parametrize 覆盖 InMemoryStore + SqliteStore (同一套断言),
确保两后端行为一致。SqliteStore 额外测真持久化 (重开实例仍在)。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pytest

from aiops_bastion.store import (
    HITL_TTL_MINUTES,
    HitlRequest,
    InMemoryStore,
    Record,
    SqliteStore,
    Store,
    _now,
)

# === Store 工厂 fixture (两种后端) ===

class _StoreFactory(Protocol):
    async def __call__(self) -> Store: ...


@pytest.fixture
async def sqlite_store(tmp_path: Path) -> SqliteStore:
    s = SqliteStore(tmp_path / "store.sqlite")
    yield s
    await s.aclose()


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, sqlite_store: SqliteStore) -> Store:
    """parametrize 两种后端, 共用契约测试。"""
    if request.param == "memory":
        return InMemoryStore()
    return sqlite_store


# === investigations ===

async def test_create_and_get_investigation(store: Store) -> None:
    inv = await store.create_investigation("exec-1", mode="chat", trigger={"host": "node-a"})
    assert inv.execution_id == "exec-1"
    assert inv.status == "PENDING"
    assert inv.mode == "chat"
    assert inv.trigger == {"host": "node-a"}
    assert inv.token_budget == 512_000   # §8.1 默认 [决策#18]

    got = await store.get_investigation("exec-1")
    assert got is not None
    assert got.execution_id == "exec-1"

    assert await store.get_investigation("nope") is None


async def test_update_investigation_status(store: Store) -> None:
    await store.create_investigation("exec-2")
    updated = await store.update_investigation("exec-2", status="IN_PROGRESS")
    assert updated is not None
    assert updated.status == "IN_PROGRESS"
    assert updated.updated_at >= updated.created_at

    summary = await store.update_investigation("exec-2", status="COMPLETED", summary_md="## 报告")
    assert summary is not None
    assert summary.status == "COMPLETED"
    assert summary.summary_md == "## 报告"

    assert await store.update_investigation("nope", status="COMPLETED") is None


# === records ===

async def test_add_and_list_records_ordered(store: Store) -> None:
    await store.create_investigation("exec-3")
    t0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    t1 = datetime(2026, 7, 1, 12, 5, tzinfo=UTC)
    await store.add_record("exec-3", Record(record_id="r1", record_type="symptom", content="down", ts=t0))
    await store.add_record("exec-3", Record(record_id="r2", record_type="finding", content="root", ts=t1))

    recs = await store.list_records("exec-3")
    assert [r.record_id for r in recs] == ["r1", "r2"]   # 按 ts 升序
    assert recs[0].record_type == "symptom"
    assert recs[1].content == "root"

    assert await store.list_records("empty") == []


# === hitl_requests 生命周期 ===

async def test_create_hitl_request_pending(store: Store) -> None:
    req = await store.create_hitl_request(
        "exec-4", target_host="node-a", action_type="restart_service",
        rendered_cmd="systemctl restart nginx", impact="重启 nginx 服务",
    )
    assert req.status == "PENDING"
    assert req.request_id == req.approval_id   # §8.1 PK 同值
    assert req.execution_id == "exec-4"
    # §6.7: expires_at = created + 30min
    assert req.expires_at - req.created_at >= timedelta(minutes=HITL_TTL_MINUTES - 1, seconds=-5)

    got = await store.get_hitl_request(req.request_id)
    assert got is not None
    assert got.target_host == "node-a"
    assert await store.get_hitl_request("nope") is None


async def test_approve_hitl_pending_to_approved(store: Store) -> None:
    req = await store.create_hitl_request(
        "exec-5", target_host="node-a", action_type="restart_service",
        rendered_cmd="systemctl restart nginx", impact="重启",
    )
    approved = await store.approve_hitl(req.request_id, decided_by="alice")
    assert approved is not None
    assert approved.status == "APPROVED"
    assert approved.decided_by == "alice"
    assert approved.decided_at is not None

    # 二次 approve (已 APPROVED, 非 PENDING) -> None
    assert await store.approve_hitl(req.request_id) is None


async def test_reject_hitl(store: Store) -> None:
    req = await store.create_hitl_request(
        "exec-6", target_host="node-a", action_type="restart_container",
        rendered_cmd="docker restart nginx", impact="重启容器",
    )
    rejected = await store.reject_hitl(req.request_id, decided_by="bob")
    assert rejected is not None
    assert rejected.status == "REJECTED"
    # reject 后再 approve -> None (已非 PENDING)
    assert await store.approve_hitl(req.request_id) is None


# === C2: 一次性消费 (§4.3 验收) ===

async def test_consume_hitl_one_time(store: Store) -> None:
    req = await store.create_hitl_request(
        "exec-7", target_host="node-a", action_type="restart_service",
        rendered_cmd="systemctl restart nginx", impact="重启",
    )
    # 未审批 (PENDING) 不可消费
    assert await store.consume_hitl(req.request_id) is False

    await store.approve_hitl(req.request_id)
    # 首次消费 -> True (APPROVED -> CONSUMED)
    assert await store.consume_hitl(req.request_id) is True
    after = await store.get_hitl_request(req.request_id)
    assert after is not None
    assert after.status == "CONSUMED"
    assert after.consumed_at is not None

    # C2: 二次消费同一 approval_id -> False (已 CONSUMED)
    assert await store.consume_hitl(req.request_id) is False


async def test_consume_nonexistent(store: Store) -> None:
    assert await store.consume_hitl("does-not-exist") is False


async def test_consume_rejected_not_consumable(store: Store) -> None:
    req = await store.create_hitl_request(
        "exec-8", target_host="node-a", action_type="clear_cache",
        rendered_cmd="/usr/local/bin/clear_cache.sh /tmp/app-cache/", impact="清缓存",
    )
    await store.reject_hitl(req.request_id)
    assert await store.consume_hitl(req.request_id) is False   # REJECTED 不可消费


# === SqliteStore 真持久化 (重开实例仍在) ===

async def test_sqlite_persistence_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "persist.sqlite"
    s1 = SqliteStore(db_path)
    await s1.create_investigation("exec-persist", mode="chat")
    req = await s1.create_hitl_request(
        "exec-persist", target_host="node-a", action_type="restart_service",
        rendered_cmd="systemctl restart nginx", impact="重启",
    )
    await s1.approve_hitl(req.request_id)
    await s1.aclose()

    # 重开新实例指向同文件 -> 数据仍在
    s2 = SqliteStore(db_path)
    inv = await s2.get_investigation("exec-persist")
    assert inv is not None
    assert inv.status == "PENDING"
    got_req = await s2.get_hitl_request(req.request_id)
    assert got_req is not None
    assert got_req.status == "APPROVED"   # 跨实例保留
    await s2.aclose()


# === HitlRequest.is_expired ===

def test_hitl_request_expiry() -> None:
    base = HitlRequest(
        request_id="r", execution_id="e", target_host="h", action_type="restart_service",
        rendered_cmd="c", impact="i",
    )
    # 已过期
    expired = replace(base, expires_at=_now() - timedelta(minutes=5))
    assert expired.is_expired() is True
    # 未过期 (默认 +30min)
    assert base.is_expired() is False
    # 指定 now 校验
    future = base.expires_at + timedelta(minutes=1)
    assert base.is_expired(now=future) is True

