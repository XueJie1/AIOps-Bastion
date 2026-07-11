"""真靶机 SSH 集成测试 (env 门控)。

需配 env (见 tests/conftest.py 的 ssh_target fixture):
  AIOPS_TEST_SSH_HOST / AIOPS_TEST_SSH_KEY (必需)
  AIOPS_TEST_SSH_USER / AIOPS_TEST_SSH_PORT / AIOPS_TEST_SSH_KNOWN_HOSTS (可选)
  AIOPS_TEST_SSH_SERVICE / AIOPS_TEST_SSH_FORM (L1/L2 探测所需)

无 env 时全 skip, CI 不跑真靶机。L3 修复对真靶机有破坏性, 此处绝不打真 restart
(仅 FakeSSHExecutor 单测覆盖)。

覆盖 (设计 §3.4/§4.2/§4.3):
- L1 探测 execute_discovery (systemd/docker 真实状态)
- L2 日志 fetch_service_logs (真实抓取 + 截断)
- C1 第二道防线: shlex.join 端到端 (echo 含元字符 arg 往返为字面量)
- 第三道防线: rbash 拒绝受限命令 (cd)
"""
from __future__ import annotations

import pytest

from aiops_bastion.execution import READONLY_TIMEOUT, AsyncSSHExecutor
from aiops_bastion.tools import execute_discovery, fetch_service_logs
from tests.conftest import SSHTarget

pytestmark = pytest.mark.integration


# === L1 探测 ===

async def test_l1_discovery_real_target(
    ssh_executor: AsyncSSHExecutor, ssh_target: SSHTarget
) -> None:
    """L1 探测真靶机服务, 返回 ok 且 status ∈ {active,inactive,unknown} (§5.2)。"""
    if not ssh_target.service:
        pytest.skip("AIOPS_TEST_SSH_SERVICE 未设置, 跳过 L1 service 探测")
    result = await execute_discovery(
        ssh_executor, ssh_target.host, ssh_target.service, ssh_target.form
    )
    assert result["ok"] is True, f"探测失败: {result}"
    assert result["data"]["status"] in {"active", "inactive", "unknown"}
    assert result["data"]["target_host"] == ssh_target.host


# === L2 日志 ===

async def test_l2_fetch_logs_real_target(
    ssh_executor: AsyncSSHExecutor, ssh_target: SSHTarget
) -> None:
    """L2 抓取真靶机日志, 返回 ok (§5.3)。"""
    if not ssh_target.service:
        pytest.skip("AIOPS_TEST_SSH_SERVICE 未设置, 跳过 L2 日志抓取")
    result = await fetch_service_logs(
        ssh_executor, ssh_target.host, ssh_target.service, ssh_target.form, 50
    )
    assert result["ok"] is True, f"取日志失败: {result}"
    assert "logs" in result["data"]
    assert isinstance(result["data"]["truncated"], bool)


# === C1: 第二道防线 shlex.join 端到端 ===

async def test_c1_shlex_join_roundtrip(
    ssh_executor: AsyncSSHExecutor, ssh_target: SSHTarget
) -> None:
    """shlex.join 把含元字符的 arg 包成单引号字面量, 远端 shell 不解析 (§4.3)。

    下发 ["echo", "nginx; rm -rf /"] -> shlex.join -> "echo 'nginx; rm -rf /'"。
    若未转义, 远端 shell 会解析 ; 致 echo 仅输出 "nginx" (且 rm -rf / 会执行!)。
    断言 stdout 恰为 "nginx; rm -rf /" 证明: arg 被视为字面量, 无命令注入。
    """
    result = await ssh_executor._exec(  # noqa: SLF001 - 直测 _exec 的 shlex.join 路径
        ssh_target.host,
        ["echo", "nginx; rm -rf /"],
        READONLY_TIMEOUT,
        30.0,
    )
    assert result.exit_code == 0, f"echo 失败: exit={result.exit_code} stderr={result.stderr!r}"
    # 远端 echo 输出应恰为字面量 "nginx; rm -rf /" (可能带尾部换行)
    assert result.stdout.strip() == "nginx; rm -rf /", (
        f"shlex.join 未生效, 远端解析了元字符: stdout={result.stdout!r}"
    )


# === 第三道防线: rbash 拒绝受限命令 ===

async def test_rbash_restricted_command_rejected(
    ssh_executor: AsyncSSHExecutor, ssh_target: SSHTarget
) -> None:
    """rbash 第三道防线: cd (受限命令) 应被拒绝 (§4.2 R7)。

    正常 bash: "cd /tmp" exit 0; rbash: cd 受限, exit != 0 或 stderr 含 restricted。
    若此测试失败, 说明靶机未配 rbash forced-command (见 docs/REMOTE_HARDENING.md)。
    """
    result = await ssh_executor._exec(  # noqa: SLF001 - 直测受限命令拒绝
        ssh_target.host,
        ["cd", "/tmp"],
        READONLY_TIMEOUT,
        30.0,
    )
    rbash_active = (
        result.exit_code != 0
        or "restrict" in result.stderr.lower()
        or "rbash" in result.stderr.lower()
    )
    assert rbash_active, (
        f"rbash 第三道防线未生效 (cd 未被拒): exit={result.exit_code} stderr={result.stderr!r};"
        "请按 docs/REMOTE_HARDENING.md 配置 authorized_keys forced-command"
    )
