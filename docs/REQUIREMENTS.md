# AIOps-Bastion：基于 MCP 与 RAG 的多节点智能运维堡垒机
**需求分析与架构设计说明书 (PRD)**

---

## 1. 项目背景与目标

随着自托管服务（如 `xuejie1.top` 域名相关的 Web 节点、Navidrome 媒体服务器、Vaultwarden 密码库、Code-server 等）及底层宿主机的增加，日常运维面临告警噪音大、跨节点排查繁琐、以及历史排查经验难以沉淀等痛点。

本项目旨在开发一个基于 Model Context Protocol (MCP) 的中心化 AI 运维网关，并配套现代化的 Web Dashboard。系统将实现从“被动接收告警”到“主动收集证据、结合专属运维知识库分析根因，并提供修复建议”的全自动运维闭环。

**目标规模：** 个人/家庭自托管场景，纳管 **1~3 台** 服务器节点。架构按轻量单进程设计，不引入分布式任务队列，但预留向更大规模演进的可能。

**核心目标：**
1. **控制爆炸半径 (Blast Radius Control)：** 彻底摒弃将主机 Root Shell 暴露给通用 AI 的危险做法，通过 MCP 提供严格受限的 API 网关。
2. **多节点聚合 (Fleet Management)：** 采用中心化堡垒机模式，单点管理多台服务器集群。
3. **隐匿穿透 (Zero Trust Networking)：** 依托 Cloudflare 隧道，内网堡垒机无需公网 IP 即可安全接收云端 Webhook。
4. **专属运维大脑 (Operational RAG)：** 结合自动探索与人类审核的混合架构，沉淀私有架构拓扑与高危 SOP。

---

## 2. 系统架构选型

* **部署模式：** 异地带外管理（Out-of-Band）。中心化 Bastion Server 与 Web Dashboard 部署于**独立的家庭内网机器/NAS**，与云端业务节点物理隔离——堡垒机本身**不**是被纳管的 1~3 台节点之一，避免“自监控自身故障”盲区。堡垒机以 **Docker Compose** 形态部署，保证环境可复现、升级可回滚。
* **AI 大脑：** 采用**云端大模型**，并通过 LangChain 的 Provider 抽象层同时支持 **Anthropic Claude 与 OpenAI GPT**，可在配置层切换，避免厂商锁定。
  > ⚠️ **数据流取舍：** 云端模型意味着排查日志、进程拓扑等运维数据会**出网发送给模型厂商**。这是为获取强推理/工具调用能力而接受的取舍（详见 §4.1）。凭证（Master Password、SSH 私钥、API Token）**绝不**出网。
* **前后端分离：** 前端采用 React/Vue 构建 Web Dashboard，后端利用 Firebase 作为实时数据库和鉴权中心，天然契合 AI 应用的敏捷全栈开发。
* **通信协议：** MCP Server 与 Agent 同进程部署于堡垒机内，基于 `stdio` 通信，零网络端口暴露。
* **入站网关 (Inbound Gateway)：** 集成 **Cloudflare Tunnels (`cloudflared`)**，作为 Docker Compose 中的常驻服务建立出站长连接，以接收公网 Webhook 唤醒信号（堡垒机无公网 IP，此为唯一入站通道）。
* **执行引擎：** Python + LangChain（Agent 逻辑、Provider 抽象）与 `mcp` 官方 SDK。
* **远程控制：** 依赖 `asyncssh` 进行底层多并发 SSH 指令下发。
* **告警源：** **Uptime Kuma**（自托管 uptime 监控），其 Webhook 作为事件驱动模式的唯一触发源。
* **推送通道：** 异步调查报告通过 **Telegram Bot** 推送，同时在 WebUI 展示。

---

## 3. 功能性需求 (Functional Requirements)

### 3.1 Web Dashboard 与凭证初始化 (WebUI & Vault)
系统不再依赖终端界面，全面转向现代化的 Web 控制台进行基建管理。
* **FR 1.1 引导初始化：** 首次访问 WebUI 时，引导设置 Master Password，并录入：目标服务器集群的 SSH 私钥、Cloudflare API Token、Telegram Bot Token。系统为**单用户**模型，Master Password 即为主门；Firebase Auth 先以单账号实现，但数据模型预留 `role` 字段供日后多用户扩展。
* **FR 1.2 加密落盘：** 后端使用 Master Password 派生密钥（如通过 `PBKDF2`），采用对称加密（如 `cryptography.fernet`）将凭证持久化落盘。Master Password 不落盘，仅在运行期驻留内存用于解密。
* **FR 1.3 实时面板：** WebUI 提供：① **全局健康度看板**（各节点服务存活状态 + CPU/内存/磁盘水位）；② **实时 Webhook 触发流**；③ **Agent 思考过程（思维链）的流式展示**；④ **工单排查进度条**。

### 3.2 核心 MCP 工具集 (Tooling API)
MCP Server 需向 Agent 暴露严格定义的 JSON-RPC 接口，划分为五个核心域：

| 工具名称 / API | 权限域 | 描述与输入参数 |
| :--- | :--- | :--- |
| `setup_webhook_tunnel` | **L0: 基建** | **无参数**。基于已录入的 Cloudflare API Token 创建/配置隧道与 DNS 路由，打通内网至云端的 Webhook 通道；`cloudflared` 作为常驻容器由 Docker Compose 拉起。 |
| `execute_discovery` | **L1: 探测** | **参数:** `target_host`, `service_name`。统一封装 `systemctl` / `docker inspect` / `docker compose ps` 三种探测方式，适配 systemd 服务、单容器、Compose 编排三种部署形态，获取存活状态。 |
| `fetch_service_logs` | **L2: 日志** | **参数:** `target_host`, `service_name`, `lines`。抓取报错日志，Server 端强制执行 Token 长度截断，避免撑爆上下文。 |
| `submit_journal` | **L2: 归档** | **参数:** `execution_id`, `record_type`, `content`。将排查中间发现结构化写入 Firebase。 |
| `query_runbook` | **L2: 知识库** | **参数:** `query_string`。连接本地向量库检索历史工单与 SOP 规范。 |
| `execute_remediation` | **L3: 高危** | **参数:** `target_host`, `action_type`。**强制挂起并推送到 WebUI 请求人类授权 (HITL)**；人类批准后由 **Agent 自动执行**。`action_type` 枚举固定为下表三项，**整机 reboot 明确排除**。 |

**`execute_remediation` 的 `action_type` 白名单：**

| action_type | 对应动作 | 约束 |
| :--- | :--- | :--- |
| `restart_service` | `systemctl restart <unit>` | unit 名经正则白名单校验 |
| `restart_container` | `docker restart <name>` / `docker compose restart <svc>` | name 经正则白名单校验 |
| `clear_cache` | 清理指定路径缓存/临时文件 | **路径必须命中预配置路径白名单**，否则拒绝 |

### 3.3 双模 Agent 工作流 (Dual-Mode Workflow)
* **FR 3.1 对话排查模式 (Sync Chat)：** 用户在 WebUI 的 Chat 侧边栏发起自然语言查询，Agent 实时规划步骤，调用工具并流式返回结果。**对话模式触发的 L3 修复同样强制 HITL 授权**，与事件模式保持一致。
* **FR 3.2 异步事件驱动模式 (Event-driven)：** Uptime Kuma 监控目标宕机后向 Cloudflare Webhook 域名发包。触发语义：**按 `target_host` + `service_name` 去重**——同一对象已有进行中工单则并入不新建；**仅 `DOWN` 级告警启动深度侦察**，`UP` 恢复仅将对应工单标记为 `RESOLVED`。Agent 挂载后台执行 5~15 分钟的深度侦察，任务完成时生成 Markdown 报告，通过 WebUI 与 **Telegram** 推送。单次调查设 **Token 硬上限**，超限即中止并标记失败（详见 §4.4）。

### 3.4 结构化调查日志 (Journal Records)
对标企业级标准，Agent 在调查中必须输出标准认知链路至 Firebase：
1. `symptom` (症状描述)
2. `observation` (客观观测)
3. `finding` (深度发现与根因)
4. `investigation_gap` (信息缺口)：由于权限或日志截断导致的盲区。
5. `summary_md` (最终摘要)：面向人类的根因分析报告。

### 3.5 人机协同专属知识库 (Hybrid Operational RAG)
* **FR 5.1 自动拓扑探索：** 纳管新节点时，Agent 自动执行端口扫描与进程梳理，生成《系统拓扑基线》推入向量库。**知识库冷启动为“纯从零沉淀”**——不导入任何存量文档，完全依赖 Agent 自动探索 + 人工审核逐步积累；冷启动期 Agent 主要依靠通用推理能力，知识库随运维事件逐步丰满。
* **FR 5.2 SOP 审核机制：** Agent 在日常排查中生成的规程（如“发现需先清空缓存再重启”），统一标记为 `DRAFT_PENDING_REVIEW`，推送到 WebUI 待办列表，由人类确认修改后正式合入 RAG 语料库。
* **向量库选型：** 建议采用嵌入式轻量向量库（如 **Chroma**），与堡垒机同机本地部署，零外部依赖，契合 1~3 节点小规模与从零沉淀场景。

---

## 4. 非功能性需求 (Non-Functional Requirements)

### 4.1 安全与隔离 (Security)
* **内网隐匿：** 依靠 Cloudflare Zero Trust，系统拒绝一切主动入站请求，防范端口扫描。
* **数据流边界：** 运维日志/拓扑等排查数据会出网至云端 LLM 厂商（已接受的取舍）；**Master Password、SSH 私钥、API Token、Telegram Bot Token 等凭证绝不离开堡垒机**，仅以派生密钥加密落盘、运行期内存解密。
* **指令安全模型（结构化动作 + 白名单）：** 摒弃易绕过的符号黑名单。
  * **L3 修复：** Agent 仅传递 `action_type` + 受校验参数，服务端映射到**硬编码命令模板**，**绝不拼接 shell 字符串**，从根上杜绝注入。
  * **L1/L2 只读探测：** 走**预定义命令白名单**（如 `systemctl status`、`docker inspect`、`docker compose ps`、`journalctl`），并对所有参数施加**严格正则校验**，拦截 `;`、`|`、`&&`、`>`、`$()`、反引号、换行等一切 shell 元字符。

### 4.2 性能设计 (Performance)
* **轻量并发：** 鉴于 1~3 节点规模，采用**单进程 + 受限并发**模型——底层仍用 `asyncio`/`asyncssh` 并发发起跨节点 SSH，但**不引入任务队列、执行器池或跨节点编排**，避免过度设计。
* **超时熔断：** 针对单机 SSH 或拉取巨量日志，设置全局 5 秒超时，避免局部故障引发阻塞雪崩。

### 4.3 工程规范 (Code Quality)
* 参照顶级开源社区（如 AutoMQ）的 Committer 提交标准：引入严格的 Ruff/Flake8 Linting、完备的 Docstrings 以及 GitHub Actions 自动化测试流，保证项目的企业级可读性与复用性。

### 4.4 成本控制 (Cost Control)
* **Token 硬上限：** 每次事件驱动调查设置 token 用量硬上限（默认值可配置），超限即**中止调查**并将工单标记为 `FAILED_TOKEN_BUDGET`，避免长时深度侦察导致云端模型成本失控。
* **日志截断：** `fetch_service_logs` 在 Server 端强制截断，从源头控制单次上下文体积。

---

## 5. 数据流与架构设计 (Firebase Schema)

利用 Firebase 的实时同步特性，驱动 Web Dashboard 的无缝刷新：

* **Collection: `nodes`** (主机元数据)
  * `host_id` (如 xuejie1.top), `services` (Array), `last_seen` (Timestamp)
* **Collection: `investigations`** (调查工单)
  * `execution_id`, `status` (`PENDING` / `IN_PROGRESS` / `COMPLETED` / `RESOLVED` / `FAILED_TOKEN_BUDGET`)
  * `dedup_key` (`host_id` + `service_name`，用于 DOWN 告警去重合并)
  * `token_usage` (累计 token 消耗，用于硬上限判定)
  * `records` (Subcollection: 存储 symptom, observation, finding, investigation_gap, summary_md)
* **Collection: `runbooks`** (运维知识库元数据)
  * `document_id`, `status` (`DRAFT_PENDING_REVIEW` / `APPROVED`), `content_hash`

---

## 6. 实施路线图 (Milestones)

* **Milestone 1: 基建与控制台 (Week 1)**
  开发前端 WebUI，打通 Firebase 鉴权；实现凭证加密逻辑；编写 Docker Compose 编排（含 `cloudflared` 常驻服务）与 Cloudflare Tunnel 自动化拉起（IaC）脚本。
* **Milestone 2: 探测网关与工具链 (Week 2)**
  基于 Python 编写 MCP Server，实现 L1/L2 SSH 探测（systemd / Docker / Docker Compose 三形态）、命令白名单 + 参数正则校验、以及日志截断机制。
* **Milestone 3: Agent 大脑接入 (Week 3)**
  接入 LangChain 框架，配置 SRE 人设提示词，搭建 Claude/GPT 双厂商 Provider 抽象层，打通“异常触发 -> 工具链排查 -> 写入 Firebase 调查日志”的完整数据流。
* **Milestone 4: Webhook 全自动闭环 (Week 4)**
  联调 Uptime Kuma → Cloudflare Webhook → 本地 Agent 的端到端事件流；实现 DOWN 去重与 UP 恢复语义、Telegram 报告推送、Token 硬上限熔断；Web Dashboard 实时展示工单排查进度条。
* **Milestone 5: 知识库自进化 (Week 5 进阶)**
  集成嵌入式向量库（Chroma），实现 `query_runbook` RAG 工具，上线 WebUI 的 SOP 审批面板；纯从零沉淀，随运维事件逐步扩充语料。

---

## 7. 测试与验证策略

1. **沙盒靶机演练：** 使用本地 Docker 构建靶机集群，严禁初期直接连接生产环境进行“破坏性测试”。
2. **命令注入对抗：** 编写单元测试，模拟大模型幻觉，尝试下发包含 `;`、`$()`、反引号、换行等注入特征的命令，验证 L1/L2 白名单 + 参数正则的拦截有效性；并验证 L3 仅接受白名单 `action_type`、模板不可被参数污染。
3. **断网与状态同步测试：** 模拟出网链路中断，测试云端隧道的重连机制，以及 Firebase 离线缓存的鲁棒性。
4. **事件语义测试：** 模拟 Uptime Kuma 重复 DOWN 告警验证去重合并、UP 恢复验证工单 `RESOLVED` 标记。
5. **Token 护栏测试：** 构造超长日志/循环排查场景，验证硬上限触发后工单正确标记 `FAILED_TOKEN_BUDGET` 并中止。

---

## 8. 决策记录 (Decision Log)

下表汇总需求确认阶段的所有关键决策，便于后续追溯与变更管理：

| # | 决策项 | 选择 | 影响章节 |
| :-- | :--- | :--- | :--- |
| 1 | AI 大模型形态 | 云端模型（数据出网至厂商，已接受取舍） | §2、§4.1 |
| 2 | 模型厂商 | Anthropic Claude + OpenAI GPT 可切换（LangChain Provider 抽象） | §2、§6 M3 |
| 3 | 纳管规模 | 1~3 台轻量，单进程 + 受限并发，不引入任务队列 | §1、§4.2 |
| 4 | 告警源 | Uptime Kuma | §2、§3.2 |
| 5 | 数据存储 | 坚持 Firebase（接受出网取舍） | §2、§5 |
| 6 | 自治边界 | 只读全自动 + L3 修复需授权（授权后 Agent 自动执行） | §3.2、§3.3 |
| 7 | L3 动作白名单 | `restart_service` / `restart_container` / `clear_cache`，排除 reboot | §3.2、§4.1 |
| 8 | SSH 指令安全 | 结构化动作模板（L3）+ 命令白名单/参数正则（L1/L2），取代符号黑名单 | §4.1、§7 |
| 9 | 告警触发语义 | 按 host+service 去重，仅 DOWN 启动调查，UP 标记 RESOLVED | §3.2、§5 |
| 10 | 知识库冷启动 | 纯从零沉淀，不导入存量；建议向量库 Chroma | §3.5、§6 M5 |
| 11 | 堡垒机部署 | 独立家庭内网机器/NAS，Docker Compose 形态，非纳管节点 | §2、§6 M1 |
| 12 | 用户模型 | 单用户，Master Password 为主门，预留 `role` 扩展 | §3.1、§5 |
| 13 | 服务部署形态 | systemd + Docker 容器 + Docker Compose 三种 | §3.2、§6 M2 |
| 14 | 推送通道 | Telegram（+ WebUI） | §2、§3.2、§6 M4 |
| 15 | Token 成本护栏 | 单次调查硬上限，超限中止并标记 `FAILED_TOKEN_BUDGET` | §3.2、§4.4、§7 |
| 16 | 自定义 Base URL | Anthropic/OpenAI 可分别配置自定义 Base URL（支持反代/中转网关） | §2、§3.5（设计文档） |
| 17 | Master Password 恢复 | 24 词 BIP-39 恢复短语包裹 Fernet key，遗忘主密码可解锁并重设 | §3.7、§4.1（设计文档） |
| 18 | Token 默认值 | 单次调查硬上限默认 512k tokens | §6.6、§8.1（设计文档） |
| 19 | Provider 路由策略 | 固定单一 vendor，不按任务复杂度自动路由 | §3.5、§7.3（设计文档） |
| 20 | 前端框架 | 确认 React + TypeScript | §2.3、§3.1（设计文档） |
| 21 | Webhook 鉴权 | 共享密钥自定义 header（`X-Webhook-Secret`） | §4.4、§6.2（设计文档） |
| 22 | 拓扑基线审核 | 自动入库，无需人工审核 | §3.6、§9.3（设计文档） |
