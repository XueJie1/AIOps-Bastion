"""
Spike 02 — MCP in-process 加载 (验证 §10.4)

假设: langchain-mcp-adapters 能 in-process 加载 MCP Server 协程,
      绕过 stdio 子进程边界, 供 CI 测试。

做法:
  1. 用 mcp SDK 写最小 Server (2 工具: ping + 假 execute_discovery 返回 canned 状态)
  2. 用 mcp.shared.memory.create_connected_server_and_client_session 做 in-process 连接
  3. 包成 langchain-mcp-adapters 的 BaseTool 列表
  4. 调用, 断言返回 {ok, data} 契约 (对齐设计 §5 统一返回格式)

PASS:  in-process 可调, 无子进程, 返回结构化契约
PARTIAL: 仅 stdio 可用 -> CI 用子进程 (可接受, 记录)
FAIL:   记录到 REPORT.md
"""
import sys
import asyncio

from mcp.server import Server
import mcp.types as mcp_types
from mcp.shared.memory import create_connected_server_and_client_session

from langchain_mcp_adapters.tools import load_mcp_tools


# ---------- 构建 MCP Server (lowlevel, 协程式) ----------
def build_server() -> Server:
    server = Server("spike-02")

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name="ping",
                description="健康探活",
                inputSchema={"type": "object", "properties": {}},
            ),
            mcp_types.Tool(
                name="execute_discovery",
                description="探测目标主机某服务存活状态",
                inputSchema={
                    "type": "object",
                    "required": ["target_host", "service_name", "form"],
                    "properties": {
                        "target_host": {"type": "string"},
                        "service_name": {"type": "string"},
                        "form": {"type": "string", "enum": ["systemd", "docker", "compose"]},
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[mcp_types.TextContent]:
        # 设计 §5 统一返回: {ok: bool, data?: ..., error?: {code, message}}
        if name == "ping":
            import json
            return [mcp_types.TextContent(type="text", text=json.dumps({"ok": True, "data": {"status": "pong"}}))]
        if name == "execute_discovery":
            import json
            return [mcp_types.TextContent(
                type="text",
                text=json.dumps({
                    "ok": True,
                    "data": {
                        "target_host": arguments.get("target_host"),
                        "service_name": arguments.get("service_name"),
                        "status": "inactive",
                        "detail": "canned for spike",
                    },
                }),
            )]
        # 白名单外动词 -> 拒绝 (对齐设计 §4.3: 白名单外直接拒绝)
        import json
        return [mcp_types.TextContent(
            type="text",
            text=json.dumps({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": f"unknown tool {name}"}}),
        )]

    return server


# ---------- in-process 加载 + 调用 (须在同一 session 上下文内) ----------
def _extract_text(result) -> str:
    """langchain-mcp-adapters 0.3.0 ainvoke 返回 content block 列表, 取首个 text。"""
    if isinstance(result, list) and result:
        block = result[0]
        if isinstance(block, dict):
            return block.get("text", "")
        return getattr(block, "text", str(block))
    if isinstance(result, str):
        return result
    return str(result)


async def load_and_test_inprocess():
    server = build_server()
    import json

    # create_connected_server_and_client_session: 启动 server 协程 + 返回 ClientSession
    # 不创建子进程, 全在当前 asyncio 事件循环内
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        tools = await load_mcp_tools(session)
        print(f"\n加载到 {len(tools)} 个工具:")
        for t in tools:
            print(f"  - {t.name}: {t.description}")

        if len(tools) != 2:
            print(f"FAIL: 期望 2 个工具, 实际 {len(tools)}")
            return False

        # ---------- 调用工具, 断言契约 ----------
        print("\n--- 调用 ping ---")
        ping_tool = next(t for t in tools if t.name == "ping")
        ping_result = await ping_tool.ainvoke({})
        print(f"  原始返回: {ping_result}")
        parsed = json.loads(_extract_text(ping_result))
        assert parsed["ok"] is True and parsed["data"]["status"] == "pong", f"ping 契约不符: {parsed}"
        print("  ping 契约 {ok, data} ✓")

        print("\n--- 调用 execute_discovery ---")
        disc_tool = next(t for t in tools if t.name == "execute_discovery")
        disc_result = await disc_tool.ainvoke({
            "target_host": "node-a",
            "service_name": "nginx",
            "form": "systemd",
        })
        print(f"  原始返回: {disc_result}")
        parsed = json.loads(_extract_text(disc_result))
        assert parsed["ok"] is True
        assert parsed["data"]["target_host"] == "node-a"
        assert parsed["data"]["service_name"] == "nginx"
        assert parsed["data"]["status"] == "inactive"
        print("  execute_discovery 契约 {ok, data:{target_host,service_name,status,detail}} ✓")

    return True


async def main():
    print("=" * 60)
    print("Spike 02: MCP in-process 加载")
    print("=" * 60)

    try:
        ok = await load_and_test_inprocess()
    except Exception as e:
        print(f"FAIL (in-process 加载/调用失败): {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        print("\n>>> 02 结论: PARTIAL/FAIL (in-process 不可用, 退回 stdio)")
        sys.exit(1)

    if not ok:
        sys.exit(1)

    print("\n" + "-" * 40)
    print("in-process 加载成功, 无子进程 ✓")
    print("工具契约 {ok, data} / {ok, error} ✓")
    print("返回结构化 JSON, 非 shell 字符串 ✓")
    print("\n>>> 02 结论: PASS")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
