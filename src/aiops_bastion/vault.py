"""凭证管理系统 (设计 §3.7)。

Master Password 为根信任, 绝不落盘、绝不出网 [PRD §4.1]。

派生链:
  Master Password → PBKDF2HMAC(SHA256, 600k iters, salt) → Fernet key
  Fernet key 加密 CredentialBundle JSON (SSH 私钥/CF Token/TG Token/LLM API Key)

异步化 [P0-2]: PBKDF2 (600k 迭代, 弱 NAS 上 1~2s) 与 Fernet 解密经
  asyncio.to_thread() 在默认线程池执行, 不阻塞 asyncio 事件循环。

内存清零安全声明 [评审补充#R6]: Python 层无法保证凭证内存物理清零,
  本系统不声称能做到。防护依赖 OS 级 (禁 core dump + 进程隔离) +
  凭证驻留最小化 (lock() 丢弃引用, get() 用完即弃)。

嵌套路径 (§4.6): update_credential / get 支持点路径, 如
  "llm_providers.deepseek.api_key"; host_id 含 '.' 时传 list 避免 split 歧义。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .exceptions import VaultLockedError

# === 常量 (设计 §3.7 / §8.3) ===
PBKDF2_ITERATIONS = 600_000   # [决策#1] 弱 NAS 上约 1~2s, 已异步化
SALT_LEN = 16                 # bytes, 随机, 每实例唯一
RECOVERY_SALT_LEN = 16        # §8.3: recovery_salt 始终占 16B
VAULT_MAGIC = b"AIOV"         # vault.enc magic (§8.3)
VAULT_VERSION = 1
# 固定头: magic(4) + version(1) + salt(16) + iters(4) + recovery_salt(16) = 41
VAULT_HEADER_LEN = 4 + 1 + SALT_LEN + 4 + RECOVERY_SALT_LEN

# === CredentialBundle (明文 JSON, 仅内存; §8.3) ===
DEFAULT_BUNDLE: dict[str, Any] = {
    "ssh_keys": {},           # {host_id: "-----BEGIN OPENSSH PRIVATE KEY-----..."}
    "cf_api_token": "",
    "telegram_bot_token": "",
    "llm_providers": {
        # vendor="openai" 覆盖 OpenAI 兼容厂商 (GLM/DeepSeek) [决策#16]
        "deepseek": {"vendor": "openai", "model": "deepseek-v4-pro",
                     "api_key": "", "base_url": "https://api.deepseek.com/v1"},
        "glm":      {"vendor": "openai", "model": "glm-5.2",
                     "api_key": "", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
        "anthropic": {"vendor": "anthropic", "model": "claude-...",
                      "api_key": "", "base_url": "https://api.anthropic.com"},
        "openai":   {"vendor": "openai", "model": "gpt-...",
                     "api_key": "", "base_url": "https://api.openai.com/v1"},
    },
    "llm_active_provider": "deepseek",   # [决策#19] 固定单一 vendor
    "webhook_secret": "",                # [决策#21] Uptime Kuma 共享密钥
}


class Vault:
    """凭证管理: 生成/加密/解密/销毁全部凭证。

    Master Password 及其派生密钥、所有原始凭证绝不离开 Bastion,
    仅运行期内存解密使用 [PRD §4.1]。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._key: bytes | None = None       # 仅内存; lock() 丢弃引用
        self._ct: bytes | None = None        # 缓存密文, 按需解密单条凭证

    # === 派生与加密 (P0-2 异步化) ===

    def _derive_key(self, master_password: str, salt: bytes) -> bytes:
        """PBKDF2 派生 32 raw bytes (CPU 密集, 须 asyncio.to_thread 调用)。

        注意: 返回的是 raw bytes, 须经 _to_fernet_key 编码为 urlsafe base64
        才能交给 Fernet (Fernet 要求 32 bytes 的 urlsafe base64 编码字符串)。
        """
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=PBKDF2_ITERATIONS,
        )
        return kdf.derive(master_password.encode())

    @staticmethod
    def _to_fernet_key(raw_key: bytes) -> bytes:
        """raw 32 bytes → urlsafe base64 (Fernet 可用格式)。

        PBKDF2 输出的是任意 32 bytes, Fernet 要求 32 url-safe base64-encoded bytes。
        设计 §3.7 "PBKDF2 → Fernet key" 省略了这步编码, 此处补齐。
        """
        return base64.urlsafe_b64encode(raw_key)

    def _encrypt_bundle(self, raw_key: bytes, bundle: dict[str, Any]) -> bytes:
        """Fernet key 加密 CredentialBundle JSON。"""
        return Fernet(self._to_fernet_key(raw_key)).encrypt(json.dumps(bundle).encode())

    def _decrypt_bundle(self, raw_key: bytes, ct: bytes) -> dict[str, Any]:
        """Fernet key 解密 CredentialBundle (CPU 密集, 须 asyncio.to_thread 调用)。

        Fernet token 自带 HMAC 认证, 篡改可检测。
        """
        plaintext = Fernet(self._to_fernet_key(raw_key)).decrypt(ct)
        return json.loads(plaintext)

    # === 嵌套路径解析 (§4.6: llm_providers.<name>.api_key / ssh_keys.<host>) ===

    @staticmethod
    def _split_path(name: str | Sequence[str]) -> list[str]:
        """凭证名 → 路径段。

        - str: 按 '.' 拆分 (如 "llm_providers.deepseek.api_key" → 3 段)。
        - Sequence[str]: 直接用, 避免 host_id 含 '.' 时歧义
          (如 ["ssh_keys", "xuejie1.top"] 不被误拆成 3 段)。

        设计 §3.7 示例为扁平 bundle[name], §4.6 表格用点路径 —— 二者矛盾,
        此处取 §4.6 点路径语义 (实际轮转需求); 扁平 top-level 键 (如 webhook_secret)
        单段路径同样适用。
        """
        if isinstance(name, str):
            return name.split(".")
        return list(name)

    @staticmethod
    def _get_path(bundle: dict[str, Any], parts: list[str]) -> Any:
        """按路径段下钻取值 (叶节点可能是 str 或 dict)。"""
        cur: Any = bundle
        for p in parts:
            cur = cur[p]
        return cur

    @staticmethod
    def _set_path(bundle: dict[str, Any], parts: list[str], value: Any) -> None:
        """按路径段下钻, 写入叶节点 (自动创建嵌套结构不存在时报 KeyError)。"""
        cur: Any = bundle
        for p in parts[:-1]:
            cur = cur[p]
        cur[parts[-1]] = value

    # === 生命周期 (设计 §3.7 状态机) ===

    async def initialize(self, master_password: str) -> dict[str, Any]:
        """首次 Onboarding: 派生 key + 加密默认空 bundle + 落盘 vault.enc。

        返回默认 CredentialBundle (供 Onboarding UI 录入凭证后 update_credential)。
        PBKDF2 经 asyncio.to_thread 执行, 不阻塞事件循环。

        注: 恢复短语 (§3.7 决策#17 BIP-39) 延后实现; vault.enc 仍按 §8.3
        写入 16B recovery_salt 占位 (随机, 无语义) + 空 wrapped_key, 使格式
        与设计一致。未来启用恢复短语时仅需在 wrapped_key 前加长度前缀。
        """
        salt = os.urandom(SALT_LEN)
        recovery_salt = os.urandom(RECOVERY_SALT_LEN)  # 占位, 未启用恢复短语
        key = await asyncio.to_thread(self._derive_key, master_password, salt)
        ct = self._encrypt_bundle(key, DEFAULT_BUNDLE)
        self._write_vault(salt, ct, recovery_salt=recovery_salt, wrapped_key=b"")
        self._key = key
        self._ct = ct
        return DEFAULT_BUNDLE

    async def unlock(self, master_password: str) -> None:
        """输入主密码解锁: 读取 vault.enc → PBKDF2 派生 → 验证解密。

        PBKDF2 与 Fernet 解密经 asyncio.to_thread, 不阻塞事件循环。
        解密失败 (密码错) 抛 InvalidToken。
        """
        salt, _iters, _rsalt, _wkey, ct = self._read_vault()
        key = await asyncio.to_thread(self._derive_key, master_password, salt)
        # 验证 key 正确: 尝试解密 bundle
        await asyncio.to_thread(self._decrypt_bundle, key, ct)
        self._key = key
        self._ct = ct

    async def lock(self) -> None:
        """锁定: 丢弃 _key 引用 (不声称物理清零, 见安全声明)。"""
        self._key = None
        # _ct 保留 (密文, 无敏感信息)

    async def get(self, name: str | Sequence[str]) -> Any:
        """取单条凭证 (按需解密, 调用方用完即弃, 不缓存到长生命周期对象)。

        支持点路径 (§4.6): "llm_providers.deepseek.api_key" 下钻取子字段。
        路径段含 '.' (如 ssh_keys 的 host_id=xuejie1.top) 时传 list。
        叶节点可能是 str (api_key) 或 dict (llm_providers 子树)。
        """
        if self._key is None or self._ct is None:
            raise VaultLockedError("Vault 未 unlock, 无法取凭证")
        bundle = await asyncio.to_thread(self._decrypt_bundle, self._key, self._ct)
        return self._get_path(bundle, self._split_path(name))

    async def update_credential(self, name: str | Sequence[str], value: Any) -> None:
        """[评审补充#R5] 单条凭证热更新: 解密 bundle → 更新字段 → 重新加密落盘。

        支持点路径嵌套 (§4.6): "llm_providers.deepseek.api_key" 更新子字段;
        host_id 含 '.' 时传 list (如 ["ssh_keys", "xuejie1.top"])。
        无需重新 Onboarding, 无需重新 unlock (须先 unlocked)。
        """
        if self._key is None or self._ct is None:
            raise VaultLockedError("Vault 未 unlock, 无法更新凭证")
        bundle = await asyncio.to_thread(self._decrypt_bundle, self._key, self._ct)
        self._set_path(bundle, self._split_path(name), value)
        new_ct = self._encrypt_bundle(self._key, bundle)
        # 重写: salt/recovery_salt/wrapped_key 保持不变 (§4.6 一致性约束)
        salt, _iters, recovery_salt, wrapped_key, _ = self._read_vault()
        self._write_vault(salt, new_ct, recovery_salt=recovery_salt, wrapped_key=wrapped_key)
        self._ct = new_ct

    async def rotate_master_password(self, new_master: str) -> None:
        """重设主密码: 重新派生 + 重加密 bundle。

        recovery_salt/wrapped_key 原样回写 (M1 无恢复短语包裹关系, 仅保留占位;
        启用恢复短语后此处置入 wrapped_key, 包裹关系随主密码轮转保持不变)。
        """
        if self._key is None or self._ct is None:
            raise VaultLockedError("Vault 未 unlock, 无法轮转主密码")
        bundle = await asyncio.to_thread(self._decrypt_bundle, self._key, self._ct)
        new_salt = os.urandom(SALT_LEN)
        new_key = await asyncio.to_thread(self._derive_key, new_master, new_salt)
        new_ct = self._encrypt_bundle(new_key, bundle)
        # recovery_salt/wrapped_key 保留不变
        _salt, _iters, recovery_salt, wrapped_key, _ = self._read_vault()
        self._write_vault(new_salt, new_ct, recovery_salt=recovery_salt, wrapped_key=wrapped_key)
        self._key = new_key
        self._ct = new_ct

    # === vault.enc 读写 (§8.3 格式) ===

    def _write_vault(self, salt: bytes, ct: bytes, *,
                     recovery_salt: bytes, wrapped_key: bytes) -> None:
        """写 vault.enc (§8.3): magic + version + salt + iters
        + recovery_salt(16) + wrapped_key(变长) + ct(变长)。

        recovery_salt 始终写 16B (未启用恢复短语时为随机占位)。
        M1 wrapped_key=b""; 启用恢复短语后此处置入 Fernet token, 并须在
        wrapped_key 前加 4B 长度前缀以区分 wrapped_key/ct 边界
        (§8.3 两个变长 Fernet token 连续存放, 边界未定义 —— 设计待补)。
        """
        if len(recovery_salt) != RECOVERY_SALT_LEN:
            raise ValueError(f"recovery_salt 须 {RECOVERY_SALT_LEN}B, 实际 {len(recovery_salt)}")
        iters = PBKDF2_ITERATIONS.to_bytes(4, "big")
        blob = (
            VAULT_MAGIC
            + bytes([VAULT_VERSION])
            + salt
            + iters
            + recovery_salt
            + wrapped_key
            + ct
        )
        self._path.write_bytes(blob)
        # 文件权限 0600 (§4.1); 容器内非属主场景可能失败, 忽略
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)

    def _read_vault(self) -> tuple[bytes, int, bytes, bytes, bytes]:
        """读 vault.enc, 返回 (salt, iters, recovery_salt, wrapped_key, ct)。

        格式 (§8.3): magic(4) + version(1) + salt(16) + iters(4)
                      + recovery_salt(16) + wrapped_key(变长) + ct(变长)

        固定头 41B (含 recovery_salt)。M1 未实现恢复短语 (§3.7 决策#17),
        wrapped_key=b"", 故 ct 从偏移 41 (VAULT_HEADER_LEN) 开始。
        启用恢复短语后, 须按长度前缀解析 wrapped_key 与 ct 的边界。
        """
        blob = self._path.read_bytes()
        if len(blob) < VAULT_HEADER_LEN:
            raise ValueError("vault.enc 损坏 (过短)")
        if blob[:4] != VAULT_MAGIC:
            raise ValueError("vault.enc magic 不符")
        version = blob[4]
        if version != VAULT_VERSION:
            raise ValueError(f"vault.enc 版本不支持: {version}")
        salt = blob[5:21]
        iters = int.from_bytes(blob[21:25], "big")
        recovery_salt = blob[25:41]
        # M1: wrapped_key 为空, ct 从偏移 41 开始
        wrapped_key = b""
        ct = blob[VAULT_HEADER_LEN:]
        return salt, iters, recovery_salt, wrapped_key, ct
