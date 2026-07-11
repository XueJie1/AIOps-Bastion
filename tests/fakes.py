"""SSHExecutor 测试替身 (§10.4 可测试性架构)。

FakeSSHExecutor 实现 SSHExecutor Protocol, 按脚本返回 ExecResult, 不连真 SSH。
仍先走**真实校验** (防御#1): 复用 build_status_cmd / build_logs_cmd / render,
故注入对抗在 Fake 路径同样生效 (含元字符的参数在到达 _respond 前即被拒绝)。

用法:
    fake = FakeSSHExecutor(
        responses={"node-a": ExecResult(exit_code=0, stdout='{"State":{"Running":true}}')},
        fail_hosts={"bad-node"},
    )
    result = await fake.run_readonly("node-a", "docker", "nginx")

工具层单测 (test_tools) 经 Fake 验证状态映射/截断/错误传播, 无需真 SSH;
真 asyncssh 路径由 test_executor (mock asyncssh) + test_integration_ssh (真靶机) 覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass

from aiops_bastion.exceptions import (
    ExecTimeoutError,
    HITLRejectedError,
    SSHConnectionError,
)
from aiops_bastion.execution import (
    ExecResult,
    _validate_host,
    build_logs_cmd,
    build_status_cmd,
    render,
)


@dataclass
class FakeCall:
    """记录一次 Fake 调用, 供测试断言实际下发的 argv。"""
    host: str
    argv: list[str]
    method: str   # "readonly" / "logs" / "remediation"


class FakeSSHExecutor:
    """SSHExecutor 测试替身。

    - responses: {host: ExecResult} 按 host 返回 canned 结果 (缺失则用 default);
    - fail_hosts: 这些 host 抛 SSHConnectionError (模拟连接失败);
    - exec_timeout_hosts: 这些 host 抛 ExecTimeoutError(kind="exec");
    - queue_timeout_hosts: 这些 host 抛 ExecTimeoutError(kind="queue");
    - default_*: 未命中 responses 时的默认返回;
    - calls: 记录每次调用的 argv (供断言), 字段可在测试中读取。
    """

    def __init__(
        self,
        responses: dict[str, ExecResult] | None = None,
        *,
        fail_hosts: set[str] | frozenset[str] = frozenset(),
        exec_timeout_hosts: set[str] | frozenset[str] = frozenset(),
        queue_timeout_hosts: set[str] | frozenset[str] = frozenset(),
        default_exit: int = 0,
        default_stdout: str = "",
        default_stderr: str = "",
    ) -> None:
        self._responses = responses or {}
        self._fail_hosts = set(fail_hosts)
        self._exec_timeout_hosts = set(exec_timeout_hosts)
        self._queue_timeout_hosts = set(queue_timeout_hosts)
        self._default = ExecResult(
            exit_code=default_exit, stdout=default_stdout, stderr=default_stderr
        )
        self.calls: list[FakeCall] = []

    async def run_readonly(
        self, host: str, form: str, name: str, *, wait_slot: float = 30.0
    ) -> ExecResult:
        _validate_host(host)
        argv = build_status_cmd(form, name)   # 防御#1: 真实校验
        self.calls.append(FakeCall(host, argv, "readonly"))
        return self._respond(host)

    async def run_logs(
        self, host: str, form: str, name: str, lines: int, *, wait_slot: float = 30.0
    ) -> ExecResult:
        _validate_host(host)
        argv = build_logs_cmd(form, name, lines)   # 防御#1
        self.calls.append(FakeCall(host, argv, "logs"))
        return self._respond(host)

    async def run_remediation(
        self,
        host: str,
        action_type: str,
        params: dict[str, str],
        *,
        approval_id: str | None = None,
        wait_slot: float = 30.0,
    ) -> ExecResult:
        _validate_host(host)
        if not approval_id:
            raise HITLRejectedError("L3 修复缺 approval_id (PermissionGate 授权失败)")
        argv = render(action_type, params)   # 防御#1
        self.calls.append(FakeCall(host, argv, "remediation"))
        return self._respond(host)

    def _respond(self, host: str) -> ExecResult:
        """按 host 查脚本: 失败/超时优先, 否则返回 canned 或 default。"""
        if host in self._queue_timeout_hosts:
            raise ExecTimeoutError(
                f"[fake] slot 等待超时: {host}", kind="queue"
            )
        if host in self._exec_timeout_hosts:
            raise ExecTimeoutError(
                f"[fake] SSH 执行超时: {host}", kind="exec"
            )
        if host in self._fail_hosts:
            raise SSHConnectionError(f"[fake] 连接失败: {host}")
        return self._responses.get(host, self._default)


# 用于类型断言: FakeSSHExecutor 满足 SSHExecutor Protocol (结构化)
# (运行期 isinstance 检查见 test_executor)
