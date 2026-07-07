"""注入对抗测试 (设计 §4.3)。

M1 安全地基的验收标准: 8 类注入 payload + 白名单边界 + L3 越权
全部被 CommandValidationError / PathNotAllowlistedError / UnknownActionError 拒绝。

每条对应设计 §4.3 表格的一行。
"""
import pytest

from aiops_bastion.exceptions import (
    CommandValidationError,
    PathNotAllowlistedError,
    UnknownActionError,
)
from aiops_bastion.execution import (
    IDENT_RE,
    ExecutionEngine,
    build_status_cmd,
    render,
)

# === 8 类注入 payload (§4.3 表) ===

INJECTION_PAYLOADS = [
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
def test_injection_payloads_rejected(desc, payload, expected_exc):
    """8 类注入 payload 须被 IDENT_RE fullmatch 拒绝。"""
    assert not IDENT_RE.fullmatch(payload), f"{desc}: payload 未被拒绝: {payload!r}"


# === 白名单边界 ===

@pytest.mark.injection
def test_unknown_readonly_verb_rejected():
    """白名单外动词 (rm/curl/bash) 直接拒绝 (§4.3)。"""
    # build_status_cmd 只接受 form 枚举, 不接受任意动词
    with pytest.raises(CommandValidationError):
        build_status_cmd("bash", "nginx")  # form 非 systemd/docker/compose


@pytest.mark.injection
def test_overlong_identifier_rejected():
    """超长标识符 (>128 字符) 拒绝 (IDENT_RE {1,128})。"""
    long_name = "a" * 129
    assert not IDENT_RE.fullmatch(long_name)


@pytest.mark.injection
def test_valid_identifier_accepted():
    """合法标识符通过 (反向断言, 确保白名单不过度收紧)。"""
    assert IDENT_RE.fullmatch("nginx")
    assert IDENT_RE.fullmatch("my-service.1")
    assert IDENT_RE.fullmatch("container_name-2")


# === L3 越权 ===

@pytest.mark.injection
def test_reboot_action_rejected():
    """L3 action_type=reboot 拒绝 (无该模板, [决策#7])。"""
    with pytest.raises(UnknownActionError):
        render("reboot", {"unit": "nginx"})


@pytest.mark.injection
def test_unknown_action_type_rejected():
    """L3 未知 action_type 拒绝 (仅 3 枚举)。"""
    with pytest.raises(UnknownActionError):
        render("rm_rf", {"path": "/"})


@pytest.mark.injection
def test_clear_cache_path_traversal_rejected():
    """clear_cache 路径穿越拒绝 (不在白名单, §4.3)。"""
    with pytest.raises(PathNotAllowlistedError):
        render("clear_cache", {"path": "../../etc"})


@pytest.mark.injection
def test_clear_cache_path_whitelist_exact_match():
    """clear_cache 路径须精确匹配白名单 (非前缀, §4.2)。"""
    # 前缀看似合法但不在白名单 → 拒绝
    with pytest.raises(PathNotAllowlistedError):
        render("clear_cache", {"path": "/var/cache/nginx/../../etc"})
    # 精确命中白名单 → 通过
    argv = render("clear_cache", {"path": "/var/cache/nginx/"})
    assert argv == ["/usr/local/bin/clear_cache.sh", "/var/cache/nginx/"]


# === L3 模板渲染产物为纯 list[str] argv ===

@pytest.mark.injection
def test_l3_render_produces_argv_list():
    """L3 模板渲染产物为纯 list[str] argv, 无 shell 拼接 (§4.3)。"""
    # restart_service
    argv = render("restart_service", {"unit": "nginx"})
    assert argv == ["systemctl", "restart", "nginx"]
    assert all(isinstance(a, str) for a in argv)

    # restart_container
    argv = render("restart_container", {"name": "my-app"})
    assert argv == ["docker", "restart", "my-app"]


# === L3 approval_id 缺失拒绝 (spike-04 修订, §3.3/§6.7) ===

@pytest.mark.injection
def test_l3_without_approval_id_rejected():
    """L3 修复缺 approval_id 被 PermissionGate 拒绝 (spike-04 修订)。"""
    engine = ExecutionEngine()
    with pytest.raises(CommandValidationError):
        engine.run_remediation("node-a", "restart_service", {"unit": "nginx"})


@pytest.mark.injection
def test_l3_with_approval_id_accepted():
    """L3 修复带 approval_id 通过 (渲染 argv)。"""
    engine = ExecutionEngine()
    argv = engine.run_remediation(
        "node-a", "restart_service", {"unit": "nginx"}, approval_id="approval-test"
    )
    assert argv == ["systemctl", "restart", "nginx"]


# === ExecutionEngine.run_readonly 返回已校验 argv ===

@pytest.mark.injection
def test_readonly_returns_argv():
    """只读命令构建器返回 list[str] argv。"""
    engine = ExecutionEngine()
    argv = engine.run_readonly("node-a", "systemd", "nginx")
    assert argv == ["systemctl", "status", "nginx"]


@pytest.mark.injection
def test_readonly_host_injection_rejected():
    """target_host 也走 IDENT_RE (§5.2 schema)。"""
    engine = ExecutionEngine()
    with pytest.raises(CommandValidationError):
        engine.run_readonly("node-a; rm -rf /", "systemd", "nginx")
