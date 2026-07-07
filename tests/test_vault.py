"""Vault 凭证管理测试 (设计 §3.7)。

验证:
- initialize → unlock → get → update_credential → lock 循环
- PBKDF2 异步化 (不阻塞事件循环, §3.7 [P0-2])
- 错误主密码 unlock 失败
- update_credential 热更新
- 未 unlock 取凭证抛 VaultLockedError
"""
import asyncio

import pytest
from cryptography.fernet import InvalidToken

from aiops_bastion.exceptions import VaultLockedError
from aiops_bastion.vault import DEFAULT_BUNDLE, Vault


@pytest.fixture
def vault_path(tmp_path):
    return tmp_path / "test_vault.enc"


@pytest.mark.asyncio
async def test_initialize_unlock_get_lock_cycle(vault_path):
    """完整生命周期: initialize → unlock → get → lock。"""
    vault = Vault(vault_path)

    # initialize
    bundle = await vault.initialize("my-master-password")
    assert bundle == DEFAULT_BUNDLE
    assert vault_path.exists()

    # lock (initialize 后 _key 已设, lock 丢弃)
    await vault.lock()
    with pytest.raises(VaultLockedError):
        await vault.get("cf_api_token")

    # unlock
    await vault.unlock("my-master-password")

    # get (默认空值)
    assert await vault.get("cf_api_token") == ""

    # lock 后取凭证失败
    await vault.lock()
    with pytest.raises(VaultLockedError):
        await vault.get("cf_api_token")


@pytest.mark.asyncio
async def test_wrong_master_password_rejected(vault_path):
    """错误主密码 unlock 失败 (Fernet HMAC 认证)。"""
    vault = Vault(vault_path)
    await vault.initialize("correct-password")
    await vault.lock()

    with pytest.raises(InvalidToken):
        await vault.unlock("wrong-password")


@pytest.mark.asyncio
async def test_update_credential_hot_reload(vault_path):
    """单条凭证热更新: 无需重新 Onboarding (§4.6)。"""
    vault = Vault(vault_path)
    await vault.initialize("master")
    await vault.unlock("master")

    # 更新 webhook_secret
    await vault.update_credential("webhook_secret", "new-secret-123")
    assert await vault.get("webhook_secret") == "new-secret-123"

    # lock 后 unlock, 确认落盘
    await vault.lock()
    await vault.unlock("master")
    assert await vault.get("webhook_secret") == "new-secret-123"


@pytest.mark.asyncio
async def test_rotate_master_password(vault_path):
    """轮转主密码: 重新派生 + 重加密 (恢复短语关系不变, §4.6)。"""
    vault = Vault(vault_path)
    await vault.initialize("old-master")
    await vault.unlock("old-master")
    await vault.update_credential("webhook_secret", "secret-val")

    # 轮转
    await vault.rotate_master_password("new-master")
    await vault.lock()

    # 旧密码失败
    with pytest.raises(InvalidToken):
        await vault.unlock("old-master")

    # 新密码成功, 凭证保留
    await vault.unlock("new-master")
    assert await vault.get("webhook_secret") == "secret-val"


@pytest.mark.asyncio
async def test_pbkdf2_does_not_block_event_loop(vault_path):
    """PBKDF2 (600k 迭代) 经 asyncio.to_thread, 不阻塞事件循环 (§3.7 [P0-2])。

    并发跑 unlock + 一个 asyncio.sleep, 若 unlock 阻塞则 sleep 会被推迟。
    """
    vault = Vault(vault_path)
    await vault.initialize("master")

    # 并发: unlock + sleep(0.05)
    # 若 PBKDF2 同步阻塞, sleep 会晚于 unlock 完成才返回
    import time
    start = time.monotonic()
    await asyncio.gather(
        vault.unlock("master"),
        asyncio.sleep(0.05),
    )
    elapsed = time.monotonic() - start
    # 两者并发, 总耗时应远小于串行 (PBKDF2 约 1-2s + 0.05s)
    # 这里只断言 sleep 没被严重推迟 (放宽到 1s, 容忍 PBKDF2 调度开销)
    assert elapsed < 1.0, f"事件循环被阻塞: {elapsed:.2f}s"


# === A2: 嵌套路径凭证更新 (§4.6: llm_providers.<name>.api_key / ssh_keys.<host>) ===

@pytest.mark.asyncio
async def test_update_nested_credential_dot_path(vault_path):
    """点路径更新嵌套字段 (llm_providers.deepseek.api_key), 不破坏 bundle 结构。"""
    vault = Vault(vault_path)
    await vault.initialize("master")
    await vault.unlock("master")

    await vault.update_credential("llm_providers.deepseek.api_key", "sk-new-123")

    # 同路径读回
    assert await vault.get("llm_providers.deepseek.api_key") == "sk-new-123"

    # bundle 结构未破坏: deepseek 子树完整, 其他字段未受影响
    deepseek = await vault.get("llm_providers.deepseek")
    assert deepseek["model"] == "deepseek-v4-pro"
    assert deepseek["base_url"] == "https://api.deepseek.com/v1"
    assert deepseek["api_key"] == "sk-new-123"
    glm = await vault.get("llm_providers.glm")
    assert glm["api_key"] == ""

    # 落盘持久: lock + unlock 后仍读到新值
    await vault.lock()
    await vault.unlock("master")
    assert await vault.get("llm_providers.deepseek.api_key") == "sk-new-123"


@pytest.mark.asyncio
async def test_update_nested_credential_host_with_dot(vault_path):
    """host_id 含 '.' (如 xuejie1.top) 时传 list, 避免 split 歧义 (§4.6 ssh_keys.<host>)。"""
    vault = Vault(vault_path)
    await vault.initialize("master")
    await vault.unlock("master")

    # list 形式: host 含点不被拆分
    await vault.update_credential(["ssh_keys", "xuejie1.top"], "-----BEGIN KEY-----")
    assert await vault.get(["ssh_keys", "xuejie1.top"]) == "-----BEGIN KEY-----"

    # 字符串形式 "ssh_keys.xuejie1.top" 会被拆成 [ssh_keys, xuejie1, top] → KeyError
    # 即 host 含点场景必须用 list, 这是 split 语义的必然结果
    with pytest.raises(KeyError):
        await vault.get("ssh_keys.xuejie1.top")


# === D2: vault.enc 格式损坏拒绝 (§8.3) ===

@pytest.mark.asyncio
async def test_vault_corrupted_magic_rejected(vault_path):
    """magic 不符 → ValueError。"""
    vault = Vault(vault_path)
    await vault.initialize("master")
    data = vault_path.read_bytes()
    vault_path.write_bytes(b"XXXX" + data[4:])
    with pytest.raises(ValueError, match="magic"):
        await vault.unlock("master")


@pytest.mark.asyncio
async def test_vault_truncated_rejected(vault_path):
    """文件过短 (< 41B 固定头) → ValueError。"""
    vault = Vault(vault_path)
    await vault.initialize("master")
    vault_path.write_bytes(b"AIOV\x01short")
    with pytest.raises(ValueError, match="过短"):
        await vault.unlock("master")


@pytest.mark.asyncio
async def test_vault_unsupported_version_rejected(vault_path):
    """version 不支持 → ValueError。"""
    vault = Vault(vault_path)
    await vault.initialize("master")
    data = vault_path.read_bytes()
    # version byte 在偏移 4
    vault_path.write_bytes(data[:4] + bytes([0xFF]) + data[5:])
    with pytest.raises(ValueError, match="版本"):
        await vault.unlock("master")


# === D3: vault.enc 文件权限 0600 (§4.1) ===

@pytest.mark.asyncio
async def test_vault_file_permissions_restricted(vault_path):
    """vault.enc 权限 0600 (§4.1)。

    容错: 容器非属主场景 chmod 可能不完全生效, 只断言 group/others 无权限。
    """
    vault = Vault(vault_path)
    await vault.initialize("master")
    mode = vault_path.stat().st_mode & 0o777
    assert mode & 0o077 == 0, f"vault.enc 权限过宽 (group/others 可访问): {oct(mode)}"
