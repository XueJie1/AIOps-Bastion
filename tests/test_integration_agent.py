"""真 LLM 驱动 Agent 集成测试 (§3.5/§10.4, env 门控)。

真 deepseek-v4-pro 驱动 react agent 全图 (对 FakeSSHExecutor), 验证:
  - 真模型能选 execute_discovery (L1) 并解析工具结果;
  - 强 prompt 下能走到 L3 -> interrupt -> resume -> 执行。

无 AIOPS_TEST_LLM env 时 skip (CI 不触网/不花钱)。本地复现见 HANDOFF §7。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from aiops_bastion.agent import build_agent, get_pending_interrupt
from aiops_bastion.execution import ExecResult
from aiops_bastion.llm import ProviderConfig, build_llm
from aiops_bastion.permission_gate import PermissionGate
from aiops_bastion.store import InMemoryStore
from tests.fakes import FakeSSHExecutor

pytestmark = pytest.mark.integration


def _env_or_skip() -> tuple[str, str, str]:
    """读 LLM env; 缺失则 skip。返回 (provider, model, api_key)。"""
    provider = os.environ.get("AIOPS_TEST_LLM_PROVIDER")
    model = os.environ.get("AIOPS_TEST_LLM_MODEL")
    api_key = os.environ.get("AIOPS_TEST_LLM_KEY")
    if not (provider and model and api_key) or api_key.startswith("sk-your"):
        pytest.skip("AIOPS_TEST_LLM_PROVIDER/MODEL/KEY 未设置, 跳过真 LLM 集成")
    return provider, model, api_key


@pytest.fixture
def llm() -> object:
    provider, model, api_key = _env_or_skip()
    if provider == "deepseek":
        cfg = ProviderConfig(
            vendor="openai", model=model, api_key=api_key,
            base_url="https://api.deepseek.com/v1", temperature=0.0,
        )
    elif provider == "glm":
        cfg = ProviderConfig(
            vendor="openai", model=model, api_key=api_key,
            base_url="https://open.bigmodel.cn/api/paas/v4", temperature=0.0,
        )
    else:
        pytest.skip(f"未知 AIOPS_TEST_LLM_PROVIDER={provider}")
    return build_llm(cfg)


async def test_real_llm_picks_discovery_tool(llm: object, tmp_path: Path) -> None:
    """真 LLM 应选 execute_discovery 探测, 工具结果被解析 (无 L3, 不 interrupt)。"""
    store = InMemoryStore()
    await store.create_investigation("exec-real")
    executor = FakeSSHExecutor(
        responses={"node-a": ExecResult(exit_code=0, stdout="active")},
        default_stdout="active", default_exit=0,
    )
    gate = PermissionGate(store)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "real.sqlite")) as cp:
        app = build_agent(llm, executor, gate, store, "exec-real", cp)   # type: ignore[arg-type]
        cfg = {"configurable": {"thread_id": "exec-real"}}
        await app.ainvoke(
            {"messages": [HumanMessage(
                content="节点 node-a 的 nginx 服务疑似异常, 请用 execute_discovery 探测其 "
                        "systemd 状态。只探测, 不要修复。"
            )]},
            config=cfg,
        )
        snap = await app.aget_state(cfg)
    # 至少探测过一次 (L1)
    assert any(c.method == "readonly" for c in executor.calls)
    # 仅探测不修复 -> 不应 interrupt (L1/L2 自主)
    assert get_pending_interrupt(snap) is None


async def test_real_llm_l3_interrupt_and_resume(llm: object, tmp_path: Path) -> None:
    """强 prompt 下真 LLM 走到 L3 -> interrupt -> approve -> resume -> 执行一次 (C1 风格)。

    注: 真 LLM 行为非确定性, 本测试用强 prompt 引导走到 L3; 若模型未选 L3 则 skip
    (不作为硬失败, 记录模型行为差异)。
    """
    store = InMemoryStore()
    await store.create_investigation("exec-l3")
    executor = FakeSSHExecutor(
        responses={"node-a": ExecResult(exit_code=0, stdout="active")},
        default_exit=0, default_stdout="active",
    )
    gate = PermissionGate(store)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "l3.sqlite")) as cp:
        app = build_agent(llm, executor, gate, store, "exec-l3", cp)   # type: ignore[arg-type]
        cfg = {"configurable": {"thread_id": "exec-l3"}}
        await app.ainvoke(
            {"messages": [HumanMessage(
                content="节点 node-a 的 nginx 服务挂了。必须修复: 先用 execute_discovery 确认 "
                        "inactive, 再用 execute_remediation (action_type=restart_service, "
                        "params={'unit':'nginx'}) 重启。不能只探测不修复。"
            )]},
            config=cfg,
        )
        snap = await app.aget_state(cfg)
        intr = get_pending_interrupt(snap)
        if intr is None:
            pytest.skip("真 LLM 本轮未走到 L3 (行为非确定), skip")

        # L3 -> interrupt, approve + resume
        await store.approve_hitl(intr["approval_id"], decided_by="alice")
        await app.ainvoke(
            __import__("langgraph.types", fromlist=["Command"]).Command(
                resume=intr["approval_id"]
            ),
            config=cfg,
        )

    # L3 恰好执行 1 次 (不重放)
    rem_calls = [c for c in executor.calls if c.method == "remediation"]
    assert len(rem_calls) == 1
    req = await store.get_hitl_request(intr["approval_id"])
    assert req is not None
    assert req.status == "CONSUMED"
