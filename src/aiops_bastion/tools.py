"""MCP 工具逻辑 (设计 §5)。

M2 交付可直测的 async 工具函数, 返回 {ok, data, error} 契约 (§5 统一返回)。
M3 再包为 MCP Server 的 @server.call_tool (§3.3, §10.1 映射)。

工具层依赖 SSHExecutor Protocol (§10.4), 与具体实现解耦:
  - 单测注入 FakeSSHExecutor (tests/fakes.py);
  - 真靶机集成注入 AsyncSSHExecutor (tests/test_integration_ssh.py);
  - 生产 (M3) 经 MCP Server 持有 AsyncSSHExecutor。

统一错误码 (§5.7):
  CommandValidationError/PathNotAllowlistedError/UnknownActionError -> VALIDATION_ERROR/PATH_NOT_ALLOWLISTED
  ExecTimeoutError -> EXEC_TIMEOUT
  SSHConnectionError -> INTERNAL
  HITLRejectedError -> HITL_REJECTED
"""
from __future__ import annotations

import json
from typing import Any, Literal

from .exceptions import (
    AIOpsError,
    CommandValidationError,
    ExecTimeoutError,
    HITLRejectedError,
    PathNotAllowlistedError,
    SSHConnectionError,
    UnknownActionError,
)
from .execution import SSHExecutor

# === 错误码 (§5.7) ===
_ERR_CODE: dict[type[AIOpsError], str] = {
    CommandValidationError: "VALIDATION_ERROR",
    UnknownActionError: "VALIDATION_ERROR",
    PathNotAllowlistedError: "PATH_NOT_ALLOWLISTED",
    ExecTimeoutError: "EXEC_TIMEOUT",
    SSHConnectionError: "INTERNAL",
    HITLRejectedError: "HITL_REJECTED",
}


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    """成功返回契约: {ok: true, data: ...}。"""
    return {"ok": True, "data": data}


def _err(exc: AIOpsError, *, message: str | None = None) -> dict[str, Any]:
    """失败返回契约: {ok: false, error: {code, message}} (§5.7)。"""
    code = _ERR_CODE.get(type(exc), "INTERNAL")
    return {"ok": False, "error": {"code": code, "message": message or str(exc)}}


# === token 截断估算 (§5.3: chars/4, 默认 ≤ 8k tokens) ===
DEFAULT_TOKEN_BUDGET = 8000
_CHARS_PER_TOKEN = 4


def _truncate_to_budget(text: str, token_budget: int) -> tuple[str, bool]:
    """按 token 估算截断 (§5.3/§7.3): chars ≈ token*4, 超出则截断并标记。

    返回 (截断后文本, truncated)。原始日志全文仅本地, Agent 取摘要后入 LLM (§4.5)。
    """
    max_chars = token_budget * _CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


# === §5.2 execute_discovery (L1 探测) ===

DiscoveryForm = Literal["systemd", "docker", "compose"]


def _map_status(form: str, result: Any) -> tuple[str, str]:
    """状态映射 [评审补充#R11]: systemctl/docker inspect/compose ps -> active|inactive|unknown。

    注: build_status_cmd 用 "systemctl status" (非 is-active); exit code 映射等价
    (0=active, 3=inactive, 其余=unknown) -- §5.2 R11 的 is-active 语义一致 (见 §5.2 修订)。
    """
    if form == "systemd":
        # systemctl status: exit 0=active, 3=inactive (unit 未运行/未找到), 其余=unknown
        if result.exit_code == 0:
            return "active", "systemctl status exit 0"
        if result.exit_code == 3:
            return "inactive", "systemctl status exit 3 (unit not active)"
        return "unknown", f"systemctl status exit {result.exit_code}: {result.stderr.strip()[:200]}"
    if form == "docker":
        # docker inspect: 解析 .State.Running (空/非 JSON -> unknown)
        return _map_docker_status(result)
    if form == "compose":
        return _map_compose_status(result)
    return "unknown", f"unknown form: {form}"


def _map_docker_status(result: Any) -> tuple[str, str]:
    """docker inspect JSON .State.Running -> active/inactive (§5.2 R11)。"""
    stdout = result.stdout.strip()
    if not stdout:
        return "unknown", "docker inspect 无输出"
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "unknown", "docker inspect 输出非 JSON"
    # docker inspect 返回数组, 取首个容器的 .State.Running
    if isinstance(data, list) and data:
        state = data[0].get("State", {})
        running = state.get("Running")
        if running is True:
            return "active", "docker inspect .State.Running=true"
        if running is False:
            return "inactive", "docker inspect .State.Running=false"
        return "unknown", f"docker inspect .State.Running={running!r}"
    if isinstance(data, dict):
        # 部分场景 inspect 返回单对象
        running = data.get("State", {}).get("Running")
        if running is True:
            return "active", "container running"
        if running is False:
            return "inactive", "container not running"
    return "unknown", "docker inspect 结构无法解析 .State.Running"


def _map_compose_status(result: Any) -> tuple[str, str]:
    """docker compose ps 输出: 有 running 行 -> active, 否则 inactive (§5.2 R11)。

    compose ps 默认人类可读表; 解析表头后行含 "running" 即 active。
    """
    stdout = result.stdout.strip()
    if not stdout:
        return "inactive", "compose ps 无输出 (服务未起或无容器)"
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    # 表头后每行代表一个服务; 含 "running" 状态即 active
    body = lines[1:] if len(lines) > 1 else lines
    running = any("running" in ln.lower() for ln in body)
    return ("active" if running else "inactive"), f"compose ps: {len(body)} service line(s)"


async def execute_discovery(
    executor: SSHExecutor,
    target_host: str,
    service_name: str,
    form: str,
) -> dict[str, Any]:
    """§5.2 L1 探测: 探测服务存活状态 (systemd/docker/compose)。

    输入校验 + 经 executor.run_readonly; 状态映射见 _map_status (R11)。
    返回 {ok, data:{target_host, service_name, status, detail}}。
    """
    try:
        result = await executor.run_readonly(target_host, form, service_name)
    except AIOpsError as e:
        return _err(e)
    status, detail = _map_status(form, result)
    return _ok({
        "target_host": target_host,
        "service_name": service_name,
        "status": status,
        "detail": detail,
    })


# === §5.3 fetch_service_logs (L2 日志) ===

LogsForm = Literal["systemd", "docker"]   # compose 日志延后


async def fetch_service_logs(
    executor: SSHExecutor,
    target_host: str,
    service_name: str,
    form: str,
    lines: int,
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> dict[str, Any]:
    """§5.3 L2 日志: 抓取报错日志, Server 端强制截断 [PRD §4.4]。

    - lines 上限 500 (build_logs_cmd 校验);
    - 返回前按 token 估算截断 (chars/4, 默认 ≤ 8k tokens), truncated 标记;
    - 原始日志全文仅本地消费, Agent 本地摘要后入 LLM (§4.5 出网边界)。

    修订: §5.3 schema 补 form 参数 (systemd|docker), 选 journalctl vs docker logs。
    返回 {ok, data:{target_host, service_name, logs, truncated}}。
    """
    try:
        result = await executor.run_logs(target_host, form, service_name, lines)
    except AIOpsError as e:
        return _err(e)
    logs, truncated = _truncate_to_budget(result.stdout, token_budget)
    return _ok({
        "target_host": target_host,
        "service_name": service_name,
        "logs": logs,
        "truncated": truncated,
    })
