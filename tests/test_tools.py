"""工具层单测 (FakeSSHExecutor, 无网络)。

覆盖 (设计 §5.2/§5.3):
- execute_discovery 三形态状态映射 (systemd/docker/compose) [R11]
- fetch_service_logs lines 截断 + token 截断 + truncated 标记 [§5.3/§7.3]
- 空日志仍 ok (data.logs="")
- 错误码传播 (VALIDATION_ERROR/EXEC_TIMEOUT/INTERNAL) [§5.7]
"""
from __future__ import annotations

import json

from tests.fakes import FakeSSHExecutor

from aiops_bastion.execution import ExecResult
from aiops_bastion.tools import (
    DEFAULT_TOKEN_BUDGET,
    execute_discovery,
    fetch_service_logs,
)

# === execute_discovery: systemd 状态映射 ===

async def test_discovery_systemd_active() -> None:
    """systemctl status exit 0 -> active (R11)。"""
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=0, stdout="● nginx.service - active"),
    })
    result = await execute_discovery(fake, "node-a", "nginx", "systemd")
    assert result["ok"] is True
    assert result["data"]["status"] == "active"


async def test_discovery_systemd_inactive() -> None:
    """systemctl status exit 3 -> inactive (R11)。"""
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=3, stderr="Unit nginx.service could not be found."),
    })
    result = await execute_discovery(fake, "node-a", "nginx", "systemd")
    assert result["data"]["status"] == "inactive"


async def test_discovery_systemd_unknown() -> None:
    """systemctl status exit 其他 -> unknown。"""
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=1, stderr="some error"),
    })
    result = await execute_discovery(fake, "node-a", "nginx", "systemd")
    assert result["data"]["status"] == "unknown"


# === execute_discovery: docker 状态映射 ===

async def test_discovery_docker_active() -> None:
    """docker inspect .State.Running=true -> active (R11)。"""
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=0, stdout=json.dumps([
            {"State": {"Running": True, "Status": "running"}},
        ])),
    })
    result = await execute_discovery(fake, "node-a", "my-app", "docker")
    assert result["data"]["status"] == "active"


async def test_discovery_docker_inactive() -> None:
    """docker inspect .State.Running=false -> inactive。"""
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=0, stdout=json.dumps([
            {"State": {"Running": False, "Status": "exited"}},
        ])),
    })
    result = await execute_discovery(fake, "node-a", "my-app", "docker")
    assert result["data"]["status"] == "inactive"


async def test_discovery_docker_non_json_unknown() -> None:
    """docker inspect 非 JSON 输出 -> unknown。"""
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=1, stderr="Error: No such object"),
    })
    result = await execute_discovery(fake, "node-a", "my-app", "docker")
    assert result["data"]["status"] == "unknown"


# === execute_discovery: compose 状态映射 ===

async def test_discovery_compose_active() -> None:
    """compose ps 含 running 行 -> active。"""
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=0, stdout=(
            "NAME                SERVICE   STATUS     PORTS\n"
            "web-1               web       running    0.0.0.0:80->80/tcp\n"
        )),
    })
    result = await execute_discovery(fake, "node-a", "web", "compose")
    assert result["data"]["status"] == "active"


async def test_discovery_compose_inactive() -> None:
    """compose ps 无 running 行 (全 exited) -> inactive。"""
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=0, stdout=(
            "NAME                SERVICE   STATUS\n"
            "web-1               web       exited (0)\n"
        )),
    })
    result = await execute_discovery(fake, "node-a", "web", "compose")
    assert result["data"]["status"] == "inactive"


async def test_discovery_contract_fields() -> None:
    """execute_discovery 返回契约含 target_host/service_name/status/detail (§5.2)。"""
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=0, stdout="active"),
    })
    result = await execute_discovery(fake, "node-a", "nginx", "systemd")
    assert set(result["data"]) == {"target_host", "service_name", "status", "detail"}


# === fetch_service_logs: 截断 ===

async def test_logs_no_truncation_under_budget() -> None:
    """日志在 token 预算内 -> truncated=False。"""
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=0, stdout="error line 1\nerror line 2\n"),
    })
    result = await fetch_service_logs(fake, "node-a", "nginx", "systemd", 100)
    assert result["ok"] is True
    assert result["data"]["logs"] == "error line 1\nerror line 2\n"
    assert result["data"]["truncated"] is False


async def test_logs_truncated_over_budget() -> None:
    """日志超 token 预算 -> 截断到 budget*4 chars, truncated=True (§5.3)。"""
    long_log = "A" * 100_000
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=0, stdout=long_log),
    })
    result = await fetch_service_logs(fake, "node-a", "nginx", "systemd", 100, token_budget=100)
    assert result["data"]["truncated"] is True
    assert len(result["data"]["logs"]) == 100 * 4   # token_budget * 4 chars


async def test_logs_empty_still_ok() -> None:
    """日志为空仍 ok, data.logs="" (§5.3 错误处理)。"""
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=0, stdout=""),
    })
    result = await fetch_service_logs(fake, "node-a", "nginx", "systemd", 100)
    assert result["ok"] is True
    assert result["data"]["logs"] == ""
    assert result["data"]["truncated"] is False


async def test_logs_default_budget_8k() -> None:
    """默认 token 预算 8k -> 截断阈值 32000 chars (§7.3)。"""
    assert DEFAULT_TOKEN_BUDGET == 8000
    fake = FakeSSHExecutor(responses={
        "node-a": ExecResult(exit_code=0, stdout="X" * 40000),
    })
    result = await fetch_service_logs(fake, "node-a", "nginx", "systemd", 100)
    assert result["data"]["truncated"] is True
    assert len(result["data"]["logs"]) == 32000


# === 错误码传播 (§5.7) ===

async def test_discovery_validation_error_propagated() -> None:
    """非法 service_name -> VALIDATION_ERROR (经 build_status_cmd IDENT_RE)。"""
    fake = FakeSSHExecutor()
    result = await execute_discovery(fake, "node-a", "nginx; rm -rf /", "systemd")
    assert result["ok"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"


async def test_discovery_host_validation_error() -> None:
    """非法 target_host -> VALIDATION_ERROR。"""
    fake = FakeSSHExecutor()
    result = await execute_discovery(fake, "node-a; rm", "nginx", "systemd")
    assert result["error"]["code"] == "VALIDATION_ERROR"


async def test_logs_exec_timeout_propagated() -> None:
    """SSH 执行超时 -> EXEC_TIMEOUT (§5.7)。"""
    fake = FakeSSHExecutor(exec_timeout_hosts={"node-a"})
    result = await fetch_service_logs(fake, "node-a", "nginx", "systemd", 100)
    assert result["ok"] is False
    assert result["error"]["code"] == "EXEC_TIMEOUT"


async def test_discovery_connection_error_propagated() -> None:
    """SSH 连接失败 -> INTERNAL (§5.7)。"""
    fake = FakeSSHExecutor(fail_hosts={"bad-node"})
    result = await execute_discovery(fake, "bad-node", "nginx", "systemd")
    assert result["ok"] is False
    assert result["error"]["code"] == "INTERNAL"


async def test_logs_lines_bounds_validation() -> None:
    """lines 超上限 (501) -> VALIDATION_ERROR (build_logs_cmd 校验)。"""
    fake = FakeSSHExecutor()
    result = await fetch_service_logs(fake, "node-a", "nginx", "systemd", 501)
    assert result["error"]["code"] == "VALIDATION_ERROR"


async def test_logs_unknown_form_validation() -> None:
    """compose 日志 form 暂不支持 -> VALIDATION_ERROR。"""
    fake = FakeSSHExecutor()
    result = await fetch_service_logs(fake, "node-a", "web", "compose", 100)
    assert result["error"]["code"] == "VALIDATION_ERROR"
