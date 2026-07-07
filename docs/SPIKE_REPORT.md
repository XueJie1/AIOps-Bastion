# Spike Report — 4 项技术假设验证结果

> 配合 [`DETAILED_DESIGN_v1.2.md`](./DETAILED_DESIGN_v1.2.md) 与 [`TRADEOFFS_v1.2.md`](./TRADEOFFS_v1.2.md) 阅读。
> 本报告决定: 能否进入实施, 以及实施前需修订哪些设计章节。

---

## 总结

| # | 验证项 | 结论 | 对设计的影响 |
|:-:|:------|:----:|:------------|
| 01 | LangGraph interrupt + Checkpointer 崩溃恢复 | ✅ PASS | §6.8/§11 恢复模型成立,无需改 |
| 02 | MCP in-process 加载 | ✅ PASS | §10.4 成立;**2 项回写**(见下) |
| 03 | DeepSeek tool-calling | ✅ PASS | §3.5/决策#16 成立;`deepseek-v4-pro` 确认为真实模型 id,默认思考模式;`usage_metadata` 真实可用 |
| 04 | 端到端 HITL 闭环 | 🟡 PARTIAL FAIL | 机制通、Agent 决策对,但 **approval_id 透传断链**——§3.3 需补充实现机制 |

**总判定: 3 项核心技术假设全部成立,可进入实施。** 04 的 PARTIAL FAIL 不否定架构,而是暴露了一个实施细节(approval_id 如何从 resume 注入到工具参数),spike 已定位根因,实施时按 §3.3 的"职责拆分"补一个 `InjectedToolArg` 或自定义 ToolNode 即可。

---

## 01 — LangGraph interrupt + Checkpointer 崩溃恢复 ✅ PASS

**验证内容:** interrupt() 挂起态经 SqliteSaver 持久化;进程崩溃重启后从检查点恢复,只读工具不重放,L3 不重复执行。

**结果:**
- `readonly_calls=1`(只读未重放)✓
- `l3_exec_calls=1`(L3 执行一次,不重复)✓
- 状态 `resumed=True` 正确恢复 ✓

**结论:** 设计 §6.8/§11.1 的恢复模型成立。"检查点粒度为工具调用边界,崩溃至多重放一步只读探测"得到验证。

**对实施的意义:** 01 用的是**同步 `SqliteSaver` + 手写同步图**;04 暴露 react agent(异步路径)必须用 **`AsyncSqliteSaver`**。这是 1.x 的实际约束。
→ **回写设计 §11.1**:Checkpointer 后端应明确为 `AsyncSqliteSaver`(异步路径),同步 `SqliteSaver` 仅适用于纯同步测试图。Agent 代码全程用 `ainvoke`/`aget_state`,不能混用同步 `get_state`(会触发 `InvalidStateError`)。

---

## 02 — MCP in-process 加载 ✅ PASS

**验证内容:** langchain-mcp-adapters 能 in-process 加载 MCP Server 协程,绕过 stdio 子进程边界,供 CI 测试。

**结果:**
- `create_connected_server_and_client_session(server)` + `load_mcp_tools(session)` 成功加载 2 个工具,无子进程 ✓
- 工具返回 `{ok, data}` 结构化契约 ✓

**对实施的意义(2 项回写设计):**

1. **工具生命周期绑定 session。** `load_mcp_tools` 返回的 `BaseTool` 离开 `async with create_connected_server_and_client_session(...)` 上下文后 session 关闭,工具失效。
   → **回写设计 §3.3/§10.4:** MCP ClientSession 必须与 Agent 同生命周期(整个调查期间常驻),不能每次工具调用新建/销毁 session。实施时 MCP Client 作为 Agent 的长生命周期依赖注入。

2. **返回结构是 content block 列表,非字符串。** `ainvoke` 返回 `[{type:"text", text:"..."}]`,设计 §5 写的"统一返回 `{ok, data}` JSON"是**内层** JSON,外层还包一层 MCP content block。
   → **回写设计 §5:** 明确分层——MCP 工具返回 = `[TextContent(text=<{ok,data} JSON>)]`;Agent/MCP Server 侧须做 `_extract_text(result)` 提取内层 JSON。这层分层 v1.2 没写清,实施时要补。

---

## 03 — DeepSeek tool-calling ✅ PASS

**验证内容:** DeepSeek 经 OpenAI 兼容端点支持 tool calling;`ChatOpenAI + 自定义 base_url` 可用。

**结果:**
- 真实可用模型 id: **`deepseek-v4-pro`**(设计首选,确认无误)
- `deepseek-v4-pro` 默认启用**思考模式**(`usage.output_token_details.reasoning=31`),即官方推荐的复杂 Agent 场景配置
- tool_call 结构化返回,工具名 + 参数正确 ✓
- `usage_metadata` 返回真实 `input_tokens`/`output_tokens`/`total_tokens`/`output_token_details.reasoning` ✓

**模型别名澄清(曾误判,已纠正):**
- `deepseek-v4-pro` 与 `deepseek-v4-flash` 是当前官方模型 id(V4 预览版上线)
- `deepseek-chat` / `deepseek-reasoner` 是**旧别名**,将于 **2026-07-24 23:59 弃用**(当前指向 v4-flash 的非思考/思考模式)
- 设计文档 §3.5 写 "DeepSeek-V4-Pro" **正确**,spike 首轮误用 `deepseek-chat` 验证(虽通过但验证的是 v4-flash 非思考模式,非设计首选),已纠正为优先测 `deepseek-v4-pro` 并通过

**对实施的意义:**
→ **回写设计 §6.6 闸 3:** "用真实 `usage` 更新 token_usage" 可行,`usage_metadata` 字段齐全(含 reasoning tokens),无需估算修正。**注意:** 思考模式的 reasoning tokens 也计入 output_tokens,Token 预算四道闸(§6.6)的"事后累计"须把 reasoning tokens 一并计入,避免思考模式长推理导致预算偷偷超支。
→ **回写设计 §3.5:** base_url 示例 `https://api.deepseek.com/v1` 实测可用;`deepseek-v4-pro` 作为默认模型 id 确认,思考模式默认启用(对 Agent 场景有利)。Provider 抽象层(决策#16)与固定单一 vendor(决策#19)成立。

---

## 04 — 端到端 HITL 闭环 🟡 PARTIAL FAIL

**验证内容:** 真 LLM 驱动 Agent 选 L3 工具 → interrupt 挂起 → resume → MCP 工具执行一次。

**结果(强 prompt 下):**
- PHASE 1: Agent 正确选 `execute_discovery` (L1 探测),interrupt 在 `tools` 前 ✓
- PHASE 2: resume → 执行 discovery → Agent 再决策 → 正确选 `restart_service` (L3) ✓
- PHASE 3: resume 执行 `restart_service` → **`l3_exec_calls=0`** ✗

**根因定位:** Agent 调用 `restart_service` 时**没传 `approval_id` 参数**(Agent 不知道要传)。MCP Server 的 `call_tool` 里 `if not approval_id` 直接返回 `HITL_REJECTED`,没走到计数 +1。**resume 注入的 `approval_id` 没有自动透传到工具参数**——设计 §3.3 写的"resume 时由 Agent 注入 approval_id"在 `create_react_agent` 默认机制下**不会自动发生**。

**这不是架构失败,是实施机制缺失。** 设计 §3.3 的"职责拆分"图里画的是:Agent resume 后"将 approval_id 注入 execute_remediation 调用"。但 spike 证实 `create_react_agent` 默认不会做这个注入——需要**显式机制**。

**对实施的意义(关键):**
→ **回写设计 §3.3 / §6.7:** approval_id 透传须用以下之一:
  - **方案 A(推荐):** 用 langchain 的 `InjectedToolArg` 机制,把 `approval_id` 声明为注入参数,由 ToolNode 从图状态读取并注入。Agent 不需要在 tool_call 里传它。
  - **方案 B:** 自定义 ToolNode(resume 时拦截 L3 调用,手动注入 `approval_id` 到 args 再执行)。
  - **方案 C:** MCP Server 侧 PermissionGate 不依赖工具参数,改为从图状态/上下文读取 approval_id(更贴近设计 §3.3 的"防御性校验"定位)。

→ **回写设计 §6.7 HITL 流程图:** 第 4 步"PermissionGate 校验 approval_id"的 approval_id 来源须明确——不是 Agent 传入工具参数,而是**从 LangGraph 状态读取**(resume 时写入 state,ToolNode/PermissionGate 读取)。

**spike 编排方式的教训:** `interrupt_before=["tools"]`(所有工具前中断)+ 靠 Agent 自主走到 L3,**不足以验证 HITL 闭环**——弱 prompt 下 Agent 探测完就 END,根本不走 L3。真实实现必须:
1. **仅 L3 中断**(非所有工具),L1/L2 自主执行;
2. **approval_id 经 state 透传**(非工具参数)。
spike 用 `interrupt_before=["tools"]` 是为了简化验证中断机制本身,这两点是实施时要补的真实逻辑。

---

## 附:环境与依赖发现(回写设计 §2.3 技术栈)

**Python 版本:** 系统仅 Python 3.14.6(无 3.11/3.12/3.13)。3.14 能跑全部依赖,满足设计 "3.11+" 要求。

**依赖版本(resolver 自动升级到大版本,设计钉的旧版 pin 因冲突装不上):**

| 包 | 设计文档 pin | 实际安装 | 影响 |
|:---|:---|:---|:---|
| langgraph | 0.2.62 | **1.2.7** | API 有变化,`create_react_agent` 已 deprecated(移至 `langchain.agents.create_agent`) |
| langchain-core | 0.3.28 | **1.4.8** | — |
| langchain-openai | 0.2.14 | **1.3.3** | — |
| langchain-mcp-adapters | 0.1.6 | **0.3.0** | in-process API 变化(`load_mcp_tools(session)` 签名) |
| langgraph-checkpoint-sqlite | 2.0.6 | **3.1.0** | `AsyncSqliteSaver` 必须用于异步路径 |
| mcp | 1.2.0 | **1.28.1** | `create_connected_server_and_client_session` 在 `mcp.shared.memory` |
| openai | — | 2.44.0 | — |

→ **回写设计 §2.3 技术栈表:** 版本 pin 全部更新为上述实际版本;Python 要求改为 "3.11+(实测 3.14 可用)"。实施时 `requirements.txt` 用 `>=` 而非 `==`,让 resolver 解决兼容性。

---

## 实施建议

### 可进入实施 ✅

4 项核心技术假设中 3 项 PASS、1 项 PARTIAL FAIL(已定位根因,有明确修复方案)。架构成立,无需重新设计。

### 实施前必须修订的设计章节

1. **§2.3 技术栈表:** 更新依赖版本为实际安装的大版本(见附表);Python 改 "3.11+(3.14 实测可用)"。
2. **§3.3 MCP Server:** 补 approval_id 透传机制(推荐 `InjectedToolArg`);补"MCP ClientSession 与 Agent 同生命周期"约束。
3. **§3.5 / §8.3:** 模型 id `deepseek-v4-pro` 确认为真实可用,默认启用思考模式(reasoning tokens 计入 output);`base_url=https://api.deepseek.com/v1` 实测可用。
4. **§5 错误码/返回契约:** 补 MCP content block 分层说明(内层 `{ok,data}` JSON,外层 `[TextContent]`)。
5. **§6.6 闸 3:** 确认 `usage_metadata` 真实可用(含 reasoning tokens),四道闸"事后累计"须把 reasoning tokens 一并计入,避免思考模式长推理导致预算偷偷超支。
6. **§6.7 HITL 流程图:** approval_id 来源改为"从 LangGraph 状态读取",非"Agent 注入工具参数"。
7. **§11.1 Checkpointer:** 明确异步路径用 `AsyncSqliteSaver`,同步 `SqliteSaver` 仅测试用。

### 优先开发路径(据 §10.2 调整)

1. **安全地基:** Vault(§3.7)+ 执行引擎白名单/模板(§3.4)+ 注入测试(§4.3)。**不变**。
2. **MCP 工具骨架 + approval_id 透传机制(新增):** 先落地 `InjectedToolArg` 或自定义 ToolNode,确保 L3 的 approval_id 经 state 透传。**这是 spike 04 暴露的关键实施前置**。
3. **Agent 最小闭环:** Sync Chat + 单工具 + AsyncSqliteSaver + `aget_state`/`ainvoke` 全异步路径。
4. **崩溃恢复:** AsyncSqliteSaver + Recovery Sweep 与最小闭环同步落地。
5. **事件模式:** Chat 稳定后接 Webhook + 去重事务 + Token 四道闸。
6. **RAG 最后。**
