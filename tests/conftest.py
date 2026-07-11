"""测试公共 fixture。

集成测试 (test_integration_ssh) 经 env 门控: 无 AIOPS_TEST_SSH_* 环境变量时跳过,
CI 不跑真靶机。私钥仅在本地文件 -> Vault, 绝不入提交/日志/LLM。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from aiops_bastion.execution import AsyncSSHExecutor
from aiops_bastion.vault import Vault


@dataclass
class SSHTarget:
    """真靶机集成配置 (来自 env)。"""
    host: str
    user: str | None
    port: int
    key_path: Path
    known_hosts: str | None
    service: str | None
    form: str


def _env_or_skip(name: str) -> str:
    """读 env; 未设则 skip (env 门控, 无真靶机时跳过集成测试)。"""
    val = os.environ.get(name)
    if not val:
        pytest.skip(f"{name} 未设置, 跳过真靶机集成测试")
    return val


@pytest.fixture
def ssh_target() -> SSHTarget:
    """真靶机配置 (env 门控)。

    必需 env:
      - AIOPS_TEST_SSH_HOST: 目标 host
      - AIOPS_TEST_SSH_KEY: Bastion 专用私钥文件路径 (仅本地)
    可选 env:
      - AIOPS_TEST_SSH_USER: SSH 用户名 (默认随系统)
      - AIOPS_TEST_SSH_PORT: 端口 (默认 22)
      - AIOPS_TEST_SSH_KNOWN_HOSTS: known_hosts 路径 (默认 None 禁用主机密钥校验, 作品集权衡)
      - AIOPS_TEST_SSH_SERVICE: 探测的服务名 (未设则跳过 L1/L2 service 测试)
      - AIOPS_TEST_SSH_FORM: systemd|docker (默认 systemd)
    """
    host = _env_or_skip("AIOPS_TEST_SSH_HOST")
    key_path = _env_or_skip("AIOPS_TEST_SSH_KEY")
    if not Path(key_path).exists():
        pytest.skip(f"SSH 私钥文件不存在: {key_path}")
    return SSHTarget(
        host=host,
        user=os.environ.get("AIOPS_TEST_SSH_USER"),
        port=int(os.environ.get("AIOPS_TEST_SSH_PORT", "22")),
        key_path=Path(key_path),
        known_hosts=os.environ.get("AIOPS_TEST_SSH_KNOWN_HOSTS"),
        service=os.environ.get("AIOPS_TEST_SSH_SERVICE"),
        form=os.environ.get("AIOPS_TEST_SSH_FORM", "systemd"),
    )


@pytest.fixture
async def ssh_executor(ssh_target: SSHTarget, tmp_path: Path) -> AsyncSSHExecutor:
    """真靶机 AsyncSSHExecutor: 建 Vault + 载入私钥 (仅本地, §4.6 点路径)。

    私钥从本地文件读入 Vault 内存, 绝不落日志/不入提交。每个测试独立 Vault+池。
    """
    vault = Vault(tmp_path / "integration_vault.enc")
    await vault.initialize("integration-test-master")
    # host 含 '.' 时传 list 避免 split 歧义 (§4.6); 此处统一用 list 形式
    await vault.update_credential(
        ["ssh_keys", ssh_target.host],
        ssh_target.key_path.read_text(),
    )
    executor = AsyncSSHExecutor(
        vault,
        default_username=ssh_target.user,
        default_port=ssh_target.port,
        known_hosts=ssh_target.known_hosts,
    )
    try:
        yield executor
    finally:
        await executor.aclose()
