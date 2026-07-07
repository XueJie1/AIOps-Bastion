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
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .exceptions import VaultLockedError

# === 常量 (设计 §3.7) ===
PBKDF2_ITERATIONS = 600_000   # [决策#1] 弱 NAS 上约 1~2s, 已异步化
SALT_LEN = 16                 # bytes, 随机, 每实例唯一
VAULT_MAGIC = b"AIOV"         # vault.enc magic (§8.3)
VAULT_VERSION = 1

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

    # === 生命周期 (设计 §3.7 状态机) ===

    async def initialize(self, master_password: str) -> dict[str, Any]:
        """首次 Onboarding: 派生 key + 加密默认空 bundle + 落盘 vault.enc。

        返回默认 CredentialBundle (供 Onboarding UI 录入凭证后 update_credential)。
        PBKDF2 经 asyncio.to_thread 执行, 不阻塞事件循环。
        """
        salt = os.urandom(SALT_LEN)
        key = await asyncio.to_thread(self._derive_key, master_password, salt)
        ct = self._encrypt_bundle(key, DEFAULT_BUNDLE)
        self._write_vault(salt, ct, recovery_salt=b"", wrapped_key=b"")
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

    async def get(self, name: str) -> str:
        """取单条凭证 (按需解密, 调用方用完即弃, 不缓存到长生命周期对象)。

        经 asyncio.to_thread 解密 bundle, 不阻塞事件循环。
        """
        if self._key is None or self._ct is None:
            raise VaultLockedError("Vault 未 unlock, 无法取凭证")
        bundle = await asyncio.to_thread(self._decrypt_bundle, self._key, self._ct)
        return bundle[name]

    async def update_credential(self, name: str, value: Any) -> None:
        """[评审补充#R5] 单条凭证热更新: 解密 bundle → 更新字段 → 重新加密落盘。

        无需重新 Onboarding, 无需重新 unlock (须先 unlocked)。
        """
        if self._key is None or self._ct is None:
            raise VaultLockedError("Vault 未 unlock, 无法更新凭证")
        bundle = await asyncio.to_thread(self._decrypt_bundle, self._key, self._ct)
        bundle[name] = value
        new_ct = self._encrypt_bundle(self._key, bundle)
        # 重写密文部分 (salt/recovery_salt/wrapped_key 不变)
        salt, iters, recovery_salt, wrapped_key, _ = self._read_vault()
        self._write_vault(salt, new_ct, recovery_salt=recovery_salt, wrapped_key=wrapped_key)
        self._ct = new_ct

    async def rotate_master_password(self, new_master: str) -> None:
        """重设主密码: 重新派生 + 重加密 bundle (恢复短语包裹关系不变)。"""
        if self._key is None or self._ct is None:
            raise VaultLockedError("Vault 未 unlock, 无法轮转主密码")
        bundle = await asyncio.to_thread(self._decrypt_bundle, self._key, self._ct)
        new_salt = os.urandom(SALT_LEN)
        new_key = await asyncio.to_thread(self._derive_key, new_master, new_salt)
        new_ct = self._encrypt_bundle(new_key, bundle)
        self._write_vault(new_salt, new_ct, recovery_salt=b"", wrapped_key=b"")
        self._key = new_key
        self._ct = new_ct

    # === vault.enc 读写 (§8.3 格式) ===

    def _write_vault(self, salt: bytes, ct: bytes, *,
                     recovery_salt: bytes, wrapped_key: bytes) -> None:
        """写 vault.enc: magic + version + salt + iters + recovery_salt + wrapped_key + ct。"""
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

        M1 阶段未实现恢复短语 (§3.7 决策#17), recovery_salt 与 wrapped_key
        写入为空 b"", 故 ct 从偏移 25 开始 (4+1+16+4)。恢复短语机制启用后,
        须改为长度前缀编码以区分 wrapped_key 与 ct 的边界。
        """
        blob = self._path.read_bytes()
        if len(blob) < 4 + 1 + 16 + 4:
            raise ValueError("vault.enc 损坏 (过短)")
        if blob[:4] != VAULT_MAGIC:
            raise ValueError("vault.enc magic 不符")
        version = blob[4]
        if version != VAULT_VERSION:
            raise ValueError(f"vault.enc 版本不支持: {version}")
        salt = blob[5:21]
        iters = int.from_bytes(blob[21:25], "big")
        # M1: recovery_salt/wrapped_key 为空, ct 从偏移 25 开始
        recovery_salt = b""
        wrapped_key = b""
        ct = blob[25:]
        return salt, iters, recovery_salt, wrapped_key, ct
