"""PermissionGate (设计 §3.3 职责拆分 / §6.7)。

L3 修复执行前的**防御性校验**: 校验 approval_id 对应的 hitl_requests 记录
存在、status==APPROVED、未过期、归属匹配, 通过后**原子消费** (APPROVED->CONSUMED)。

C2 一次性消费 (§4.3 验收): 同一 approval_id 二次执行 -> consume_hitl 返回 False
(已 CONSUMED) -> 本模块抛 HITLRejectedError。

定位 (§3.3 [P1-4] 修订): 不做"挂起 + 写 hitl_requests + 等待审批" (那在 stdio 子进程
模型下不可行), 仅做防御性校验。即使 Agent 被诱导绕过 interrupt, 本模块仍拒绝无授权的
L3 执行。挂起/审批决策由 Agent 侧 LangGraph interrupt + Store 完成 (§6.7)。
"""
from __future__ import annotations

from .exceptions import HITLRejectedError
from .store import HitlRequest, Store


class PermissionGate:
    """L3 执行前校验 + 一次性消费 approval_id (§3.3 / §6.7 / C2)。

    依赖 Store Protocol (§10.4), 与具体后端解耦:
      - InMemoryStore / SqliteStore: 本地, 测试与单进程;
      - (未来) FirebaseStore: 真后端。
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    async def validate_and_consume(
        self,
        approval_id: str,
        *,
        execution_id: str,
        target_host: str,
        action_type: str,
    ) -> HitlRequest:
        """校验 approval_id 并原子消费 (C2)。校验失败抛 HITLRejectedError。

        校验链 (§6.7 第 4 步):
          1. 记录存在;
          2. status == APPROVED (非 PENDING/REJECTED/EXPIRED/CONSUMED);
          3. 未过期 (expires_at, §6.7: 创建+30min);
          4. 归属匹配 (execution_id / target_host / action_type) -- 防跨工单挪用;
          5. 原子 APPROVED -> CONSUMED (C2); 并发/重放场景下仅一次成功。

        全部通过返回 HitlRequest (已 CONSUMED); Agent 据此执行 L3。
        """
        req = await self._store.get_hitl_request(approval_id)
        if req is None:
            raise HITLRejectedError(f"approval_id 不存在: {approval_id}")
        if req.status != "APPROVED":
            # 含 PENDING (未审批) / REJECTED / EXPIRED / CONSUMED (已消费)
            raise HITLRejectedError(
                f"approval_id 状态非 APPROVED: {req.status} (id={approval_id})"
            )
        if req.is_expired():
            raise HITLRejectedError(
                f"approval_id 已过期 (expires_at={req.expires_at.isoformat()}, id={approval_id})"
            )
        if (
            req.execution_id != execution_id
            or req.target_host != target_host
            or req.action_type != action_type
        ):
            raise HITLRejectedError(
                "approval_id 归属不匹配 "
                f"(期望 exec={execution_id} host={target_host} action={action_type}; "
                f"实际 exec={req.execution_id} host={req.target_host} action={req.action_type})"
            )
        # 原子消费 (C2): APPROVED -> CONSUMED。并发/重放仅一次成功。
        consumed = await self._store.consume_hitl(approval_id)
        if not consumed:
            # 理论竞态: 校验通过与消费之间被另一路消费 -> 这里拒绝 (C2 兜底)
            raise HITLRejectedError(
                f"approval_id 已被消费 (C2 一次性, id={approval_id})"
            )
        # 返回消费后的最新状态 (consume 已置 CONSUMED/consumed_at)
        updated = await self._store.get_hitl_request(approval_id)
        return updated if updated is not None else req
