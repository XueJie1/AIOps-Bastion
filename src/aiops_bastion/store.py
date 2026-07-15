"""持久层抽象 (设计 §8.1) + InMemory / SQLite 后端。

Store Protocol 定义 investigations / records / hitl_requests 三类数据的 CRUD,
对齐 §8.1 Firebase Schema 的子集 (M3 用; Firebase 真后端延后, Protocol 已对齐字段,
未来实现即插即用)。

- InMemoryStore: dict + 锁, 测试默认, 零外部依赖;
- SqliteStore: aiosqlite 单文件, 真持久化 (作品集可演示崩溃不丢工单)。

C2 一次性消费 (§4.3 验收): consume_hitl 原子 APPROVED->CONSUMED,
二次消费返回 False -> PermissionGate 转 HITLRejectedError。
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

# === 枚举 (§8.1, 与 §6.3 状态机逐项核对) ===

InvestigationStatus = Literal[
    "PENDING", "IN_PROGRESS", "HITL_SUSPENDED",
    "COMPLETED", "RESOLVED", "FAILED_TOKEN_BUDGET", "ABORTED",
]
HitlStatus = Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED", "CONSUMED"]
RecordType = Literal[
    "symptom", "observation", "finding", "investigation_gap", "summary_md",
]
InvestigationMode = Literal["chat", "event"]

# HITL 审批默认有效期 (§6.7: 创建+30min)
HITL_TTL_MINUTES = 30


# === 时间 / ID 工具 ===

def _now() -> datetime:
    """当前 UTC 时间 (Store 内部统一 UTC, 序列化时转 ISO)。"""
    return datetime.now(UTC)


def _uuid() -> str:
    """UUID4 字符串 (record_id / execution_id / approval_id)。"""
    return str(uuid.uuid4())


# === 数据模型 (§8.1) ===

@dataclass(frozen=True, slots=True)
class Investigation:
    """调查工单 (§8.1 investigations)。

    token_usage / token_budget / checkpoint_id 为 M4+ 字段, M3 仅持有默认值不主动管理
    (§6.6 四道闸属 M4); 此处保留以与 §8.1 schema 一致, 避免未来加列。
    """
    execution_id: str
    status: InvestigationStatus
    mode: InvestigationMode = "chat"
    dedup_key: str | None = None
    trigger: dict[str, Any] | None = None
    token_usage: int = 0
    token_budget: int = 512_000
    checkpoint_id: str | None = None
    summary_md: str | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class Record:
    """Journal Record (§8.1 investigations/{id}/records, §6.5)。"""
    record_id: str = field(default_factory=_uuid)
    record_type: RecordType = "observation"
    content: str = ""
    ts: datetime = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class HitlRequest:
    """HITL 授权请求 (§8.1 hitl_requests)。

    request_id == approval_id (§8.1 PK)。status 流转:
    PENDING -> APPROVED/REJECTED/EXPIRED; APPROVED -> CONSUMED (一次性, C2)。
    """
    request_id: str                         # = approval_id
    execution_id: str
    target_host: str
    action_type: str
    rendered_cmd: str
    impact: str
    status: HitlStatus = "PENDING"
    decided_by: str | None = None
    decided_at: datetime | None = None
    consumed_at: datetime | None = None
    expires_at: datetime = field(
        default_factory=lambda: _now() + timedelta(minutes=HITL_TTL_MINUTES)
    )
    created_at: datetime = field(default_factory=_now)

    @property
    def approval_id(self) -> str:
        """§8.1: request_id / approval_id 同值。"""
        return self.request_id

    def is_expired(self, now: datetime | None = None) -> bool:
        """是否超过 expires_at (§6.7: 创建+30min)。"""
        return (now or _now()) > self.expires_at


# === Store 抽象 (§10.4 可测试性: Protocol + 多后端) ===

class Store(Protocol):
    """持久层接口 (§8.1)。

    InMemoryStore / SqliteStore / (未来) FirebaseStore 均实现此接口。
    Agent / PermissionGate / MCP Server 依赖此 Protocol, 与具体后端解耦。
    """

    # --- investigations ---
    async def create_investigation(
        self, execution_id: str, *, mode: InvestigationMode = "chat",
        trigger: dict[str, Any] | None = None, dedup_key: str | None = None,
    ) -> Investigation: ...

    async def get_investigation(self, execution_id: str) -> Investigation | None: ...

    async def update_investigation(
        self, execution_id: str, *, status: InvestigationStatus | None = None,
        summary_md: str | None = None, checkpoint_id: str | None = None,
    ) -> Investigation | None: ...

    # --- records ---
    async def add_record(self, execution_id: str, record: Record) -> None: ...

    async def list_records(self, execution_id: str) -> list[Record]: ...

    # --- hitl_requests ---
    async def create_hitl_request(
        self, execution_id: str, *, target_host: str, action_type: str,
        rendered_cmd: str, impact: str, ttl_minutes: int = HITL_TTL_MINUTES,
    ) -> HitlRequest: ...

    async def get_hitl_request(self, approval_id: str) -> HitlRequest | None: ...

    async def approve_hitl(self, approval_id: str, *, decided_by: str = "user") -> HitlRequest | None:
        """PENDING -> APPROVED; 非 PENDING 返回 None。过期由 PermissionGate consume 时校验。"""

    async def reject_hitl(self, approval_id: str, *, decided_by: str = "user") -> HitlRequest | None:
        """PENDING -> REJECTED; 非 PENDING 返回 None。"""

    async def consume_hitl(self, approval_id: str) -> bool:
        """原子 APPROVED -> CONSUMED (C2 一次性消费, §4.3)。

        返回 True 表示本次消费成功; 已 CONSUMED / 非 APPROVED 返回 False。
        PermissionGate 据此转 HITLRejectedError。
        """


# === InMemoryStore (测试默认, dict + 锁) ===

class InMemoryStore:
    """内存 Store: dict 持有, asyncio.Lock 保 consume 原子。

    无持久化 (进程退出即失); 测试默认用此, 零外部依赖。
    真持久化见 SqliteStore / (未来) FirebaseStore。
    """

    def __init__(self) -> None:
        self._investigations: dict[str, Investigation] = {}
        self._records: dict[str, list[Record]] = {}
        self._hitl: dict[str, HitlRequest] = {}
        self._lock = asyncio.Lock()   # hitl 状态流转原子用

    async def create_investigation(
        self, execution_id: str, *, mode: InvestigationMode = "chat",
        trigger: dict[str, Any] | None = None, dedup_key: str | None = None,
    ) -> Investigation:
        inv = Investigation(
            execution_id=execution_id, status="PENDING", mode=mode,
            trigger=trigger, dedup_key=dedup_key,
        )
        self._investigations[execution_id] = inv
        self._records[execution_id] = []
        return inv

    async def get_investigation(self, execution_id: str) -> Investigation | None:
        return self._investigations.get(execution_id)

    async def update_investigation(
        self, execution_id: str, *, status: InvestigationStatus | None = None,
        summary_md: str | None = None, checkpoint_id: str | None = None,
    ) -> Investigation | None:
        inv = self._investigations.get(execution_id)
        if inv is None:
            return None
        updated = replace(
            inv,
            status=status if status is not None else inv.status,
            summary_md=summary_md if summary_md is not None else inv.summary_md,
            checkpoint_id=checkpoint_id if checkpoint_id is not None else inv.checkpoint_id,
            updated_at=_now(),
        )
        self._investigations[execution_id] = updated
        return updated

    async def add_record(self, execution_id: str, record: Record) -> None:
        self._records.setdefault(execution_id, []).append(record)

    async def list_records(self, execution_id: str) -> list[Record]:
        return list(self._records.get(execution_id, []))

    async def create_hitl_request(
        self, execution_id: str, *, target_host: str, action_type: str,
        rendered_cmd: str, impact: str, ttl_minutes: int = HITL_TTL_MINUTES,
    ) -> HitlRequest:
        req = HitlRequest(
            request_id=_uuid(), execution_id=execution_id,
            target_host=target_host, action_type=action_type,
            rendered_cmd=rendered_cmd, impact=impact,
            expires_at=_now() + timedelta(minutes=ttl_minutes),
        )
        self._hitl[req.request_id] = req
        return req

    async def get_hitl_request(self, approval_id: str) -> HitlRequest | None:
        return self._hitl.get(approval_id)

    async def approve_hitl(self, approval_id: str, *, decided_by: str = "user") -> HitlRequest | None:
        async with self._lock:
            req = self._hitl.get(approval_id)
            if req is None or req.status != "PENDING":
                return None
            updated = replace(
                req, status="APPROVED", decided_by=decided_by, decided_at=_now(),
            )
            self._hitl[approval_id] = updated
            return updated

    async def reject_hitl(self, approval_id: str, *, decided_by: str = "user") -> HitlRequest | None:
        async with self._lock:
            req = self._hitl.get(approval_id)
            if req is None or req.status != "PENDING":
                return None
            updated = replace(
                req, status="REJECTED", decided_by=decided_by, decided_at=_now(),
            )
            self._hitl[approval_id] = updated
            return updated

    async def consume_hitl(self, approval_id: str) -> bool:
        async with self._lock:
            req = self._hitl.get(approval_id)
            if req is None or req.status != "APPROVED":
                return False   # 不存在 / 非 APPROVED (含已 CONSUMED) -> 不可消费
            self._hitl[approval_id] = replace(req, status="CONSUMED", consumed_at=_now())
            return True


# === SqliteStore (aiosqlite 真持久化) ===

class SqliteStore:
    """SQLite 持久化 Store (aiosqlite, 单文件)。

    作品集可演示: 进程重启后 investigation/hitl_requests 仍在。
    consume_hitl 经 SQL `UPDATE ... WHERE status='APPROVED'` 原子完成 (C2)。

    aiosqlite 无类型 stub (ignore_missing_imports), 连接对象类型化为 Any。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._db: Any = None   # aiosqlite.Connection (无 stub), 懒连接

    async def _conn(self) -> Any:
        import aiosqlite
        if self._db is None:
            self._db = await aiosqlite.connect(self._path)
            await self._init_schema(self._db)
        return self._db

    async def aclose(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _init_schema(self, db: Any) -> None:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS investigations (
                execution_id TEXT PRIMARY KEY,
                status TEXT NOT NULL, mode TEXT NOT NULL,
                dedup_key TEXT, trigger_json TEXT,
                token_usage INTEGER NOT NULL DEFAULT 0,
                token_budget INTEGER NOT NULL DEFAULT 512000,
                checkpoint_id TEXT, summary_md TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                record_type TEXT NOT NULL, content TEXT NOT NULL,
                ts TEXT NOT NULL,
                FOREIGN KEY (execution_id) REFERENCES investigations(execution_id)
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS hitl_requests (
                request_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                target_host TEXT NOT NULL, action_type TEXT NOT NULL,
                rendered_cmd TEXT NOT NULL, impact TEXT NOT NULL,
                status TEXT NOT NULL,
                decided_by TEXT, decided_at TEXT, consumed_at TEXT,
                expires_at TEXT NOT NULL, created_at TEXT NOT NULL
            )"""
        )
        await db.commit()

    # --- 序列化辅助 ---

    @staticmethod
    def _inv_from_row(row: tuple[Any, ...]) -> Investigation:
        import json
        return Investigation(
            execution_id=row[0], status=row[1], mode=row[2],
            dedup_key=row[3], trigger=json.loads(row[4]) if row[4] else None,
            token_usage=row[5], token_budget=row[6], checkpoint_id=row[7],
            summary_md=row[8],
            created_at=datetime.fromisoformat(row[9]),
            updated_at=datetime.fromisoformat(row[10]),
        )

    @staticmethod
    def _record_from_row(row: tuple[Any, ...]) -> Record:
        return Record(
            record_id=row[0], record_type=row[1], content=row[2],
            ts=datetime.fromisoformat(row[3]),
        )

    @staticmethod
    def _hitl_from_row(row: tuple[Any, ...]) -> HitlRequest:
        return HitlRequest(
            request_id=row[0], execution_id=row[1],
            target_host=row[2], action_type=row[3],
            rendered_cmd=row[4], impact=row[5], status=row[6],
            decided_by=row[7],
            decided_at=datetime.fromisoformat(row[8]) if row[8] else None,
            consumed_at=datetime.fromisoformat(row[9]) if row[9] else None,
            expires_at=datetime.fromisoformat(row[10]),
            created_at=datetime.fromisoformat(row[11]),
        )

    # --- investigations ---

    async def create_investigation(
        self, execution_id: str, *, mode: InvestigationMode = "chat",
        trigger: dict[str, Any] | None = None, dedup_key: str | None = None,
    ) -> Investigation:
        import json
        db = await self._conn()
        now = _now()
        inv = Investigation(
            execution_id=execution_id, status="PENDING", mode=mode,
            trigger=trigger, dedup_key=dedup_key,
            token_usage=0, token_budget=512_000, checkpoint_id=None,
            summary_md=None, created_at=now, updated_at=now,
        )
        await db.execute(
            "INSERT INTO investigations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (inv.execution_id, inv.status, inv.mode, inv.dedup_key,
             json.dumps(trigger) if trigger else None, inv.token_usage,
             inv.token_budget, inv.checkpoint_id, inv.summary_md,
             inv.created_at.isoformat(), inv.updated_at.isoformat()),
        )
        await db.commit()
        return inv

    async def get_investigation(self, execution_id: str) -> Investigation | None:
        db = await self._conn()
        async with db.execute(
            "SELECT * FROM investigations WHERE execution_id=?", (execution_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._inv_from_row(row) if row else None

    async def update_investigation(
        self, execution_id: str, *, status: InvestigationStatus | None = None,
        summary_md: str | None = None, checkpoint_id: str | None = None,
    ) -> Investigation | None:
        inv = await self.get_investigation(execution_id)
        if inv is None:
            return None
        updated = replace(
            inv,
            status=status if status is not None else inv.status,
            summary_md=summary_md if summary_md is not None else inv.summary_md,
            checkpoint_id=checkpoint_id if checkpoint_id is not None else inv.checkpoint_id,
            updated_at=_now(),
        )
        db = await self._conn()
        await db.execute(
            "UPDATE investigations SET status=?, summary_md=?, checkpoint_id=?, updated_at=? "
            "WHERE execution_id=?",
            (updated.status, updated.summary_md, updated.checkpoint_id,
             updated.updated_at.isoformat(), execution_id),
        )
        await db.commit()
        return updated

    async def add_record(self, execution_id: str, record: Record) -> None:
        db = await self._conn()
        await db.execute(
            "INSERT INTO records VALUES (?,?,?,?,?)",
            (record.record_id, execution_id, record.record_type,
             record.content, record.ts.isoformat()),
        )
        await db.commit()

    async def list_records(self, execution_id: str) -> list[Record]:
        db = await self._conn()
        async with db.execute(
            "SELECT record_id, record_type, content, ts FROM records "
            "WHERE execution_id=? ORDER BY ts ASC",
            (execution_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._record_from_row(r) for r in rows]

    async def create_hitl_request(
        self, execution_id: str, *, target_host: str, action_type: str,
        rendered_cmd: str, impact: str, ttl_minutes: int = HITL_TTL_MINUTES,
    ) -> HitlRequest:
        db = await self._conn()
        now = _now()
        req = HitlRequest(
            request_id=_uuid(), execution_id=execution_id,
            target_host=target_host, action_type=action_type,
            rendered_cmd=rendered_cmd, impact=impact,
            expires_at=now + timedelta(minutes=ttl_minutes), created_at=now,
        )
        await db.execute(
            "INSERT INTO hitl_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (req.request_id, req.execution_id, req.target_host, req.action_type,
             req.rendered_cmd, req.impact, req.status, req.decided_by,
             req.decided_at.isoformat() if req.decided_at else None,
             req.consumed_at.isoformat() if req.consumed_at else None,
             req.expires_at.isoformat(), req.created_at.isoformat()),
        )
        await db.commit()
        return req

    async def get_hitl_request(self, approval_id: str) -> HitlRequest | None:
        db = await self._conn()
        async with db.execute(
            "SELECT * FROM hitl_requests WHERE request_id=?", (approval_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._hitl_from_row(row) if row else None

    async def approve_hitl(self, approval_id: str, *, decided_by: str = "user") -> HitlRequest | None:
        # 原子: 仅 PENDING -> APPROVED (过期由 PermissionGate consume 时校验;
        # Recovery Sweep §6.8, 后续负责把悬挂 PENDING 置 EXPIRED)
        db = await self._conn()
        cur = await db.execute(
            "UPDATE hitl_requests SET status='APPROVED', decided_by=?, decided_at=? "
            "WHERE request_id=? AND status='PENDING'",
            (decided_by, _now().isoformat(), approval_id),
        )
        await db.commit()
        if cur.rowcount == 0:
            return None
        return await self.get_hitl_request(approval_id)

    async def reject_hitl(self, approval_id: str, *, decided_by: str = "user") -> HitlRequest | None:
        db = await self._conn()
        cur = await db.execute(
            "UPDATE hitl_requests SET status='REJECTED', decided_by=?, decided_at=? "
            "WHERE request_id=? AND status='PENDING'",
            (decided_by, _now().isoformat(), approval_id),
        )
        await db.commit()
        if cur.rowcount == 0:
            return None
        return await self.get_hitl_request(approval_id)

    async def consume_hitl(self, approval_id: str) -> bool:
        """原子 APPROVED -> CONSUMED (C2)。SQL WHERE status='APPROVED' 保证一次性。"""
        db = await self._conn()
        cur = await db.execute(
            "UPDATE hitl_requests SET status='CONSUMED', consumed_at=? "
            "WHERE request_id=? AND status='APPROVED'",
            (_now().isoformat(), approval_id),
        )
        await db.commit()
        rowcount: int = cur.rowcount
        return rowcount > 0
