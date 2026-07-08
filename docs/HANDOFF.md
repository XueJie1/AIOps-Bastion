# AIOps-Bastion 任务交接文档

> 给开新 session 用。自包含，读完即可进入状态。
> 最后更新: 2026-07-08 | 对应提交 `5e5c0f3` | 本地与 GitHub 同步

---

## 0. 一句话现状

**M1 安全地基已完成并通过严格审查修复，已推 GitHub。下一步 M2 需真 SSH 靶机。**
设计 v1.3 定稿 → spike 验证 → 脚手架 → M1 → 审查修复，链路完整。

---

## 1. 项目本质与硬约束（必须先读）

- **这是什么:** 基于 MCP + RAG 的多节点智能运维堡垒（AIOps-Bastion）。
- **定位: 求职作品集，非生产系统。** 精致度是卖点，但每个决策须能在面试讲清取舍。详 `docs/TRADEOFFS_v1.2.md`。
- **架构:** 单进程 asyncio。LangGraph + LangChain Provider（§3.5）+ MCP Server stdio（§3.3）+ asyncssh（§3.4）+ Chroma RAG（§3.6）+ Firebase（§3.2）+ Cloudflare Tunnel ingress + Docker Compose（§7.4）。
- **规模:** ≤3 台业务节点，单用户，弱 NAS（2C4G）。
- **三层安全防线（§4.2）:** `IDENT_RE` fullmatch（核心）→ asyncssh `shlex.quote` list-argv（第二）→ 远端 `rbash`（第三，推荐）。
- **HITL 语义:** 单用户模型 → "防误触确认"，非权限隔离。
- **用户协作方式:** 用户不熟 Agent 开发，需求须分批提问确认，勿猜测意图。给方案时附取舍。

### 安全红线（不可破）
- `.env` 含真实 DeepSeek API key，用户自填，绝不外传/不写入任何外发内容。
- 凭证（Master Password/私钥/API Key）绝不落盘、绝不出网，仅 Vault 内存态。
- 原始日志全文不出网；Agent 本地摘要后才送 LLM（§4.5）。

---

## 2. 环境与工具链

| 项 | 值 |
| :--- | :--- |
| 工作目录 | `/home/cao/workspace/get-a-job/aiops` |
| 远程 | `origin` = `git@github.com:XueJie1/AIOps-Bastion.git`（main 已跟踪） |
| Python | 3.12 |
| venv | `.venv/`（已装 dev 依赖，含 pytest/ruff/mypy） |
| 跑测试 | `.venv/bin/python -m pytest -q` |
| 跑 lint | `.venv/bin/ruff check src tests` |
| 跑类型 | `.venv/bin/python -m mypy src`（strict，CI 中 continue-on-error） |
| 配置 | `pyproject.toml`（ruff line-length 100、mypy strict + ignore_missing_imports、pytest asyncio_mode=auto、markers: injection/budget/crash） |
| CI | `.github/workflows/ci.yml`（ruff + mypy + pytest + coverage） |

> 系统 PATH 里没有 pytest/ruff/mypy，须用 `.venv/bin/` 前缀。

---

## 3. 文档地图

| 文件 | 内容 |
| :--- | :--- |
| `docs/DETAILED_DESIGN_v1.2.md` | 详细设计 v1.3（文件名未改，标题行注 v1.3 修订记录）。1472 行，定稿。 |
| `docs/REQUIREMENTS.md` | PRD |
| `docs/TRADEOFFS_v1.2.md` | 取舍说明，作品集面试用 |
| `docs/SPIKE_REPORT.md` | 4 个 spike 验证结果 |
| `docs/HANDOFF.md` | 本文件 |
| `spike/01-04_*.py` | 4 个验证脚本（留作参考，非生产代码） |

设计文档章节速查：§3.3 MCP Server / §3.4 执行引擎 / §3.5 Agent / §3.6 RAG / §3.7 Vault / §4 安全 / §5 工具集 / §5.7 错误码 / §6 工作流 / §7 非功能 / §8.3 vault.enc 格式 / §10 milestone 映射 / §11 灾难恢复。

---

## 4. 进度：已完成

### Git 历史（最新在上）
```
5e5c0f3 docs: §8.3 同步 spike-03 - deepseek-v4-pro / active=deepseek
dc60c3b chore: gitignore 补 Pi agent 忽略
1b0ee4d fix(m1): 3 项审查缺陷 - approval_id 错误码 / 嵌套凭证 / vault.enc 格式
fb3e349 feat(m1): 安全地基 - 执行引擎白名单/模板 + Vault 凭证加密 + 注入测试
fae8438 chore: 项目脚手架 - pyproject + Docker + CI + 包结构
e538544 docs: 设计文档 v1.3 - 据 spike 验证落地 7 项修订
0409564 chore: 项目基线 - 设计文档 + 取舍说明 + spike 验证
```

### M1 安全地基（已完成，已审查修复）
源码三件 + 测试三件，34 测试全过、ruff 干净：

| 文件 | 职责 |
| :--- | :--- |
| `src/aiops_bastion/exceptions.py` | 异常体系（对齐 §5.7 错误码） |
| `src/aiops_bastion/execution.py` | 执行引擎：白名单 + IDENT_RE + L3 模板（§3.4） |
| `src/aiops_bastion/vault.py` | Vault：PBKDF2+Fernet 全异步、嵌套点路径、§8.3 格式（§3.7） |
| `tests/test_injection.py` | 注入对抗 + 白名单边界 + L3 越权（§4.3） |
| `tests/test_vault.py` | Vault 生命周期 + 异步 + 损坏拒绝 + 权限 |
| `tests/test_smoke.py` | 冒烟（导入、Python 版本） |

**M1 已验证：** 8 类注入 payload 全拒、IDENT_RE fullmatch、L3 仅 3 枚举无 reboot、clear_cache 路径精确匹配、approval_id 缺失拒（HITLRejectedError）、PBKDF2 异步不阻塞、Vault 全生命周期、嵌套凭证热更新、vault.enc 损坏拒绝、文件权限 0600。

**M1 审查发现并修复的 3 个真实缺陷（commit 1b0ee4d）：**
- A1 `approval_id` 缺失误用 VALIDATION_ERROR → 改 HITL_REJECTED
- A2 `update_credential`/`get` 原扁平 `bundle[name]` 不支持 §4.6 点路径 → 加 `_split_path`/`_get_path`/`_set_path`
- A3 vault.enc 未写 16B recovery_salt 占位 → 对齐 §8.3，ct 从偏移 41 读

### Spike 验证（已完成，4 个脚本）
3 PASS + 1 PARTIAL FAIL（根因已定位）：
- 01 LangGraph interrupt + SqliteSaver 崩溃恢复 — PASS
- 02 MCP in-process 加载（`create_connected_server_and_client_session` + content block 分层）— PASS
- 03 DeepSeek tool-calling — PASS（`deepseek-v4-pro` 真实可用、默认思考模式）
- 04 E2E HITL — PARTIAL FAIL：`create_react_agent` 不自动注入 approval_id，需 InjectedToolArg 或自定义 ToolNode（设计 §3.3 修订点）

---

## 5. 已知坑（动手前必看）

| 坑 | 真相 | 出处 |
| :--- | :--- | :--- |
| DeepSeek 模型 id | `deepseek-v4-pro` 真实可用（默认思考模式）；`deepseek-chat`/`deepseek-reasoner` 是旧别名，2026-07-24 停用，当前指向 v4-flash | spike-03 + 官方文档 |
| MCP 返回分层 | `ainvoke` 返回 content block 列表 `[{type:text,text:...}]`，非字符串；内层 JSON 是 `{ok,data}` | spike-02 |
| approval_id 注入 | `create_react_agent` 不自动注入 approval_id 到 tool args；resume 后需 `InjectedToolArg` 或自定义 ToolNode | spike-04 |
| async 路径 Checkpointer | `create_react_agent` 是 async，sync `SqliteSaver` 抛 InvalidStateError → 用 `AsyncSqliteSaver.from_conn_string()` | spike-04 |
| get_state 异步 | `app.get_state` 同步但 AsyncSqliteSaver 拒同步 → 用 `await app.aget_state` | spike-04 |
| Fernet key 编码 | PBKDF2 出 32 raw bytes，Fernet 要 32 urlsafe base64 bytes → `base64.urlsafe_b64encode()` | M1 实现 |
| vault.enc 边界 | §8.3 两个变长 Fernet token（wrapped_key/ct）连续存放、未定义分隔。M1 用 wrapped_key=b"" 绕过；启用恢复短语时须加长度前缀 | 设计 §8.3 待补 |
| 设计文档矛盾 | §3.7 `get` 示例扁平 `bundle[name]` vs §4.6 点路径 → 已取 §4.6 语义实现 | M1 审查 |
| 依赖版本 | 勿用 `==` pin；langchain-mcp-adapters 需 langchain-core>=0.3.36，`>=` pin 解析到主版本 | spike |

---

## 6. 进度：待办

### Milestone 映射（设计 §10.1/§10.2 优先路径）
| MS | 交付物 | 状态 |
| :--- | :--- | :--- |
| M1 基建与控制台 | §3.7 Vault + §3.4 白名单/模板 + §4.3 注入测试（§3.1 前端、§7.4 Docker 后续） | ✅ 核心完成（Vault+执行引擎+注入测试）；前端 Onboarding/Dashboard 未做 |
| M2 探测网关与工具链 | §3.4 真 asyncssh 连接池+Semaphore(4)+超时熔断、§5.2/5.3 工具、§4.2 白名单+正则+远端硬化、§5 日志截断 | ⏳ 下一步 |
| M3 Agent 大脑接入 | §3.5 LangGraph+Provider+Checkpointer、§3.3 MCP Server、§6.1 Chat 流、§8.1 investigations/records | ⏳ |
| M4 Webhook 全自动闭环 | §6.2 事件流、§6.3 状态机、§6.4 去重事务、§6.6 Token 四道闸、Telegram 推送 | ⏳ |
| M5 知识库自进化 | §3.6 Chroma+混合检索、§5.5 query_runbook、§8.2 向量元数据、SOP 审核面板 | ⏳ |
| 跨里程碑 | §5.7 错误码规约、§7.5 重试矩阵、§11 灾难恢复（建议 M3 起逐步落地） | ⏳ |

### M1 审查遗留（非 M1 范围，按归属消化）
| 项 | 内容 | 归属 |
| :--- | :--- | :--- |
| B | `ExecutionEngine` 接口签名偏差：M1 同步返回 `list[str]`、无 `wait_slot`、无 Semaphore；设计要 `async -> ExecResult` + `wait_slot` + 连接池 | M2（接 asyncssh 自然改） |
| C1 | asyncssh `shlex.quote` 行为验证（§4.3 验收项） | M2 |
| C2 | `approval_id` 一次性消费/复用被拒（§4.3 验收项） | M3 PermissionGate |
| E | mypy 3 个 `no-any-return`（TEMPLATES 值 Any、`json.loads` Any、`bundle[name]` Any） | 后续清类型化 |

### 恢复短语（§3.7 决策#17）
M1 未实现 BIP-39 助记词。vault.enc 已留 16B recovery_salt 占位 + 空 wrapped_key。启用时须：生成助记词 → 派生 recovery_key → 包裹 Fernet key 落 wrapped_key → 定义变长边界（长度前缀）。

---

## 7. M2 卡点（下一步行动前必须解决）

**M2 要连真 SSH 靶机。** 当前无靶机环境。两条路：
1. **有靶机** → 直接接真 asyncssh，跑通 §5.2 `execute_discovery` + §5.3 `fetch_service_logs`。
2. **无靶机** → 先做 `FakeSSHExecutor` stub（spike 已验证可行），把接口和连接池骨架搭起来，靶机就位再接真实现。

需向用户确认靶机状态后再动 M2。

---

## 8. 开发约定

- **提交规范:** `type(scope): 中文描述`，type 用 feat/fix/docs/chore/test/refactor。描述具体到缺陷点。
- **测试:** 新功能配测试，标记 `@pytest.mark.injection`/`budget`/`crash` 按类别。
- **类型:** 目标 mypy strict 全绿（当前 3 遗留，E 类）。新增代码尽量带类型。
- **安全:** 任何外发内容（LLM 上下文、提交信息、文档）不得含 `.env` 值或凭证。
- **改动设计文档:** 须附 `> 🔧 [来源] 修改说明` 注记，保留修订溯源。
- **不擅自动手:** 用户偏好分批确认需求；动手前对齐方案，尤其涉及多文件/架构选择时用 EnterPlanMode。

---

## 9. 立即可做的下一步

1. 读 `docs/DETAILED_DESIGN_v1.2.md` §3.4、§5.2、§5.3、§4.2（M2 范围）。
2. 问用户：SSH 靶机环境是否就绪？
3. 据答案定 M2 路径（真接入 vs stub），进 EnterPlanMode 出方案后再写码。

> 本项目优先级：安全 > 可逆性 > 泛用性 > 性能 > 功能广度（见 TRADEOFFS §0）。M1 已守住安全红线。
