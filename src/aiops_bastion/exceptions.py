"""AIOps-Bastion 异常体系。

所有自定义异常在此定义, 按 L0-L3 权限分级与错误码 (§5.7) 对齐。
设计依据: §4.2 指令安全模型, §5.7 错误码目录。
"""


class AIOpsError(Exception):
    """AIOps-Bastion 所有自定义异常的基类。"""


# === 指令安全 (§4.2) ===
class CommandValidationError(AIOpsError):
    """命令/参数校验失败 (L1/L2 白名单 + IDENT_RE fullmatch)。

    对应错误码 VALIDATION_ERROR (§5.7)。
    拒绝一切 shell 元字符: ; | & > < $ 反引号 ( ) \\n \\r \\\\ ' "
    """


class PathNotAllowlistedError(AIOpsError):
    """clear_cache 路径未命中白名单 (L3)。

    对应错误码 PATH_NOT_ALLOWLISTED (§5.7)。
    路径须精确匹配 CLEAR_CACHE_PATH_WHITELIST 集合成员, 非前缀匹配。
    """


class UnknownActionError(AIOpsError):
    """L3 action_type 不在枚举内 (restart_service/restart_container/clear_cache)。

    对应错误码 VALIDATION_ERROR (§5.7)。
    """


# === HITL 授权 (§3.3/§6.7) ===
class HITLRejectedError(AIOpsError):
    """L3 审批被拒, 或 approval_id 无效/已复用。

    对应错误码 HITL_REJECTED (§5.7)。
    approval_id 一次性消费, 执行后置 CONSUMED, 二次执行被拒。
    """


class VaultLockedError(AIOpsError):
    """Vault 未 unlock 即取凭证。

    对应错误码 AUTH_REQUIRED (§5.7)。
    """


# === SSH 执行 (§3.4 / §5.7) ===
class ExecTimeoutError(AIOpsError):
    """SSH 执行超时或 slot 排队等待超时。

    对应错误码 EXEC_TIMEOUT (§5.7)。
    两种来源 (§3.4 [P2-8]):
      - slot 等待超 wait_slot (queue wait timeout) -> 不重试, Agent 记 investigation_gap;
      - 命令执行超 5s(只读)/30s(修复) -> 释放连接, 记超时。
    """

    def __init__(self, message: str, *, kind: str = "exec") -> None:
        """kind: "exec" (命令执行超时) 或 "queue" (slot 等待超时), 用于区分告警来源。"""
        self.kind = kind
        super().__init__(message)


class SSHConnectionError(AIOpsError):
    """SSH 连接失败/异常断开。

    对应错误码 INTERNAL (§5.7)。
    连接池剔除该 host 连接, 下次按需重建。
    """
