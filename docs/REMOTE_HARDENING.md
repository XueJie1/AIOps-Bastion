# 远端 SSH 硬化指南 (rbash 第三道防线)

> 设计 §4.2 [评审补充#R7] / §4.3 验收项。M2 范围。
> 适用: 业务节点 (≤3 台), Bastion 经专用 key 登录, 仅允许受限命令集。
> **本指南面向靶机侧操作, 由用户在业务节点上执行。**

---

## 0. 为什么需要

三层安全防线 (§4.2):
1. **核心** - Bastion 侧 `IDENT_RE` fullmatch: 参数进入命令前拒绝一切 shell 元字符。
2. **第二** - Bastion 侧 `shlex.join(argv)`: 即便含元字符也单引号转义。
3. **第三 (本指南)** - 远端 `authorized_keys` forced-command `command="rbash"`:
   远端 shell 即便收到异常字符串, 也仅能在受限 shell 内执行,
   彻底消除 shell 元字符解析面 (纵深防御, 不依赖 Bastion 侧校验)。

> 🔧 [P2-7 修正] asyncssh `run()` 接受单 command 字符串且原样下发、不做引用
> (源码核对, 见设计 §3.4)。故第二道防线由 Bastion 侧 `shlex.join` 显式持有;
> 第三道防线 rbash 进一步兜底。

---

## 1. 生成 Bastion 专用 keypair

**不动你现有的通用 SSH key。** 为 Bastion 单独生成一对:

```bash
# 在 Bastion 主机 (或本地) 生成专用密钥 (ed25519, 无 passphrase 或独立 passphrase)
ssh-keygen -t ed25519 -f ~/.ssh/aiops_bastion_ed25519 -C "aiops-bastion"
# 产出:
#   ~/.ssh/aiops_bastion_ed25519      (私钥, 仅本地, 后续入 Vault)
#   ~/.ssh/aiops_bastion_ed25519.pub  (公钥, 下发到靶机)
```

私钥 `aiops_bastion_ed25519` **绝不入 git、绝不入日志**。后续经 Vault
`update_credential(["ssh_keys", "<host>"], <私钥内容>)` 入内存态。

---

## 2. 靶机侧: 建 rbash 登录账户 (可选但推荐)

为隔离权限, 建议为 Bastion 建专用账户 (复用现有账户亦可, 见 §3 备选):

```bash
# 在业务节点上 (root 或 sudo)
sudo useradd -m -s /bin/rbash bastion-ops
# -s /bin/rbash: 登录 shell 即受限 bash
```

> 若系统无 `/bin/rbash`, 链接: `sudo ln -s /bin/bash /bin/rbash`
> (rbash 即 `bash -r`, 启动时进入受限模式)。

确保该账户能执行运维所需命令 (systemctl/docker/journalctl 通常在 `/usr/bin`):
```bash
# rbash 默认 PATH 含 /usr/bin; 若命令在非标路径, 需配置 PATH
# 一般无需额外配置, /usr/bin 已含 systemd/docker 工具
```

---

## 3. 靶机侧: authorized_keys (restrict, 受限靠登录 shell)

将 Bastion 公钥追加到靶机账户的 `authorized_keys`, 加 `restrict`:

```bash
# 切到靶机账户
sudo -u bastion-ops bash

# 若 .ssh 不存在
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys

# 追加 Bastion 公钥 (粘贴 aiops_bastion_ed25519.pub 内容), 带 restrict
echo 'restrict ssh-ed25519 AAAA... bastion' >> ~/.ssh/authorized_keys
```

**关键选项说明:**
- `restrict`: 禁用端口转发 / X11 / agent 转发 / pty 等 (仅留 exec)。
- **受限 shell 靠账户登录 shell = `/bin/rbash`**(§2 `useradd -s /bin/rbash`): SSH
  执行 `ssh host "cmd"` 时, sshd 用登录 shell 跑 `rbash -c "cmd"`, 受限语义自然生效。

> ⚠️ **不要用 `command="/bin/rbash"`**: 该 forced-command 会让 sshd 执行
> `rbash -c "/bin/rbash"`, 而 rbash 铁律之一是**拒绝运行命令名含 `/` 的命令**,
> 于是 rbash 拒绝自己 -> **所有命令(含放行的 systemctl status)都跑不通**
> (报 `rbash: /bin/rbash: restricted: cannot specify '/' in command names`)。
> `command=` 的语义是"忽略客户端命令、强制执行 X", 与"让客户端命令在受限 shell 里跑"
> 是两回事。既然登录 shell 已是 rbash, forced-command 完全不需要。

> **备选 (不建专用账户, 复用现有账户):** 现有账户若登录 shell 不是 rbash, 无法靠
> 登录 shell 受限。此时改用 **forced-command wrapper 脚本**(见 §3b), 或接受仅前两道
> 防线 (IDENT_RE + shlex.join, 已足够拦截注入)。

### 3b. 备选: forced-command 白名单 wrapper (更精确, 不依赖 rbash -c 行为)

若需比 rbash 更精确的命令白名单 (只放行 systemctl status / docker inspect /
journalctl 等), 用 wrapper 脚本承接 `$SSH_ORIGINAL_COMMAND`:

```bash
# 靶机: /usr/local/bin/aiops_ssh_wrapper.sh (0755, root 属主)
#!/bin/bash
set -euo pipefail
cmd="${SSH_ORIGINAL_COMMAND:-}"
case "$cmd" in
  "systemctl status "*|"systemctl is-active "*|"docker inspect "*|\
  "docker ps"*|"docker compose ps "*|"journalctl -u "*|"docker logs "*|"echo "*)
    exec bash -c "$cmd" ;;
  *) echo "rejected by wrapper: $cmd" >&2; exit 126 ;;
esac
```
```bash
# authorized_keys 指向 wrapper
echo 'restrict,command="/usr/local/bin/aiops_ssh_wrapper.sh" ssh-ed25519 AAAA... bastion' \
  >> ~/.ssh/authorized_keys
```
> wrapper 本身须安全审查 (case 模式勿含通配漏洞); asyncssh 尊重 `command=`
> (源码 `channel.py:1680 get_key_option('command')`)。此方案不依赖 rbash `-c`
> 的受限严格度, 白名单显式可控。

---

## 4. 验证: 放行命令通 + 受限命令拒

在 Bastion 侧 (配置好 Vault 私钥后), 跑集成测试:

```bash
# 必需 env
export AIOPS_TEST_SSH_HOST=<靶机 host>
export AIOPS_TEST_SSH_KEY=~/.ssh/aiops_bastion_ed25519   # 私钥路径
export AIOPS_TEST_SSH_USER=bastion-ops                    # 或你的账户
# 可选: 真实服务名 (用于 L1/L2 探测)
export AIOPS_TEST_SSH_SERVICE=nginx                       # 或 docker 容器名
export AIOPS_TEST_SSH_FORM=systemd                         # 或 docker

.venv/bin/python -m pytest tests/test_integration_ssh.py -v
```

期望:
- `test_c1_shlex_join_roundtrip` PASS: `echo 'nginx; rm -rf /'` 往返为字面量
  (证明第二道防线 `shlex.join` 生效)。
- `test_rbash_restricted_command_rejected` PASS: `cd /tmp` 被拒
  (证明第三道防线 rbash 生效)。
- `test_l1_discovery_real_target` / `test_l2_fetch_logs_real_target` PASS:
  真实 systemctl/docker/journalctl 在 rbash 下可执行 (PATH 含 /usr/bin)。

若 `test_rbash_restricted_command_rejected` 失败 (cd 未被拒),
说明 `authorized_keys` 的 `command=` 未生效 -- 检查 §3 配置。

---

## 5. PATH 注意事项

rbash 受限模式下, 若 `systemctl`/`docker`/`journalctl` 不在默认 PATH,
放行命令也会 "command not found"。确认:

```bash
# 在靶机以 bastion-ops 登录, 或: rbash -c 'echo $PATH'
sudo -u bastion-ops rbash -c 'which systemctl docker journalctl'
```

应输出 `/usr/bin/systemctl` 等。若缺失, 在账户的 `~/.bashrc` (rbash 启动读)
显式 `export PATH=/usr/bin:/bin` (rbash 允许设置 PATH, 但之后不可用绝对路径绕过)。

> 注: rbash 限制 cd / 重定向 / 修改 PATH 后绕过 等, 但**不限制命令本身的内容**。
> 真正限制命令集的是 Bastion 侧白名单 (防线#1/#2)。rbash 是"消除 shell 解析面"
> 的兜底, 非"命令白名单"。

---

## 6. 安全红线复核

- [ ] Bastion 专用私钥 `aiops_bastion_ed25519` 未入 git (`.gitignore` 或仅存本地)。
- [ ] 私钥经 `update_credential` 入 Vault 内存, 绝不落日志 / 绝不出网。
- [ ] 靶机 `authorized_keys` 仅含 Bastion 公钥行 (或专用账户独立)。
- [ ] `restrict` 已设 (禁端口转发等)。
- [ ] 集成测试 `test_rbash_restricted_command_rejected` PASS。

---

## 7. 故障排查

| 现象 | 原因 / 修复 |
| :--- | :--- |
| `rbash: /bin/rbash: restricted: cannot specify '/' in command names` (所有命令都跑不通) | authorized_keys 用了 `command="/bin/rbash"`, 自我拒绝。删掉 `command=` 段, 受限靠登录 shell (§3)。 |
| `Permission denied (publickey)` | 私钥与靶机公钥不匹配; 或 `restrict` 过严禁了 exec (不应)。检查 §1/§3。 |
| `cd /tmp` 未被拒 (rbash 测试失败) | 登录 shell 非 `/bin/rbash` (`getent passwd bastion-ops` 确认); 或 `/bin/rbash` 不存在 (`ln -s /bin/bash /bin/rbash`); 或该 bash 版本 `-c` 模式受限不严 -> 改用 §3b wrapper。 |
| `systemctl: command not found` | rbash PATH 不含 /usr/bin。见 §5。 |
| `Host key verification failed` | 设 `AIOPS_TEST_SSH_KNOWN_HOSTS` 指向 known_hosts, 或首次 `ssh-keyscan` 入库; 未设则默认禁用校验 (作品集权衡)。 |
| 连接超时 | 防火墙 / SSH 端口; 设 `AIOPS_TEST_SSH_PORT`。 |
