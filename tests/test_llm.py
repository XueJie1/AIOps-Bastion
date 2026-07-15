"""llm.py 测试: Provider 配置 + build_llm 构造 + FakeLLM 脚本推进 (§3.5/§10.4)。

build_llm 不触网 (仅构造对象); FakeLLM 无网/无 key 驱动 ainvoke + bind_tools。
"""
from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage

from aiops_bastion.llm import FakeLLM, ProviderConfig, build_llm, load_provider_config
from aiops_bastion.vault import Vault

# === build_llm 构造 (不触网) ===

def test_build_llm_openai_compat() -> None:
    """DeepSeek/GLM 经 vendor=openai + base_url 接入 (决策#16)。"""
    cfg = ProviderConfig(
        vendor="openai", model="deepseek-v4-pro", api_key="sk-test",
        base_url="https://api.deepseek.com/v1", temperature=0.0,
    )
    llm = build_llm(cfg)
    assert isinstance(llm, BaseChatModel)
    # ChatOpenAI 暴露 model_name / model (langchain-openai)
    assert getattr(llm, "model_name", None) == "deepseek-v4-pro" or \
        getattr(llm, "model", None) == "deepseek-v4-pro"


def test_build_llm_max_tokens_override() -> None:
    cfg = ProviderConfig(vendor="openai", model="gpt-4o", api_key="sk-test")
    llm = build_llm(cfg, max_tokens=2048)
    assert isinstance(llm, BaseChatModel)
    assert getattr(llm, "max_tokens", None) == 2048


def test_build_llm_unknown_vendor_raises() -> None:
    # vendor 枚举外 -> ValueError (mypy 层面 Vendor 限定, 运行期显式校验)
    cfg = ProviderConfig(  # type: ignore[arg-type]
        vendor="grok", model="x", api_key="k",  # 运行期非法
    )
    with pytest.raises(ValueError, match="未知 vendor"):
        build_llm(cfg)


# === load_provider_config 从 Vault 读激活 provider ===

async def test_load_provider_config_from_vault(tmp_path) -> None:
    vault = Vault(tmp_path / "v.enc")
    await vault.initialize("master")
    # 默认 bundle 含 deepseek provider (§8.3), 录入 api_key
    await vault.update_credential(["llm_providers", "deepseek", "api_key"], "sk-real")
    cfg = await load_provider_config(vault)
    assert cfg.vendor == "openai"
    assert cfg.model == "deepseek-v4-pro"
    assert cfg.api_key == "sk-real"
    assert cfg.base_url == "https://api.deepseek.com/v1"


async def test_load_provider_config_explicit_name(tmp_path) -> None:
    vault = Vault(tmp_path / "v.enc")
    await vault.initialize("master")
    await vault.update_credential(["llm_providers", "glm", "api_key"], "glm-key")
    cfg = await load_provider_config(vault, name="glm")
    assert cfg.model == "glm-5.2"
    assert cfg.api_key == "glm-key"


# === FakeLLM 脚本推进 ===

async def test_fake_llm_script_sequence() -> None:
    llm = FakeLLM(script=[
        AIMessage(content="first"),
        AIMessage(content="second"),
    ])
    r1 = await llm.ainvoke([HumanMessage(content="hi")])
    r2 = await llm.ainvoke([HumanMessage(content="hi2")])
    r3 = await llm.ainvoke([HumanMessage(content="hi3")])
    assert r1.content == "first"
    assert r2.content == "second"
    assert r3.content == ""   # 脚本耗尽 -> 空 (终止循环)


async def test_fake_llm_dict_entries_with_tool_calls() -> None:
    llm = FakeLLM(script=[
        {"tool_calls": [{"name": "execute_discovery", "args": {"x": 1}}]},
        "done",
    ])
    r1 = await llm.ainvoke([HumanMessage(content="go")])
    assert r1.tool_calls[0]["name"] == "execute_discovery"
    assert r1.tool_calls[0]["args"] == {"x": 1}
    assert r1.tool_calls[0]["type"] == "tool_call"   # _to_ai_message 补全
    assert r1.tool_calls[0]["id"]   # 补 id

    r2 = await llm.ainvoke([HumanMessage(content="go2")])
    assert r2.content == "done"
    assert r2.tool_calls == []


async def test_fake_llm_records_calls() -> None:
    llm = FakeLLM(script=[AIMessage(content="ok")])
    await llm.ainvoke([HumanMessage(content="q1"), HumanMessage(content="q2")])
    assert len(llm.calls) == 1
    assert len(llm.calls[0]) == 2


def test_fake_llm_bind_tools_returns_self() -> None:
    """bind_tools 是 no-op, 返回 self (react agent 调用不报 NotImplementedError)。"""
    llm = FakeLLM(script=[AIMessage(content="ok")])
    bound = llm.bind_tools([])
    assert bound is llm


async def test_fake_llm_empty_script_terminates() -> None:
    """空脚本 -> 首次即返回空 (无 tool_calls), 适合测'直接回答'场景。"""
    llm = FakeLLM()
    r = await llm.ainvoke([HumanMessage(content="hi")])
    assert r.content == ""
    assert r.tool_calls == []
