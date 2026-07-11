"""AsyncSSHExecutor 单测 (mock asyncssh, 无网络)。

覆盖 (设计 §3.4):
- Semaphore 限并发 (≤ max_concurrent)
- slot 排队 wait_slot 超时 -> ExecTimeoutError(kind=queue) [P2-8]
- 执行超时 -> ExecTimeoutError(kind="exec") + 连接剔除 [R4]
- 连接按 host 复用 (connect 一次)
- 连接失败 -> SSHConnectionError + 下次重建
- 校验先于连接 (坏 host 不触发 connect)
- shlex.join 命令串 (防御#2)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import asyncssh
import pytest

from aiops_bastion.exceptions import (
    CommandValidationError,
    ExecTimeoutError,
    HITLRejectedError,
    SSHConnectionError,
)
from aiops_bastion.execution import AsyncSSHExecutor
from aiops_bastion.vault import Vault

# === fakes ===

class FakeProc:
    """模拟 asyncssh.SSHCompletedProcess。"""

    def __init__(self, exit_status: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr


class FakeConn:
    """模拟 asyncssh.SSHClientConnection。"""

    def __init__(
        self,
        *,
        proc: FakeProc | None = None,
        raise_on_run: BaseException | None = None,
        run_delay: float = 0.0,
        tracker: _ConcurrencyTracker | None = None,
    ) -> None:
        self._proc = proc or FakeProc(stdout="ok")
        self._raise = raise_on_run
        self._delay = run_delay
        self._tracker = tracker
        self.commands: list[str] = []
        self.closed = False

    async def run(
        self, command: str, *, check: bool = False, **kwargs: object
    ) -> FakeProc:
        # 镜像 asyncssh.SSHClientConnection.run(command, timeout=, check=);
        # timeout 等额外 kwarg 被 **kwargs 吸收忽略 (超时行为由 raise_on_run 模拟)。
        self.commands.append(command)
        if self._tracker is not None:
            self._tracker.enter()
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._raise is not None:
                raise self._raise
        finally:
            if self._tracker is not None:
                self._tracker.exit()
        return self._proc

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class _ConcurrencyTracker:
    """记录并发 run() 调用峰值, 供 Semaphore 测试断言。"""

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0

    def enter(self) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)

    def exit(self) -> None:
        self.current -= 1


@pytest.fixture
def patched_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    """真实 Vault 实例, get 被 patch 返回 dummy key (不触发 PBKDF2, 不触网)。

    import_private_key 也被 mock, 故 key 内容无关; 用真实 Vault 仅满足类型。
    """
    vault = Vault(tmp_path / "test_vault.enc")

    async def fake_get(name: object) -> str:
        return "DUMMY-KEY-CONTENT"

    monkeypatch.setattr(vault, "get", fake_get)
    return vault


@pytest.fixture
def mock_import_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncssh.import_private_key -> sentinel (避免解析真实 key)。"""
    monkeypatch.setattr(asyncssh, "import_private_key", lambda data: "FAKE-KEY")


def _patch_connect(
    monkeypatch: pytest.MonkeyPatch, conn: FakeConn, *, calls: list[str] | None = None
) -> None:
    """asyncssh.connect -> 返回 conn; 记录调用次数到 calls。"""
    async def fake_connect(*args: object, **kwargs: object) -> FakeConn:
        if calls is not None:
            calls.append("connect")
        return conn
    monkeypatch.setattr(asyncssh, "connect", fake_connect)


# === 校验先于连接 (防御#1) ===

async def test_validation_before_connection(
    patched_vault: Vault, mock_import_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """坏 host -> CommandValidationError, 不触发 asyncssh.connect。"""
    calls: list[str] = []
    _patch_connect(monkeypatch, FakeConn(), calls=calls)
    engine = AsyncSSHExecutor(patched_vault)

    with pytest.raises(CommandValidationError):
        await engine.run_readonly("node-a; rm -rf /", "systemd", "nginx")
    assert calls == [], f"坏 host 不应触发 connect, 实际 {calls}"


async def test_remediation_approval_check_before_connection(
    patched_vault: Vault, mock_import_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L3 缺 approval_id -> HITLRejectedError, 不触发 connect。"""
    calls: list[str] = []
    _patch_connect(monkeypatch, FakeConn(), calls=calls)
    engine = AsyncSSHExecutor(patched_vault)

    with pytest.raises(HITLRejectedError):
        await engine.run_remediation("node-a", "restart_service", {"unit": "nginx"})
    assert calls == []


# === 基本 happy path + shlex.join 命令串 (防御#2) ===

async def test_run_readonly_executes_joined_command(
    patched_vault: Vault, mock_import_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_readonly 下发 shlex.join(argv) (防御#2), 非 raw argv。"""
    conn = FakeConn(proc=FakeProc(exit_status=0, stdout="active"))
    _patch_connect(monkeypatch, conn)
    engine = AsyncSSHExecutor(patched_vault)

    result = await engine.run_readonly("node-a", "systemd", "nginx")
    assert result.exit_code == 0
    assert result.stdout == "active"
    # shlex.join 产物: "systemctl status nginx" (无元字符时不加引号)
    assert conn.commands == ["systemctl status nginx"]


async def test_run_logs_executes_joined_command(
    patched_vault: Vault, mock_import_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_logs 下发 journalctl 命令串。"""
    conn = FakeConn(proc=FakeProc(stdout="log line"))
    _patch_connect(monkeypatch, conn)
    engine = AsyncSSHExecutor(patched_vault)

    result = await engine.run_logs("node-a", "systemd", "nginx", 100)
    assert result.stdout == "log line"
    assert conn.commands == ["journalctl -u nginx -n 100 --no-pager"]


async def test_run_remediation_executes_joined_command(
    patched_vault: Vault, mock_import_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L3 带 approval_id 执行, 下发 systemctl restart。"""
    conn = FakeConn(proc=FakeProc(exit_status=0))
    _patch_connect(monkeypatch, conn)
    engine = AsyncSSHExecutor(patched_vault)

    result = await engine.run_remediation(
        "node-a", "restart_service", {"unit": "nginx"}, approval_id="apv-1"
    )
    assert result.exit_code == 0
    assert conn.commands == ["systemctl restart nginx"]


# === 连接复用 ===

async def test_connection_reused_per_host(
    patched_vault: Vault, mock_import_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同 host 顺序两次调用 -> asyncssh.connect 仅一次 (§3.4 按 host 复用)。"""
    conn = FakeConn(proc=FakeProc(stdout="active"))
    calls: list[str] = []
    _patch_connect(monkeypatch, conn, calls=calls)
    engine = AsyncSSHExecutor(patched_vault)

    await engine.run_readonly("node-a", "systemd", "nginx")
    await engine.run_readonly("node-a", "systemd", "nginx")
    assert calls == ["connect"], f"应仅建连一次, 实际 {calls}"
    assert len(conn.commands) == 2


async def test_distinct_hosts_distinct_connections(
    patched_vault: Vault, mock_import_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不同 host 各自建连。"""
    conn_map: dict[str, FakeConn] = {}

    async def fake_connect(host: str, **kwargs: object) -> FakeConn:
        if host not in conn_map:
            conn_map[host] = FakeConn(proc=FakeProc(stdout="ok"))
        return conn_map[host]

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    engine = AsyncSSHExecutor(patched_vault)

    await engine.run_readonly("node-a", "systemd", "nginx")
    await engine.run_readonly("node-b", "systemd", "nginx")
    assert set(conn_map) == {"node-a", "node-b"}


# === Semaphore 限并发 ===

async def test_semaphore_limits_concurrency(
    patched_vault: Vault, mock_import_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """max_concurrent=2, 3 并发调用 -> run() 峰值并发 ≤ 2 [决策#3]。"""
    tracker = _ConcurrencyTracker()
    conn = FakeConn(run_delay=0.05, tracker=tracker)
    _patch_connect(monkeypatch, conn)
    engine = AsyncSSHExecutor(patched_vault, max_concurrent=2)

    await asyncio.gather(*[
        engine.run_readonly("node-a", "systemd", "nginx") for _ in range(3)
    ])
    assert tracker.peak <= 2, f"并发应 ≤2, 实际峰值 {tracker.peak}"
    assert tracker.peak == 2, f"应打满 2, 实际峰值 {tracker.peak}"


# === slot 排队超时 [P2-8] ===

async def test_slot_wait_timeout_raises_queue_timeout(
    patched_vault: Vault, mock_import_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """max_concurrent=1, 第 1 调用占满 slot; 第 2 调用 wait_slot 超时 -> EXEC_TIMEOUT(queue)。"""
    conn = FakeConn(run_delay=0.2)   # 占 slot 一会儿
    _patch_connect(monkeypatch, conn)
    engine = AsyncSSHExecutor(patched_vault, max_concurrent=1)

    task = asyncio.create_task(engine.run_readonly("node-a", "systemd", "nginx"))
    await asyncio.sleep(0.02)   # 让第 1 调用先拿到 slot

    with pytest.raises(ExecTimeoutError) as exc_info:
        await engine.run_readonly("node-a", "systemd", "nginx", wait_slot=0.05)
    assert exc_info.value.kind == "queue"

    await task   # 收尾


# === 执行超时 [R4] ===

async def test_exec_timeout_raises_and_evicts_connection(
    patched_vault: Vault, mock_import_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run() 抛 TimeoutError -> ExecTimeoutError(kind=exec) + 连接剔除 (下次重建)。"""
    conns = [
        FakeConn(raise_on_run=TimeoutError()),                    # 第 1 次: 超时
        FakeConn(proc=FakeProc(exit_status=0, stdout="ok")),      # 第 2 次: 重建后成功
    ]
    calls: list[str] = []

    async def connect(host: str, **kwargs: object) -> FakeConn:
        calls.append("connect")
        return conns.pop(0)

    monkeypatch.setattr(asyncssh, "connect", connect)
    engine = AsyncSSHExecutor(patched_vault)

    with pytest.raises(ExecTimeoutError) as exc_info:
        await engine.run_readonly("node-a", "systemd", "nginx")
    assert exc_info.value.kind == "exec"

    # 剔除后, 下次调用重新建连 (calls 又 +1) 且这次成功
    result = await engine.run_readonly("node-a", "systemd", "nginx")
    assert result.stdout == "ok"
    assert calls == ["connect", "connect"], f"超时后应重建连接, 实际 {calls}"


# === 连接失败 -> SSHConnectionError + 重建 ===

async def test_connection_failure_raises_and_retries(
    patched_vault: Vault, mock_import_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """asyncssh.connect 抛 OSError -> SSHConnectionError; 下次调用重新尝试建连。"""
    attempts: list[int] = []

    async def flaky_connect(host: str, **kwargs: object) -> FakeConn:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("connection refused")
        return FakeConn(proc=FakeProc(stdout="ok"))

    monkeypatch.setattr(asyncssh, "connect", flaky_connect)
    engine = AsyncSSHExecutor(patched_vault)

    with pytest.raises(SSHConnectionError):
        await engine.run_readonly("node-a", "systemd", "nginx")
    # 第 2 次重试成功
    result = await engine.run_readonly("node-a", "systemd", "nginx")
    assert result.stdout == "ok"
    assert len(attempts) == 2


async def test_connect_timeout_raises_ssh_connection_error(
    patched_vault: Vault, mock_import_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """connect 超过 connect_timeout -> SSHConnectionError。"""

    async def slow_connect(host: str, **kwargs: object) -> FakeConn:
        await asyncio.sleep(1.0)
        return FakeConn()

    monkeypatch.setattr(asyncssh, "connect", slow_connect)
    engine = AsyncSSHExecutor(patched_vault, connect_timeout=0.05)

    with pytest.raises(SSHConnectionError):
        await engine.run_readonly("node-a", "systemd", "nginx")


# === no vault key ===

async def test_no_ssh_key_raises_connection_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Vault 无该 host 私钥 -> SSHConnectionError (不触网)。"""
    vault = Vault(tmp_path / "v.enc")

    async def empty_get(name: object) -> str:
        return ""

    monkeypatch.setattr(vault, "get", empty_get)
    monkeypatch.setattr(asyncssh, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应连网")))
    engine = AsyncSSHExecutor(vault)

    with pytest.raises(SSHConnectionError):
        await engine.run_readonly("node-a", "systemd", "nginx")
