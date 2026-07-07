# AIOps-Bastion 详细设计文档 (Detailed Design Document)

---

## 1. 文档信息

| 项 | 内容 |
| :--- | :--- |
| 项目名称 | AIOps-Bastion：基于 MCP 与 RAG 的多节点智能运维堡垒机 |
| 文档版本 | 1.0（Detailed Design） |
| 设计日期 | 2026-07-04 |
| 设计依据 | AIOps-Bastion 需求分析与架构设计说明书 (PRD) v1.0 |
| 作者 | SRE + 全栈架构师 |
| 状态 | Draft（待评审） |

**修订记录**

| 版本 | 日期 | 修订人 | 说明 |
| :--- | :--- | :--- | :--- |
| 1.0 | 2026-07-04 | 架构组 | 首版详细设计，对齐 PRD 全部 15 项决策 |

**决策追溯约定：** 本文每个关键设计点以 `[PRD §X.Y]` 或 `[决策#N]`（N 对应 PRD §8 决策记录表序号）标注来源，确保可双向追溯。

---

## 2. 系统架构总览

### 2.1 高层次架构图

```mermaid
flowchart TB
    subgraph CloudNet["云端（出网边界外）"]
        UK["Uptime Kuma<br/>(用户自托管监控)"]
        LLM["LLM 厂商 API<br/>Anthropic Claude / OpenAI GPT"]
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
    CFDB -->|"3 本地回环"| BASTION
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
- Bastion 本机防火墙 `DROP` 一切主动入站；唯一入站路径是 `cloudflared` 通过本地回环（127.0.0.1）回源到 FastAPI 的 Webhook 端点。

### 2.3 技术栈最终确认表

| 层 | 选型 | 版本/说明 | PRD 依据 |
| :--- | :--- | :--- | :--- |
| 后端语言 | Python | 3.11+（类型提示、asyncio 原生） | §2 |
| Web 框架 | FastAPI + Uvicorn | 单 worker，承载 REST + SSE + Webhook | §3.1 |
| Agent 编排 | LangGraph | 原生支持 HITL interrupt、状态机、步骤级 token 计量 | §3.3 |
| LLM Provider | LangChain + `langchain-anthropic` / `langchain-openai` | Provider 抽象层可切换 Claude/GPT | 决策#2 |
| MCP | `mcp` 官方 Python SDK | stdio 传输，子进程方式被 Agent 加载 | §2 |
| MCP↔LangChain 桥 | `langchain-mcp-adapters` | 将 MCP 工具包装为 LangChain Tool | §3.2 |
| SSH | asyncssh | 异步并发，受限命令执行 | §2 |
| 加密 | cryptography（Fernet + PBKDF2HMAC） | 凭证加密落盘 | §3.1 FR1.2 |
| 向量库 | Chroma（嵌入式，持久化到卷） | 本地零依赖 | 决策#10 |
| 嵌入模型 | sentence-transformers `all-MiniLM-L6-v2` | 本地推理，零 API 成本，文本不出网 | §3.5 |
| 实时 DB/Auth | Firebase（Firestore + Auth） | 工单/日志/拓扑实时同步 | 决策#5 |
| 前端 | React 18 + TypeScript + Vite | 已确认 `[决策#20]` | §3.1 |
| 前端 UI | TailwindCSS + shadcn/ui + TanStack Query + Zustand | 现代化仪表盘 | §3.1 |
| 容器编排 | Docker Compose | bastion-app + cloudflared 两服务 | 决策#11 |
| CI | GitHub Actions | Ruff + Flake8 + mypy + pytest | §4.3 |

### 2.4 数据流与信任边界图

```mermaid
flowchart TB
    subgraph VaultZone["凭证隔离区（绝不出网）"]
        V["Vault: Master Password → PBKDF2 → Fernet Key<br/>SSH 私钥 / CF Token / TG Token / LLM API Key"]
    end
    subgraph EgressZone["出网白名单区（受控）"]
        E1["→ Anthropic/OpenAI API：仅排查日志/拓扑片段"]
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
    FBWrite["Firebase 写入"] --> E2
    TGPush["报告推送"] --> E3
    I1 --> WH["Webhook 处理器"]
```

**信任边界三原则 `[PRD §4.1]`：**
1. **凭证隔离：** Master Password 及其派生密钥、所有原始凭证**绝不离开 Bastion**，仅运行期内存解密使用。
2. **出网最小化：** Bastion 仅允许出站到上表 4 个白名单目的；LLM 仅接收经截断的排查日志/拓扑片段，不接收任何凭证。
3. **入站唯一性：** 唯一入站是经 CF Tunnel 的 Uptime Kuma Webhook；Bastion 无任何对公网监听的端口。

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
    ORCH --> LLM[LLM Provider 层<br/>Claude/GPT]
    API --> VLT[Vault 凭证管理]
    EXEC -.->|取 SSH 私钥| VLT
    LLM -.->|取 API Key| VLT
```

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
| HITL 审批 | `ApprovalModal`（显示 action_type/目标/影响） | Firebase `hitl_requests` 订阅 |
| SOP 审核 | `RunbookReviewQueue` / `RunbookEditor` | Firebase `runbooks` |

**实时更新机制：**
- 工单状态/进度：`onSnapshot(doc(investigations, id))`。
- Journal Records：`onSnapshot(collection(investigations, id, records))`，按 `ts` 排序流式追加。
- 思维链（CoT）：`EventSource('/api/v1/chat/{id}/stream')`，后端按 Agent 步骤推送 `step` / `tool_call` / `tool_result` / `token_delta` 事件。

**与其他模块交互：** 经 REST 调后端发起 Chat / 审批 / 节点纳管；经 Firebase 直读工单与日志；经 SSE 接收思维链。

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

**索引建议：**
- `investigations`：`dedup_key` 升序 + `status` 升序（去重查询）；`status` + `created_at` 降序（工单列表）。
- `records`：`execution_id` + `ts` 升序（日志时序）。
- `runbooks`：`status` 升序（审核队列）。
- `hitl_requests`：`status`（PENDING）+ `created_at`。

**与其他模块交互：** Agent 经 `FirebaseWriter` 写工单/日志；前端经 SDK 订阅；HITL 模块读写 `hitl_requests`。

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
        Tool(name="setup_webhook_tunnel", description="...",
             inputSchema={"type": "object", "properties": {}}),          # L0
        Tool(name="execute_discovery", description="...",
             inputSchema=DISCOVERY_SCHEMA),                              # L1
        Tool(name="fetch_service_logs", description="...",
             inputSchema=LOGS_SCHEMA),                                   # L2
        Tool(name="submit_journal", description="...",
             inputSchema=JOURNAL_SCHEMA),                                # L2
        Tool(name="query_runbook", description="...",
             inputSchema=RUNBOOK_SCHEMA),                                # L2
        Tool(name="execute_remediation", description="...",
             inputSchema=REMEDIATION_SCHEMA),                            # L3
    ]
```

**权限分级实现（PermissionGate）：**

```mermaid
flowchart TB
    Call["Agent 调用工具"] --> Gate{"PermissionGate"}
    Gate -->|"L0/L1/L2"| Audit["审计日志"] --> Exec["直接执行"]
    Gate -->|"L3"| Suspend["挂起 + 写 hitl_requests(PENDING)"]
    Suspend --> Wait["等待人工审批"]
    Wait -->|"approve"| Exec
    Wait -->|"reject / 超时"| Fail["返回 ABORTED"]
    Exec --> FB["写 Firebase + 返回结果"]
```

**JSON-RPC 接口契约：** 统一遵循 MCP 协议 `tools/call`；输入/输出 JSON Schema 详见 §5。所有工具返回结构统一为 `{ok: bool, data?: ..., error?: {code, message}}`。

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

    async def run_readonly(self, host: str, cmd: ReadonlyCommand) -> ExecResult:
        """L1/L2 只读：走白名单命令构建器，5s 超时。"""

    async def run_remediation(self, host: str, action: RemediationAction) -> ExecResult:
        """L3 修复：走硬编码模板，参数经正则校验后代入，5s 超时。"""
```

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

> **关键安全细节：** asyncssh `run()` 最终由远端用户 shell 解析命令字符串。因此防护不是“避免 shell”，而是**保证任何 shell 元字符永远无法进入参数**——`IDENT_RE` 仅允许字母数字与 `_.-`，`;`、`|`、`&`、`$`、反引号、`()`、换行等全部被正则在 fullmatch 阶段拒绝。命令以 `list[str]` 形式交给 asyncssh，进一步降低拼接风险。

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
    Idle --> Acquiring: 调用(run_readonly/run_remediation)
    Acquiring --> Connected: 获取信号量 + 复用/新建 SSH
    Acquiring --> Failed: 连接失败
    Connected --> Executing: 下发命令(list argv)
    Executing --> Done: 5s 内返回
    Executing --> TimedOut: 超过 5s 全局熔断
    Done --> Idle
    TimedOut --> Idle: 记录超时 + 释放连接
    Failed --> Idle: 返回错误
```

**与其他模块交互：** 从 Vault 取 SSH 私钥（内存解密）；被 MCP Server 的 `execute_discovery` / `fetch_service_logs` / `execute_remediation` 调用；结果回传 MCP Server。

### 3.5 Agent 核心（LangGraph + LangChain Provider）

**职责与边界：** 推理与编排中枢。接收自然语言或事件触发，规划步骤，调用 MCP 工具，输出结构化 Journal，控制 Token 预算与 HITL 中断。**不直接**执行 SSH，所有副作用经 MCP 工具。

**Provider 抽象层 `[决策#2]`：**

```python
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

def build_llm(cfg: ProviderConfig):
    if cfg.vendor == "anthropic":
        return ChatAnthropic(model=cfg.model, api_key=cfg.api_key,
                             base_url=cfg.base_url, temperature=0)
    if cfg.vendor == "openai":
        return ChatOpenAI(model=cfg.model, api_key=cfg.api_key,
                          base_url=cfg.base_url, temperature=0)
    raise ValueError(cfg.vendor)
```
API Key 与自定义 Base URL 从 Vault 内存解密获取，不落日志。`base_url` 支持自定义反代/中转网关，Anthropic/OpenAI 可分别配置 `[决策#16]`；为空则用官方端点。`temperature=0` 降低运维场景幻觉。

**SRE 人设 Prompt 框架：**

```
[System]
你是 AIOps-Bastion 的 SRE Agent。严格遵守：
1. 仅通过提供的 MCP 工具操作，绝不构造或猜测 shell 命令。
2. 只读探测可自主进行；任何修复动作必须调用 execute_remediation 并等待人类授权。
3. 每个调查阶段必须产出 Journal Record（symptom/observation/finding/investigation_gap/summary_md）。
4. 日志可能被截断，需在 investigation_gap 记录盲区。
5. 受 Token 预算约束，优先低成本探测路径。
6. 不得在 any 输出中包含凭证、私钥、Token。
```

**LangGraph 工作流（双模式共享同一图，入口不同） `[决策#6]`：**

```mermaid
flowchart TB
    Start([入口]) --> Plan[规划步骤]
    Plan --> ToolCall{调用 MCP 工具}
    ToolCall -->|L0/L1/L2| Exec[执行]
    ToolCall -->|L3| HitlInterrupt[LangGraph interrupt: 写 hitl_requests]
    HitlInterrupt --> Wait{等待审批}
    Wait -->|approve| Exec
    Wait -->|reject| EndAbort([ABORTED])
    Exec --> Journal[submit_journal]
    Journal --> Budget{Token 超限?}
    Budget -->|否| Done{调查完成?}
    Budget -->|是| EndBudget([FAILED_TOKEN_BUDGET])
    Done -->|否| Plan
    Done -->|是| Summary[生成 summary_md]
    Summary --> End([COMPLETED])
```

- **Sync Chat 入口：** 用户消息触发，`Plan` 起点带上对话上下文，CoT 经 SSE 流式回前端。
- **Event-driven 入口：** Webhook 触发，`Plan` 起点带告警上下文（host/service/DOWN），后台执行 5~15 分钟，结束生成 Markdown 报告并推送 Telegram。
- **HITL 中断：** L3 工具调用经 LangGraph `interrupt` 挂起，状态持久化；前端审批后 `Command(resume=...)` 恢复。

**Tool Calling 设计：** 经 `langchain-mcp-adapters` 将 MCP 工具转为 LangChain `Tool`，Agent 通过原生 tool calling 选择工具。工具的权限等级对 Agent 透明——Agent 只看到工具签名，**强制约束在 MCP Server 侧 PermissionGate 执行**，不依赖 Agent 自觉。

**与其他模块交互：** 经 MCP Client 调工具；经 FirebaseWriter 写工单/日志；经 SSE 推 CoT；从 Vault 取 LLM API Key。

### 3.6 RAG 知识库模块（Chroma）

**职责与边界：** 本地嵌入式向量库，存储 SOP、拓扑基线、历史 finding，供 `query_runbook` 检索。**纯从零沉淀**，不导入存量文档 `[决策#10]`。嵌入模型本地推理，文本不出网。

**Chroma 集成：**
- 持久化路径：`/data/chroma`（Docker 卷）。
- 嵌入函数：`chromadb.utils.embedding_functions.SentenceTransformerEmbeddingFunction("all-MiniLM-L6-v2")`，首次启动本地下载，之后离线运行。

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

**检索语义：** `query_runbook(query_string)` → 本地嵌入 query → top-k（默认 5）检索三 collection → 合并去重 → 返回带元数据的片段给 Agent。冷启动期库为空，返回空结果，Agent 依赖通用推理 `[决策#10]`。

**与其他模块交互：** 被 MCP `query_runbook` 调用；拓扑探索被 `execute_discovery` 间接驱动；SOP 审核状态由前端 + Firebase 驱动。

### 3.7 凭证管理系统（Vault）

**职责与边界：** 生成、加密、解密、销毁全部凭证。Master Password 为根信任，**绝不落盘、绝不出网** `[PRD §4.1]`。

**派生与加密：**
- Master Password → `PBKDF2HMAC(algorithm=SHA256, length=32, salt=vault.salt, iterations=600000)` → Fernet key。
- Fernet key 加密凭证束（JSON：SSH 私钥们、CF API Token、Telegram Bot Token、LLM API Key×2）。
- 密文 + salt + iterations 写入 `vault.enc`（Docker 卷，定期备份）。

**关键类：**

```python
class Vault:
    def __init__(self, path: Path): self._path = path; self._key: bytes | None = None

    def initialize(self, master_password: str, bundle: CredentialBundle) -> str:
        """派生 Fernet key 加密 bundle；生成 24 词 BIP-39 恢复短语，
        用其派生 recovery_key 包裹 Fernet key。返回 mnemonic（仅显示一次）。"""
        ...

    def unlock(self, master_password: str) -> None:
        salt, iters, ct = read_header(self._path)
        key = PBKDF2HMAC(..., salt=salt, iterations=iters).derive(master_password.encode())
        self._key = key  # 仅内存

    def unlock_with_recovery(self, mnemonic: str) -> None:
        """遗忘主密码时：用恢复短语派生 recovery_key，解包 Fernet key 解锁。"""
        ...

    def rotate_master_password(self, new_master: str) -> None:
        """解锁后重设主密码（重新派生 + 重包裹 Fernet key）。"""

    def get(self, name: str) -> str:
        if self._key is None: raise VaultLockedError()
        return decrypt(self._key, ct)[name]

    def lock(self) -> None:
        zeroize(self._key); self._key = None   # 内存清零
```

**恢复短语机制 `[决策#17]`：** 初始化时生成 24 词 BIP-39 助记词，由 `PBKDF2(mnemonic, recovery_salt)` 派生 `recovery_key`，用它**包裹**主密码派生的 Fernet key（`wrapped_fernet_key`）落盘。助记词**仅在 Onboarding 显示一次**，用户须离线保管；系统不存储明文助记词。遗忘主密码时，`unlock_with_recovery(mnemonic)` 解包 Fernet key 解锁，随后 `rotate_master_password` 设新主密码。

**生命周期状态机：**

```mermaid
stateDiagram-v2
    [*] --> Uninitialized
    Uninitialized --> Locked: initialize(首次设主密码 + 录入凭证)
    Locked --> Unlocked: unlock(输入主密码, 派生密钥)
    Unlocked --> Locked: lock(内存清零)
    Unlocked --> Locked: 进程退出/重启
    Locked --> Unlocked: unlock
    Locked --> Unlocked: unlock_with_recovery(助记词, 遗忘主密码时)
```

**运行期内存管理：**
- Master Password 解密后以 `bytes` 持有，`lock()`/进程退出时 `zeroize`（`ctypes.memset` 覆写）。
- 凭证按需 `get()`，调用方用完即弃，不缓存到长生命周期对象。
- 日志/异常信息强制过滤凭证字段（structlog processor redaction）。

**与其他模块交互：** 执行引擎取 SSH 私钥；LLM 层取 API Key；CF 隧道配置取 CF Token；Telegram 推送取 Bot Token。所有取用均在 Bastion 进程内，不出网。

---

## 4. 安全设计（重中之重）

### 4.1 凭证生命周期管理 `[PRD §4.1][决策#1]`

```mermaid
flowchart LR
    Gen["生成: Onboarding 录入"] --> Enc["加密: PBKDF2 派生 Fernet Key, 加密凭证束"]
    Enc --> Disk["落盘: vault.enc (salt+iters+密文)"]
    Disk --> Use["使用: 运行期 unlock, 内存解密, 按需 get()"]
    Use --> Dest["销毁: lock() 内存清零 / 进程退出"]
```

| 阶段 | 规则 |
| :--- | :--- |
| 生成 | 仅 Onboarding 一次；Master Password 由用户掌握，系统不存储、不传输 |
| 加密 | PBKDF2(SHA256, 600k iters) + Fernet；salt 随机 16B，每实例唯一 |
| 落盘 | 仅密文 + salt + iters 入 `vault.enc`；文件权限 0600，属主容器用户 |
| 使用 | 仅 `Vault.unlock()` 后内存态可用；调用方按需 `get()`，不缓存 |
| 销毁 | `lock()` 或进程退出即 `zeroize` 内存；`vault.enc` 保留（重启需重新 unlock） |

**根信任失效处理：** Master Password 遗忘时，可用 **24 词恢复短语**（BIP-39）解锁并重设主密码 `[决策#17]`；恢复短语仅在 Onboarding 显示一次，离线保管。若主密码与恢复短语同时遗失，则凭证不可恢复。

### 4.2 指令安全模型 `[决策#7][决策#8]`

| 层级 | 机制 | 实现要点 |
| :--- | :--- | :--- |
| L0 基建 | 受控编排 | `setup_webhook_tunnel` 仅用预置 CF Token 调 Cloudflare API，不接受外部命令参数 |
| L1 探测 | 白名单 + 参数正则 | `ALLOWED_READONLY` 封闭动词集；`IDENT_RE` fullmatch 校验 unit/container 名 |
| L2 日志 | 白名单 + 长度截断 | `journalctl`/`docker logs` 走白名单；Server 端按 `lines` 与 token 上限截断 |
| L3 修复 | 硬编码模板 + HITL | `action_type` 仅 3 枚举；`clear_cache` 路径白名单；强制 `interrupt` 等待审批 |

**参数校验正则（核心）：**
- 标识符：`IDENT_RE = ^[A-Za-z0-9_.-]{1,128}$`（fullmatch，拒绝一切 shell 元字符）。
- 元字符拒绝集：`;` `|` `&` `>` `<` `$` 反引号 `(` `)` `\n` `\r` `\\` `'` `"` —— 任一出现即 `CommandValidationError`。
- `clear_cache` 路径：必须**精确匹配** `CLEAR_CACHE_PATH_WHITELIST` 集合成员，非前缀匹配。

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

并验证：L1/L2 任何白名单外动词（如 `rm`、`curl`、`bash`）直接拒绝；L3 模板渲染产物为纯 `list[str]` argv，无 shell 拼接。

### 4.4 Zero Trust 与网络隔离（Cloudflare Tunnel 配置）

**Bastion 主机防火墙（nftables/ufw 策略）：**
- 默认 `DROP` 所有入站。
- 允许出站仅到白名单：Anthropic/OpenAI API、Firebase、Telegram API、Cloudflare API。
- 允许 SSH 出站到业务节点（带外管理网段）。

**`cloudflared` 配置（Docker Compose 常驻服务） `[决策#11]`：**
```yaml
# cloudflared config.yml
tunnel: <TUNNEL_ID>
credentials-file: /etc/cloudflared/creds.json
ingress:
  - hostname: bastion.<your-domain>.top
    service: http://127.0.0.1:8080   # 回源到 FastAPI Webhook 端点
    path: /api/v1/webhook/uptime-kuma
  - service: http_status:404
```
- `cloudflared` 以**出站**长连接到 Cloudflare 边缘，Bastion 无需任何公网监听端口。
- Webhook 端点校验**共享密钥自定义 header** `[决策#21]`：Uptime Kuma 侧配置 header `X-Webhook-Secret: <随机长串>`（密钥存于 Vault），Bastion 端 FastAPI 中间件严格校验该 header 等于预期值，不匹配返回 401。密钥经 HTTPS/CF Tunnel 加密传输。Uptime Kuma 原生支持自定义 header，无需 HMAC。
- CF Access（可选）对 Dashboard 域名加一层 SSO，进一步收敛暴露面。

### 4.5 数据出网边界与最小化策略 `[决策#1][决策#5]`

| 数据类别 | 是否出网 | 目的 | 最小化措施 |
| :--- | :--- | :--- | :--- |
| Master Password / SSH 私钥 / API Token | ❌ 绝不 | — | 仅本地 Vault 内存 |
| 排查日志/拓扑片段 | ✅ 出网 | LLM API | `fetch_service_logs` 强制截断；仅传必要片段 |
| 工单/日志/拓扑元数据 | ✅ 出网 | Firebase | 结构化字段，不含凭证；Firebase 安全规则限单用户 |
| 调查报告 | ✅ 出网 | Telegram | 仅 `summary_md`，发送前 redaction 扫描凭证模式 |
| LLM API Key | ✅ 出网（请求头） | LLM API | 仅 unlock 后内存态，随请求发出 |

**Redaction 处理器：** 所有出网文本（LLM 上下文、Telegram 报告）经 structlog/输出过滤器扫描 `ssh-`、`-----BEGIN`、`Bearer `、长 hex/token 模式并脱敏。

---

## 5. MCP 工具集详细规范

统一返回：`{ok: bool, data?: object, error?: {code: string, message: string}}`。错误码：`VALIDATION_ERROR` / `AUTH_REQUIRED` / `HITL_REJECTED` / `HITL_TIMEOUT` / `EXEC_TIMEOUT` / `BUDGET_EXCEEDED` / `PATH_NOT_ALLOWLISTED` / `INTERNAL`。

### 5.1 `setup_webhook_tunnel`（L0 基建）
**职责：** 基于 Vault 中 CF API Token 创建/校验隧道与 DNS 路由，确保 `cloudflared` 已配置。无外部命令参数输入。

**输入 Schema：** `{type: object, properties: {}, additionalProperties: false}`
**输出 Schema：** `{ok: true, data: {tunnel_id, hostname, status}}`
**安全校验：** 仅使用预置 Token；不执行任意 shell；操作幂等。
**错误处理：** CF API 失败 → `INTERNAL` + 明确错误；不暴露 Token。

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
**错误处理：** 连接失败 `INTERNAL`；超时 `EXEC_TIMEOUT`。

### 5.3 `fetch_service_logs`（L2 日志）
**职责：** 抓取报错日志，Server 端强制 token 长度截断 `[PRD §4.4]`。

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
**错误处理：** Firebase 写入失败 `INTERNAL`。

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
    "params": {"type": "object"}
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
**安全校验流程：**
1. `action_type` 枚举校验（拒绝 `reboot` 等）。
2. `params` 走模板渲染 + IDENT_RE / 路径白名单校验。
3. **PermissionGate 拦截** → 写 `hitl_requests(PENDING)`，附 `rendered_cmd` 预览、`target_host`、`action_type`、影响说明。
4. LangGraph `interrupt` 挂起，等待 `approve`/`reject`。
5. `approve` → `ExecutionEngine.run_remediation` 执行；`reject` 或超时（默认 30 分钟）→ `HITL_REJECTED`/`HITL_TIMEOUT`。
**错误处理：** 审批拒绝 `HITL_REJECTED`；执行超时 `EXEC_TIMEOUT`；路径越权 `PATH_NOT_ALLOWLISTED`。

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
    loop 规划-工具-日志
        G->>MCP: execute_discovery/fetch_service_logs (L1/L2)
        MCP-->>G: 结果
        G->>MCP: submit_journal(observation/finding)
        G-->>API: SSE token_delta/step
        API-->>U: 思维链流式
    end
    alt 触发 L3
        G->>MCP: execute_remediation
        MCP->>FB: 写 hitl_requests(PENDING)
        MCP-->>G: interrupt(挂起)
        FB-->>U: 推送审批弹窗
        U->>API: POST /hitl/{id}/approve
        API->>G: resume(approve)
        G->>MCP: 执行模板
        MCP-->>G: 结果
    end
    G->>MCP: submit_journal(summary_md)
    API->>FB: investigation(COMPLETED)
    API-->>U: 最终回复 + 报告
```

### 6.2 Event-driven 模式（异步事件）`[PRD FR3.2][决策#9]`

```mermaid
sequenceDiagram
    participant UK as Uptime Kuma
    participant CF as Cloudflare
    participant API as FastAPI Webhook
    participant DEDUP as 去重器
    participant G as LangGraph Agent
    participant TG as Telegram
    participant FB as Firebase
    UK->>CF: Webhook {monitor, status: DOWN}
    CF->>API: 隧道回源
    API->>API: 校验 X-Webhook-Secret + 解析 host+service
    API->>DEDUP: 查 dedup_key
    alt 已有进行中工单
        DEDUP-->>API: 并入(不新建)
        API-->>UK: 200 (已合并)
    else 新故障
        DEDUP->>FB: 创建 investigation(IN_PROGRESS, dedup_key)
        DEDUP->>G: 后台启动深度侦察(5~15min)
        loop 证据收集
            G->>G: 调 MCP 探测/日志/RAG
            G->>FB: submit_journal
        end
        G->>G: 检查 Token 预算
        G->>FB: investigation(COMPLETED|FAILED_TOKEN_BUDGET)
        G->>TG: 推送 Markdown 报告
    end
    Note over UK,FB: UP 恢复: status=UP → 标记 RESOLVED, 不启动调查
```

### 6.3 调查状态机 `[决策#9][决策#15]`

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> IN_PROGRESS: Agent 启动
    IN_PROGRESS --> HITL_SUSPENDED: 触发 L3
    HITL_SUSPENDED --> IN_PROGRESS: 审批通过
    HITL_SUSPENDED --> ABORTED: 拒绝/超时
    IN_PROGRESS --> COMPLETED: 调查完成
    IN_PROGRESS --> FAILED_TOKEN_BUDGET: Token 超限
    COMPLETED --> RESOLVED: 收到 UP 恢复
    FAILED_TOKEN_BUDGET --> [*]
    ABORTED --> [*]
    RESOLVED --> [*]
```

### 6.4 事件去重与 UP/DOWN 语义
- **dedup_key = `host_id + "::" + service_name`**。DOWN 到达时查 `investigations` 中同 key 且 `status ∈ {PENDING, IN_PROGRESS, HITL_SUSPENDED}` 的工单：存在则并入（附加 observation），不新建。
- **DOWN** → 若无活跃工单则新建并启动深度侦察；若有则并入。
- **UP** → 将同 key 活跃工单标记 `RESOLVED`，附 `observation: service recovered`，**不**启动新调查。

### 6.5 结构化 Journal Records 生成规范 `[PRD §3.4]`

| record_type | 何时产出 | 内容约束 |
| :--- | :--- | :--- |
| `symptom` | 调查起点 | 告警/用户描述的客观症状，1~2 句 |
| `observation` | 每次工具调用后 | 工具返回的客观事实（status/logs 摘要） |
| `finding` | 根因分析时 | 推理得出的发现与根因 |
| `investigation_gap` | 遇盲区时 | 因权限/日志截断导致的未知，须显式记录 |
| `summary_md` | 调查终点 | 面向人类的 Markdown 报告：症状/根因/已采取措施/建议 |

每条 record 经 `submit_journal` 写入 `investigations/{id}/records`，含 `ts`、`record_type`、`content`；`content` 写入前 redaction。

### 6.6 Token 预算控制与熔断 `[决策#15]`
- **预算账户：** 每个 investigation 在 LangGraph state 持 `token_usage`（input+output 累计），同步写 Firebase。
- **硬上限：** `TOKEN_BUDGET`（默认可配，初始 **512k tokens/次** `[决策#18]`）。每步工具调用后检查；`token_usage >= TOKEN_BUDGET` → 立即终止图执行，工单置 `FAILED_TOKEN_BUDGET`，附 `investigation_gap: token budget exhausted`。
- **截断协同：** `fetch_service_logs` 单次返回 ≤ 8k tokens，从源头控制单步消耗。
- **熔断不可恢复：** 一旦 `FAILED_TOKEN_BUDGET`，该工单不再恢复，需人工发起新调查。

### 6.7 HITL 授权流程 `[决策#6]`

```mermaid
flowchart TB
    L3["Agent 调 execute_remediation"] --> Render["模板渲染 + 校验"]
    Render --> Req["写 hitl_requests(PENDING)<br/>含 rendered_cmd 预览/影响"]
    Req --> Interrupt["LangGraph interrupt 挂起"]
    Interrupt --> UI["WebUI 推送审批弹窗"]
    UI --> Decision{人工}
    Decision -->|approve| Resume["resume(approve)"]
    Decision -->|reject| Rej["resume(reject) → ABORTED"]
    Decision -->|30min 超时| Rej
    Resume --> Exec["ExecutionEngine.run_remediation"]
    Exec --> Record["submit_journal(observation)"]
```

- 审批界面**必须**展示：`target_host`、`action_type`、渲染后的完整命令预览、预期影响、关联工单。
- 审批决策记入 `hitl_requests`（`decision`、`decided_by`、`decided_at`）供审计。
- 仅 L3 触发 HITL；L1/L2 只读自主执行，但全部记审计日志。

---

## 7. 非功能性设计

### 7.1 性能与并发 `[决策#3]`
- **单进程 + 受限并发：** 一个 asyncio 事件循环；SSH 并发用 `asyncio.Semaphore(4)` 限流，**不引入任务队列/执行器池/跨节点编排**。
- **超时熔断：** 单次 SSH 操作全局 5s 超时 `[PRD §4.2]`；LLM 调用单次 30s 超时；超时即释放连接、记录 `EXEC_TIMEOUT`，不阻塞事件循环。
- **连接复用：** 按 `target_host` 复用 `asyncssh.SSHClient`，避免反复握手；连接异常自动重建。
- **背压：** Webhook 突发（如多服务同时 DOWN）经去重器合并；并发调查数上限（默认 3），超出排队，防 token 与 SSH 资源耗尽。

### 7.2 可观测性
- **结构化日志：** structlog JSON 日志，含 `execution_id`、`tool`、`host`、`latency`、`token_delta`；凭证字段经 redaction processor。
- **思维链流式：** Agent 每步经 SSE 推 `step`/`tool_call`/`tool_result`/`token_delta` 到前端 `[PRD FR1.3]`。
- **Firebase 实时同步：** 工单状态/进度/Journal 经 `onSnapshot` 驱动前端无缝刷新。
- **审计：** 所有 MCP 工具调用（含 L1/L2）记审计日志：`ts, execution_id, tool, level, host, params_redacted, ok, latency`。
- **指标：** 暴露 `/metrics`（Prometheus 格式可选）：调查数、token 消耗、SSH 超时率、HITL 平均审批时长。

### 7.3 成本控制 `[决策#15]`
- **Token 硬上限：** 见 §6.6，每调查独立预算，超限即 `FAILED_TOKEN_BUDGET`。
- **日志截断：** `fetch_service_logs` 双重限制（`lines ≤ 500` + token 估算 ≤ 8k）。
- **RAG 兜底：** 命中知识库可减少反复探测的 token 消耗；冷启动期接受较高 token 成本。
- **Provider 固定：** 部署时配置单一 vendor（Claude 或 GPT），不按任务复杂度自动路由 `[决策#19]`。

### 7.4 部署与运维 `[决策#11]`

**Docker Compose 结构：**
```yaml
services:
  bastion-app:
    image: aiops-bastion:1.0
    env_file: .env              # 非敏感配置
    volumes:
      - ./data/vault:/data/vault        # vault.enc
      - ./data/chroma:/data/chroma      # 向量库
      - ./data/logs:/data/logs
    ports: ["127.0.0.1:8080:8080"]      # 仅本地回环，cloudflared 回源
    restart: unless-stopped
  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel run
    volumes: ["./cloudflared:/etc/cloudflared:ro"]
    restart: unless-stopped
    depends_on: [bastion-app]
```

- **cloudflared 集成：** 常驻容器，出站长连到 CF 边缘；creds.json 与 config.yml 挂载只读。
- **升级：** 镜像版本化 tag；`docker compose pull && up -d`；Vault/Chroma 卷保留，跨版本兼容由 schema migration 脚本保证。
- **回滚：** 保留前一版本镜像 tag，`docker compose up -d` 指定旧 tag；`vault.enc` 与 Chroma 卷不受影响。
- **备份：** 定期冷备 `vault.enc` + `chroma/` + Firebase 导出至离线介质；Master Password 离线保管。
- **首启：** Onboarding 经 WebUI 设 Master Password + 录入凭证 → 生成 `vault.enc` → `setup_webhook_tunnel` 打通入站。

---

## 8. 数据模型详细定义

### 8.1 Firebase Schema

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
| `status` | string | 枚举：PENDING/IN_PROGRESS/HITL_SUSPENDED/COMPLETED/RESOLVED/FAILED_TOKEN_BUDGET/ABORTED | |
| `dedup_key` | string | `host_id::service_name` | DOWN 去重键 |
| `mode` | string | 枚举：chat/event | 触发模式 |
| `trigger` | map | 必填 | {type, host_id, service_name, severity, raw} |
| `token_usage` | int | 默认 0，单调递增 | 累计 token，硬上限判定 |
| `token_budget` | int | 默认 512000 | 本次预算上限（`[决策#18]`） |
| `created_at` / `updated_at` | timestamp | 自动 | |
| `summary_md` | string | 可空 | 最终报告 |
| `assigned_to` | string | 预留 | 未来多用户 |

**Subcollection `investigations/{id}/records`**（Journal）`[PRD §3.4]`

| 字段 | 类型 | 约束 | 说明 |
| :--- | :--- | :--- | :--- |
| `record_id` | string | PK | |
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
| `request_id` | string | PK | |
| `execution_id` | string | FK→investigations | |
| `target_host` | string | IDENT_RE | |
| `action_type` | string | 枚举 3 项 | |
| `rendered_cmd` | string | 模板渲染预览 | 供人工审阅 |
| `impact` | string | 必填 | 预期影响说明 |
| `status` | string | PENDING/APPROVED/REJECTED/EXPIRED | |
| `decided_by` / `decided_at` | string/timestamp | 可空 | 审计 |
| `expires_at` | timestamp | 创建+30min | 超时自动 EXPIRED |

**索引汇总：** `investigations(dedup_key,status)`、`investigations(status,created_at↓)`、`records(execution_id,ts↑)`、`runbooks(status)`、`hitl_requests(status,created_at)`。

### 8.2 Chroma 向量库元数据设计

| Collection | document 内容 | metadata | 距离 | 索引 |
| :--- | :--- | :--- | :--- | :--- |
| `runbooks` | SOP 分块文本 | `{runbook_id, status, service, updated_at}` | cosine | HNSW |
| `topology` | 拓扑基线分块 | `{host_id, baseline_at}` | cosine | HNSW |
| `findings` | 历史 finding 摘要 | `{execution_id, host_id, service}` | cosine | HNSW |

- 嵌入维度：384（all-MiniLM-L6-v2）。
- 检索：`query_runbook` 跨三 collection 各取 top_k，按 score 合并、按 metadata 过滤（如 `runbooks.status==APPROVED` 才返回）。
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
  "llm_api_keys": {"anthropic": "sk-ant-...", "openai": "sk-..."},
  "llm_base_urls": {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}
}
```
- 文件权限 `0600`，属主为容器非 root 用户。
- 解密后 `bytes` 持有，`lock()` 时 `zeroize`；明文 JSON 仅在 `get()` 短暂构造后丢弃。

---

## 9. 风险、权衡与开放问题

### 9.1 已知风险与缓解

| 风险 | 影响 | 缓解 |
| :--- | :--- | :--- |
| LLM 幻觉构造危险命令 | 误操作业务节点 | 命令白名单 + 模板，Agent 无 shell 拼接能力；L3 强制 HITL |
| Master Password 遗忘 | 凭证锁定 | 24 词恢复短语解锁并重设 `[决策#17]`；助记词离线保管；主密码+助记词双失则不可恢复 |
| CF Tunnel 中断 | 无法接收 Webhook | Uptime Kuma 自身保留直连通知通道作为兜底；`cloudflared` 自动重连 |
| Firebase 出网依赖 | 网络断时前端实时性丢失 | Firebase 离线缓存；Bastion 本地仍可执行调查，恢复后同步 |
| Token 成本失控 | 云端账单飙升 | 单调查硬上限；日志截断；RAG 命中减探测 |
| Chroma 冷启动 RAG 弱 | 早期排查质量低 | 接受；依赖通用推理；随事件沉淀逐步增强 |
| Bastion 单点故障 | 无 HA，宕机即全停 | 1~3 节点规模接受单点；Docker `restart: unless-stopped` + 卷持久化 |
| Webhook 伪造 | 伪造告警触发调查 | 端点校验共享密钥/HMAC 签名 |

### 9.2 设计权衡记录

| 权衡点 | 选择 | 牺牲了什么 | 理由 |
| :--- | :--- | :--- | :--- |
| 云端 LLM vs 本地 | 云端 `[决策#1]` | 运维数据出网 | 获取强推理/工具调用能力；接受最小化出网 |
| Firebase vs 自托管 | Firebase `[决策#5]` | 数据出网到 Google | 实时同步与 Auth 开发效率 |
| LangGraph vs 纯 LangChain | LangGraph | 学习曲线 | 原生 HITL interrupt、状态机、步骤级 token 计量 |
| 本地嵌入 vs 云端嵌入 | 本地 MiniLM | 嵌入质量略低 | 零 API 成本、文本不出网、契合从零沉淀 |
| 结构化模板 vs 通用 shell | 模板+白名单 `[决策#8]` | L3 动作灵活性 | 安全第一，可枚举动作即满足运维修复需求 |

### 9.3 需产品侧进一步确认的事项

> **已确认**（详见正文与 PRD §8 决策记录）：前端 React `[决策#20]`、Token 默认 512k `[决策#18]`、Master Password 恢复短语 `[决策#17]`、Provider 固定不自动路由 `[决策#19]`、自定义 Base URL `[决策#16]`、Webhook 共享密钥 header `[决策#21]`、拓扑基线自动入库 `[决策#22]`。

当前无待确认事项。

---

## 10. 实施建议

### 10.1 与 PRD Milestone 的映射

| PRD Milestone | 本设计交付物 |
| :--- | :--- |
| M1 基建与控制台 | §2.4 信任边界、§3.1 前端 Onboarding/Dashboard、§3.7 Vault、§7.4 Docker Compose + cloudflared |
| M2 探测网关与工具链 | §3.4 执行引擎、§5.2/5.3 工具、§4.2 白名单+正则、§5 日志截断 |
| M3 Agent 大脑接入 | §3.5 LangGraph + Provider、§3.3 MCP Server、§6.1 Chat 流、§8.1 investigations/records |
| M4 Webhook 全自动闭环 | §6.2 事件流、§6.3 状态机、§6.4 去重/UP-DOWN、§6.6 Token 熔断、Telegram 推送 |
| M5 知识库自进化 | §3.6 Chroma、§5.5 query_runbook、§8.2 向量元数据、SOP 审核面板 |

### 10.2 优先开发路径建议

1. **安全地基先行：** Vault（§3.7）+ 执行引擎白名单/模板（§3.4）+ 注入测试（§4.3）——安全红线未落地前不接生产。
2. **MCP 工具骨架：** 先 L1/L2 只读工具，验证 SSH 链路与截断；L3 模板 + HITL 随后。
3. **Agent 最小闭环：** Sync Chat 单工具调用 + Journal 写入，再扩到多步规划。
4. **事件模式：** 在 Chat 稳定后接 Webhook + 去重 + Token 熔断。
5. **RAG 最后：** 工具与 Agent 稳定后接入 Chroma 与审核面板。

### 10.3 测试策略细化

| 测试类别 | 内容 | 通过标准 |
| :--- | :--- | :--- |
| 注入对抗（§4.3） | 8 类 payload 表驱动单测 | 全部被 `CommandValidationError`/`PathNotAllowlistedError` 拒绝 |
| 白名单边界 | 白名单外动词、超长标识符、枚举外 action_type | 拒绝；L3 `reboot` 拒绝 |
| Token 熔断（§6.6） | 构造超长日志/循环排查触发超限 | 工单置 `FAILED_TOKEN_BUDGET`，附 `investigation_gap`，图终止 |
| 事件语义（§6.4） | 重复 DOWN 合并、UP 标记 RESOLVED、DOWN+UP 序列 | 去重正确，不重复建单，状态转换符合状态机 |
| HITL 流程（§6.7） | approve/reject/超时三分支 | 各分支状态与审计记录正确；L3 未审批绝不执行 |
| 断网/状态同步 | 出网中断、CF 重连、Firebase 离线缓存 | 调查可继续，恢复后同步，无数据丢失 |
| 沙盒演练 | 本地 Docker 靶机集群模拟服务宕机 | 端到端闭环：DOWN→探测→根因→HITL→修复→报告 |
| 凭证安全 | 日志/LLM 上下文/Telegram 报告 redaction 扫描 | 无凭证模式泄漏 |

**CI 门禁：** Ruff + Flake8 + mypy 全过；pytest 含注入与熔断套件全过；方可合并。
