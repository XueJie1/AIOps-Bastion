"""执行引擎: asyncssh + 白名单 + 模板 (设计 §3.4)。

唯一的 SSH 指令执行入口。绝不接受 Agent 传入的原始 shell 字符串。

三层防线 (设计 §4.2 [P2-7]):
  1. IDENT_RE fullmatch —— 参数进入命令前拒绝一切 shell 元字符 (核心防线)
  2. list[str] argv + asyncssh shlex.quote —— 即便含元字符也被单引号转义 (第二防线)
  3. 远端 rbash (推荐, 业务节点 authorized_keys 配置) —— 彻底消除 shell 解析面

L3 硬编码模板 (设计 §4.2 [决策#7]): action_type 仅 3 枚举, 无 reboot。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from .exceptions import (
    CommandValidationError,
    HITLRejectedError,
    PathNotAllowlistedError,
    UnknownActionError,
)

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
    """结构化构建只读命令 (L1/L2) [决策#8]。

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
    stdout: str
    stderr: str = ""
    truncated: bool = False


# === 执行引擎 (M1 阶段: 仅校验+渲染, 真 SSH 在 M2 接入) ===
#
# M1 安全地基只验证命令构建与模板渲染的安全性 (白名单 + IDENT_RE + 模板)。
# 真 asyncssh 连接池、超时熔断、信号量限流在 M2 (探测网关) 接入。
# 设计 §3.4 的 ExecutionEngine.run_readonly/run_remediation 接口在此预留,
# 实现体返回"已校验的 argv", 供注入测试断言。

class ExecutionEngine:
    """SSH 执行引擎 (设计 §3.4)。

    M1 阶段: 仅做命令校验与模板渲染, 不连真 SSH。
    M2 阶段: 接入 asyncssh 连接池 + Semaphore(4) + 超时熔断。

    绝不接受 Agent 传入的原始 shell 字符串 —— 所有命令经 build_status_cmd
    (L1/L2) 或 render (L3) 构建为 list[str] argv。
    """

    def __init__(self, max_concurrent: int = 4) -> None:
        self._sem_size = max_concurrent  # 设计 [决策#3]: Semaphore(4)

    def run_readonly(self, host: str, form: str, name: str) -> list[str]:
        """L1/L2 只读: 走白名单命令构建器, 返回已校验 argv。

        M2 接入真 SSH 后, 此方法改为执行并返回 ExecResult。
        """
        # host 也要走 IDENT_RE (§5.2 execute_discovery schema: target_host pattern)
        if not re.fullmatch(IDENT_RE, host):
            raise CommandValidationError(f"非法 target_host: {host!r}")
        return build_status_cmd(form, name)

    def run_remediation(
        self,
        host: str,
        action_type: str,
        params: dict[str, str],
        approval_id: str | None = None,
    ) -> list[str]:
        """L3 修复: 走硬编码模板, 返回已校验 argv。

        approval_id 须非空 (PermissionGate 校验, §3.3/§6.7)。
        M2 接入真 SSH 后, 此方法改为执行并返回 ExecResult。
        """
        if not re.fullmatch(IDENT_RE, host):
            raise CommandValidationError(f"非法 target_host: {host!r}")
        if not approval_id:
            # PermissionGate: 无 approval_id 即授权不通过 (§3.3/§5.7 HITL_REJECTED;
            # exceptions.py HITLRejectedError docstring 涵盖 "approval_id 无效/已复用")
            raise HITLRejectedError(
                "L3 修复缺 approval_id (PermissionGate 授权失败)"
            )
        return render(action_type, params)
