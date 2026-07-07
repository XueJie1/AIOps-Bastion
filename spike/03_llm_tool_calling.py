"""
Spike 03 — DeepSeek tool-calling (验证 §3.5 + 决策#16)

假设: DeepSeek 经 OpenAI 兼容端点支持 tool calling;
      ChatOpenAI + 自定义 base_url 可用。

做法:
  1. 核实 "DeepSeek-V4-Pro" 是否为真实模型 id (计划假设的 id)
  2. ChatOpenAI(model=<真实 id>, base_url=https://api.deepseek.com/v1)
  3. 定义 get_status / restart_service 工具
  4. 发 "节点A nginx 挂了, 先查状态"
  5. 断言返回 tool_call (非纯文本) + 正确工具名 + 解析参数

PASS:  结构化 tool_call, 工具名+参数正确
FAIL:   仅文本无 tool_call -> 记录 + 切换 provider 建议
需: DEEPSEEK_API_KEY (在 spike/.env)
"""
import os
import sys
import json
import asyncio

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool


# ---------- 工具定义 ----------
@tool
def get_status(target_host: str, service_name: str, form: str) -> str:
    """探测目标主机某服务的存活状态。form 取值: systemd/docker/compose。

    返回 {ok, data:{target_host, service_name, status, detail}} 的 JSON 字符串。
    """
    # spike 里 canned, 不真连 SSH
    return json.dumps({
        "ok": True,
        "data": {
            "target_host": target_host,
            "service_name": service_name,
            "status": "inactive",
            "detail": f"canned: {form} service {service_name} on {target_host} is down",
        },
    })


@tool
def restart_service(target_host: str, unit: str) -> str:
    """重启目标主机上的 systemd 服务 (L3 高危, 需 HITL 审批)。"""
    return json.dumps({"ok": True, "data": {"target_host": target_host, "unit": unit, "exit_code": 0}})


TOOLS = [get_status, restart_service]


# ---------- 候选模型 id (核实用) ----------
# 顺序: 优先测设计首选的 deepseek-v4-pro (Agent 能力最强),
# 再测 v4-flash (经济版), 最后才是即将弃用的旧别名 (chat/reasoner)
# 注: deepseek-chat / deepseek-reasoner 2026-07-24 弃用, 当前指向 v4-flash
CANDIDATE_MODELS = ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"]


async def probe_model(llm: ChatOpenAI, model_id: str) -> tuple[bool, str, dict | None]:
    """对单个模型发测试请求, 返回 (支持tool_calling?, 原因, 调用详情)。"""
    llm_with_tools = llm.bind_tools(TOOLS)
    try:
        resp = await llm_with_tools.ainvoke(
            "节点A (node-a) 上的 nginx 服务挂了, 先用 get_status 查一下它的状态。form 用 systemd。"
        )
    except Exception as e:
        return False, f"调用异常: {type(e).__name__}: {e}", None

    # 检查 tool_calls
    tool_calls = getattr(resp, "tool_calls", None) or []
    content = getattr(resp, "content", "")
    usage = getattr(resp, "usage_metadata", None)

    detail = {
        "model": model_id,
        "tool_calls": [
            {"name": tc["name"], "args": tc["args"]}
            for tc in tool_calls
        ],
        "content_preview": (content[:200] if isinstance(content, str) else str(content)[:200]),
        "usage": str(usage),
    }

    if tool_calls:
        tc = tool_calls[0]
        if tc["name"] == "get_status":
            args = tc["args"]
            if args.get("target_host") and args.get("service_name"):
                return True, f"tool_call 正确: {tc['name']}({args})", detail
            return False, f"tool_call 正确但参数不全: {args}", detail
        return False, f"tool_call 选错工具: {tc['name']}", detail
    return False, f"无 tool_call, 仅文本: {detail['content_preview']}", detail


async def main():
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key or api_key.startswith("sk-your"):
        print("FAIL: 未配置 DEEPSEEK_API_KEY")
        print("  请复制 spike/.env.example 为 spike/.env 并填入真实 key")
        sys.exit(2)

    print("=" * 60)
    print("Spike 03: DeepSeek tool-calling")
    print("=" * 60)
    print(f"API Key: {api_key[:8]}...{api_key[-4:]}")

    base_url = "https://api.deepseek.com/v1"

    # ---------- 逐个核实候选模型 ----------
    print("\n--- 核实模型 id (设计假设 'DeepSeek-V4-Pro') ---")
    results = []
    for model_id in CANDIDATE_MODELS:
        print(f"\n尝试模型: {model_id}")
        llm = ChatOpenAI(
            model=model_id,
            api_key=api_key,
            base_url=base_url,
            temperature=0,
            max_tokens=500,
        )
        ok, reason, detail = await probe_model(llm, model_id)
        print(f"  {reason}")
        results.append((model_id, ok, reason, detail))
        if ok:
            print(f"  ✓ 该模型支持 tool-calling, 可用")
            break  # 找到一个可用即可

    # ---------- 断言 ----------
    print("\n" + "=" * 60)
    print("结论")
    print("=" * 60)
    working = [(m, d) for (m, ok, r, d) in results if ok]
    if not working:
        print("所有候选模型均未通过 tool-calling 测试:")
        for m, ok, r, d in results:
            print(f"  {m}: {r}")
        print("\n>>> 03 结论: FAIL")
        print("影响: 整个 Agent 方案 (DeepSeek 驱动) 不成立, 需切换 provider 或加解析 shim")
        sys.exit(1)

    model_id, detail = working[0]
    print(f"可用模型 id: {model_id}")
    print(f"tool_call: {detail['tool_calls']}")
    print(f"usage: {detail['usage']}")

    # ---------- 记录设计修订需求 ----------
    design_assumed = "deepseek-v4-pro"
    if model_id != design_assumed and design_assumed not in [m for m, _, _, _ in results if m == design_assumed and False]:
        print(f"\n⚠ 设计文档 (§3.5/§8.3/取舍 §1.1/§1.9) 写的 'DeepSeek-V4-Pro' 实际 id 为 '{model_id}'")
        print(f"  -> 实施时需同步修正设计文档中的模型 id")

    print("\n- model: " + model_id)
    print("- base_url: " + base_url)
    print("- tool calling: ✓ 结构化返回, 工具名+参数正确")
    print("\n>>> 03 结论: PASS")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
