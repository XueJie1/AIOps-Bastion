"""注入对抗测试 (设计 §4.3)。

M1 安全地基验收: 8 类注入 payload + 白名单边界 + L3 越权
全部被 CommandValidationError / PathNotAllowlistedError / UnknownActionError 拒绝。
M2 新增: 第二道防线 shlex.join 行为验证 (C1, §4.3) + host 校验 + 构建器正向用例。

每条对应设计 §4.3 表格的一行。
"""
import shlex
from pathlib import Path

import asyncssh
import pytest

from aiops_bastion.exceptions import (
    CommandValidationError,
    HITLRejectedError,
    PathNotAllowlistedError,
    UnknownActionError,
)
from aiops_bastion.execution import (
    IDENT_RE,
    AsyncSSHExecutor,
    _validate_host,
    build_logs_cmd,
    build_status_cmd,
    render,
)
from aiops_bastion.vault import Vault

# === 8 类注入 payload (§4.3 表) ===

INJECTION_PAYLOADS: list[tuple[str, str, type[Exception]]] = [
    # (描述, payload, 期望异常)
    ("命令分隔",   "nginx; rm -rf /",          CommandValidationError),
    ("管道",       "nginx | cat /etc/shadow",  CommandValidationError),
    ("命令替换",   "$(whoami)",                CommandValidationError),
    ("反引号",     "`whoami`",                 CommandValidationError),
    ("重定向",     "nginx > /tmp/x",           CommandValidationError),
    ("换行注入",   "nginx\nrm -rf /",          CommandValidationError),
    ("L3 参数污染", "nginx;reboot",            CommandValidationError),
    ("环境变量",   "nginx$HOME",               CommandValidationError),
]


@pytest.mark.injection
@pytest.mark.parametrize("desc, payload, expected_exc", INJECTION_PAYLOADS)
def test_injection_payloads_rejected(desc: str, payload: str, expected_exc: type[Exception]) -> None:
    """8 类注入 payload 须被 IDENT_RE fullmatch 拒绝。"""
    assert not IDENT_RE.fullmatch(payload), f"{desc}: payload 未被拒绝: {payload!r}"


# === 白名单边界 ===

@pytest.mark.injection
def test_unknown_readonly_verb_rejected() -> None:
    """白名单外动词 (rm/curl/bash) 直接拒绝 (§4.3)。"""
    # build_status_cmd 只接受 form 枚举, 不接受任意动词
    with pytest.raises(CommandValidationError):
        build_status_cmd("bash", "nginx")  # form 非 systemd/docker/compose


@pytest.mark.injection
def test_overlong_identifier_rejected() -> None:
    """超长标识符 (>128 字符) 拒绝 (IDENT_RE {1,128})。"""
    long_name = "a" * 129
    assert not IDENT_RE.fullmatch(long_name)


@pytest.mark.injection
def test_valid_identifier_accepted() -> None:
    """合法标识符通过 (反向断言, 确保白名单不过度收紧)。"""
    assert IDENT_RE.fullmatch("nginx")
    assert IDENT_RE.fullmatch("my-service.1")
    assert IDENT_RE.fullmatch("container_name-2")


# === host 校验 (§5.2 schema: target_host pattern 同 IDENT_RE) ===

@pytest.mark.injection
@pytest.mark.parametrize("bad_host", [
    "node-a; rm -rf /",
    "node-a|cat /etc/shadow",
    "node-a\nrm",
    "$(whoami)",
    "node-a > /tmp/x",
])
def test_host_injection_rejected(bad_host: str) -> None:
    """target_host 走 IDENT_RE, 元字符直接拒绝 (run_readonly/run_remediation 对称)。"""
    with pytest.raises(CommandValidationError):
        _validate_host(bad_host)


@pytest.mark.injection
def test_host_valid_accepted() -> None:
    """合法 host 通过 (含 . - _, 如 xuejie1.top)。"""
    _validate_host("node-a")
    _validate_host("xuejie1.top")
    _validate_host("host_2")


# === 构建器正向用例 (M2: 补 build_status_cmd / build_logs_cmd 正向覆盖) ===

@pytest.mark.injection
def test_build_status_cmd_positive() -> None:
    """build_status_cmd 三形态正向 argv (§5.2)。"""
    assert build_status_cmd("systemd", "nginx") == ["systemctl", "status", "nginx"]
    assert build_status_cmd("docker", "my-app") == ["docker", "inspect", "my-app"]
    assert build_status_cmd("compose", "web") == ["docker", "compose", "ps", "web"]


@pytest.mark.injection
def test_build_logs_cmd_positive() -> None:
    """build_logs_cmd 两形态正向 argv, flag 硬编码 (§5.3 修订: 补 form)。"""
    assert build_logs_cmd("systemd", "nginx", 100) == [
        "journalctl", "-u", "nginx", "-n", "100", "--no-pager",
    ]
    assert build_logs_cmd("docker", "my-app", 50) == [
        "docker", "logs", "my-app", "--tail", "50",
    ]


@pytest.mark.injection
@pytest.mark.parametrize("bad_lines", [0, -1, 501, 1.5, "100", True, False])
def test_build_logs_cmd_lines_bounds(bad_lines: int) -> None:
    """lines 限 1~500 整数, 防 DoS 靶机 (§5.3)。bool 视为非法 (isinstance 检查)。

    注解 int (合法类型), 运行时由 parametrize 注入非 int 值 (1.5/"100"/bool) 验证拒绝。
    """
    with pytest.raises(CommandValidationError):
        build_logs_cmd("systemd", "nginx", bad_lines)

@pytest.mark.injection
def test_build_logs_cmd_compose_rejected() -> None:
    """compose 日志延后 (需先解析容器名), form 暂拒 compose。"""
    with pytest.raises(CommandValidationError):
        build_logs_cmd("compose", "web", 100)


# === L3 越权 ===

@pytest.mark.injection
def test_reboot_action_rejected() -> None:
    """L3 action_type=reboot 拒绝 (无该模板, [决策#7])。"""
    with pytest.raises(UnknownActionError):
        render("reboot", {"unit": "nginx"})


@pytest.mark.injection
def test_unknown_action_type_rejected() -> None:
    """L3 未知 action_type 拒绝 (仅 3 枚举)。"""
    with pytest.raises(UnknownActionError):
        render("rm_rf", {"path": "/"})


@pytest.mark.injection
def test_clear_cache_path_traversal_rejected() -> None:
    """clear_cache 路径穿越拒绝 (不在白名单, §4.3)。"""
    with pytest.raises(PathNotAllowlistedError):
        render("clear_cache", {"path": "../../etc"})


@pytest.mark.injection
def test_clear_cache_path_whitelist_exact_match() -> None:
    """clear_cache 路径须精确匹配白名单 (非前缀, §4.2)。"""
    # 前缀看似合法但不在白名单 -> 拒绝
    with pytest.raises(PathNotAllowlistedError):
        render("clear_cache", {"path": "/var/cache/nginx/../../etc"})
    # 精确命中白名单 -> 通过
    argv = render("clear_cache", {"path": "/var/cache/nginx/"})
    assert argv == ["/usr/local/bin/clear_cache.sh", "/var/cache/nginx/"]


# === L3 模板渲染产物为纯 list[str] argv ===

@pytest.mark.injection
def test_l3_render_produces_argv_list() -> None:
    """L3 模板渲染产物为纯 list[str] argv, 无 shell 拼接 (§4.3)。"""
    # restart_service
    argv = render("restart_service", {"unit": "nginx"})
    assert argv == ["systemctl", "restart", "nginx"]
    assert all(isinstance(a, str) for a in argv)

    # restart_container
    argv = render("restart_container", {"name": "my-app"})
    assert argv == ["docker", "restart", "my-app"]


# === L3 approval_id 缺失拒绝 (spike-04 修订, §3.3/§6.7) ===
# M2: 经 AsyncSSHExecutor 验证 (approval 校验在执行前, 不触网; vault 不被访问)

def _dummy_executor() -> AsyncSSHExecutor:
    """approval_id/host 校验在连网前完成, vault 从不被访问; 用未 unlock 的桩即可。"""
    return AsyncSSHExecutor(vault=Vault(Path("/nonexistent-dummy")))


@pytest.mark.injection
async def test_l3_without_approval_id_rejected() -> None:
    """L3 修复缺 approval_id -> HITL_REJECTED (§5.7); 校验在连网前, 不触网。"""
    engine = _dummy_executor()
    with pytest.raises(HITLRejectedError):
        await engine.run_remediation("node-a", "restart_service", {"unit": "nginx"})


@pytest.mark.injection
async def test_l3_host_injection_rejected_async() -> None:
    """AsyncSSHExecutor.run_remediation 的 target_host 注入同样拒绝 (与 run_readonly 对称)。"""
    engine = _dummy_executor()
    with pytest.raises(CommandValidationError):
        await engine.run_remediation(
            "node-a; rm -rf /",
            "restart_service",
            {"unit": "nginx"},
            approval_id="apv-1",
        )


@pytest.mark.injection
async def test_readonly_host_injection_rejected_async() -> None:
    """AsyncSSHExecutor.run_readonly 的 target_host 注入拒绝。"""
    engine = _dummy_executor()
    with pytest.raises(CommandValidationError):
        await engine.run_readonly("node-a; rm -rf /", "systemd", "nginx")


# === C1: 第二道防线 shlex.join 行为验证 (§4.3 防御性测试) ===
# 设计 §3.4 [P2-7] 修正: asyncssh run() 接受单 command 字符串且原样下发、不做引用;
# 故引用职责由执行器 shlex.join 显式持有。此处验证即便 IDENT_RE 被绕过 (防御性),
# shlex.join 仍把含元字符的 arg 单引号转义, 使远端 shell 视为字面量。

@pytest.mark.injection
@pytest.mark.parametrize("argv,expected_substring", [
    # 含元字符的 arg 被 shlex.quote 包成单引号字面量
    (["echo", "nginx; rm -rf /"], "'nginx; rm -rf /'"),
    (["echo", "nginx | cat /etc/shadow"], "'nginx | cat /etc/shadow'"),
    (["echo", "$(whoami)"], "'$(whoami)'"),
    (["echo", "`whoami`"], "'`whoami`'"),
    (["echo", "nginx\nrm"], "'nginx\nrm'"),
    # 管道/分号无法逃出单引号字面量
    (["systemctl", "status", "nginx;reboot"], "'nginx;reboot'"),
])
def test_shlex_join_escapes_metachars(argv: list[str], expected_substring: str) -> None:
    """shlex.join 把含元字符的 argv 元素单引号转义 (第二道防线)。"""
    command = shlex.join(argv)
    assert expected_substring in command, f"未转义: argv={argv} -> {command!r}"
    # 元字符不得出现在未引用的命令分隔位置 (整体被 shlex 解析回单个安全 argv)
    re_split = shlex.split(command)
    assert re_split == argv, f"往返不一致: {re_split} != {argv}"


@pytest.mark.injection
def test_asyncssh_version_meets_floor() -> None:
    """锁定 asyncssh >= 2.14 (§4.3; HANDOFF 已装 2.24.0)。"""
    # asyncssh 不公开 quote API (源码核对), shlex.quote 由我方 shlex.join 持有;
    # 版本下限确保 SSHClientConnection / run 行为与实现一致。
    version = asyncssh.__version__
    major, minor = (int(x) for x in version.split(".")[:2])
    assert (major, minor) >= (2, 14), f"asyncssh {version} < 2.14"
