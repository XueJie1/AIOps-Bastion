"""执行引擎: asyncssh + 白名单 + 模板 (设计 §3.4)。

唯一的 SSH 指令执行入口。绝不接受 Agent 传入的原始 shell 字符串。

三层防线 (设计 §4.2 [P2-7], 已据 asyncssh 2.24 源码修正):
  1. IDENT_RE fullmatch -- 参数进入命令前拒绝一切 shell 元字符 (核心防线)
  2. 执行器侧 shlex.join(argv) -- 即便含元字符也被单引号转义 (第二防线)
     注: asyncssh run() 接受**单个 command 字符串**且原样下发、**不做引用** (源码核对,
     见 §3.4 修订); 故引用职责由本模块 shlex.join 显式持有, 不依赖第三方内部行为。
  3. 远端 rbash (靶机 authorized_keys forced-command, 第三防线, 推荐加固)。

L3 硬编码模板 (设计 §4.2 [决策#7]): action_type 仅 3 枚举, 无 reboot。

可测试性 (§10.4): 抽象 SSHExecutor Protocol, 测试注入 FakeSSHExecutor,
无需真实 SSH 即可单测工具层; 真靶机集成测试经 env 门控。
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import asyncssh

from .exceptions import (
    CommandValidationError,
    ExecTimeoutError,
    HITLRejectedError,
    PathNotAllowlistedError,
    SSHConnectionError,
    UnknownActionError,
)
from .vault import Vault

# === 白名单与正则 (§4.2) ===

# 允许的只读动词 (封闭集合, 不可扩展为变更类) [决策#8]
ALLOWED_READONLY = frozenset({
    "systemctl status",
    "systemctl is-active",
    "docker inspect",
    "docker ps",
    "docker compose ps",
    "journalctl -u",
    "docker logs",
})

# 标识符正则: unit/container/service 名 [决策#8]
# fullmatch, 拒绝一切 shell 元字符 (; | & > < $ 反引号 ( ) \n \r \\ ' ")
IDENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# 元字符拒绝集 (§4.2, 用于对抗测试参考)
SHELL_METACHARS = frozenset(";|&><$`()\n\r\\'\"")

# L3 模板 (§4.2 [决策#7]): 3 枚举, 无 reboot
TEMPLATES: dict[str, Any] = {
    "restart_service":   lambda p: ["systemctl", "restart", p["unit"]],
    "restart_container": lambda p: ["docker", "restart", p["name"]],
    "clear_cache":       lambda p: ["/usr/local/bin/clear_cache.sh", p["path"]],
}

# clear_cache 路径白名单 (§4.2): 精确匹配集合成员, 非前缀
CLEAR_CACHE_PATH_WHITELIST = frozenset({
    "/var/cache/nginx/",
    "/tmp/app-cache/",
})


# === 只读命令构建器 (L1/L2) ===

Form = Literal["systemd", "docker", "compose"]


def build_status_cmd(form: str, name: str) -> list[str]:
    """结构化构建只读探测命令 (L1, §5.2) [决策#8]。

    返回 list[str] argv, 无 shell 拼接。
    参数经 IDENT_RE fullmatch 校验, 元字符直接拒绝。
    """
    if not re.fullmatch(IDENT_RE, name):
        raise CommandValidationError(f"非法标识符 (IDENT_RE fullmatch 失败): {name!r}")
    if form == "systemd":
        return ["systemctl", "status", name]
    if form == "docker":
        return ["docker", "inspect", name]
    if form == "compose":
        return ["docker", "compose", "ps", name]
    raise CommandValidationError(f"未知 form: {form!r} (须 systemd/docker/compose)")


def build_logs_cmd(form: str, name: str, lines: int) -> list[str]:
    """结构化构建日志抓取命令 (L2, §5.3) [决策#8]。

    - name 走 IDENT_RE (与 build_status_cmd 同一校验语义);
    - lines 限 1~500 (§5.3 schema), 防止 DoS 靶机;
    - 返回 list[str] argv, journalctl/docker logs 在 ALLOWED_READONLY (§4.2);
    - flag (-n/--tail/--no-pager) 硬编码, 非 Agent 输入, 无注入面。

    注: §5.3 原 schema 无 form 字段, 无法选 journalctl vs docker logs,
    本实现补 form 参数 (systemd|docker, 与 execute_discovery 对齐, 见 §5.3 修订)。
    compose 日志需先解析容器名, 延后。
    """
    if not isinstance(lines, int) or isinstance(lines, bool) or not (1 <= lines <= 500):
        raise CommandValidationError(f"lines 须 1~500 整数, 实际 {lines!r}")
    if not re.fullmatch(IDENT_RE, name):
        raise CommandValidationError(f"非法标识符 (IDENT_RE fullmatch 失败): {name!r}")
    if form == "systemd":
        return ["journalctl", "-u", name, "-n", str(lines), "--no-pager"]
    if form == "docker":
        return ["docker", "logs", name, "--tail", str(lines)]
    raise CommandValidationError(f"form 须 systemd/docker, 实际 {form!r}")


# === L3 模板渲染 (§4.2 [决策#7]) ===

def render(action_type: str, params: dict[str, str]) -> list[str]:
    """渲染 L3 修复命令为 list[str] argv [决策#7]。

    - action_type 须在 3 枚举内 (restart_service/restart_container/clear_cache)
    - params 经 IDENT_RE 校验 (clear_cache 的 path 走白名单精确匹配)
    - 无 reboot 模板
    """
    if action_type not in TEMPLATES:
        raise UnknownActionError(
            f"未知 action_type: {action_type!r} "
            f"(须 restart_service/restart_container/clear_cache, 无 reboot)"
        )

    if action_type == "clear_cache":
        path = params["path"]
        if path not in CLEAR_CACHE_PATH_WHITELIST:
            raise PathNotAllowlistedError(
                f"路径未命中白名单 (须精确匹配): {path!r}"
            )
        # path 命中白名单后, 直接代入模板 (白名单成员本身安全)
        return TEMPLATES[action_type](params)

    # restart_service / restart_container: 参数走 IDENT_RE
    for v in params.values():
        if not re.fullmatch(IDENT_RE, v):
            raise CommandValidationError(f"非法标识符 (IDENT_RE fullmatch 失败): {v!r}")
    return TEMPLATES[action_type](params)


# === 执行结果 ===

@dataclass
class ExecResult:
    """SSH 执行结果。"""
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False


# === 超时常量 (§3.4 [R4]: 只读 5s / 修复 30s 可配) ===
READONLY_TIMEOUT: float = 5.0
LOGS_TIMEOUT: float = 5.0
REMEDIATION_TIMEOUT: float = 30.0


def _validate_host(host: str) -> None:
    """target_host 走 IDENT_RE (§5.2 schema: pattern 同 IDENT_RE)。"""
    if not re.fullmatch(IDENT_RE, host):
        raise CommandValidationError(f"非法 target_host: {host!r}")


# === SSHExecutor 抽象 (§10.4 可测试性架构) ===

@runtime_checkable
class SSHExecutor(Protocol):
    """SSH 执行器接口。

    真实现 AsyncSSHExecutor 连真靶机; 测试替身 FakeSSHExecutor 按脚本返回。
    工具层依赖此 Protocol, 与具体实现解耦 (§10.4)。
    """

    async def run_readonly(
        self, host: str, form: str, name: str, *, wait_slot: float = 30.0
    ) -> ExecResult: ...

    async def run_logs(
        self, host: str, form: str, name: str, lines: int, *, wait_slot: float = 30.0
    ) -> ExecResult: ...

    async def run_remediation(
        self,
        host: str,
        action_type: str,
        params: dict[str, str],
        *,
        approval_id: str | None = None,
        wait_slot: float = 30.0,
    ) -> ExecResult: ...


# === 真执行引擎 (asyncssh) ===

class AsyncSSHExecutor:
    """asyncssh 执行引擎 (设计 §3.4)。

    - 连接池按 host 复用 asyncssh.SSHClientConnection [决策#3];
    - Semaphore(max_concurrent) 限并发 (默认 4) [决策#3];
    - 超时熔断: 只读 5s / 日志 5s / 修复 30s 可配 [R4];
    - slot 排队等待 wait_slot 超时 -> ExecTimeoutError(kind=queue) [P2-8];
    - SSH 私钥从 Vault 内存解密获取 (§4.6, host 含 '.' 传 list 避免歧义);
    - 命令经 shlex.join 拼安全命令串 (防御#2) 后交 asyncssh.run 原样下发;
    - 连接异常 -> 剔除该 host 连接, 下次按需重建 (失败即重建, 后台探活延后)。

    known_hosts: None 表示禁用主机密钥校验 (作品集权衡, 生产应 pin 主机密钥);
                 或传 known_hosts 路径 / (known, revoked) 元组启用校验。
    """

    def __init__(
        self,
        vault: Vault,
        *,
        max_concurrent: int = 4,
        connect_timeout: float = 10.0,
        known_hosts: Any = None,
        default_username: str | None = None,
        usernames: dict[str, str] | None = None,
        default_port: int = 22,
    ) -> None:
        self._vault = vault
        self._sem = asyncio.Semaphore(max_concurrent)   # 受限并发 [决策#3]
        self._pools: dict[str, asyncssh.SSHClientConnection] = {}
        self._conn_locks: dict[str, asyncio.Lock] = {}   # per-host 建连锁, 防并发首次 race
        self._connect_timeout = connect_timeout
        self._known_hosts = known_hosts
        self._default_username = default_username
        self._usernames = usernames or {}
        self._default_port = default_port

    # --- 公开接口 (实现 SSHExecutor Protocol) ---

    async def run_readonly(
        self, host: str, form: str, name: str, *, wait_slot: float = 30.0
    ) -> ExecResult:
        """L1 探测: 走白名单构建器, 5s 超时 [R4]。"""
        _validate_host(host)
        argv = build_status_cmd(form, name)   # 防御#1 (含 name IDENT_RE)
        return await self._exec(host, argv, READONLY_TIMEOUT, wait_slot)

    async def run_logs(
        self, host: str, form: str, name: str, lines: int, *, wait_slot: float = 30.0
    ) -> ExecResult:
        """L2 日志: 走白名单构建器, 5s 超时, lines 限 1~500 [R4/§5.3]。"""
        _validate_host(host)
        argv = build_logs_cmd(form, name, lines)   # 防御#1
        return await self._exec(host, argv, LOGS_TIMEOUT, wait_slot)

    async def run_remediation(
        self,
        host: str,
        action_type: str,
        params: dict[str, str],
        *,
        approval_id: str | None = None,
        wait_slot: float = 30.0,
    ) -> ExecResult:
        """L3 修复: 走硬编码模板, 30s 超时 [R4]。

        approval_id 须非空 (PermissionGate 校验, §3.3/§6.7; 一次性消费 C2 属 M3,
        此处仅校验存在性)。asyncssh 真执行; 对真靶机有破坏性, 集成测试不打真 restart。
        """
        _validate_host(host)
        if not approval_id:
            raise HITLRejectedError("L3 修复缺 approval_id (PermissionGate 授权失败)")
        argv = render(action_type, params)   # 防御#1
        return await self._exec(host, argv, REMEDIATION_TIMEOUT, wait_slot)

    # --- 内部: slot + 连接 + 执行 ---

    async def _exec(
        self, host: str, argv: list[str], exec_timeout: float, wait_slot: float
    ) -> ExecResult:
        """获取 slot -> 取连接 -> shlex.join 下发 -> 收结果。

        校验已在调用前完成 (防御#1), 此处不再校验 argv。
        """
        # slot 排队等待 [P2-8]: 超过 wait_slot 未获取 -> EXEC_TIMEOUT(queue)
        try:
            await asyncio.wait_for(self._sem.acquire(), wait_slot)
        except TimeoutError:
            raise ExecTimeoutError(
                f"slot 等待超时 (queue wait > {wait_slot}s)", kind="queue"
            ) from None

        try:
            conn = await self._get_connection(host)
            command = shlex.join(argv)   # 防御#2: 我方显式引用
            try:
                proc = await conn.run(command, timeout=exec_timeout, check=False)
            except TimeoutError:
                # 执行超时: 连接状态可能不一致, 剔除重建
                self._drop_connection(host)
                raise ExecTimeoutError(
                    f"SSH 执行超时 ({exec_timeout}s)", kind="exec"
                ) from None
            except asyncssh.Error as e:
                self._drop_connection(host)
                raise SSHConnectionError(f"SSH 执行失败: {e}") from e
        finally:
            self._sem.release()

        exit_status = proc.exit_status if proc.exit_status is not None else -1
        stdout = proc.stdout if isinstance(proc.stdout, str) else (
            proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
        )
        stderr = proc.stderr if isinstance(proc.stderr, str) else (
            proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        )
        return ExecResult(exit_code=exit_status, stdout=stdout, stderr=stderr)

    async def _get_connection(self, host: str) -> asyncssh.SSHClientConnection:
        """按 host 复用连接; 无则从 Vault 取私钥新建 (§4.6)。

        per-host 锁防并发首次调用 race (都见 pool 空、都建连); double-check 后才建。
        """
        conn = self._pools.get(host)
        if conn is not None:
            return conn

        if host not in self._conn_locks:
            self._conn_locks[host] = asyncio.Lock()
        async with self._conn_locks[host]:
            # double-check: 持锁后可能他者已建好
            conn = self._pools.get(host)
            if conn is not None:
                return conn

            # host 含 '.' 时传 list 避免 split 歧义 (§4.6, 如 xuejie1.top)
            key_str = await self._vault.get(["ssh_keys", host])
            if not key_str or not isinstance(key_str, str):
                raise SSHConnectionError(f"Vault 无 {host} 的 SSH 私钥")

            # import_private_key 解析快, 但走线程池避免大 RSA 阻塞事件循环
            key = await asyncio.to_thread(asyncssh.import_private_key, key_str)
            username = self._usernames.get(host, self._default_username)

            try:
                conn = await asyncio.wait_for(
                    asyncssh.connect(
                        host,
                        port=self._default_port,
                        username=username,
                        client_keys=[key],
                        known_hosts=self._known_hosts,
                    ),
                    self._connect_timeout,
                )
            except TimeoutError:
                raise SSHConnectionError(
                    f"连接 {host} 超时 (>{self._connect_timeout}s)"
                ) from None
            except (asyncssh.Error, OSError) as e:
                raise SSHConnectionError(f"连接 {host} 失败: {e}") from e

            self._pools[host] = conn
            return conn

    def _drop_connection(self, host: str) -> None:
        """剔除连接 (失败/超时后), 下次按需重建 (§4.6: 取新私钥)。"""
        conn = self._pools.pop(host, None)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    async def aclose(self) -> None:
        """关闭所有连接池连接 (优雅关闭)。"""
        for _host, conn in list(self._pools.items()):
            with contextlib.suppress(Exception):
                conn.close()
                await conn.wait_closed()
        self._pools.clear()
