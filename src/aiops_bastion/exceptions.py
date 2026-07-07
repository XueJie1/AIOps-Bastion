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
