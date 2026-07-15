"""LLM Provider 抽象层 (设计 §3.5) + FakeLLM 测试替身。

build_llm: 据 ProviderConfig 构造 ChatOpenAI / ChatAnthropic。
  - vendor="openai": 覆盖 OpenAI 兼容厂商 (DeepSeek/GLM, 决策#16 经 base_url 接入);
  - vendor="anthropic": Claude 原生。
  API Key / base_url 从 Vault 内存解密获取 (§3.5), 不落日志。
  temperature=0 降低运维幻觉; max_tokens_per_call 由 Token 预算控制器动态注入 (§6.6, M4)。

FakeLLM: 脚本式 BaseChatModel, 按预设 AIMessage 序列返回 (含 tool_calls),
驱动 react agent 全图单测, 无真 LLM/无网/无 API key (§10.4 可测试性)。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr

from .vault import Vault

Vendor = Literal["openai", "anthropic"]


# === Provider 配置 (§3.5) ===

@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """单个 LLM 厂商配置 (§8.3 llm_providers.<name>)。"""
    vendor: Vendor
    model: str
    api_key: str
    base_url: str | None = None
    temperature: float = 0.0
    max_tokens: int | None = None


async def load_provider_config(vault: Vault, name: str | None = None) -> ProviderConfig:
    """从 Vault 读激活 provider 配置 (§8.3 llm_active_provider)。

    name 为空时取 llm_active_provider; 经点路径下钻 llm_providers.<name>。
    """
    active = name or await vault.get("llm_active_provider")
    if not isinstance(active, str):
        raise ValueError("llm_active_provider 未配置或非字符串")
    cfg = await vault.get(["llm_providers", active])
    if not isinstance(cfg, dict):
        raise ValueError(f"llm_providers.{active} 配置缺失或非 dict")
    return ProviderConfig(
        vendor=cfg["vendor"],
        model=cfg["model"],
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        temperature=float(cfg.get("temperature", 0.0)),
        max_tokens=cfg.get("max_tokens"),
    )


def build_llm(cfg: ProviderConfig, *, max_tokens: int | None = None) -> BaseChatModel:
    """据 ProviderConfig 构造 LLM (§3.5)。

    max_tokens 覆盖 cfg.max_tokens (Token 预算控制器动态注入, §6.6; M3 传 None 用厂商默认)。
    """
    mt = max_tokens if max_tokens is not None else cfg.max_tokens
    if cfg.vendor == "openai":
        # 覆盖 OpenAI 兼容厂商 (DeepSeek/GLM) 经 base_url 接入 [决策#16]
        from langchain_openai import ChatOpenAI
        kwargs: dict[str, Any] = {
            "model": cfg.model, "api_key": cfg.api_key, "temperature": cfg.temperature,
        }
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        if mt is not None:
            kwargs["max_tokens"] = mt
        return ChatOpenAI(**kwargs)
    if cfg.vendor == "anthropic":
        from langchain_anthropic import ChatAnthropic
        kwargs = {
            "model": cfg.model, "api_key": cfg.api_key, "temperature": cfg.temperature,
        }
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        if mt is not None:
            kwargs["max_tokens"] = mt
        return ChatAnthropic(**kwargs)
    raise ValueError(f"未知 vendor: {cfg.vendor!r} (须 openai/anthropic)")


# === FakeLLM (§10.4 可测试性: 无网/无 key 驱动 react agent) ===

def _to_ai_message(entry: AIMessage | str | dict[str, Any]) -> AIMessage:
    """脚本条目归一化为 AIMessage。

    - AIMessage: 原样;
    - str: AIMessage(content=str) (无 tool_calls -> 终止 Agent 循环);
    - dict: {"content": ..., "tool_calls": [{"name","args","id"}]} -> AIMessage。
    """
    if isinstance(entry, AIMessage):
        return entry
    if isinstance(entry, str):
        return AIMessage(content=entry)
    if isinstance(entry, dict):
        tc = entry.get("tool_calls", [])
        # 补 type/id (react agent 要求 tool_call 含 id/type)
        normalized = [
            {**t, "type": t.get("type", "tool_call"),
             "id": t.get("id", f"call-{i}")}
            for i, t in enumerate(tc)
        ]
        return AIMessage(content=entry.get("content", ""), tool_calls=normalized)
    raise TypeError(f"FakeLLM 脚本条目类型不支持: {type(entry)!r}")


class FakeLLM(BaseChatModel):
    """脚本式 LLM 替身 (§10.4)。

    按脚本序列顺序返回 AIMessage (含 tool_calls); 脚本耗尽后返回空 AIMessage
    (无 tool_calls -> 终止 react agent 循环, END)。

    bind_tools 重写为 no-op (返回 self): 脚本已显式编码 tool_calls,
    不依赖真实工具绑定; react agent 调 model.bind_tools(tools) 后仍走 _generate。

    用法:
        llm = FakeLLM(script=[
            {"tool_calls": [{"name": "execute_discovery", "args": {...}}]},
            {"tool_calls": [{"name": "execute_remediation", "args": {...}}]},  # L3 -> interrupt
            "调查完成",   # resume 后 -> END
        ])
    """

    script: list[AIMessage] = Field(default_factory=list)
    calls: list[list[BaseMessage]] = Field(default_factory=list)   # 每次调用的输入 messages
    _index: int = PrivateAttr(default=0)

    def __init__(self, script: list[AIMessage | str | dict[str, Any]] | None = None) -> None:
        normalized = [_to_ai_message(e) for e in (script or [])]
        super().__init__(script=normalized)   # type: ignore[call-arg]

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(
        self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any
    ) -> BaseChatModel:
        """Fake: 脚本已编码 tool_calls, 工具绑定是 no-op, 返回 self。

        react agent 在 __init__ 调 model.bind_tools(tool_specs) 一次,
        此后每步 ainvoke 仍走 _generate 弹出脚本条目。
        """
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,   # noqa: ARG002
        run_manager: Any = None,          # noqa: ARG002
        **kwargs: Any,                    # noqa: ARG002
    ) -> ChatResult:
        self.calls.append(list(messages))
        if self._index < len(self.script):
            msg = self.script[self._index]
            self._index += 1
        else:
            # 脚本耗尽: 返回无 tool_calls 的空消息 -> 终止 react agent 循环
            msg = AIMessage(content="")
        return ChatResult(generations=[ChatGeneration(message=msg)])
