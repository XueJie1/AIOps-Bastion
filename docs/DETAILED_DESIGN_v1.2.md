# AIOps-Bastion 详细设计文档 (Detailed Design Document) — 评审定稿版 v1.2

---

## 1. 文档信息

| 项 | 内容 |
| :--- | :--- |
| 项目名称 | AIOps-Bastion：基于 MCP 与 RAG 的多节点智能运维堡垒机 |
| 文档版本 | 1.3（Detailed Design — spike 验证后修订版） |
| 设计日期 | 2026-07-04 |
| 修订日期 | 2026-07-07（据 spike 验证结果修订） |
| 评审日期 | 2026-07-04 |
| 设计依据 | AIOps-Bastion 需求分析与架构设计说明书 (PRD) v1.0 |
| 作者 | SRE + 全栈架构师 |
| 状态 | Approved（6 项决策已定稿 + 7 项 spike 修订已落地） |

**修订记录**

| 版本 | 日期 | 修订人 | 说明 |
| :--- | :--- | :--- | :--- |
| 1.0 | 2026-07-04 | 架构组 | 首版详细设计，对齐 PRD 全部 15 项决策 |
| 1.1 | 2026-07-04 | 评审组 | 深度技术评审改进：修正 P0 安全/正确性缺陷 3 项、P1 逻辑/架构缺陷 2 项、P2 工程实践 3 项；新增错误处理规约、灾难恢复、凭证热更新等章节；标记 6 项待确认事项 |
| 1.2 | 2026-07-04 | 评审组 | 据用户确认定稿 6 项决策：HITL=防误触确认、嵌入=MiniLM+BM25 混合、机器=2C4G NAS、Token=维持 512k（采用 GLM-5.2/DeepSeek-V4-Pro 等国产低价模型，成本非约束）、L3=维持 3 枚举、OS 加固=基础加固；新增 Provider 层对 OpenAI 兼容厂商（GLM/DeepSeek）的支持说明 |
| 1.3 | 2026-07-07 | 架构组 | 据 spike 验证落地 7 项修订（详见 [`SPIKE_REPORT.md`](./SPIKE_REPORT.md)）：①技术栈版本更新为实测可用大版本（langgraph 1.2.7 / langchain-core 1.4.8 / mcp 1.28.1 等，Python 3.14 实测）；②§3.3 补 approval_id 透传机制（InjectedToolArg）+ MCP ClientSession 生命周期约束 + content block 分层；③§3.5 确认 `deepseek-v4-pro` 真实可用且默认思考模式，旧别名 chat/reasoner 将弃用；④§5 补 MCP 返回 content block 分层说明；⑤§6.6 闸 3 补 reasoning tokens 计入；⑥§6.7 HITL 流程图 approval_id 改为从图 state 读取；⑦§11.1 明确异步路径用 AsyncSqliteSaver |

> 🔧 [版本] 修改说明：本文为 v1.0 的评审定稿版（v1.2）。v1.1 为评审改进草稿（含 6 项 ❓ 待确认），v1.2 据用户回复将 6 项定稿并移除 ❓ 标注（改为 `✅ [已定稿]`）；工程修正点以 `> 🔧 [Pn-m]` 标注，新增章节以 `> 🆕 新增章节` 标注。v1.0 原文保留于 `DETAILED_DESIGN.md` 未改动，便于逐节对照。

**决策追溯约定：** 本文每个关键设计点以 `[PRD §X.Y]` 或 `[决策#N]`（N 对应 PRD §8 决策记录表序号）标注来源。评审新增的设计决策以 `[评审补充#Rn]` 标注，与 PRD 决策体系互补但不变更已确认决策（如决策#7 排除 reboot、决策#18 默认 512k、决策#19 固定 vendor 等均维持不变）。

---

## 2. 系统架构总览

### 2.1 高层次架构图

```mermaid
flowchart TB
    subgraph CloudNet["云端（出网边界外）"]
        UK["Uptime Kuma<br/>(用户自托管监控)"]
        LLM["LLM 厂商 API<br/>GLM-5.2 / DeepSeek-V4-Pro / Claude / GPT"]
        FB["Firebase<br/>实时DB + Auth"]
        CF["Cloudflare 边缘网络"]
        TG["Telegram Bot API"]
    end

    subgraph HomeNet["家庭内网（Zero Trust：拒绝一切主动入站）"]
        BASTION["Bastion Server（单进程）<br/>FastAPI + Agent(LangGraph) + MCP Server + Vault + Chroma"]
        CFDB["cloudflared<br/>出站长连接容器"]
        UI["Web Dashboard<br/>(浏览器)"]
    end

    subgraph Fleet["被纳管业务节点 1~3 台（带外隔离）"]
        N1["Node A<br/>systemd / Docker / Compose"]
        N2["Node B"]
        N3["Node C"]
    end

    UK -->|"1 Webhook DOWN/UP"| CF
    CF -->|"2 隧道回源"| CFDB
    CFDB -->|"3 Docker 内部网络"| BASTION
    BASTION -->|"4 SSH (asyncssh, 受限命令)"| N1
    BASTION -->|"4 SSH"| N2
    BASTION -->|"4 SSH"| N3
    BASTION -.->|"5 排查日志/拓扑（出网）"| LLM
    BASTION -->|"6 工单/日志/拓扑"| FB
    BASTION -->|"7 调查报告"| TG
    FB <-.->|"8 实时订阅 / 鉴权"| UI
    BASTION -->|"8' SSE 思维链流"| UI
    UI -->|"9 REST 动作(聊天/审批)"| BASTION
```

> 🔧 [P2-7] 修改说明：将原图“3 本地回环”更正为“3 Docker 内部网络”。cloudflared 与 bastion-app 分属不同容器，127.0.0.1 无法跨容器寻址（详见 §4.4 修正）。“本地回环”的安全语义改为“Docker 专用网络且不对外发布端口”。

### 2.2 部署拓扑（带外物理隔离）

```mermaid
flowchart LR
    subgraph DMZ["家庭内网信任域"]
        direction TB
        BASTION["Bastion<br/>(独立机器/NAS, Docker Compose)"]
        CFDB["cloudflared 容器"]
        BASTION --- CFDB
    end
    subgraph Prod["生产业务信任域"]
        N1["业务节点 1"]
        N2["业务节点 2"]
        N3["业务节点 3"]
    end
    BASTION -->|"SSH (带外管理网)"| N1
    BASTION -->|"SSH"| N2
    BASTION -->|"SSH"| N3
    CFDB -->|"出站长连接"| CF["Cloudflare"]
    CF -->|"Webhook 回源"| CFDB
```

**隔离要点 `[PRD §2]`：**
- Bastion 部署于**独立**家庭内网机器/NAS，**不是**被纳管的 1~3 台业务节点之一（避免自监控自身故障盲区）。
- 业务节点仅暴露 SSH（建议非标准端口 + 密钥-only + fail2ban），且仅允许来自 Bastion IP 的入站。
- Bastion 本机防火墙 `DROP` 一切主动入站；唯一入站路径是 `cloudflared` 容器经 **Docker 专用内部网络**回源到 FastAPI 的 Webhook 端点，该端口不发布到宿主机/局域网。

### 2.3 技术栈最终确认表

| 层 | 选型 | 版本/说明 | PRD 依据 |
| :--- | :--- | :--- | :--- |
| 后端语言 | Python | 3.11+（类型提示、asyncio 原生；spike 实测 3.14 可用） | §2 |
| Web 框架 | FastAPI + Uvicorn | 单 worker，承载 REST + SSE + Webhook | §3.1 |
| Agent 编排 | LangGraph | 原生支持 HITL interrupt、状态机、步骤级 token 计量；**须配持久化 Checkpointer**（见 §3.5）。spike 实测：`langgraph>=1.2.7`，`create_react_agent` 已 deprecated 迁移至 `langchain.agents.create_agent`（见 §3.5） | §3.3 |
| LLM Provider | LangChain + `langchain-openai` / `langchain-anthropic` | Provider 抽象层：Claude/GPT 原生；**`deepseek-v4-pro` 等 OpenAI 兼容厂商经 base_url 接入**（决策#16）。spike 实测：`langchain-openai>=1.3.3` | 决策#2 |
| MCP | `mcp` 官方 Python SDK | stdio 传输，子进程方式被 Agent 加载。spike 实测：`mcp>=1.28.1`，in-process 加载经 `mcp.shared.memory.create_connected_server_and_client_session`（见 §3.3/§10.4） | §2 |
| MCP↔LangChain 桥 | `langchain-mcp-adapters` | 将 MCP 工具包装为 LangChain Tool。spike 实测：`langchain-mcp-adapters>=0.3.0`，`load_mcp_tools(session)` 路径（见 §3.3） | §3.2 |
| SSH | asyncssh（≥ 2.14） | 异步并发，受限命令执行；list 形参依赖其 `shlex.quote` 行为（见 §3.4） | §2 |
| 加密 | cryptography（Fernet + PBKDF2HMAC） | 凭证加密落盘 | §3.1 FR1.2 |
| 向量库 | Chroma（嵌入式，持久化到卷） | 本地零依赖 | 决策#10 |
| 嵌入模型 | sentence-transformers `all-MiniLM-L6-v2` + BM25 混合检索（已定稿） | 本地推理，零 API 成本，文本不出网 | §3.5 |
| 实时 DB/Auth | Firebase（Firestore + Auth） | 工单/日志/拓扑实时同步 | 决策#5 |
| 前端 | React 18 + TypeScript + Vite | 已确认 `[决策#20]` | §3.1 |
| 前端 UI | TailwindCSS + shadcn/ui + TanStack Query + Zustand | 现代化仪表盘 | §3.1 |
| 容器编排 | Docker Compose | bastion-app + cloudflared 两服务，共享专用网络 | 决策#11 |
| CI | GitHub Actions | Ruff + Flake8 + mypy + pytest | §4.3 |
| Checkpointer 后端 | `langgraph-checkpoint-sqlite` | spike 实测：`>=3.1.0`；**异步路径须用 `AsyncSqliteSaver`**（`langgraph.checkpoint.sqlite.aio`），同步 `SqliteSaver` 仅适用于纯同步测试图（见 §11.1） | §3.5/§11.1 |

> 🔧 [P2-6] 修改说明：嵌入模型已定稿为 MiniLM + BM25 混合检索（用户确认，机器为 2C4G NAS，不升级大模型）。MiniLM 对中英混杂运维日志语义较弱，靠 BM25 关键词检索补足，详见 §3.6。
> 🔧 [评审补充#R1] 修改说明：明确 LangGraph 须配置持久化 Checkpointer（v1.0 仅写“状态持久化”未指定后端），否则进程崩溃后挂起的 HITL 调查无法恢复（见 §11）。

### 2.4 数据流与信任边界图

```mermaid
flowchart TB
    subgraph VaultZone["凭证隔离区（绝不出网）"]
        V["Vault: Master Password → PBKDF2 → Fernet Key<br/>SSH 私钥 / CF Token / TG Token / LLM API Key"]
    end
    subgraph EgressZone["出网白名单区（受控）"]
        E1["→ LLM API（GLM/DeepSeek/Claude/GPT）：仅日志/拓扑摘要"]
        E2["→ Firebase：工单状态/日志/拓扑元数据"]
        E3["→ Telegram API：调查报告 Markdown"]
        E4["→ Cloudflare API：隧道配置（仅初始化）"]
    end
    subgraph IngressZone["入站通道（唯一）"]
        I1["← Uptime Kuma Webhook（经 CF Tunnel）"]
    end
    V -.->|"解密后内存使用"| SSH["asyncssh SSH 执行器"]
    V -.->|"解密后内存使用"| LLMCall["LLM 调用层"]
    SSH --> Nodes["业务节点"]
    LLMCall --> E1
    FBWrite["Firebase 写入器"] --> E2
    TGPush["报告推送"] --> E3
    I1 --> WH["Webhook 处理器"]
```

> 🔧 [P1-5] 修改说明：E1 标注由“仅排查日志/拓扑片段”强化为“仅 Agent 本地提取的**结构化摘要**，不含原始日志全文”（见 §4.5）。原始日志在 Bastion 本地消费，不出网。

**信任边界三原则 `[PRD §4.1]`：**
1. **凭证隔离：** Master Password 及其派生密钥、所有原始凭证**绝不离开 Bastion**，仅运行期内存解密使用。
2. **出网最小化：** Bastion 仅允许出站到上表 4 个白名单目的；LLM 仅接收经截断与**本地摘要化**的排查片段，不接收任何凭证，亦不接收原始日志全文。
3. **入站唯一性：** 唯一入站是经 CF Tunnel 的 Uptime Kuma Webhook；Bastion 无任何对公网/局域网监听的端口。


---

## 3. 模块与组件详细设计

Bastion Server 为**单进程**架构 `[决策#3]`，内部以模块化组件组合，共享同一 asyncio 事件循环：

```mermaid
flowchart LR
    API[FastAPI 层<br/>REST + SSE + Webhook] --> ORCH[Agent 编排器<br/>LangGraph]
    ORCH --> MCPClient[MCP Client<br/>langchain-mcp-adapters]
    MCPClient -->|stdio| MCPSrv[MCP Server<br/>mcp SDK 子进程]
    MCPSrv --> EXEC[执行引擎<br/>asyncssh + 白名单]
    MCPSrv --> RAG[RAG 模块<br/>Chroma]
    MCPSrv --> FBW[Firebase 写入器]
    ORCH --> LLM[LLM Provider 层<br/>GLM/DeepSeek/Claude/GPT]
    API --> VLT[Vault 凭证管理]
    EXEC -.->|取 SSH 私钥| VLT
    LLM -.->|取 API Key| VLT
```

> 🔧 [P0-2] 修改说明：Vault 与执行引擎/LLM 层之间的凭证取用必须走异步路径（`async get()`），避免 PBKDF2 或 Fernet 解密阻塞事件循环（见 §3.7）。

### 3.1 Web Dashboard（前端）

**职责与边界：** 浏览器侧控制台，负责初始化引导、健康看板、Chat 排查、HITL 审批、SOP 审核队列。**不直接执行任何 SSH/修复操作**，所有变更类动作经 REST 调用 Bastion 后端；实时状态经 Firebase 订阅、思维链经 SSE 订阅。

**技术选型（已确认 `[决策#20]`）：** React 18 + TypeScript + Vite + TailwindCSS + shadcn/ui。

**状态管理分层：**
- **服务端缓存：** TanStack Query 管理 REST 请求（节点列表、审批队列）。
- **实时数据：** Firebase JS SDK `onSnapshot` 订阅 `investigations`、`nodes`、`records`，驱动看板/进度条无缝刷新。
- **本地 UI 状态：** Zustand 管理侧边栏、当前选中工单、审批弹窗开关等。

**关键页面与组件：**

| 页面 | 核心组件 | 数据来源 |
| :--- | :--- | :--- |
| Onboarding | `MasterPasswordSetup` / `CredentialImportForm` | REST `/api/v1/init` |
| Dashboard | `HealthBoard` / `WebhookStream` / `InvestigationProgress` | Firebase onSnapshot |
| Chat | `ChatSidebar` / `CoTStream` / `ToolCallCard` | REST + SSE `/chat/{id}/stream` |
| HITL 审批 | `ApprovalModal`（显示 action_type/目标/影响/渲染命令） | Firebase `hitl_requests` 订阅 |
| SOP 审核 | `RunbookReviewQueue` / `RunbookEditor` | Firebase `runbooks` |

**实时更新机制：**
- 工单状态/进度：`onSnapshot(doc(investigations, id))`。
- Journal Records：`onSnapshot(collection(investigations, id, records))`，按 `ts` 排序流式追加。
- 思维链（CoT）：后端按 Agent 步骤推送 `step` / `tool_call` / `tool_result` / `token_delta` 事件。

> 🔧 [评审补充#R2] 修改说明：思维链传输方式由 `EventSource` 改为 **`fetch` + `ReadableStream`** 流式读取。`EventSource` API 不支持自定义请求头，无法携带 Firebase Auth ID Token 做 SSE 鉴权；改用 `fetch` 流式可在 Header 中携带 `Authorization: Bearer <idToken>`，且支持按需中断。后端 FastAPI 用 `StreamingResponse` 配合 `text/event-stream` 输出，语义与原 `step/tool_call/tool_result/token_delta` 事件保持一致。

**与其他模块交互：** 经 REST 调后端发起 Chat / 审批 / 节点纳管；经 Firebase 直读工单与日志；经 fetch 流接收思维链。

### 3.2 Firebase 数据模型与 Schema 设计

**职责与边界：** 作为实时数据库与鉴权中心，存储**非凭证类**运维数据：节点元数据、调查工单、Journal Records、Runbook 元数据、HITL 请求。**凭证不入 Firebase**（凭证仅在本地 Vault）。详细字段见 §8。

**Collection 概览：**

```mermaid
flowchart TB
    nodes["nodes (主机元数据)"]
    inv["investigations (调查工单)"]
    rec["investigations/{id}/records (Journal 子集合)"]
    rb["runbooks (知识库元数据)"]
    hitl["hitl_requests (HITL 授权请求)"]
    inv --- rec
    inv -.->|"触发"| hitl
    rb -.->|"引用内容向量"| chroma["Chroma 向量库(本地)"]
```

**安全规则（单用户，预留 role） `[决策#12]`：**
```
rules_version = '2';
service cloud.firestore {
  match /databases/{db}/documents {
    function isAuthed() { return request.auth != null; }
    match /{document=**} {
      allow read, write: if isAuthed();
    }
  }
}
```
> 单用户阶段：任何已登录用户可读写全部数据。预留 `role` 字段后，可细化为 `request.auth.token.role == 'admin'` 等规则。Firebase Auth 仅启用邮箱/密码单账号。

> 🔧 [P1-4] 修改说明：单用户模型下，触发调查的账号与审批 L3 的账号为同一身份，HITL 无法实现“权限隔离”，仅可作为“防误触确认”。此为 `[决策#12]` 单用户前提下的既定权衡，详见 §6.7（已定稿为防误触确认）。

**索引建议：**
- `investigations`：`dedup_key` 升序 + `status` 升序（去重查询）；`status` + `created_at` 降序（工单列表）。
- `records`：`execution_id` + `ts` 升序（日志时序）。
- `runbooks`：`status` 升序（审核队列）。
- `hitl_requests`：`status`（PENDING）+ `created_at`。

> 🔧 [评审补充#R3] 修改说明：新增 `hitl_requests` 复合索引 `(execution_id, status)` 与 TTL 清理策略（见 §11），避免 PENDING 记录因崩溃无限堆积。

**与其他模块交互：** Agent 经 `FirebaseWriter` 写工单/日志；前端经 SDK 订阅；HITL 模块读写 `hitl_requests`；**去重器经 Firestore 事务**原子读写（见 §6.4）。

### 3.3 MCP Server

**职责与边界：** 向 Agent 暴露严格定义的 JSON-RPC 工具接口，是**唯一**的运维操作出口。所有 SSH/RAG/Firebase 副作用均经 MCP 工具发生，便于权限分级与审计。以 `mcp` SDK 实现，**stdio 传输**，作为子进程被 Agent（LangGraph）通过 `langchain-mcp-adapters` 加载 `[PRD §2]`。

**工具注册与权限分级：**

```python
from mcp.server import Server
from mcp.types import Tool

server = Server("aiops-bastion")

# 权限等级在工具元数据声明，由 PermissionGate 统一拦截
LEVELS = {"L0": "基建", "L1": "探测", "L2": "日志/归档", "L3": "高危"}

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="setup_webhook_tunnel", description="...",            # L0
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="execute_discovery", description="...",               # L1
             inputSchema=DISCOVERY_SCHEMA),
        Tool(name="fetch_service_logs", description="...",              # L2
             inputSchema=LOGS_SCHEMA),
        Tool(name="submit_journal", description="...",                  # L2
             inputSchema=JOURNAL_SCHEMA),
        Tool(name="query_runbook", description="...",                   # L2
             inputSchema=RUNBOOK_SCHEMA),
        Tool(name="execute_remediation", description="...",             # L3
             inputSchema=REMEDIATION_SCHEMA),
    ]
```

**权限分级实现（PermissionGate）—— 职责拆分修正：**

```mermaid
flowchart TB
    Call["Agent 识别 L3 工具"] --> Interrupt["LangGraph interrupt<br/>写 hitl_requests(PENDING)"]
    Interrupt --> Wait["等待人工审批（状态持久化）"]
    Wait -->|"approve (带 approval_id)"| Dispatch["Agent 调 execute_remediation(approval_id)"]
    Wait -->|"reject / 超时"| Fail["返回 ABORTED"]
    Dispatch --> Gate{"PermissionGate 校验 approval_id"}
    Gate -->|"有效且未过期"| Audit["审计日志"] --> Exec["执行模板"]
    Gate -->|"无效/已用过/过期"| Reject["返回 HITL_REJECTED"]
    Exec --> FB["写 Firebase + 返回结果"]
```

> 🔧 [P1-4/架构修正] 修改说明：v1.0 将“挂起 + 写 hitl_requests + 等待审批”放在 MCP Server 侧 PermissionGate，这在 stdio 子进程模型下不可行——子进程阻塞等待人工会卡死 Agent 调用且无法跨进程崩溃恢复。修正为职责拆分：
> - **Agent（LangGraph）侧**：识别 L3 工具调用 → 调 `interrupt()` 挂起图并写 `hitl_requests(PENDING)`，状态由持久化 Checkpointer 保存，进程崩溃可恢复。
> - **MCP Server 侧 PermissionGate**：仅做**防御性校验**——执行 L3 前校验调用方传入的 `approval_id` 对应的 `hitl_requests` 记录存在、`status==APPROVED`、未过期、且未被复用（一次性消费，执行后置 `CONSUMED`）。校验失败返回 `HITL_REJECTED`。即使 Agent 被诱导绕过 interrupt，MCP Server 仍拒绝无授权的 L3 执行。
> - `execute_remediation` 的输入 Schema 新增可选 `approval_id` 字段（resume 时由 Agent 注入）。

**JSON-RPC 接口契约：** 统一遵循 MCP 协议 `tools/call`；输入/输出 JSON Schema 详见 §5。所有工具返回结构统一为 `{ok: bool, data?: ..., error?: {code, message}}`。

> 🔧 [P1-4/spike-04 修订] 修改说明：spike 04 暴露 `approval_id` 透传断链问题——`create_react_agent` 默认机制下 resume 注入的 `approval_id` **不会自动透传到 `execute_remediation` 工具参数**。v1.2 原文写"resume 时由 Agent 注入"过于笼统，此处明确实现机制（三选一，推荐方案 A）：
> - **方案 A（推荐）— `InjectedToolArg`：** 将 `approval_id` 声明为 `langchain_core.tools.InjectedToolArg`，由 LangGraph ToolNode 从图状态读取并注入，Agent 调用工具时无需在 `args` 里传它。`approval_id` 在 resume 时写入图 state（见 §6.7 修订）。
> - **方案 B — 自定义 ToolNode：** resume 后拦截 L3 调用，手动把 state 里的 `approval_id` 注入 tool args 再执行。
> - **方案 C — MCP Server 侧从上下文读取：** PermissionGate 不依赖工具参数，改为从调用上下文/图状态读取 `approval_id`（更贴近"防御性校验"定位，但 MCP Server 须能访问图状态）。
>
> **MCP ClientSession 生命周期约束 `[spike-02 修订]`：** `langchain-mcp-adapters` 的 `load_mcp_tools(session)` 返回的 `BaseTool` **生命周期绑定 session**——离开 `async with create_connected_server_and_client_session(...)` 上下文后 session 关闭，工具失效。故 **MCP ClientSession 须与 Agent 同生命周期**（整个调查期间常驻），不能每次工具调用新建/销毁 session。实施时 MCP Client 作为 Agent 的长生命周期依赖注入（生产实现用 stdio 子进程常驻；CI 测试用 in-process 常驻 session，见 §10.4）。
>
> **MCP 返回结构分层 `[spike-02 修订]`：** MCP 工具经 `langchain-mcp-adapters` 包装后，`ainvoke` 返回 **content block 列表** `[TextContent(text=...)]`，**非纯字符串**。本设计 §5 所述"统一返回 `{ok, data}` JSON"是该 content block 的 **text 字段内层 JSON**，外层还包一层 MCP content block。Agent 读取工具结果时须经 `_extract_text(result)` 提取首个 text block 的 `text` 字段，再 `json.loads` 解析内层契约。

**与其他模块交互：** 工具实现内部调用执行引擎（SSH）、RAG（Chroma）、FirebaseWriter、Vault。

### 3.4 执行引擎（asyncssh + 白名单 + 模板）

**职责与边界：** 唯一的 SSH 指令执行入口，负责连接池、并发控制、命令安全校验、超时熔断。**绝不**接受 Agent 传入的原始 shell 字符串。

**关键类：**

```python
class ExecutionEngine:
    def __init__(self, vault: Vault, max_concurrent: int = 4):
        self._vault = vault
        self._sem = asyncio.Semaphore(max_concurrent)   # 受限并发 [决策#3]
        self._pools: dict[str, asyncssh.SSHClient] = {}  # 按 host 复用连接

    async def run_readonly(self, host: str, cmd: ReadonlyCommand, wait_slot: float = 30.0) -> ExecResult:
        """L1/L2 只读：走白名单命令构建器，5s 超时；slot 等待上限 wait_slot。"""

    async def run_remediation(self, host: str, action: RemediationAction, wait_slot: float = 30.0) -> ExecResult:
        """L3 修复：走硬编码模板，参数经正则校验后代入，30s 超时（可配）。"""
```

> 🔧 [P2-8] 修改说明：`run_readonly` / `run_remediation` 新增 `wait_slot` 参数（默认 30s）。当 4 个 SSH slot 全被占时，新调用排队等待；超过 `wait_slot` 仍未获取 slot → 返回 `EXEC_TIMEOUT`（错误码复用，消息标注“queue wait timeout”），Agent 记 `investigation_gap`。配套增加队列深度指标与告警（见 §7.1、§7.2）。
> 🔧 [评审补充#R4] 修改说明：L3 修复超时由 5s 调整为 **30s 可配**。`docker restart` 含 stop grace period 常超过 5s，5s 会误熔断正常修复；只读探测维持 5s。此为对 `[PRD §4.2]` “5s 超时”的细化：只读 5s、修复 30s。

**命令白名单与参数校验（L1/L2） `[决策#8]`：**

只读命令以**结构化构建器**生成，每个构建器对应一个允许的动词，参数经类型化正则校验：

```python
# 允许的只读动词（封闭集合，不可扩展为变更类）
ALLOWED_READONLY = {
    "systemctl status", "systemctl is-active",
    "docker inspect", "docker ps", "docker compose ps",
    "journalctl -u", "docker logs",
}

IDENT_RE = r"^[A-Za-z0-9_.-]{1,128}$"   # unit / container / service 名

def build_status_cmd(form: str, name: str) -> list[str]:
    if not re.fullmatch(IDENT_RE, name):
        raise CommandValidationError(name)
    if form == "systemd":   return ["systemctl", "status", name]
    if form == "docker":    return ["docker", "inspect", name]
    if form == "compose":   return ["docker", "compose", "ps", name]
    raise CommandValidationError(form)
```

> 🔧 [P2-7] 修改说明：更正 asyncssh 行为描述。asyncssh `run()` 收到 `list[str]` 时，会对每个元素调用 `shlex.quote()` 转义后再用空格拼接成**单个字符串**交给远端 SSH exec，远端仍由登录 shell 解析——**并非 execve 数组传参**。因此：
> - **真正的核心防线是 `IDENT_RE` fullmatch**：在参数进入命令前即拒绝一切 shell 元字符（`;` `|` `&` `$` 反引号 `()` 换行 等）。
> - **`list[str]` 形态是第二道防线**：即便某参数含元字符，asyncssh 的 `shlex.quote` 会将其单引号包裹使其被 shell 视为字面量（注入测试须验证此行为，并锁定 asyncssh ≥ 2.14）。
> - **第三道防线（远端硬化，推荐）**：在业务节点 `authorized_keys` 中为 Bastion 公钥设置 `command="rbash"` 或 `restrict`，或使用 forced-command wrapper，使远端 shell 仅允许受限命令集，彻底消除 shell 解析面。此为可选加固，见 §4.2。
> v1.0 “进一步降低拼接风险”的表述因不精确而修正为上述三层防线描述。

**L3 硬编码模板映射 `[决策#7][决策#8]`：**

```python
TEMPLATES = {
    "restart_service":    lambda p: ["systemctl", "restart", p["unit"]],
    "restart_container":  lambda p: ["docker", "restart", p["name"]],
    "clear_cache":        lambda p: ["/usr/local/bin/clear_cache.sh", p["path"]],
    # 注意：无 reboot 模板 [决策#7]
}
CLEAR_CACHE_PATH_WHITELIST = {"/var/cache/nginx/", "/tmp/app-cache/"}

def render(action_type: str, params: dict) -> list[str]:
    if action_type == "clear_cache":
        if params["path"] not in CLEAR_CACHE_PATH_WHITELIST:
            raise PathNotAllowlistedError(params["path"])
    for v in params.values():
        if not re.fullmatch(IDENT_RE, v) and action_type != "clear_cache":
            raise CommandValidationError(v)
    return TEMPLATES[action_type](params)
```

**连接与超时状态机：**

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Queued: 调用(run_readonly/run_remediation)
    Queued --> Acquiring: 获取信号量 slot
    Queued --> SlotTimeout: 等待超 wait_slot
    Acquiring --> Connected: 复用/新建 SSH
    Acquiring --> Failed: 连接失败
    Connected --> Executing: 下发命令(list argv, shlex.quote 转义)
    Executing --> Done: 超时窗内返回
    Executing --> TimedOut: 超过 5s(只读)/30s(修复) 熔断
    Done --> Idle
    TimedOut --> Idle: 记录超时 + 释放连接
    SlotTimeout --> Idle: 返回 EXEC_TIMEOUT(queue)
    Failed --> Idle: 返回错误
```

> 🔧 [P2-8] 修改说明：状态机新增 `Queued` / `SlotTimeout` 状态，显式建模 slot 排队与超时，避免静默排队。

**与其他模块交互：** 从 Vault 取 SSH 私钥（`async get()`，内存解密）；被 MCP Server 的 `execute_discovery` / `fetch_service_logs` / `execute_remediation` 调用；结果回传 MCP Server。


### 3.5 Agent 核心（LangGraph + LangChain Provider）

**职责与边界：** 推理与编排中枢。接收自然语言或事件触发，规划步骤，调用 MCP 工具，输出结构化 Journal，控制 Token 预算与 HITL 中断。**不直接**执行 SSH，所有副作用经 MCP 工具。

**Provider 抽象层 `[决策#2]`：**

```python
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

def build_llm(cfg: ProviderConfig):
    # vendor="anthropic"：原生 Claude（含自定义 base_url 反代）
    if cfg.vendor == "anthropic":
        return ChatAnthropic(model=cfg.model, api_key=cfg.api_key,
                             base_url=cfg.base_url, temperature=0,
                             max_tokens=cfg.max_tokens_per_call)   # 见 §6.6 事前预算
    # vendor="openai"：原生 GPT，亦覆盖所有 OpenAI 兼容厂商
    # ——GLM-5.2、DeepSeek-V4-Pro 等经 base_url 指向厂商端点接入（决策#16）
    if cfg.vendor == "openai":
        return ChatOpenAI(model=cfg.model, api_key=cfg.api_key,
                          base_url=cfg.base_url, temperature=0,
                          max_tokens=cfg.max_tokens_per_call)
    raise ValueError(cfg.vendor)
```
API Key 与自定义 Base URL 从 Vault 内存解密获取，不落日志。`base_url` 支持自定义反代/中转网关 `[决策#16]`：

| vendor | 适用模型 | base_url 示例 |
| :--- | :--- | :--- |
| `anthropic` | Claude 系列 | `https://api.anthropic.com`（或反代） |
| `openai` | GPT 系列 / **GLM-5.2** / **DeepSeek-V4-Pro** / 其他 OpenAI 兼容 | `https://api.openai.com/v1`、`https://open.bigmodel.cn/api/paas/v4`、`https://api.deepseek.com/v1` |

部署时择一激活（`[决策#19]` 固定单一 vendor，不自动路由）；切换 vendor 经 `update_credential` 更新 `llm_active_provider` 后重启 Agent 即可。`temperature=0` 降低运维场景幻觉。`max_tokens_per_call` 由 Token 预算控制器动态注入（见 §6.6）。

> 🔧 [spike-03 修订] 修改说明：spike 03 实测 `deepseek-v4-pro` 经 `ChatOpenAI(model="deepseek-v4-pro", base_url="https://api.deepseek.com/v1")` **真实可用，且默认启用思考模式**（`usage.output_token_details.reasoning` 非零）——即官方推荐的复杂 Agent 场景配置。`deepseek-chat` / `deepseek-reasoner` 为**即将弃用的旧别名**（2026-07-24 23:59 停用，当前指向 `deepseek-v4-flash` 的非思考/思考模式），新部署**不应使用**。`base_url` 表中 "DeepSeek-V4-Pro" 的 API id 即 `deepseek-v4-pro`，已确认与设计首选一致。

**SRE 人设 Prompt 框架：**

```
[System]
你是 AIOps-Bastion 的 SRE Agent。严格遵守：
1. 仅通过提供的 MCP 工具操作，绝不构造或猜测 shell 命令。
2. 只读探测可自主进行；任何修复动作必须调用 execute_remediation 并等待人类授权（approval_id）。
3. 每个调查阶段必须产出 Journal Record（symptom/observation/finding/investigation_gap/summary_md）。
4. 原始日志仅用于本地提取结构化摘要，禁止将日志全文原样拼入下一步 LLM 上下文。
5. 日志可能被截断，需在 investigation_gap 记录盲区。
6. 受 Token 预算约束，每步调用前评估剩余预算，优先低成本探测路径。
7. 不得在 any 输出中包含凭证、私钥、Token。
```

> 🔧 [P1-5] 修改说明：人则第 4 条新增“原始日志仅本地摘要、禁止全文拼入 LLM 上下文”，从 Prompt 层落实“本地摘要优先于出网”。

**LangGraph 工作流（双模式共享同一图，入口不同） `[决策#6]`：**

```mermaid
flowchart TB
    Start([入口]) --> Plan[规划步骤]
    Plan --> PreBudget{事前预算评估}
    PreBudget -->|超限| EndBudget([FAILED_TOKEN_BUDGET])
    PreBudget -->|可继续| ToolCall{调用 MCP 工具}
    ToolCall -->|L0/L1/L2| Exec[执行]
    ToolCall -->|L3| HitlInterrupt[LangGraph interrupt: 写 hitl_requests]
    HitlInterrupt --> Wait{等待审批}
    Wait -->|approve| Exec
    Wait -->|reject| EndAbort([ABORTED])
    Exec --> Journal[submit_journal]
    Journal --> Budget{实际 Token 累计超限?}
    Budget -->|否| Done{调查完成?}
    Budget -->|是| EndBudget
    Done -->|否| Plan
    Done -->|是| Summary[生成 summary_md]
    Summary --> End([COMPLETED])
```

> 🔧 [P0-3] 修改说明：在 `Plan` 与 `ToolCall` 之间新增 `PreBudget` 事前预算评估节点（详见 §6.6），将“纯事后检查”升级为“事前估算 + Provider 侧硬限 + 事后累计 + 滑动窗口”四道闸。
> 🔧 [评审补充#R1] 修改说明：`HitlInterrupt` 经 LangGraph `interrupt()` 挂起，状态由**持久化 Checkpointer**（默认 SQLite 本地文件 `/data/checkpoints.sqlite`）保存，进程崩溃重启后可从检查点恢复（见 §11）。

- **Sync Chat 入口：** 用户消息触发，`Plan` 起点带上对话上下文，CoT 经 SSE 流式回前端。
- **Event-driven 入口：** Webhook 触发，`Plan` 起点带告警上下文（host/service/DOWN），后台执行 5~15 分钟，结束生成 Markdown 报告并推送 Telegram。
- **HITL 中断：** L3 工具调用经 LangGraph `interrupt` 挂起，状态持久化于 Checkpointer；前端审批后 `Command(resume={"approval_id": ...})` 恢复，Agent 将 `approval_id` 注入 `execute_remediation` 调用。

**Tool Calling 设计：** 经 `langchain-mcp-adapters` 将 MCP 工具转为 LangChain `Tool`，Agent 通过原生 tool calling 选择工具。工具的权限等级对 Agent 透明——Agent 只看到工具签名，**强制约束在 MCP Server 侧 PermissionGate 执行**（L3 校验 `approval_id`），不依赖 Agent 自觉。

**与其他模块交互：** 经 MCP Client 调工具；经 FirebaseWriter 写工单/日志；经 SSE 推 CoT；从 Vault `async get()` 取 LLM API Key。

### 3.6 RAG 知识库模块（Chroma）

**职责与边界：** 本地嵌入式向量库，存储 SOP、拓扑基线、历史 finding，供 `query_runbook` 检索。**纯从零沉淀**，不导入存量文档 `[决策#10]`。嵌入模型本地推理，文本不出网。

**Chroma 集成：**
- 持久化路径：`/data/chroma`（Docker 卷）。
- 嵌入函数：`chromadb.utils.embedding_functions.SentenceTransformerEmbeddingFunction("all-MiniLM-L6-v2")`（默认），首次启动本地下载，之后离线运行。

> ✅ [已定稿] 嵌入模型与检索策略：**MiniLM + BM25 混合检索**（方案 A，用户确认）。Bastion 机器为 2C4G 轻量 NAS，不升级 ~2GB 大模型；MiniLM（384 维，~90MB）对中英混杂运维日志语义较弱，靠 BM25 关键词检索补足。若后续机器升级或召回率不达标，可按方案 B 升级 `bge-m3`/`multilingual-e5-large`（仅需替换嵌入函数与维度，混合检索框架不变）。

**混合检索实现：**
- Dense：MiniLM 向量 + Chroma HNSW（cosine）。
- Sparse：BM25（`rank_bm25`） over 分词后的文档块。
- 融合：Reciprocal Rank Fusion (RRF) 合并 dense / sparse 排序，取 top_k。
- 检索时对 `runbooks.status==APPROVED` 过滤。

**Collection 设计：**

| Collection | 存储内容 | metadata 字段 |
| :--- | :--- | :--- |
| `runbooks` | SOP 文档分块 | `runbook_id`, `status`, `service`, `updated_at` |
| `topology` | 节点拓扑基线分块 | `host_id`, `baseline_at` |
| `findings` | 历史 finding 摘要 | `execution_id`, `host_id`, `service` |

**拓扑基线定义：** 节点在“正常状态”下的快照——开放端口清单、运行中的进程/服务及其常规配置。调查时 Agent 可将当前状态与基线对比以发现异常（如“8080 端口基线为开放但当前关闭”“nginx 基线在跑但当前缺失”），并作为 RAG 检索的参照上下文。

**自动拓扑探索流程 `[PRD FR5.1][决策#22]`：**（自动入库，无需人工审核）

```mermaid
flowchart LR
    Onboard["纳管新节点"] --> Disc["execute_discovery<br/>端口扫描 + 进程梳理"]
    Disc --> Gen["生成拓扑基线 Markdown"]
    Gen --> Split["分块 + 本地嵌入"]
    Split --> ChromaT["写入 Chroma(topology)"]
    Gen --> FBN["写入 Firebase nodes"]
```

**SOP 人工审核流程 `[PRD FR5.2]`：**

```mermaid
flowchart LR
    Agent["Agent 排查中生成 SOP 草稿"] --> FB1["Firebase runbooks<br/>status=DRAFT_PENDING_REVIEW"]
    FB1 --> Queue["WebUI SOP 审核队列"]
    Queue --> Review{"人工审核"}
    Review -->|修改/批准| Approve["status=APPROVED"]
    Review -->|驳回| Drop["status=REJECTED"]
    Approve --> Reembed["重新分块嵌入 → Chroma(runbooks)"]
    Approve --> Hash["更新 content_hash"]
```

**检索语义：** `query_runbook(query_string)` → 本地嵌入 query → 跨三 collection 各取 top_k（默认 5）→ BM25 并行检索 → RRF 融合去重 → 返回带元数据的片段给 Agent。冷启动期库为空，返回空结果，Agent 依赖通用推理 `[决策#10]`。

**与其他模块交互：** 被 MCP `query_runbook` 调用；拓扑探索被 `execute_discovery` 间接驱动；SOP 审核状态由前端 + Firebase 驱动。

### 3.7 凭证管理系统（Vault）

**职责与边界：** 生成、加密、解密、销毁全部凭证。Master Password 为根信任，**绝不落盘、绝不出网** `[PRD §4.1]`。

**派生与加密：**
- Master Password → `PBKDF2HMAC(algorithm=SHA256, length=32, salt=vault.salt, iterations=600000)` → Fernet key。
- Fernet key 加密凭证束（JSON：SSH 私钥们、CF API Token、Telegram Bot Token、LLM API Key×2）。
- 密文 + salt + iterations 写入 `vault.enc`（Docker 卷，定期备份）。

**关键类（异步化修正）：**

```python
class Vault:
    def __init__(self, path: Path):
        self._path = path
        self._key: bytes | None = None        # 仅内存；bytes 不可变，见下方安全声明
        self._ct: bytes | None = None         # 缓存密文，按需解密单条凭证

    async def initialize(self, master_password: str, bundle: CredentialBundle) -> str:
        """派生 Fernet key 加密 bundle；生成 24 词 BIP-39 恢复短语包裹 Fernet key。
        PBKDF2 经 asyncio.to_thread 执行，避免阻塞事件循环。返回 mnemonic（仅显示一次）。"""
        key = await asyncio.to_thread(self._derive_key, master_password)
        ...

    async def unlock(self, master_password: str) -> None:
        salt, iters, ct = read_header(self._path)
        self._key = await asyncio.to_thread(
            PBKDF2HMAC(..., salt=salt, iterations=iters).derive, master_password.encode())
        self._ct = ct  # 仅缓存密文

    async def unlock_with_recovery(self, mnemonic: str) -> None:
        """遗忘主密码时：用恢复短语派生 recovery_key，解包 Fernet key 解锁。"""
        ...

    async def rotate_master_password(self, new_master: str) -> None:
        """解锁后重设主密码（重新派生 + 重包裹 Fernet key），PBKDF2 异步执行。"""

    async def update_credential(self, name: str, value: str) -> None:
        """[评审补充#R5] 单条凭证热更新：解密 bundle → 更新该字段 → 重新加密落盘。
        无需重新 Onboarding，无需重新 unlock（须先处于 unlocked 态）。见 §4.6。"""

    async def get(self, name: str) -> str:
        if self._key is None: raise VaultLockedError()
        bundle = await asyncio.to_thread(self._decrypt_bundle, self._key, self._ct)
        return bundle[name]   # 调用方用完即弃，不缓存到长生命周期对象

    def lock(self) -> None:
        self._key = None   # 见下方“内存清零安全声明”
```

> 🔧 [P0-2] 修改说明：`unlock` / `initialize` / `rotate_master_password` / `unlock_with_recovery` / `get` 全部改为 `async def`，PBKDF2（600k 迭代，弱 NAS 上可达 1~2s）与 Fernet 解密经 `asyncio.to_thread()` 在默认线程池执行，不再阻塞 asyncio 事件循环。审计结论：Vault 所有 CPU 密集路径均已异步化，SSE/Webhook/SSH 并发在 unlock 期间不受影响。
> 🔧 [P0-1] 修改说明：**移除 v1.0 的 `ctypes.memset` “物理清零”描述——这是伪安全。** Python `bytes` / `str` 为不可变对象，`ctypes.memset` 覆写其缓冲区会破坏 CPython 内部状态、可能触发 SegFault，且因 interns / 副本 / GC 碎片无法保证真正清零。修正后的安全模型如下：
>
> **内存清零安全声明 `[评审补充#R6]`：**
> 1. Python 层**无法保证**凭证内存的物理清零；本系统不声称能做到。
> 2. 实际防护依赖 OS 级与进程级措施：
>    - **凭证驻留最小化：** `_key` 仅在 `unlock` 后驻留，`lock()` / 进程退出即丢弃引用；`get()` 返回的字符串调用方用完即弃，不放入长生命周期容器。
>    - **禁用 core dump：** 容器启动 `ulimit -c 0`，避免凭证内存被转储。
>    - **进程隔离：** Bastion 以专用非 root 用户运行，不与其他服务共享进程空间。
> 3. 是否进一步引入 `mlock`（防凭证页被 swap 到磁盘）与 `seccomp`（ syscall 收敛）属安全策略权衡——**已定稿采用基础加固（方案 A）**，不引入 mlock/seccomp（见下）。
> 4. Redaction（§4.5）作为出网前最后一道兜底，扫描日志/LLM 上下文中的凭证模式。
>
> ✅ [已定稿] OS 级加固深度：**基础加固（方案 A）**（用户确认）。仅做“禁 core dump + 进程隔离 + 凭证驻留最小化”，接受 Python 无法物理清零的残余风险（单用户内网场景可接受）。不引入 mlock / seccomp，避免容器能力膨胀与第三方库兼容风险。若未来安全等级提升，可按方案 B 追加 `mlock`（需容器 `IPC_LOCK` 能力）。

**恢复短语机制 `[决策#17]`：** 初始化时生成 24 词 BIP-39 助记词，由 `PBKDF2(mnemonic, recovery_salt)` 派生 `recovery_key`，用它**包裹**主密码派生的 Fernet key（`wrapped_fernet_key`）落盘。助记词**仅在 Onboarding 显示一次**，用户须离线保管；系统不存储明文助记词。遗忘主密码时，`unlock_with_recovery(mnemonic)` 解包 Fernet key 解锁，随后 `rotate_master_password` 设新主密码。

**生命周期状态机：**

```mermaid
stateDiagram-v2
    [*] --> Uninitialized
    Uninitialized --> Locked: initialize(首次设主密码 + 录入凭证)
    Locked --> Unlocked: unlock(输入主密码, 异步派生密钥)
    Unlocked --> Locked: lock(丢弃 key 引用)
    Unlocked --> Locked: 进程退出/重启
    Locked --> Unlocked: unlock
    Locked --> Unlocked: unlock_with_recovery(助记词, 遗忘主密码时)
    Unlocked --> Unlocked: update_credential(单条热更新)
    Unlocked --> Unlocked: rotate_master_password(重设主密码)
```

**运行期内存管理：**
- Master Password 派生后的 `_key` 以 `bytes` 持有，`lock()` / 进程退出时丢弃引用（**不声称物理清零**，见上安全声明）。
- 凭证按需 `async get()`，调用方用完即弃，不缓存到长生命周期对象。
- 日志/异常信息强制过滤凭证字段（structlog processor redaction）。

**与其他模块交互：** 执行引擎取 SSH 私钥；LLM 层取 API Key；CF 隧道配置取 CF Token；Telegram 推送取 Bot Token。所有取用均在 Bastion 进程内，不出网，且均经 `async get()`。

---

## 4. 安全设计（重中之重）

### 4.1 凭证生命周期管理 `[PRD §4.1][决策#1]`

```mermaid
flowchart LR
    Gen["生成: Onboarding 录入"] --> Enc["加密: PBKDF2 派生 Fernet Key, 加密凭证束"]
    Enc --> Disk["落盘: vault.enc (salt+iters+密文)"]
    Disk --> Use["使用: 运行期 unlock, 内存解密, 按需 async get()"]
    Use --> Rotate["轮转: update_credential / rotate_master_password"]
    Use --> Dest["销毁: lock() 丢弃引用 / 进程退出"]
```

| 阶段 | 规则 |
| :--- | :--- |
| 生成 | 仅 Onboarding 一次；Master Password 由用户掌握，系统不存储、不传输 |
| 加密 | PBKDF2(SHA256, 600k iters) + Fernet；salt 随机 16B，每实例唯一 |
| 落盘 | 仅密文 + salt + iters 入 `vault.enc`；文件权限 0600，属主容器用户 |
| 使用 | 仅 `Vault.unlock()` 后内存态可用；调用方按需 `async get()`，不缓存 |
| 轮转 | `update_credential` 单条热更新；`rotate_master_password` 重设主密码（见 §4.6） |
| 销毁 | `lock()` 或进程退出即丢弃 `_key` 引用；`vault.enc` 保留（重启需重新 unlock） |

**根信任失效处理：** Master Password 遗忘时，可用 **24 词恢复短语**（BIP-39）解锁并重设主密码 `[决策#17]`；恢复短语仅在 Onboarding 显示一次，离线保管。若主密码与恢复短语同时遗失，则凭证不可恢复。

### 4.2 指令安全模型 `[决策#7][决策#8]`

| 层级 | 机制 | 实现要点 |
| :--- | :--- | :--- |
| L0 基建 | 受控编排 | `setup_webhook_tunnel` 仅用预置 Token 调 Cloudflare API，不接受外部命令参数 |
| L1 探测 | 白名单 + 参数正则 | `ALLOWED_READONLY` 封闭动词集；`IDENT_RE` fullmatch 校验 unit/container 名 |
| L2 日志 | 白名单 + 长度截断 + 本地摘要 | `journalctl`/`docker logs` 走白名单；Server 端按 `lines` 与 token 上限截断；**Agent 本地摘要后再入 LLM** |
| L3 修复 | 硬编码模板 + HITL | `action_type` 仅 3 枚举；`clear_cache` 路径白名单；强制 `interrupt` 等待审批；`approval_id` 一次性消费 |

**参数校验正则（核心）：**
- 标识符：`IDENT_RE = ^[A-Za-z0-9_.-]{1,128}$`（fullmatch，拒绝一切 shell 元字符）。
- 元字符拒绝集：`;` `|` `&` `>` `<` `$` 反引号 `(` `)` `\n` `\r` `\\` `'` `"` —— 任一出现即 `CommandValidationError`。
- `clear_cache` 路径：必须**精确匹配** `CLEAR_CACHE_PATH_WHITELIST` 集合成员，非前缀匹配。

**远端 SSH 硬化（推荐，第三道防线）`[评审补充#R7]`：** 在业务节点 `~/.ssh/authorized_keys` 中为 Bastion 公钥追加 `restrict,command="rbash"` 或 forced-command wrapper，使远端即便收到异常字符串也仅能在受限 shell 内执行，彻底消除 shell 元字符解析面。与 Bastion 侧 `IDENT_RE` + asyncssh `shlex.quote` 构成纵深防御。

### 4.3 注入防护验证策略

单元测试覆盖以下对抗样本（每条须被 `CommandValidationError` 或 `PathNotAllowlistedError` 拒绝）：

| 注入向量 | 示例 payload | 期望 |
| :--- | :--- | :--- |
| 命令分隔 | `nginx; rm -rf /` | 拒绝（`;`） |
| 管道 | `nginx \| cat /etc/shadow` | 拒绝（`\|`） |
| 命令替换 | `$(whoami)` / 反引号 | 拒绝 |
| 重定向 | `nginx > /tmp/x` | 拒绝（`>`） |
| 换行注入 | `nginx\nrm -rf /` | 拒绝（`\n`） |
| 路径穿越 | `clear_cache path=../../etc` | 拒绝（不在白名单） |
| L3 越权枚举 | `action_type=reboot` | 拒绝（无该模板） |
| L3 参数污染 | `restart_service unit=nginx;reboot` | 拒绝（`;`） |

并验证：
- L1/L2 任何白名单外动词（如 `rm`、`curl`、`bash`）直接拒绝；
- L3 模板渲染产物为纯 `list[str]` argv，无 shell 拼接；
- **asyncssh `shlex.quote` 行为验证**：构造含元字符的非法参数，确认即便绕过 `IDENT_RE`（防御性测试），asyncssh 传给远端的字符串中元字符已被单引号转义（锁定 asyncssh ≥ 2.14）；
- **L3 `approval_id` 一次性消费**：同一 `approval_id` 二次执行被 PermissionGate 拒绝（返回 `HITL_REJECTED`）。


### 4.4 Zero Trust 与网络隔离（Cloudflare Tunnel 配置）

**Bastion 主机防火墙（nftables/ufw 策略）：**
- 默认 `DROP` 所有入站。
- 允许出站仅到白名单：LLM API（Anthropic / OpenAI / **GLM (open.bigmodel.cn)** / **DeepSeek (api.deepseek.com)** 等当前激活厂商）、Firebase、Telegram API、Cloudflare API。
- 允许 SSH 出站到业务节点（带外管理网段）。

**`cloudflared` 配置（Docker Compose 常驻服务） `[决策#11]`：**
```yaml
# cloudflared config.yml
tunnel: <TUNNEL_ID>
credentials-file: /etc/cloudflared/creds.json
ingress:
  - hostname: bastion.<your-domain>.top
    service: http://bastion-app:8080      # Docker 服务名，经专用内部网络回源
    path: /api/v1/webhook/uptime-kuma
  - service: http_status:404
```
- `cloudflared` 以**出站**长连接到 Cloudflare 边缘，Bastion 无需任何公网监听端口。
- **网络拓扑修正 `[评审补充#R8]`：** `cloudflared` 与 `bastion-app` 分属不同容器，`127.0.0.1` 无法跨容器寻址。回源地址由 v1.0 的 `http://127.0.0.1:8080` 改为 **`http://bastion-app:8080`**（Docker 服务名 DNS），两容器接入同一 `docker network`（见 §7.4）；`bastion-app` **不发布端口到宿主机**（`ports` 字段移除或仅 `127.0.0.1:8080` 调试用）。安全语义不变：Webhook 端点仅 cloudflared 可达，不对局域网/公网暴露。
- Webhook 端点校验**共享密钥自定义 header** `[决策#21]`：Uptime Kuma 侧配置 header `X-Webhook-Secret: <随机长串>`（密钥存于 Vault），Bastion 端 FastAPI 中间件严格校验该 header 等于预期值，不匹配返回 401。密钥经 HTTPS/CF Tunnel 加密传输。Uptime Kuma 原生支持自定义 header，无需 HMAC。
- **重放与泄露风险 `[评审补充#R9]`：** 共享密钥为静态 bearer，泄露即可重放。缓解：定期经 `update_credential` 轮转密钥；启用 CF Access 对域名加 SSO 收敛暴露面；Webhook 端点额外校验 `timestamp` 防大幅延迟重放（Uptime Kuma 可在 header 附带时间戳）。此为单用户内网场景的可接受权衡。

### 4.5 数据出网边界与最小化策略 `[决策#1][决策#5]`

| 数据类别 | 是否出网 | 目的 | 最小化措施 |
| :--- | :--- | :--- | :--- |
| Master Password / SSH 私钥 / API Token | ❌ 绝不 | — | 仅本地 Vault 内存 |
| **原始日志全文** | ❌ 绝不 | — | 仅 Bastion 本地消费，Agent 提取结构化摘要 |
| 排查日志/拓扑**摘要** | ✅ 出网 | LLM API | Agent 本地摘要后送出；摘要经 redaction |
| 工单/日志/拓扑元数据 | ✅ 出网 | Firebase | 结构化字段，不含凭证；Firebase 安全规则限单用户 |
| 调查报告 | ✅ 出网 | Telegram | 仅 `summary_md`，发送前 redaction 扫描凭证模式 |
| LLM API Key | ✅ 出网（请求头） | LLM API | 仅 unlock 后内存态，随请求发出 |

> 🔧 [P1-5] 修改说明：**重构出网数据流模型**。v1.0 依赖正则 Redaction 防御日志注入，但外部业务节点日志不可控，攻击者可通过 Base64 / 多行拆分 / 编码绕过正则。修正为“本地摘要优先 + Redaction 兜底”双层：
>
> 1. **核心防线（本地摘要）：** `fetch_service_logs` 返回的原始日志**仅在 Agent 本地**用于提取结构化摘要（如 `error_lines`、`timestamp_range`、`error_count`、`suspected_module`）。送入 LLM 的是 Agent 生成的结构化摘要，**不含原始日志全文**。日志注入向量因“原文不出网”而失效。
> 2. **兜底防线（Redaction）：** 所有出网文本（LLM 上下文摘要、Telegram 报告）仍经 structlog/输出过滤器扫描 `ssh-`、`-----BEGIN`、`Bearer `、长 hex/token 模式并脱敏。Redaction 明确标注为“最后一道兜底，不作为核心防线”，并记录其已知局限（编码绕过）。
> 3. 原始日志保留在本地 `/data/logs`（按 `execution_id` 分目录，含保留期与自动清理）供审计，不出网。

### 4.6 凭证轮转与热更新 `[评审补充#R10]` 🆕 新增章节

> 🆕 新增章节：v1.0 缺少凭证过期后的更新路径，仅能整体重新 Onboarding。本节定义单条凭证热更新与轮转流程。

**热更新接口：** `Vault.update_credential(name, value)`（见 §3.7）——在 `unlocked` 态下解密 bundle → 替换指定字段 → 重新加密落盘 `vault.enc`，不触碰 Master Password 与恢复短语包裹关系。

**各凭证轮转场景：**

| 凭证 | 触发 | 更新路径 | 生效方式 |
| :--- | :--- | :--- | :--- |
| LLM API Key（GLM/DeepSeek/Claude/GPT） | 即将过期 / 怀疑泄露 / 切换厂商 | WebUI 凭证管理页 → `update_credential`（更新 `llm_providers.<name>.api_key` 或 `llm_active_provider`） | 下次 `get()` 即用新值；进行中调查不中断 |
| CF API Token | 即将过期 | `update_credential` + `setup_webhook_tunnel` 校验隧道 | 隧道配置不变（creds.json 独立于 API Token） |
| Telegram Bot Token | 轮换 | `update_credential` | 下次推送即用新值 |
| SSH 私钥 | 节点换密钥 | `update_credential("ssh_keys.<host>", ...)` + 节点侧 `authorized_keys` 同步 | 连接池按 host 失效重建 |
| Master Password | 定期轮换 | `rotate_master_password(new)` | 重新派生 + 重包裹 Fernet key；恢复短语不变（仍包裹同一 Fernet key） |

**一致性约束：**
- `update_credential` 写 `vault.enc` 后，内存 `_ct` 同步刷新，避免旧密文被再次解密。
- 热更新操作记审计日志（`actor=system/user`、`credential_name`、`ts`，**不记值**）。
- 进行中的 SSH 连接不主动断开；按 host 复用的连接在下次重建时取新私钥。

---

## 5. MCP 工具集详细规范

统一返回：`{ok: bool, data?: object, error?: {code: string, message: string}}`。错误码目录见 §5.7。

> 🔧 [spike-02 修订] 修改说明：**MCP 工具返回结构分层澄清。** spike 02 实测发现，经 `langchain-mcp-adapters` 包装后，MCP 工具的 `ainvoke` 返回值是 **content block 列表** `[TextContent(type="text", text="...")]`，而非纯字符串。本节所述"统一返回 `{ok, data}` JSON"是该 content block 的 **`text` 字段内层 JSON**，外层还包一层 MCP 协议的 content block 结构。完整结构如下：
>
> ```
> MCP 工具 ainvoke 返回 (LangChain Tool 层):
>   [ TextContent(type="text", text="<内层 JSON 字符串>") ]
>                                              │
>                                              └─ json.loads 解析得 ──> {ok: bool, data?: ..., error?: {code, message}}
> ```
>
> Agent 读取工具结果时须经提取步骤：取列表首个元素的 `text` 字段 → `json.loads` → 得内层 `{ok, data}` 契约。实施时封装 `_extract_tool_result(result) -> dict` 工具函数统一处理（spike 02 已验证）。此分层不影响工具的 JSON Schema 契约（§5.1-5.6 的输入/输出 Schema 描述的是**内层** data 结构）。

### 5.1 `setup_webhook_tunnel`（L0 基建）
**职责：** 基于 Vault 中 CF API Token 创建/校验隧道与 DNS 路由，确保 `cloudflared` 已配置。无外部命令参数输入。

**输入 Schema：** `{type: object, properties: {}, additionalProperties: false}`
**输出 Schema：** `{ok: true, data: {tunnel_id, hostname, status}}`
**安全校验：** 仅使用预置 Token；不执行任意 shell；操作幂等。
**错误处理：** CF API 失败 → 重试 3 次（指数退避）→ 仍失败 `INTERNAL` + 明确错误；不暴露 Token。

### 5.2 `execute_discovery`（L1 探测）
**职责：** 探测目标主机某服务存活状态，支持 systemd / Docker / Compose 三形态 `[决策#13]`。

**输入 Schema：**
```json
{
  "type": "object",
  "required": ["target_host", "service_name", "form"],
  "properties": {
    "target_host": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,128}$"},
    "service_name": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,128}$"},
    "form": {"type": "string", "enum": ["systemd", "docker", "compose"]}
  }
}
```
**输出 Schema：** `{ok: true, data: {target_host, service_name, status: "active"|"inactive"|"unknown", detail}}`
**安全校验：** `form` 枚举白名单；`service_name` 走 `IDENT_RE`；经 `ExecutionEngine.run_readonly`。
**状态映射 `[评审补充#R11]`：** `systemctl is-active` → `active`/`inactive`/`unknown`；`docker inspect` 据 `.State.Running` 映射；`docker compose ps` 据服务状态映射。
**错误处理：** 连接失败 `INTERNAL`；超时 `EXEC_TIMEOUT`。

### 5.3 `fetch_service_logs`（L2 日志）
**职责：** 抓取报错日志，Server 端强制 token 长度截断 `[PRD §4.4]`；**原始日志仅本地消费，Agent 本地摘要后入 LLM**。

**输入 Schema：**
```json
{
  "type": "object",
  "required": ["target_host", "service_name", "lines"],
  "properties": {
    "target_host": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,128}$"},
    "service_name": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,128}$"},
    "lines": {"type": "integer", "minimum": 1, "maximum": 500}
  }
}
```
**输出 Schema：** `{ok: true, data: {target_host, service_name, logs: string, truncated: bool}}`
**安全校验：** `lines` 上限 500；返回前按 token 估算（≈ chars/4）截断至预算内（默认 ≤ 8k tokens）；`truncated` 标记。
**错误处理：** 超时 `EXEC_TIMEOUT`；日志为空仍 `ok:true, data.logs=""`。

### 5.4 `submit_journal`（L2 归档）
**职责：** 将排查中间发现结构化写入 Firebase `[PRD §3.4]`。

**输入 Schema：**
```json
{
  "type": "object",
  "required": ["execution_id", "record_type", "content"],
  "properties": {
    "execution_id": {"type": "string"},
    "record_type": {"type": "string", "enum": ["symptom","observation","finding","investigation_gap","summary_md"]},
    "content": {"type": "string", "maxLength": 16000}
  }
}
```
**输出 Schema：** `{ok: true, data: {record_id, ts}}`
**安全校验：** `record_type` 枚举；`content` 写入前 redaction 扫描；`execution_id` 须为调用方当前工单（防越权写他人工单）。
**错误处理：** Firebase 写入失败 → 重试 3 次（退避）→ 仍失败 `INTERNAL`；**Journal 写入失败不阻塞调查状态流转**（见 §11 补偿）。

### 5.5 `query_runbook`（L2 知识库）
**职责：** 连接本地 Chroma 检索历史工单与 SOP `[决策#10]`。

**输入 Schema：**
```json
{"type": "object", "required": ["query_string"],
 "properties": {"query_string": {"type": "string", "maxLength": 1000},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}}}
```
**输出 Schema：** `{ok: true, data: {results: [{content, source, metadata, score}]}}`
**安全校验：** 本地嵌入，不出网；`query_string` 长度限制。
**错误处理：** 冷启动库空 → `ok:true, data.results:[]`；嵌入失败 `INTERNAL`。

### 5.6 `execute_remediation`（L3 高危）
**职责：** 执行修复，**强制 HITL**，审批后由 Agent 自动执行 `[决策#6][决策#7]`。

**输入 Schema：**
```json
{
  "type": "object",
  "required": ["target_host", "action_type", "params"],
  "properties": {
    "target_host": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,128}$"},
    "action_type": {"type": "string", "enum": ["restart_service","restart_container","clear_cache"]},
    "params": {"type": "object"},
    "approval_id": {"type": "string", "description": "resume 时由 Agent 注入，PermissionGate 校验"}
  }
}
```
**action_type → 模板映射：**

| action_type | params | 渲染命令（list argv） | 校验 |
| :--- | :--- | :--- | :--- |
| `restart_service` | `{unit}` | `["systemctl","restart",unit]` | unit 走 IDENT_RE |
| `restart_container` | `{name}` | `["docker","restart",name]` | name 走 IDENT_RE |
| `clear_cache` | `{path}` | `["/usr/local/bin/clear_cache.sh",path]` | path 精确命中白名单 |

**输出 Schema：** `{ok: true, data: {target_host, action_type, exit_code, stdout_truncated}}`
**安全校验流程（HITL 职责拆分，见 §3.3）：**
1. Agent 识别 L3 → `interrupt()` → 写 `hitl_requests(PENDING)`，附 `rendered_cmd` 预览、`target_host`、`action_type`、影响说明。
2. 等待 `approve`（带 `approval_id`）/ `reject` / 30 分钟超时。
3. `approve` → Agent resume，调 `execute_remediation(approval_id)`。
4. **PermissionGate 校验** `approval_id`：存在、`status==APPROVED`、未过期、未被消费 → 置 `CONSUMED` → 渲染模板 + IDENT_RE / 路径白名单校验 → `ExecutionEngine.run_remediation` 执行（30s 超时）。
5. `reject` / 超时 → `HITL_REJECTED` / `HITL_TIMEOUT`。
**错误处理：** 审批拒绝 `HITL_REJECTED`；执行超时 `EXEC_TIMEOUT`；路径越权 `PATH_NOT_ALLOWLISTED`；`approval_id` 无效/复用 `HITL_REJECTED`。

### 5.7 错误码目录与统一处理规约 `[评审补充#R12]` 🆕 新增章节

> 🆕 新增章节：v1.0 仅在 §5 开头罗列错误码，未定义来源层级与 HTTP/重试映射，存在 `AUTH_REQUIRED`、`BUDGET_EXCEEDED` 用途不清的问题。本节统一规约。

| 错误码 | 来源层级 | 触发场景 | HTTP 映射（若经 REST） | 重试策略 |
| :--- | :--- | :--- | :--- | :--- |
| `VALIDATION_ERROR` | 工具 | Schema/正则/枚举校验失败 | 400 | 不重试 |
| `AUTH_REQUIRED` | 工具/编排 | Vault 未 unlock 即取凭证；SSE 缺鉴权 | 401 | 不重试（需 unlock/登录） |
| `HITL_REJECTED` | 工具 | L3 审批被拒 / `approval_id` 无效或复用 | 200（业务拒绝） | 不重试 |
| `HITL_TIMEOUT` | 编排 | L3 审批 30min 超时 | 200（业务失败） | 不重试 |
| `EXEC_TIMEOUT` | 工具 | SSH 执行超时 / slot 等待超时 | 200（业务失败） | 不重试（记 gap） |
| `BUDGET_EXCEEDED` | 编排 | Token 预算耗尽（事前评估或事后累计） | 200（业务失败） | 不重试（工单 FAILED_TOKEN_BUDGET） |
| `PATH_NOT_ALLOWLISTED` | 工具 | `clear_cache` 路径越权 | 400 | 不重试 |
| `INTERNAL` | 工具/编排 | 外部依赖持续失败、未预期异常 | 500 | 视依赖而定（见 §7.5） |

**一致性约定：**
- 工具级错误（`VALIDATION_ERROR` / `EXEC_TIMEOUT` / `PATH_NOT_ALLOWLISTED` / `HITL_REJECTED`）经 MCP `tools/call` 返回给 Agent，Agent 决定是否记 `investigation_gap` 后继续或终止。
- 编排级错误（`BUDGET_EXCEEDED` / `HITL_TIMEOUT`）由 LangGraph 编排器产生，直接驱动状态机流转（`FAILED_TOKEN_BUDGET` / `ABORTED`）。
- `AUTH_REQUIRED` 在 Vault 未 unlock 时由凭证取用路径抛出，Agent 应终止并提示用户 unlock。


---

## 6. Agent 工作流详细设计

### 6.1 Sync Chat 模式（对话排查）`[PRD FR3.1]`

```mermaid
sequenceDiagram
    participant U as 用户(浏览器)
    participant API as FastAPI
    participant G as LangGraph Agent
    participant MCP as MCP Server
    participant FB as Firebase
    U->>API: POST /chat {message}
    API->>FB: 创建 investigation(PENDING)
    API->>G: 启动图(message, execution_id)
    loop 规划-事前预算-工具-日志
        G->>G: 事前预算评估(剩余预算)
        G->>MCP: execute_discovery/fetch_service_logs (L1/L2)
        MCP-->>G: 结果(本地摘要)
        G->>MCP: submit_journal(observation/finding)
        G-->>API: SSE token_delta/step
        API-->>U: 思维链流式(fetch)
    end
    alt 触发 L3
        G->>G: interrupt(写 hitl_requests PENDING)
        FB-->>U: 推送审批弹窗
        U->>API: POST /hitl/{id}/approve
        API->>G: resume(approval_id)
        G->>MCP: execute_remediation(approval_id)
        MCP->>MCP: PermissionGate 校验 approval_id
        MCP-->>G: 结果
    end
    G->>MCP: submit_journal(summary_md)
    API->>FB: investigation(COMPLETED)
    API-->>U: 最终回复 + 报告
```

> 🔧 [P1-4] 修改说明：序列图显式标注 `interrupt` 在 Agent 侧、`approval_id` 经 resume 注入、PermissionGate 在 MCP 侧校验，与 §3.3 职责拆分一致。

### 6.2 Event-driven 模式（异步事件）`[PRD FR3.2][决策#9]`

```mermaid
sequenceDiagram
    participant UK as Uptime Kuma
    participant CF as Cloudflare
    participant API as FastAPI Webhook
    participant DEDUP as 去重器(Firestore 事务)
    participant G as LangGraph Agent
    participant TG as Telegram
    participant FB as Firebase
    UK->>CF: Webhook {monitor, status: DOWN}
    CF->>API: 隧道回源
    API->>API: 校验 X-Webhook-Secret + 时间戳 + 解析 host+service
    API->>DEDUP: 事务查 dedup_key
    alt 已有进行中工单
        DEDUP-->>API: 并入(附加 observation, 原子)
        API-->>UK: 200 (已合并)
    else 新故障
        DEDUP->>FB: 创建 investigation(IN_PROGRESS, dedup_key)
        DEDUP->>G: 后台启动深度侦察(5~15min)
        loop 证据收集
            G->>G: 事前预算评估 + 调 MCP 探测/日志/RAG
            G->>FB: submit_journal
        end
        G->>G: 检查 Token 预算
        G->>FB: investigation(COMPLETED|FAILED_TOKEN_BUDGET)
        G->>TG: 推送 Markdown 报告(best-effort)
    end
    Note over UK,FB: UP 恢复: status=UP → 标记 RESOLVED, 不启动调查
```

> 🔧 [P2-8] 修改说明：事件模式后台调查受“并发调查数上限（默认 3）”信号量约束，超出则排队或拒绝（详见 §7.1）。
> 🔧 [评审补充#R9] 修改说明：Webhook 校验新增 `timestamp` 防延迟重放（见 §4.4）。

### 6.3 调查状态机 `[决策#9][决策#15]`

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> IN_PROGRESS: Agent 启动
    IN_PROGRESS --> HITL_SUSPENDED: 触发 L3 interrupt
    HITL_SUSPENDED --> IN_PROGRESS: 审批通过(resume)
    HITL_SUSPENDED --> ABORTED: 拒绝/超时
    IN_PROGRESS --> COMPLETED: 调查完成
    IN_PROGRESS --> FAILED_TOKEN_BUDGET: Token 超限
    IN_PROGRESS --> ABORTED: 崩溃恢复放弃(见 §6.8/§11)
    HITL_SUSPENDED --> HITL_SUSPENDED: 崩溃后从检查点恢复
    COMPLETED --> RESOLVED: 收到 UP 恢复
    FAILED_TOKEN_BUDGET --> [*]
    ABORTED --> [*]
    RESOLVED --> [*]
```

> 🔧 [评审补充#R13] 修改说明：状态机新增“崩溃恢复”转移：`IN_PROGRESS → ABORTED`（无法安全恢复时）与 `HITL_SUSPENDED` 自恢复（从 Checkpointer 恢复继续等待审批）。状态枚举与 §8.1 `investigations.status` 完全一致（PENDING/IN_PROGRESS/HITL_SUSPENDED/COMPLETED/RESOLVED/FAILED_TOKEN_BUDGET/ABORTED）。

### 6.4 事件去重与 UP/DOWN 语义

- **dedup_key = `host_id + "::" + service_name`**。
- **DOWN** → 若无活跃工单则新建并启动深度侦察；若有则并入。
- **UP** → 将同 key 活跃工单标记 `RESOLVED`，附 `observation: service recovered`，**不**启动新调查。

> 🔧 [评审补充#R14] 修改说明：**修复去重 race condition**。v1.0 的“先查后建”在两个 DOWN webhook 几乎同时到达时会各自判定无活跃工单、各建一单。修正为经 **Firestore 事务**原子完成“读活跃工单 → 存在则附加 observation / 不存在则建单”：
>
> ```python
> async def dedup_or_create(db, dedup_key, trigger):
>     doc_ref = db.collection("investigations").document()
>     @firestore.async_transactional
>     def txn(transaction):
>         active = db.collection("investigations") \\
>             .where("dedup_key", "==", dedup_key) \\
>             .where("status", "in", ["PENDING","IN_PROGRESS","HITL_SUSPENDED"]) \\
>             .stream(transaction=transaction)
>         existing = list(active)
>         if existing:
>             # 并入：原子 append observation record
>             add_observation(existing[0].id, trigger, transaction)
>             return {"merged_into": existing[0].id}
>         transaction.set(doc_ref, new_investigation(dedup_key, trigger))
>         return {"created": doc_ref.id}
>     return await txn(db.transaction())
> ```
> 事务保证至多一个并发 DOWN 创建新工单；其余并入。配合 `dedup_key + status` 复合索引（§3.2）。

### 6.5 结构化 Journal Records 生成规范 `[PRD §3.4]`

| record_type | 何时产出 | 内容约束 |
| :--- | :--- | :--- |
| `symptom` | 调查起点 | 告警/用户描述的客观症状，1~2 句 |
| `observation` | 每次工具调用后 | 工具返回的客观事实（status/**日志摘要**） |
| `finding` | 根因分析时 | 推理得出的发现与根因 |
| `investigation_gap` | 遇盲区时 | 因权限/日志截断/预算耗尽导致的未知，须显式记录 |
| `summary_md` | 调查终点 | 面向人类的 Markdown 报告：症状/根因/已采取措施/建议 |

每条 record 经 `submit_journal` 写入 `investigations/{id}/records`，含 `ts`、`record_type`、`content`；`content` 写入前 redaction。

> 🔧 [P1-5] 修改说明：`observation` 内容约束由“status/logs 摘要”明确为“status/**日志摘要**”——日志以本地摘要形式入 Journal，原始全文不出网、不入 LLM。

### 6.6 Token 预算控制与熔断 `[决策#15]`

> 🔧 [P0-3] 修改说明：将 v1.0 的“纯事后检查”升级为**四道闸**模型：

1. **预算账户：** 每个 investigation 在 LangGraph state 持 `token_usage`（input+output 累计），同步写 Firebase。
2. **闸 1 — 事前估算（PreBudget 节点）：** 每次 LLM 调用前估算 prompt tokens。**跨厂商统一用保守的字符启发式估算**（中文 ≈ chars/1.5、英文 ≈ chars/4，取较大值），不依赖特定分词器——因 GLM-5.2 / DeepSeek-V4-Pro 等国产模型分词器与 tiktoken 不同，tiktoken 估算会失真。事后由 API 响应的真实 `usage` 修正（闸 3）。若 `prompt_tokens + 预期_output > 剩余预算` → 先尝试截断上下文（丢弃较早的 Journal / 日志摘要，保留最近 + finding）；截断后仍超 → 立即终止，工单置 `FAILED_TOKEN_BUDGET`，附 `investigation_gap: token budget exhausted (pre-call)`。
3. **闸 2 — Provider 侧硬限：** 每次 LLM 调用动态设 `max_tokens = min(模型上限, 剩余预算)`，从 Provider 侧防止单次响应超支。
4. **闸 3 — 事后累计：** LLM 响应返回后，用真实 `usage` 更新 `token_usage`。**累计项须含 reasoning tokens**——`deepseek-v4-pro` 默认启用思考模式，`usage_metadata.output_token_details.reasoning` 非零且已计入 `output_tokens`，但实现时须显式读取 `output_tokens`（已含 reasoning）而非仅 `response.output` 文本长度，避免思考模式长推理导致预算偷偷超支。若 `token_usage >= TOKEN_BUDGET` → 终止、置 `FAILED_TOKEN_BUDGET`，附 `investigation_gap: token budget exhausted (post-call)`。
5. **闸 4 — 滑动窗口均值：** 维护近 N 步的 token 消耗均值，若按当前速率推算会在预算内无法完成既定探测路径 → 提前收敛（跳过低价值探测，直接进入 summary），避免硬超限。

- **硬上限：** `TOKEN_BUDGET`（默认 **512k tokens/次** `[决策#18]`，不变）。
- **截断协同：** `fetch_service_logs` 单次返回 ≤ 8k tokens，从源头控制单步消耗。
- **熔断不可恢复：** 一旦 `FAILED_TOKEN_BUDGET`，该工单不再恢复，需人工发起新调查。

> ✅ [已定稿] Token 成本上限：**维持 512k/次（方案 A）**（用户确认）。采用国产低价模型（GLM-5.2 Coding Plan 40 RMB/月、或 DeepSeek-V4-Pro 约 3 RMB/1M 输入 + 6 RMB/1M 输出），512k 单次调查成本约 2~3 RMB，成本非约束，无需叠加美元软限或分模式设限。靠 `FAILED_TOKEN_BUDGET` 自然封顶即可。模型上下文为 1M，512k 预算为半数上下文，留有充足窗口。

### 6.7 HITL 授权流程 `[决策#6]`

```mermaid
flowchart TB
    L3["Agent 识别 L3 工具"] --> Render["模板渲染 + 校验(预览)"]
    Render --> Req["interrupt: 写 hitl_requests(PENDING)<br/>含 rendered_cmd 预览/影响<br/>**同时 approval_id 写入图 state**"]
    Req --> Persist["状态持久化至 Checkpointer"]
    Persist --> UI["WebUI 推送审批弹窗"]
    UI --> Decision{人工}
    Decision -->|approve| Resume["resume: approval_id 已在 state<br/>ToolNode/InjectedToolArg 从 state 读取注入"]
    Decision -->|reject| Rej["resume(reject) → ABORTED"]
    Decision -->|30min 超时| Rej
    Resume --> Gate["MCP PermissionGate 校验 approval_id<br/>(从 state 读取, 非工具参数)"]
    Gate --> Exec["ExecutionEngine.run_remediation(30s)"]
    Gate -->|无效/复用| Rej
    Exec --> Record["submit_journal(observation)"]
```

- 审批界面**必须**展示：`target_host`、`action_type`、渲染后的完整命令预览、预期影响、关联工单。
- 审批决策记入 `hitl_requests`（`decision`、`decided_by`、`decided_at`）供审计。
- 仅 L3 触发 HITL；L1/L2 只读自主执行，但全部记审计日志。

> 🔧 [spike-04 修订] 修改说明：**`approval_id` 透传机制明确。** spike 04 暴露 `create_react_agent` 默认机制下 resume 注入的 `approval_id` **不会自动透传到 `execute_remediation` 工具参数**。v1.2 原文"resume 时由 Agent 注入"过于笼统，此处明确：**`approval_id` 在 interrupt 时写入 LangGraph 图 state，resume 后由 ToolNode（经 `InjectedToolArg`）或自定义 ToolNode 从 state 读取并注入工具调用**，Agent 调用 `execute_remediation` 时无需在 `args` 里传 `approval_id`。MCP Server 侧 PermissionGate 亦从 state（或调用上下文）读取 `approval_id` 做防御性校验，不依赖工具参数。实现方案三选一见 §3.3 修订（推荐方案 A `InjectedToolArg`）。

> 🔧 [P1-4] 修改说明：**单用户 HITL 定位声明。** `[决策#12]` 锁定单用户模型，触发者与审批者为同一账号，HITL 不构成“权限隔离”，而定位为“**防误触确认**”：要求人类在执行前显式审阅渲染命令与影响并二次确认，防 Agent 误判导致误修复。此为既定安全权衡，已在 §9.2 记录。
>
> ✅ [已定稿] HITL 在单用户下的增强策略：**防误触确认（方案 A）**（用户确认）。维持同账号二次点击确认，不引入 step-up 口令或演进多用户 RBAC。文档显式声明此权衡：HITL 定位为“防 Agent 误判导致误修复”的确认闸，非“权限隔离”。若未来 WebUI 会话劫持风险上升，可按方案 B 追加 step-up 操作口令（Vault 存独立口令，L3 审批时校验）。

### 6.8 崩溃恢复与挂起状态补偿 `[评审补充#R15]` 🆕 新增章节

> 🆕 新增章节：v1.0 未定义 Bastion 进程崩溃重启后，处于 `IN_PROGRESS` / `HITL_SUSPENDED` 的调查与 `PENDING` 的 `hitl_requests` 如何恢复，存在状态悬挂与死锁风险。本节定义恢复策略（详细机制见 §11）。

**启动时扫描（Recovery Sweep）：**
1. 扫描 `investigations` 中 `status ∈ {PENDING, IN_PROGRESS, HITL_SUSPENDED}` 的工单。
2. 扫描 `hitl_requests` 中 `status==PENDING` 的请求。

**恢复决策：**
| 工单状态 | hitl_request 状态 | 恢复动作 |
| :--- | :--- | :--- |
| `HITL_SUSPENDED` | `PENDING` 且未过期 | 从 Checkpointer 恢复图，继续等待审批 |
| `HITL_SUSPENDED` | `PENDING` 且已过期（>30min） | `hitl_requests → EXPIRED`，工单 `→ ABORTED`，记 `investigation_gap` |
| `HITL_SUSPENDED` | 无 PENDING / `APPROVED` 已消费 | 异常态：工单 `→ ABORTED`，告警 |
| `IN_PROGRESS` | — | 从最近 Checkpointer 检查点 `resume` 重放；若无可用检查点 → `→ ABORTED`，记 `investigation_gap: crashed before checkpoint` |
| `PENDING` | — | 启动 Agent 执行（未真正开始） |

**HITL 超时清扫器：** 后台周期任务（默认每 5min）扫描过期 `PENDING` `hitl_requests`，置 `EXPIRED` 并驱动对应工单 `→ ABORTED`，避免永久悬挂。

**数据一致性补偿：**
- **SSH 已执行但 Firebase 写入失败：** `submit_journal` / 状态写失败时，重试 3 次（退避）；仍失败则写入本地 outbox `/data/outbox/{execution_id}.jsonl`，由后台同步器在 Firebase 恢复后回放（at-least-once，靠 `record_id` 幂等去重）。
- **Journal 写入失败不阻塞状态流转：** Journal 为 best-effort，单条失败记本地日志 + `investigation_gap`，调查继续；状态机不因 Journal 失败而回退。


---

## 7. 非功能性设计

### 7.1 性能与并发 `[决策#3]`

- **单进程 + 受限并发：** 一个 asyncio 事件循环；SSH 并发用 `asyncio.Semaphore(4)` 限流；**调查级并发**用独立 `asyncio.Semaphore(3)` 限流（Event 模式后台调查上限），不引入任务队列/执行器池/跨节点编排。
- **超时熔断：** 只读 SSH 5s、修复 SSH 30s（可配）；LLM 调用单次 30s 超时；超时即释放连接、记录 `EXEC_TIMEOUT`，不阻塞事件循环。
- **连接复用：** 按 `target_host` 复用 `asyncssh.SSHClient`，避免反复握手；连接异常自动重建。
- **背压与优雅降级 `[评审补充#R16]`：**
  - SSH slot 排队等待上限 30s（`wait_slot`），超时返回 `EXEC_TIMEOUT(queue)`，Agent 记 `investigation_gap`。
  - 调查并发达上限 3 时，新 Event 调查排队等待；队列深度超阈值（默认 5）时拒绝新建并告警，避免 token 与 SSH 资源耗尽。
  - 队列深度、slot 占用率、调查并发数暴露为指标（§7.2），超阈值告警。
- **SSH 连接健康检查 `[评审补充#R16]`：** 周期性对连接池中空闲连接做轻量探活（`true` 命令，1s 超时），失败则剔除并在下次按需重建，避免向已僵死连接下发命令导致超时浪费。

> 🔧 [P2-8] 修改说明：补齐 v1.0 缺失的优雅降级——排队超时、队列深度监控告警、SSH 连接健康检查。

### 7.2 可观测性

- **结构化日志：** structlog JSON 日志，含 `execution_id`、`tool`、`host`、`latency`、`token_delta`、`queue_depth`；凭证字段经 redaction processor。
- **思维链流式：** Agent 每步经 SSE 推 `step`/`tool_call`/`tool_result`/`token_delta` 到前端 `[PRD FR1.3]`。
- **Firebase 实时同步：** 工单状态/进度/Journal 经 `onSnapshot` 驱动前端无缝刷新。
- **审计：** 所有 MCP 工具调用（含 L1/L2）记审计日志：`ts, execution_id, tool, level, host, params_redacted, ok, latency`。
- **指标：** 暴露 `/metrics`（Prometheus 格式可选）：调查数、调查并发数、token 消耗、SSH 超时率、SSH slot 占用率、队列深度、HITL 平均审批时长、PENDING `hitl_requests` 堆积数。

### 7.3 成本控制 `[决策#15]`

- **Token 硬上限 + 事前评估：** 见 §6.6 四道闸，每调查独立预算，超限即 `FAILED_TOKEN_BUDGET`。
- **日志截断：** `fetch_service_logs` 双重限制（`lines ≤ 500` + token 估算 ≤ 8k）。
- **RAG 兜底：** 命中知识库可减少反复探测的 token 消耗；冷启动期接受较高 token 成本。
- **Provider 固定：** 部署时配置单一 vendor（Claude / GPT / **GLM-5.2** / **DeepSeek-V4-Pro** 任一），不按任务复杂度自动路由 `[决策#19]`。采用国产低价模型时单次调查成本约 2~3 RMB，成本非约束。

### 7.4 部署与运维 `[决策#11]`

**Docker Compose 结构（网络修正）：**
```yaml
services:
  bastion-app:
    image: aiops-bastion:1.1
    env_file: .env              # 非敏感配置
    volumes:
      - ./data/vault:/data/vault        # vault.enc
      - ./data/chroma:/data/chroma      # 向量库
      - ./data/logs:/data/logs          # 本地日志(含原始日志)
      - ./data/outbox:/data/outbox      # FB 写入失败补偿 outbox [评审补充#R15]
      - ./data/checkpoints:/data/checkpoints  # LangGraph Checkpointer [评审补充#R1]
    expose: ["8080"]            # 仅对 linked 服务可见，不发布到宿主机
    ulimits:
      core: 0                   # 禁用 core dump [评审补充#R6]
    restart: unless-stopped
    networks: [bastion-net]
  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel run
    volumes: ["./cloudflared:/etc/cloudflared:ro"]
    restart: unless-stopped
    depends_on: [bastion-app]
    networks: [bastion-net]
networks:
  bastion-net:
    driver: bridge
```

> 🔧 [评审补充#R8] 修改说明：**修正 v1.0 Compose 的网络错误。** v1.0 用 `ports: ["127.0.0.1:8080:8080"]` 并声称 cloudflared 经 127.0.0.1 回源，但两容器网络命名空间隔离，127.0.0.1 不可达。改为：`bastion-app` 用 `expose`（仅 `bastion-net` 内可见，不发布到宿主机），cloudflared 经 `http://bastion-app:8080` 回源，两容器接入同一 `bridge` 网络。安全语义不变（不对公网/局域网暴露），且消除跨容器寻址错误。
> 🔧 [评审补充#R6] 修改说明：新增 `ulimits.core: 0` 禁用 core dump（防凭证内存被转储），并新增 `outbox` 与 `checkpoints` 卷支撑 §6.8 崩溃恢复。

- **cloudflared 集成：** 常驻容器，出站长连到 CF 边缘；creds.json 与 config.yml 挂载只读。
- **升级：** 镜像版本化 tag；`docker compose pull && up -d`；Vault/Chroma/Checkpoints/Outbox 卷保留，跨版本兼容由 schema migration 脚本保证。
- **回滚：** 保留前一版本镜像 tag，`docker compose up -d` 指定旧 tag；持久化卷不受影响。
- **备份：** 定期冷备 `vault.enc` + `chroma/` + `checkpoints/` + `outbox/` + Firebase 导出至离线介质；Master Password 离线保管。
- **首启：** Onboarding 经 WebUI 设 Master Password + 录入凭证 → 生成 `vault.enc` → `setup_webhook_tunnel` 打通入站。

### 7.5 重试、超时与降级矩阵 `[评审补充#R17]` 🆕 新增章节

> 🆕 新增章节：v1.0 未系统定义各外部依赖的重试/超时/降级策略。本节统一矩阵（覆盖审查维度：错误处理完备性）。

| 依赖 | 超时 | 重试 | 降级 / 失败处理 |
| :--- | :--- | :--- | :--- |
| SSH（只读） | 5s 执行 + 30s slot 等待 | 不重试（只读幂等可由 Agent 决定再探） | `EXEC_TIMEOUT`，记 `investigation_gap` |
| SSH（修复 L3） | 30s 执行 | 不重试（修复非幂等，避免重复 restart） | `EXEC_TIMEOUT`，工单记失败 |
| LLM API | 30s/调用 | 429/5xx 指数退避 3 次 | 仍失败 `INTERNAL`，Agent 记 gap；**不跨 vendor 兜底**（`[决策#19]` 固定 vendor） |
| Firebase（写） | 10s | 退避 3 次 | 写本地 outbox 待回放（§6.8）；状态机不回退 |
| Firebase（订阅） | SDK 内置 | SDK 自动离线缓存重连 | 断网时前端用本地缓存，恢复后同步 |
| Telegram 推送 | 10s | 退避 3 次 | **best-effort**：失败不回退工单状态，仅记日志 |
| Cloudflare API | 15s | 幂等操作退避 3 次 | `setup_webhook_tunnel` 失败 `INTERNAL`，不暴露 Token |

**统一原则：**
- 幂等操作（只读探测、CF 配置、Firebase 写带 `record_id`）可重试；非幂等操作（L3 修复）不自动重试，失败交人工。
- 重试上限内返回最终错误码（§5.7）；超限 `INTERNAL`。
- 所有重试记审计日志（含 attempt 次数、最终结果）。

---

## 8. 数据模型详细定义

### 8.1 Firebase Schema

> 🔧 [评审补充#R18] 修改说明：状态枚举与 §6.3 状态机逐项核对一致；新增 `hitl_requests.approval_id`、`consumed_at` 字段支撑一次性消费；新增 `investigations.checkpoint_id` 支撑崩溃恢复。

**Collection `nodes`**（主机元数据）

| 字段 | 类型 | 约束/默认 | 说明 |
| :--- | :--- | :--- | :--- |
| `host_id` | string | PK，`^[A-Za-z0-9_.-]{1,128}$` | 如 `xuejie1.top` |
| `hostname` | string | 必填 | SSH 目标地址/IP |
| `ssh_port` | int | 默认 22 | |
| `services` | array<{name,form}> | form ∈ {systemd,docker,compose} | 纳管服务清单 |
| `last_seen` | timestamp | 自动 | 最近探测时间 |
| `onboarded_at` | timestamp | 必填 | 纳管时间 |
| `topology_baseline` | map | 可空 | 拓扑基线摘要 |

**Collection `investigations`**（调查工单）`[决策#9][决策#15]`

| 字段 | 类型 | 约束/默认 | 说明 |
| :--- | :--- | :--- | :--- |
| `execution_id` | string | PK，UUID | 工单 ID |
| `status` | string | 枚举：PENDING/IN_PROGRESS/HITL_SUSPENDED/COMPLETED/RESOLVED/FAILED_TOKEN_BUDGET/ABORTED | 与 §6.3 一致 |
| `dedup_key` | string | `host_id::service_name` | DOWN 去重键 |
| `mode` | string | 枚举：chat/event | 触发模式 |
| `trigger` | map | 必填 | {type, host_id, service_name, severity, raw} |
| `token_usage` | int | 默认 0，单调递增 | 累计 token，硬上限判定 |
| `token_budget` | int | 默认 512000 | 本次预算上限（`[决策#18]`） |
| `checkpoint_id` | string | 可空 | LangGraph Checkpointer 检查点 ID（崩溃恢复用）`[评审补充#R18]` |
| `created_at` / `updated_at` | timestamp | 自动 | |
| `summary_md` | string | 可空 | 最终报告 |
| `assigned_to` | string | 预留 | 未来多用户 |

**Subcollection `investigations/{id}/records`**（Journal）`[PRD §3.4]`

| 字段 | 类型 | 约束 | 说明 |
| :--- | :--- | :--- | :--- |
| `record_id` | string | PK，UUID（幂等去重） | |
| `record_type` | string | 枚举：symptom/observation/finding/investigation_gap/summary_md | |
| `content` | string | maxLength 16000，已 redaction | |
| `ts` | timestamp | 自动 | 时序索引 |

**Collection `runbooks`**（知识库元数据）`[决策#10]`

| 字段 | 类型 | 约束 | 说明 |
| :--- | :--- | :--- | :--- |
| `document_id` | string | PK | |
| `status` | string | 枚举：DRAFT_PENDING_REVIEW/APPROVED/REJECTED | |
| `service` | string | 可空 | 关联服务 |
| `content_hash` | string | APPROVED 时计算 | 变更检测 |
| `content_ref` | string | 本地文件路径/Chroma id | 正文不全文入 FB |
| `created_at` / `updated_at` | timestamp | 自动 | |

**Collection `hitl_requests`**（HITL 授权）

| 字段 | 类型 | 约束 | 说明 |
| :--- | :--- | :--- | :--- |
| `request_id` / `approval_id` | string | PK | `approval_id` 供 PermissionGate 校验 `[评审补充#R18]` |
| `execution_id` | string | FK→investigations | |
| `target_host` | string | IDENT_RE | |
| `action_type` | string | 枚举 3 项 | |
| `rendered_cmd` | string | 模板渲染预览 | 供人工审阅 |
| `impact` | string | 必填 | 预期影响说明 |
| `status` | string | PENDING/APPROVED/REJECTED/EXPIRED/CONSUMED | 新增 CONSUMED `[评审补充#R18]` |
| `decided_by` / `decided_at` | string/timestamp | 可空 | 审计 |
| `consumed_at` | timestamp | 可空 | 执行时标记，防复用 `[评审补充#R18]` |
| `expires_at` | timestamp | 创建+30min | 超时自动 EXPIRED |

**索引汇总：** `investigations(dedup_key,status)`、`investigations(status,created_at↓)`、`investigations(status,checkpoint_id)`（恢复扫描）、`records(execution_id,ts↑)`、`runbooks(status)`、`hitl_requests(status,created_at)`、`hitl_requests(execution_id,status)` `[评审补充#R3]`。

### 8.2 Chroma 向量库元数据设计

| Collection | document 内容 | metadata | 距离 | 索引 |
| :--- | :--- | :--- | :--- | :--- |
| `runbooks` | SOP 分块文本 | `{runbook_id, status, service, updated_at}` | cosine | HNSW |
| `topology` | 拓扑基线分块 | `{host_id, baseline_at}` | cosine | HNSW |
| `findings` | 历史 finding 摘要 | `{execution_id, host_id, service}` | cosine | HNSW |

- 嵌入维度：384（all-MiniLM-L6-v2，已定稿）；若未来升级 bge-m3/e5 则为 1024（见 §3.6）。
- 检索：`query_runbook` 跨三 collection 各取 top_k，dense + BM25 经 RRF 融合、按 metadata 过滤（如 `runbooks.status==APPROVED` 才返回）。
- 持久化：`/data/chroma`（SQLite + parquet，随卷备份）。

### 8.3 加密数据存储格式（vault.enc）

```
[ magic: 4B "AIOV" ]
[ version: 1B  = 0x01 ]
[ salt: 16B (随机) ]                      # 主密码 PBKDF2 salt
[ iterations: 4B (uint32, = 600000) ]
[ recovery_salt: 16B (随机) ]             # 恢复短语 PBKDF2 salt [决策#17]
[ wrapped_fernet_key: Fernet token ]      # recovery_key 包裹的 Fernet key
[ ciphertext: Fernet token (variable) ]   # Fernet key 加密的 CredentialBundle JSON
```

**CredentialBundle JSON（明文，仅内存）：**
```json
{
  "ssh_keys": {"xuejie1.top": "-----BEGIN OPENSSH PRIVATE KEY-----..."},
  "cf_api_token": "v1.0-...",
  "telegram_bot_token": "123:abc...",
  "llm_providers": {
    "glm":       {"vendor": "openai",    "model": "glm-5.2",       "api_key": "<GLM key>",      "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    "deepseek":  {"vendor": "openai",    "model": "deepseek-v4-pro", "api_key": "<DeepSeek key>", "base_url": "https://api.deepseek.com/v1"},
    "anthropic": {"vendor": "anthropic", "model": "claude-...",    "api_key": "sk-ant-...",     "base_url": "https://api.anthropic.com"},
    "openai":    {"vendor": "openai",    "model": "gpt-...",       "api_key": "sk-...",         "base_url": "https://api.openai.com/v1"}
  },
  "llm_active_provider": "deepseek",
  "webhook_secret": "<随机长串>"
}
```
- 文件权限 `0600`，属主为容器非 root 用户。
- 解密后 `bytes` 持有，`lock()` 丢弃引用（**不声称物理清零**，见 §3.7 安全声明）；明文 JSON 仅在 `get()` 短暂构造后丢弃。
- Fernet token 自带 HMAC 认证，篡改可检测；`salt`/`iterations` 被篡改将导致派生失败（无法解密），天然防降级攻击。


---

## 9. 风险、权衡与开放问题

### 9.1 已知风险与缓解

| 风险 | 影响 | 缓解 |
| :--- | :--- | :--- |
| LLM 幻觉构造危险命令 | 误操作业务节点 | 命令白名单 + 模板，Agent 无 shell 拼接能力；L3 强制 HITL + `approval_id` 一次性消费 |
| Master Password 遗忘 | 凭证锁定 | 24 词恢复短语解锁并重设 `[决策#17]`；助记词离线保管；主密码+助记词双失则不可恢复 |
| CF Tunnel 中断 | 无法接收 Webhook | Uptime Kuma 自身保留直连通知通道作为兜底；`cloudflared` 自动重连 |
| Firebase 出网依赖 | 网络断时前端实时性丢失 | Firebase 离线缓存；Bastion 本地仍可执行调查，恢复后同步；写失败落本地 outbox 回放 |
| Token 成本失控 | 云端账单飙升 | 四道闸（事前估算 + Provider 硬限 + 事后累计 + 滑动窗口）；日志截断；RAG 命中减探测；采用国产低价模型（GLM/DeepSeek）单次约 2~3 RMB，风险进一步降低 |
| Chroma 冷启动 RAG 弱 | 早期排查质量低 | 接受；依赖通用推理；混合检索（BM25+dense）缓解 MiniLM 语义短板；随事件沉淀逐步增强 |
| MiniLM 对运维日志语义弱 | RAG 召回率低 | 已定稿混合检索（BM25+dense）兜底；机器升级后可换 bge-m3/e5（见 §3.6） |
| Bastion 单点故障 | 无 HA，宕机即全停 | 1~3 节点规模接受单点；Docker `restart: unless-stopped` + 卷持久化；崩溃恢复见 §11 |
| Webhook 伪造 / 重放 | 伪造告警触发调查 | 端点校验共享密钥 + 时间戳；CF Access SSO；密钥可经 `update_credential` 轮转 |
| 进程崩溃致调查悬挂 | 工单永久 IN_PROGRESS / HITL 挂起 | 持久化 Checkpointer + 启动 Recovery Sweep + HITL 超时清扫器（§6.8/§11） |
| Redaction 被编码绕过 | 凭证经 LLM 出网 | 原始日志不出网（本地摘要优先），Redaction 仅兜底（§4.5） |
| 去重 race | 重复建单 / 重复调查 | Firestore 事务原子去重（§6.4） |
| Python 内存无法物理清零 | 凭证残留内存 / swap | 凭证驻留最小化 + 禁 core dump + 进程隔离（已定稿基础加固，§3.7） |
| SSH slot 耗尽静默排队 | 调查卡顿无反馈 | 排队超时 + 队列深度告警 + 连接健康检查（§7.1） |

### 9.2 设计权衡记录

| 权衡点 | 选择 | 牺牲了什么 | 理由 |
| :--- | :--- | :--- | :--- |
| 云端 LLM vs 本地 | 云端 `[决策#1]` | 运维数据出网 | 获取强推理/工具调用能力；接受最小化出网 |
| Firebase vs 自托管 | Firebase `[决策#5]` | 数据出网到 Google | 实时同步与 Auth 开发效率 |
| LangGraph vs 纯 LangChain | LangGraph | 学习曲线 | 原生 HITL interrupt、状态机、步骤级 token 计量、持久化 Checkpointer |
| 本地嵌入 vs 云端嵌入 | 本地 MiniLM + BM25（已定稿） | 嵌入质量略低 | 零 API 成本、文本不出网、契合从零沉淀；BM25 关键词补足语义短板 |
| 结构化模板 vs 通用 shell | 模板+白名单 `[决策#8]` | L3 动作灵活性 | 安全第一，可枚举动作即满足运维修复需求 |
| 单用户 HITL | 防误触确认（非权限隔离，已定稿）`[决策#12]` | 真正权限分离 | 单用户既定前提；未来可加 step-up 口令增强（§6.7） |
| 事后预算 vs 事前评估 | 四道闸 | 实现复杂度 | 单纯事后检查无法阻止超支，须事前估算 + Provider 硬限 |

### 9.3 需产品侧进一步确认的事项

> **已确认**（详见正文与 PRD §8 决策记录）：前端 React `[决策#20]`、Token 默认 512k `[决策#18]`、Master Password 恢复短语 `[决策#17]`、Provider 固定不自动路由 `[决策#19]`、自定义 Base URL `[决策#16]`、Webhook 共享密钥 header `[决策#21]`、拓扑基线自动入库 `[决策#22]`。

**本次评审新增的待确认事项（共 6 项，已全部确认定稿，详见正文 `✅ [已定稿]` 标注）：**

| 编号 | 主题 | 位置 | 定稿结论 |
| :--- | :--- | :--- | :--- |
| ① | HITL 单用户增强策略 | §6.7 | **防误触确认（A）**：同账号二次点击，不引入 step-up 口令 / 多用户 RBAC |
| ② | 嵌入模型与检索策略 | §3.6 | **MiniLM + BM25 混合检索（A）**：机器 2C4G，不升级大模型 |
| ③ | Bastion 机器规格 | §11 | **2C4G 轻量 NAS**（如 N100/J4125） |
| ④ | Token 成本上限 | §6.6 | **维持 512k/次（A）**：采用 GLM-5.2 / DeepSeek-V4-Pro 等国产低价模型，成本非约束 |
| ⑤ | L3 动作可扩展性 | §11 | **维持 3 枚举 + 预留接口（A）**：不引入自定义脚本白名单 |
| ⑥ | OS 级加固深度 | §3.7 | **基础加固（A）**：禁 coredump + 进程隔离 + 凭证驻留最小化，不引入 mlock/seccomp |

> 🆕 [v1.2 新增] Provider 厂商扩展：用户确认采用国产低价模型，§3.5 / §8.3 已补充 **GLM-5.2 / DeepSeek-V4-Pro** 经 OpenAI 兼容端点接入的说明（vendor="openai" + 自定义 base_url），符合 `[决策#16]` 自定义 Base URL，不违背 `[决策#19]` 固定单一 vendor。

---

## 10. 实施建议

### 10.1 与 PRD Milestone 的映射

| PRD Milestone | 本设计交付物 |
| :--- | :--- |
| M1 基建与控制台 | §2.4 信任边界、§3.1 前端 Onboarding/Dashboard、§3.7 Vault、§7.4 Docker Compose + cloudflared |
| M2 探测网关与工具链 | §3.4 执行引擎、§5.2/5.3 工具、§4.2 白名单+正则+远端硬化、§5 日志截断 |
| M3 Agent 大脑接入 | §3.5 LangGraph + Provider + Checkpointer、§3.3 MCP Server、§6.1 Chat 流、§8.1 investigations/records |
| M4 Webhook 全自动闭环 | §6.2 事件流、§6.3 状态机、§6.4 去重事务、§6.6 Token 四道闸、Telegram 推送 |
| M5 知识库自进化 | §3.6 Chroma + 混合检索、§5.5 query_runbook、§8.2 向量元数据、SOP 审核面板 |
| 跨里程碑 | §5.7 错误码规约、§7.5 重试矩阵、§11 灾难恢复（建议 M3 起逐步落地） |

### 10.2 优先开发路径建议

1. **安全地基先行：** Vault（§3.7，含异步化 + 热更新）+ 执行引擎白名单/模板（§3.4）+ 注入测试（§4.3）——安全红线未落地前不接生产。
2. **MCP 工具骨架：** 先 L1/L2 只读工具，验证 SSH 链路与截断；L3 模板 + HITL + `approval_id` 校验随后。
3. **Agent 最小闭环：** Sync Chat 单工具调用 + Journal 写入 + 持久化 Checkpointer，再扩到多步规划。
4. **崩溃恢复：** Checkpointer + Recovery Sweep 与最小闭环同步落地，避免上线即悬挂。
5. **事件模式：** 在 Chat 稳定后接 Webhook + 去重事务 + Token 四道闸。
6. **RAG 最后：** 工具与 Agent 稳定后接入 Chroma + 混合检索与审核面板。

### 10.3 测试策略细化 `[评审补充#R19]`

| 测试类别 | 内容 | 通过标准 |
| :--- | :--- | :--- |
| 注入对抗（§4.3） | 8 类 payload + asyncssh shlex.quote 行为验证 + approval_id 复用 | 全部被拒；shlex 转义生效；复用被拒 |
| 白名单边界 | 白名单外动词、超长标识符、枚举外 action_type、reboot | 拒绝；L3 `reboot` 拒绝 |
| Token 熔断（§6.6） | 构造超长日志/循环排查触发事前评估与事后超限 | 工单置 `FAILED_TOKEN_BUDGET`，附 `investigation_gap`，图终止；事前评估先于事后触发 |
| 事件语义（§6.4） | 重复 DOWN 合并、UP 标记 RESOLVED、并发 DOWN race | 去重正确，事务下至多建一单，状态转换符合状态机 |
| HITL 流程（§6.7） | approve/reject/超时/approval_id 复用 | 各分支状态与审计记录正确；L3 未审批绝不执行；复用拒绝 |
| 崩溃恢复（§6.8/§11） | 模拟进程崩溃于 IN_PROGRESS / HITL_SUSPENDED 各阶段 | 重启后 Recovery Sweep 正确恢复或 ABORTED；无悬挂工单 |
| 数据一致性 | SSH 已执行但 FB 写失败 | outbox 回放成功；record_id 幂等去重；状态机不回退 |
| 断网/状态同步 | 出网中断、CF 重连、Firebase 离线缓存 | 调查可继续，恢复后同步，无数据丢失 |
| 沙盒演练 | 本地 Docker 靶机集群模拟服务宕机 | 端到端闭环：DOWN→探测→根因→HITL→修复→报告 |
| 凭证安全 | 日志/LLM 上下文/Telegram 报告 redaction；原始日志不出网 | 无凭证模式泄漏；LLM 上下文无原始日志全文 |
| Vault 异步 | unlock 期间并发 SSE/Webhook/SSH 不阻塞 | 事件循环无卡顿；PBKDF2 在线程池执行 |
| 可测试性架构（§10.4） | MCP Server in-process mock、SSH/LLM 录制回放 | CI 无真实外部依赖即可跑集成测试 |

### 10.4 可测试性架构 `[评审补充#R20]` 🆕 新增章节

> 🆕 新增章节：v1.0 未说明 MCP Server 作为 stdio 子进程如何在 CI 中 mock SSH 与 LLM 做集成测试。

- **MCP Server 进程内测试：** 通过 `langchain-mcp-adapters` 的 in-process 模式直接加载 MCP Server 协程，绕过 stdio 子进程边界，断言工具契约与权限分级。
- **ExecutionEngine 替身：** 抽象 `SSHExecutor` 接口，测试注入 `FakeSSHExecutor`（按预设脚本返回 status/logs），无需真实 SSH。
- **LLM 录制回放：** Provider 抽象层支持“录制/回放”模式——首次跑通时录制真实 LLM 响应（含 usage），CI 回放避免真实调用与成本；支持人工注入故障响应（429/超时）验证重试与降级。
- **Firebase 模拟器：** CI 用 Firebase Emulator Suite 提供本地 Firestore，验证事务去重、安全规则、onSnapshot。
- **端到端沙盒：** 本地 Docker 靶机集群（systemd/docker/compose 各一）模拟服务宕机，跑完整 DOWN→修复闭环。
- **LangGraph Checkpointer 测试：** 用 SQLite Checkpointer 验证 interrupt/resume 与崩溃恢复语义。

**CI 门禁：** Ruff + Flake8 + mypy 全过；pytest 含注入、熔断、崩溃恢复、事务去重套件全过；方可合并。

---

## 11. 灾难恢复与崩溃后状态恢复 `[评审补充#R21]` 🆕 新增章节

> 🆕 新增章节：v1.0 无灾难恢复章节。本节覆盖审查维度“状态机死锁”“数据一致性”的恢复侧，定义进程崩溃、数据损坏、凭证失效三类场景的恢复流程。

### 11.1 进程崩溃恢复

**前提：** LangGraph 配置持久化 Checkpointer（默认 SQLite `/data/checkpoints.sqlite`，卷持久化）。每个 investigation 的图状态（含 `token_usage`、待执行节点、interrupt 上下文）按检查点持久化。

> 🔧 [spike-01/04 修订] 修改说明：**Checkpointer 后端选型明确为 `AsyncSqliteSaver`。** spike 01 用同步 `SqliteSaver` 验证了 interrupt/resume 崩溃恢复语义（只读不重放、L3 不重复执行）；但 spike 04 暴露 `create_react_agent` 走**异步路径**，同步 `SqliteSaver` 在异步调用链里会触发 `InvalidStateError`（"Synchronous calls to AsyncSqliteSaver are only allowed from a different thread"）。故：
> - **生产实现（异步路径）**：用 `AsyncSqliteSaver`（`langgraph.checkpoint.sqlite.aio`，依赖 `aiosqlite`），全链路 `ainvoke`/`aget_state`/`aget_state_history`，**禁止混用同步 `get_state`**（会触发 `InvalidStateError`）。
> - **纯同步测试图（如 spike 01）**：可用同步 `SqliteSaver`，仅限测试场景。
> Checkpointer 检查点粒度经 spike 01 验证为"工具调用边界"，崩溃至多重放一步只读探测，与下文定稿一致。

**Recovery Sweep（启动时执行，§6.8）：**
1. 加载 Checkpointer，枚举未完成图。
2. 扫描 Firebase `investigations` 中 `status ∈ {PENDING, IN_PROGRESS, HITL_SUSPENDED}`。
3. 按 §6.8 决策表恢复或 ABORTED。
4. 扫描 `hitl_requests(PENDING)`，过期者置 `EXPIRED` 并驱动工单 `→ ABORTED`。

> ✅ [已定稿] IN_PROGRESS 崩溃恢复策略：**从最近检查点 resume 重放（方案 A）**（用户确认）。Checkpointer 检查点粒度为“每个工具调用边界”，崩溃至多重放一步只读探测，只读幂等可接受。L3 修复非幂等，但 L3 调用必先经 interrupt 挂起（状态 HITL_SUSPENDED），不会在 IN_PROGRESS 阶段直接执行，故重放不会重复修复。

### 11.2 数据损坏恢复

| 损坏对象 | 影响 | 恢复 |
| :--- | :--- | :--- |
| `vault.enc` 损坏 | 无法 unlock | 从离线冷备恢复 `vault.enc`；仍需 Master Password 或恢复短语 unlock |
| Chroma 损坏 | RAG 不可用 | `query_runbook` 返回空（降级为通用推理）；从 `chroma/` 冷备恢复或由 `runbooks`/`topology` 元数据重建索引 |
| Checkpointer 损坏 | 调查无法 resume | 工单 `→ ABORTED`，记 `investigation_gap: checkpoint corrupted`；不影响新调查 |
| Firebase 数据丢失 | 工单/日志丢失 | 从 Firebase 导出恢复；本地 outbox 回放未同步写入 |

### 11.3 凭证失效恢复

| 场景 | 恢复 |
| :--- | :--- |
| LLM API Key 过期 | `update_credential` 热更新（§4.6），无需重新 Onboarding |
| SSH 私钥被业务节点撤销 | `update_credential` 更新 + 节点侧 `authorized_keys` 同步 |
| Master Password 遗忘 | `unlock_with_recovery(助记词)` + `rotate_master_password` |
| Master Password + 助记词双失 | **不可恢复**——`vault.enc` 内凭证永久丢失，须重新 Onboarding 录入全部凭证（业务节点需重新发放 SSH 私钥等） |

### 11.4 RPO / RTO（单节点既定）

- **RPO：** 接受最近一个检查点之后的状态丢失（最多一步只读探测）。
- **RTO：** `restart: unless-stopped` 自动重启 + Recovery Sweep，目标 < 2min；冷备恢复为人工操作，RTO 视操作而定。
- **HA：** 1~3 节点规模接受单点，不提供 HA（`[决策#3]`）；通过自动重启 + 持久化卷 + 冷备兜底可用性。
- **机器规格（已定稿）：** 2C4G 轻量 NAS（如 N100/J4125）。PBKDF2 600k 解锁约 1~2s（已异步化，不阻塞事件循环）；MiniLM+BM25 本地推理无压力；并发上限维持 SSH=4 / 调查=3。

### 11.5 备份策略

| 对象 | 频率 | 介质 | 说明 |
| :--- | :--- | :--- | :--- |
| `vault.enc` | 每次凭证变更后 + 每日 | 离线冷备 | 加密态，Master Password 离线保管 |
| `chroma/` | 每日 | 离线冷备 | 可由元数据重建，非关键 |
| `checkpoints/` | 每日 | 离线冷备 | 损坏可接受 ABORTED |
| `outbox/` | 持续 | 本地 | 回放后清理 |
| Firebase 导出 | 每日 | 离线冷备 | 工单/日志/拓扑元数据 |

> 🔧 [评审补充#R21] 修改说明：本节为 v1.0 缺失的灾难恢复章节，统一回应崩溃悬挂、数据一致性补偿、凭证失效三类场景。

---

**—— 文档结束（v1.2 评审定稿版）——**

**6 项待确认事项已全部定稿：** 见 §9.3 表格。本文为最终设计基线，可进入实施。
