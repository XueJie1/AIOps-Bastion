# AIOps-Bastion 任务交接文档

> 给开新 session 用。自包含，读完即可进入状态。
> 最后更新: 2026-07-15 | 对应提交 (M3 待提交) | 本地领先 GitHub

---

## 0. 一句话现状

**M3 Agent 大脑接入完成（代码+单测+真 LLM 集成全通过，148 测试全绿，待提交）。下一步 M4。**
M3 落地 LangGraph react agent + MCP Server handler + PermissionGate(C2 一次性消费) + AsyncSqliteSaver Checkpointer + Store Protocol(InMemory/SQLite) + FakeLLM/真 deepseek-v4-pro。两个 spike 暴露点已据源码核对落地：interrupt-in-tool 模式(approval_id 经 interrupt() 返回值透传)、in-process MCP(原生 StructuredTool 调共享 handler)。
M2 把 M1 同步执行桩升级为真 asyncssh 执行引擎 + §5.2/§5.3 工具 + rbash 第三道防线端到端验证路径。
源码核对发现并修正了设计 §3.4 [P2-7] 的一处事实错误（asyncssh 不引用）。

---

## 1. 项目本质与硬约束（必须先读）

- **这是什么:** 基于 MCP + RAG 的多节点智能运维堡垒（AIOps-Bastion）。
- **定位: 求职作品集，非生产系统。** 精致度是卖点，但每个决策须能在面试讲清取舍。详 `docs/TRADEOFFS_v1.2.md`。
- **架构:** 单进程 asyncio。LangGraph + LangChain Provider（§3.5）+ MCP Server stdio（§3.3）+ asyncssh（§3.4）+ Chroma RAG（§3.6）+ Firebase（§3.2）+ Cloudflare Tunnel ingress + Docker Compose（§7.4）。
- **规模:** ≤3 台业务节点，单用户，弱 NAS（2C4G）。
- **三层安全防线（§4.2，M2 已据源码修正）:** ① `IDENT_RE` fullmatch（核心）-> ② 执行器 `shlex.join(argv)`（第二，我方代码）-> ③ 远端 `rbash` forced-command（第三，靶机侧）。**注: asyncssh `run()` 不引用**（见 §5 坑）。
- **HITL 语义:** 单用户模型 -> "防误触确认"，非权限隔离。
- **用户协作方式:** 用户不熟 Agent 开发，需求须分批提问确认，勿猜测意图。给方案时附取舍。

### 安全红线（不可破）
- `.env` 含真实 DeepSeek API key，用户自填，绝不外传/不写入任何外发内容。
- 凭证（Master Password/私钥/API Key）绝不落盘、绝不出网，仅 Vault 内存态。
- 原始日志全文不出网；Agent 本地摘要后才送 LLM（§4.5）。
- Bastion 专用 SSH 私钥仅本地文件 -> Vault，绝不入 git/日志/LLM。

---

## 2. 环境与工具链

| 项 | 值 |
| :--- | :--- |
| 工作目录 | `/home/cao/workspace/get-a-job/aiops` |
| 远程 | `origin` = `git@github.com:XueJie1/AIOps-Bastion.git`（main 已跟踪） |
| Python | 3.14（pyproject 要求 >=3.11；venv 实装 3.14.6） |
| venv | `.venv/`（已装 dev 依赖，含 pytest/ruff/mypy） |
| 跑测试 | `.venv/bin/python -m pytest -q` |
| 跑 lint | `.venv/bin/ruff check src tests` |
| 跑类型 | `.venv/bin/python -m mypy src`（strict，CI 中 continue-on-error） |
| 配置 | `pyproject.toml`（ruff line-length 100、mypy strict + ignore_missing_imports、pytest asyncio_mode=auto、markers: injection/budget/crash/integration） |
| CI | `.github/workflows/ci.yml`（ruff + mypy + pytest + coverage） |
| 真靶机集成 | 设 env `AIOPS_TEST_SSH_HOST`/`AIOPS_TEST_SSH_KEY` 等后跑 `pytest -m integration`（无 env 自动 skip） |

> 系统 PATH 里没有 pytest/ruff/mypy，须用 `.venv/bin/` 前缀。

---

## 3. 文档地图

| 文件 | 内容 |
| :--- | :--- |
| `docs/DETAILED_DESIGN_v1.2.md` | 详细设计 v1.3（文件名未改，标题行注 v1.3 修订记录）。M2 已加 4 处 🔧 修订注记。 |
| `docs/REQUIREMENTS.md` | PRD |
| `docs/TRADEOFFS_v1.2.md` | 取舍说明，作品集面试用 |
| `docs/SPIKE_REPORT.md` | 4 个 spike 验证结果 |
| `docs/REMOTE_HARDENING.md` | 靶机 rbash 第三道防线硬化步骤（M2 新增） |
| `docs/HANDOFF.md` | 本文件 |
| `spike/01-04_*.py` | 4 个验证脚本（留作参考，非生产代码） |

设计文档章节速查：§3.3 MCP Server / §3.4 执行引擎 / §3.5 Agent / §3.6 RAG / §3.7 Vault / §4 安全 / §5 工具集 / §5.7 错误码 / §6 工作流 / §7 非功能 / §8.3 vault.enc 格式 / §10 milestone 映射 / §11 灾难恢复。

---

## 4. 进度：已完成

### Git 历史（最新在上）
```
(M3 待提交) feat(m3): Agent 大脑 - react agent + MCP handler + PermissionGate(C2) + Checkpointer
af10ff2 feat(m2): 探测网关 - 真 asyncssh 执行引擎 + 工具链 + rbash 三层防线
88a9024 docs: 任务交接文档 HANDOFF.md
5e5c0f3 docs: §8.3 同步 spike-03 - deepseek-v4-pro / active=deepseek
dc60c3b chore: gitignore 补 Pi agent 忽略
1b0ee4d fix(m1): 3 项审查缺陷 - approval_id 错误码 / 嵌套凭证 / vault.enc 格式
fb3e349 feat(m1): 安全地基 - 执行引擎白名单/模板 + Vault 凭证加密 + 注入测试
fae8438 chore: 项目脚手架 - pyproject + Docker + CI + 包结构
e538544 docs: 设计文档 v1.3 - 据 spike 验证落地 7 项修订
0409564 chore: 项目基线 - 设计文档 + 取舍说明 + spike 验证
```
> **M2 已提交并推 GitHub**（commit `af10ff2`，公开仓库；HANDOFF 真实靶机信息已脱敏）。
> **M3 代码+单测+真 LLM 集成全过，待提交**（设计 🔧 修订 + TRADEOFFS 已同步；真实信息脱敏基线维持）。

### M1 安全地基（已完成，已审查修复）
源码三件 + 测试三件，34 测试全过、ruff 干净：

| 文件 | 职责 |
| :--- | :--- |
| `src/aiops_bastion/exceptions.py` | 异常体系（对齐 §5.7 错误码；M2 加 ExecTimeoutError/SSHConnectionError） |
| `src/aiops_bastion/execution.py` | 执行引擎：M2 重构为 AsyncSSHExecutor + SSHExecutor Protocol + build_logs_cmd |
| `src/aiops_bastion/vault.py` | Vault：PBKDF2+Fernet 全异步、嵌套点路径、§8.3 格式（§3.7） |
| `tests/test_injection.py` | 注入对抗 + 白名单边界 + L3 越权 + C1 shlex.join（§4.3） |
| `tests/test_vault.py` | Vault 生命周期 + 异步 + 损坏拒绝 + 权限 |
| `tests/test_smoke.py` | 冒烟（导入、Python 版本） |

**M1 已验证：** 8 类注入 payload 全拒、IDENT_RE fullmatch、L3 仅 3 枚举无 reboot、clear_cache 路径精确匹配、approval_id 缺失拒（HITLRejectedError）、PBKDF2 异步不阻塞、Vault 全生命周期、嵌套凭证热更新、vault.enc 损坏拒绝、文件权限 0600。

**M1 审查发现并修复的 3 个真实缺陷（commit 1b0ee4d）：**
- A1 `approval_id` 缺失误用 VALIDATION_ERROR -> 改 HITL_REJECTED
- A2 `update_credential`/`get` 原扁平 `bundle[name]` 不支持 §4.6 点路径 -> 加 `_split_path`/`_get_path`/`_set_path`
- A3 vault.enc 未写 16B recovery_salt 占位 -> 对齐 §8.3，ct 从偏移 41 读

### M2 探测网关与工具链（已完成，真靶机集成已通过）
执行引擎真接入 + 工具链 + 安全验收。**91 测试全过（87 单测 + 4 真靶机集成）、ruff 干净、src mypy 3 E 类基线无新增**：

| 文件 | 职责 |
| :--- | :--- |
| `src/aiops_bastion/exceptions.py` | +ExecTimeoutError（EXEC_TIMEOUT，kind=exec/queue）+SSHConnectionError（INTERNAL） |
| `src/aiops_bastion/execution.py` | AsyncSSHExecutor（pool+Semaphore+超时+wait_slot+vault取钥+shlex.join）+ SSHExecutor Protocol + build_logs_cmd |
| `src/aiops_bastion/tools.py` | execute_discovery（三形态状态映射）+ fetch_service_logs（lines≤500、token 截断）+ {ok,data,error} 契约 |
| `tests/fakes.py` | FakeSSHExecutor（按脚本返回，仍先走真实校验） |
| `tests/test_executor.py` | AsyncSSHExecutor 单测（mock asyncssh：sem/wait_slot/exec超时/连接复用/失败重建/校验先于连接） |
| `tests/test_tools.py` | 工具层单测（FakeSSHExecutor：状态映射/截断/错误码传播） |
| `tests/test_injection.py` | 迁移 + C1 shlex.join 单元 + asyncssh 版本断言 |
| `tests/conftest.py` | 集成 fixture（env 门控，私钥仅本地入 Vault） |
| `tests/test_integration_ssh.py` | 真靶机 L1/L2 + C1 echo 往返 + rbash 拒绝（env 门控） |
| `docs/REMOTE_HARDENING.md` | 靶机 rbash 硬化步骤（专用 key + authorized_keys + PATH） |

**M2 已验证（单测层）：** 8 注入 payload 回归不破、shlex.join 单元转义、Semaphore≤4 并发、wait_slot 超时 EXEC_TIMEOUT(queue)、exec 超时 EXEC_TIMEOUT(exec)+连接剔除、连接按 host 复用、失败重建、校验先于连接、三形态状态映射、lines/token 截断、错误码传播、asyncssh≥2.14。
**M2 已验证（真靶机 / 专用账户 / systemd ssh 服务，2026-07-11 通过）：** L1 探测 ssh 服务、L2 journalctl 取日志+截断、C1 echo 往返（元字符字面量、无注入）、rbash 第三道防线拒 cd/重定向。4 集成测试全过。靶机无 docker，docker 形态由 Fake 单测覆盖。

> ⚠️ **rbash 配置坑（M2 实战修正）：** 初版 REMOTE_HARDENING.md §3 写 `command="/bin/rbash"` 是**错的**——sshd 执行 `rbash -c "/bin/rbash"`，rbash 拒绝运行含 `/` 的命令名（拒绝自己），导致**所有命令跑不通**。正解：authorized_keys 只留 `restrict`，受限靠账户登录 shell = `/bin/rbash`（`useradd -s /bin/rbash`）。文档已修正 + 加 §3b wrapper 备选。实测这台 Debian bash 的 `rbash -c` 模式受限严格（cd/重定向均拒）。


### M3 Agent 大脑接入（已完成，待提交）
LangGraph react agent + MCP Server handler + PermissionGate(C2) + Checkpointer + Store Protocol。**148 测试全过（M2 的 91 + M3 新增 57）、ruff 干净、src mypy 3 E 类基线无新增**：

| 文件 | 职责 |
| :--- | :--- |
| `src/aiops_bastion/store.py` | Store Protocol + Investigation/Record/HitlRequest dataclass(§8.1) + InMemoryStore + SqliteStore(aiosqlite, consume_hitl 原子 SQL) |
| `src/aiops_bastion/permission_gate.py` | PermissionGate: validate_and_consume 校验(存在/状态==APPROVED/未过期/归属匹配) + 原子消费(C2) |
| `src/aiops_bastion/llm.py` | build_llm(§3.5 openai/anthropic, Vault 取 key) + FakeLLM(脚本式 BaseChatModel, bind_tools no-op) |
| `src/aiops_bastion/mcp_server.py` | in-process Server + 4 handler(transport-agnostic) + build_server + extract_text(spike-02 分层) |
| `src/aiops_bastion/agent.py` | build_agent(create_react_agent + AsyncSqliteSaver) + 4 原生 @tool + L3 interrupt-in-tool + SRE prompt |
| `tests/test_store.py` | InMemory+Sqlite 契约(20): CRUD + C2 一次性消费 + Sqlite 跨实例持久化 |
| `tests/test_permission_gate.py` | C2 四断言(14, 标 injection): 二次消费/过期/未审批/归属不符 |
| `tests/test_llm.py` | build_llm 构造 + FakeLLM 脚本推进(10) |
| `tests/test_mcp_server.py` | in-process 4 工具契约(10): {ok,data} + L3 缺 approval_id 拒 + C2 复用拒 |
| `tests/test_agent.py` | FakeLLM 全图(7, 标 crash): L1->L2->L3 interrupt->resume->消费->执行1次(不重放) + reject->ABORTED + 元字符拒 |
| `tests/test_integration_agent.py` | env-gated 真 deepseek-v4-pro(2, 标 integration): L1 选工具 + L3 interrupt/resume 真模型驱动 |

**M3 已验证（FakeLLM 单测层）：** L3-only interrupt(非全工具)、approval_id 经 interrupt() 返回值透传(LLM 不可见)、resume(approval_id)->消费->执行恰好1次(不重放)、reject(resume={"rejected":True})->ABORTED+L3未执行、approval_id 复用->HITL_REJECTED(C2)、元字符 unit->render 拒、L1/L2 自主不 interrupt、submit_journal 写 Store、工具结果 JSON 可解析。
**M3 已验证（真 LLM 集成 / deepseek-v4-pro / 2026-07-15 通过）：** 真 LLM 选 execute_discovery(L1) + 解析工具结果；强 prompt 下走到 L3 interrupt -> approve -> resume -> 执行1次 + approval_id CONSUMED。2 集成测试全过。

> ⚠️ **approval_id 注入机制（M3 实施修订，spike-04 暴露点#2）：** 设计 §3.3 原推荐方案 A `InjectedState("approval_id")`（已源码核对可行：tool_call_schema 隐藏 + ToolNode 剥离 LLM 伪造值）。但 M3 **改用方案 D interrupt-in-tool**（更简，安全语义等价）：L3 工具签名不含 approval_id，工具内 `interrupt(preview)` 挂起，resume 时 `interrupt()` 返回值即 approval_id。消除 InjectedState 在 resume 重跑 ToolNode 时的重注入不确定性。设计 §3.3/§6.7 已加 🔧 修订注记，TRADEOFFS §1.15 记取舍。
>
> ⚠️ **MCP 传输（M3 实施修订，spike-02 暴露点#1）：** M3 工具为原生 StructuredTool（非经 MCP 运行时传输）--原因：MCP-loaded 工具无法调 LangGraph `interrupt()`。4 工具调 `mcp_server` 共享 handler（唯一运维出口，PermissionGate+执行引擎），MCP in-process 传输仅由 `test_mcp_server.py` 验证契约。stdio 子进程延后。设计 §3.3 已加 🔧 修订注记，TRADEOFFS §1.14 记取舍。
>
> ⚠️ **持久层（M3 实施修订）：** 取 Store Protocol + InMemory/SQLite（真 Firebase 延后，需 GCP+Emulator，CI 重）。Protocol 已对齐 §8.1 字段，未来 FirebaseStore 即插即用。TRADEOFFS §1.16 记取舍。

### Spike 验证（已完成，4 个脚本）
3 PASS + 1 PARTIAL FAIL（根因已定位）：
- 01 LangGraph interrupt + SqliteSaver 崩溃恢复 - PASS
- 02 MCP in-process 加载（`create_connected_server_and_client_session` + content block 分层）- PASS
- 03 DeepSeek tool-calling - PASS（`deepseek-v4-pro` 真实可用、默认思考模式）
- 04 E2E HITL - PARTIAL FAIL：`create_react_agent` 不自动注入 approval_id，需 InjectedToolArg 或自定义 ToolNode（设计 §3.3 修订点）

---

## 5. 已知坑（动手前必看）

| 坑 | 真相 | 出处 |
| :--- | :--- | :--- |
| **asyncssh 不引用命令** | `SSHClientConnection.run()` 接受**单个 command 字符串**且 `make_request(b'exec', String(command))` **原样下发**、不做引用；包内 `shlex.quote` 仅用于 `proxy_command`。**设计 §3.4 [P2-7] 误述为 asyncssh 会 shlex.quote list 元素，已 M2 修正**：第二道防线改由执行器 `shlex.join(argv)` 持有。`asyncssh.quote` 不公开。 | M2 源码核对（asyncssh 2.24.0） |
| asyncssh forced-command 生效 | asyncssh 尊重 `authorized_keys` 的 `command=`（源码 `channel.py:1680 get_key_option('command')`）-> rbash 第三道防线可行 | M2 源码核对 |
| read_private_key 只吃路径 | `asyncssh.read_private_key(filename)` 不吃字符串；Vault 返回私钥串，须用 `asyncssh.import_private_key(data: bytes\|str)` | M2 实现 |
| DeepSeek 模型 id | `deepseek-v4-pro` 真实可用（默认思考模式）；`deepseek-chat`/`deepseek-reasoner` 是旧别名，2026-07-24 停用，当前指向 v4-flash | spike-03 + 官方文档 |
| MCP 返回分层 | `ainvoke` 返回 content block 列表 `[{type:text,text:...}]`，非字符串；内层 JSON 是 `{ok,data}` | spike-02 |
| approval_id 注入 | `create_react_agent` 不自动注入 approval_id 到 tool args；resume 后需 `InjectedToolArg` 或自定义 ToolNode | spike-04 |
| async 路径 Checkpointer | `create_react_agent` 是 async，sync `SqliteSaver` 抛 InvalidStateError -> 用 `AsyncSqliteSaver.from_conn_string()` | spike-04 |
| get_state 异步 | `app.get_state` 同步但 AsyncSqliteSaver 拒同步 -> 用 `await app.aget_state` | spike-04 |
| Fernet key 编码 | PBKDF2 出 32 raw bytes，Fernet 要 32 urlsafe base64 bytes -> `base64.urlsafe_b64encode()` | M1 实现 |
| vault.enc 边界 | §8.3 两个变长 Fernet token（wrapped_key/ct）连续存放、未定义分隔。M1 用 wrapped_key=b"" 绕过；启用恢复短语时须加长度前缀 | 设计 §8.3 待补 |
| 设计文档矛盾 | §3.7 `get` 示例扁平 `bundle[name]` vs §4.6 点路径 -> 已取 §4.6 语义实现 | M1 审查 |
| 依赖版本 | 勿用 `==` pin；langchain-mcp-adapters 需 langchain-core>=0.3.36，`>=` pin 解析到主版本 | spike |
| 连接池并发 race | 同 host 并发首次建连会 race；M2 用 per-host `asyncio.Lock` + double-check 解决 | M2 实现 |

---

## 6. 进度：待办

### Milestone 映射（设计 §10.1/§10.2 优先路径）
| MS | 交付物 | 状态 |
| :--- | :--- | :--- |
| M1 基建与控制台 | §3.7 Vault + §3.4 白名单/模板 + §4.3 注入测试（§3.1 前端、§7.4 Docker 后续） | ✅ 核心完成；前端 Onboarding/Dashboard 未做 |
| M2 探测网关与工具链 | §3.4 真 asyncssh 连接池+Semaphore(4)+超时熔断、§5.2/5.3 工具、§4.2 白名单+正则+远端硬化、§5 日志截断 | ✅ 完成（代码+单测+真靶机集成全通过） |
| M3 Agent 大脑接入 | §3.5 LangGraph+Provider+Checkpointer、§3.3 MCP Server(handler)、§6.1 Chat 流、§8.1 investigations/records | ✅ 完成（代码+单测+真 LLM 集成全通过，待提交） |
| M4 Webhook 全自动闭环 | §6.2 事件流、§6.3 状态机、§6.4 去重事务、§6.6 Token 四道闸、Telegram 推送 | ⏳ |
| M5 知识库自进化 | §3.6 Chroma+混合检索、§5.5 query_runbook、§8.2 向量元数据、SOP 审核面板 | ⏳ |
| 跨里程碑 | §5.7 错误码规约、§7.5 重试矩阵、§11 灾难恢复（建议 M3 起逐步落地） | ⏳ |

### M1 审查遗留消化情况
| 项 | 内容 | 归属 | 状态 |
| :--- | :--- | :--- | :--- |
| B | `ExecutionEngine` 接口签名偏差（同步返回 list[str]、无 wait_slot/Semaphore/连接池） | M2 | ✅ M2 消化（AsyncSSHExecutor: async->ExecResult + wait_slot + Semaphore + pool） |
| C1 | asyncssh `shlex.quote` 行为验证（§4.3 验收项） | M2 | ✅ M2 消化（修正为 shlex.join；单元 + 真靶机集成路径就绪） |
| C2 | `approval_id` 一次性消费/复用被拒（§4.3 验收项） | M3 PermissionGate | ✅ M3 消化（PermissionGate.validate_and_consume + Store.consume_hitl 原子双保险；4 断言过） |
| E | mypy 3 个 `no-any-return`（TEMPLATES 值 Any、`json.loads` Any、`bundle[name]` Any） | 后续清类型化 | ⏳ 仍 3 个（M3 无新增） |

### M2 范围外（明确延后）
- §3.3 MCP Server / stdio 包装 / `@server.call_tool` 注册 -> **M3**（§10.1 映射）。M2 交付可直测的 async 工具函数。
- `execute_remediation` MCP 工具 + PermissionGate（approval_id 一次性消费 = C2）-> **M3**。M2 只建 `run_remediation` 执行原语。
- §7.2 指标/可观测（queue_depth、slot 占用率、structlog）-> **跨里程碑，M3+**。
- 周期性后台健康探活（§7.1 R16）-> M2 用「失败即重建」简化版。
- compose 日志（需先解析容器名）-> 延后。

### 恢复短语（§3.7 决策#17）
M1 未实现 BIP-39 助记词。vault.enc 已留 16B recovery_salt 占位 + 空 wrapped_key。启用时须：生成助记词 -> 派生 recovery_key -> 包裹 Fernet key 落 wrapped_key -> 定义变长边界（长度前缀）。

---

## 7. M3 已完成 / 真 LLM 集成复现

**M3 真 LLM 集成测试已通过（2026-07-15）：** 真 `deepseek-v4-pro` 驱动 react agent 全图（对 FakeSSHExecutor）。复现命令（env 门控，无 env 自动 skip；API key 仅本地 `.env`/spike/.env，不入 git/日志/LLM）：
```bash
set -a; . spike/.env; set +a   # 或自建 .env 含 DEEPSEEK_API_KEY
export AIOPS_TEST_LLM_PROVIDER=deepseek
export AIOPS_TEST_LLM_MODEL=deepseek-v4-pro
export AIOPS_TEST_LLM_KEY="$DEEPSEEK_API_KEY"
.venv/bin/python -m pytest tests/test_integration_agent.py -v   # 2 测试, ~60s
```
真靶机 SSH 集成（M2 落地，env `AIOPS_TEST_SSH_HOST`/`AIOPS_TEST_SSH_KEY` 等）复现命令见 §4 M2 小节。

**M4 范围（下一步）：** §6.2 事件流（Webhook）+ §6.3 状态机驱动 + §6.4 去重事务（Firestore 事务语义，M3 用 SQLite 单机简化）+ §6.6 Token 四道闸（含 reasoning tokens 累计，§3.5 spike-03）+ Telegram 推送。可选同步落地：§6.8 Recovery Sweep + HITL 超时清扫器（§11 崩溃恢复，Checkpointer 已在 M3 就绪）、FastAPI `/chat` + SSE（§6.1 web 层）、真 FirebaseStore 后端（Store Protocol 已就绪）。

## 8. 开发约定

- **提交规范:** `type(scope): 中文描述`，type 用 feat/fix/docs/chore/test/refactor。描述具体到缺陷点。
- **测试:** 新功能配测试，标记 `@pytest.mark.injection`/`budget`/`crash`/`integration` 按类别。
- **类型:** 目标 mypy strict 全绿（当前 3 遗留，E 类；测试函数注解未完备，CI continue-on-error 不阻塞）。新增代码尽量带类型。
- **安全:** 任何外发内容（LLM 上下文、提交信息、文档）不得含 `.env` 值或凭证。
- **改动设计文档:** 须附 `> 🔧 [来源] 修改说明` 注记，保留修订溯源。
- **不擅自动手:** 用户偏好分批确认需求；动手前对齐方案，尤其涉及多文件/架构选择时用 EnterPlanMode。

---

## 9. 立即可做的下一步

1. **提交 M3**：`git add` 五源码(store/permission_gate/llm/mcp_server/agent)+六测试 + 设计🔧修订 + TRADEOFFS + 本 HANDOFF，提交 `feat(m3): Agent 大脑 - react agent + MCP handler + PermissionGate(C2) + Checkpointer`，推 GitHub（公开仓库，提交前脱敏复核）。
2. **M4 启动**：读 §6.2/§6.3/§6.4/§6.6，进 EnterPlanMode 对齐 Webhook 事件流 + 去重事务 + Token 四道闸方案。可选先补 Recovery Sweep（§6.8，Checkpointer 已就绪）或 FastAPI `/chat`（§6.1）。

> 本项目优先级：安全 > 可逆性 > 泛用性 > 性能 > 功能广度（见 TRADEOFFS §0）。M1 守住安全红线；M2 三层防线端到端贯通（真靶机验证）；M3 Agent 大脑闭环（interrupt-in-tool HITL + C2 一次性消费，FakeLLM 全图 + 真 deepseek-v4-pro 集成验证）。
