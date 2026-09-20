# 方案 B：反向隧道（不需要 ngrok 账号）

## 安全保证：不影响现有 `:22`

本方案 **只增加一条出站隧道**，不会：

- ❌ 修改 `sshd_config` / reload sshd
- ❌ 改 iptables / 防火墙
- ❌ 占用或替换 `:22` 端口监听
- ❌ 影响现有用户直连 `36.103.198.236:22`

隧道只是把外部流量 **转发到** 已在监听的 `127.0.0.1:22`，与现有连接并行存在。

安装前后 CI 会检查：`sshd` 仍监听 `:22`。

### 若之前跑过「Fix 236 port 22」workflow

该 workflow 会改 MaxStartups / iptables，**可能影响现有 :22**。
请再跑一次：**Revert port 22 hardening (restore existing SSH)** 恢复原状。

---

```
236 ──出站──► 公网隧道服务（bore / localhost.run / ngrok）
Cloud Agent ──► 隧道地址 ──► 236:22
```

**不需要**登网关、**不需要**直连 `36.103.198.236`，绕开 AWS IP 被拦。

---

## 你要做的（2 步）

### 1. 更新 Cursor Secret `VIKNOW_236_SSH_KEY`

Gitea Actions run **#602** 日志 → `CLOUD_AGENT_CURSOR_SSH_KEY` 完整私钥。

### 2. 触发 Gitea CI

**Actions** → **Setup reverse SSH tunnel for Cloud Agent**

| 参数 | 推荐值 |
|------|--------|
| branch | `agent/cloud-agent-tunnel` |
| backend | `localhost`（无需账号，优先试） |
| action | `install` |

打开 run 日志，找：

```
Cloud Agent: ssh -p 22222 cursor-agent@xxxxx.localhost.run
```

或 bore 成功时：

```
Cloud Agent: ssh -p XXXXX cursor-agent@bore.pub
```

---

## Cloud Agent 连接

```bash
ssh -p <PORT> cursor-agent@<HOST>
docker exec -it viknow2-test-dev bash
```

把 `<HOST>` / `<PORT>` 写进 `AGENTS.md`（隧道重启后可能变，用 `action=status` 再查）。

---

## backend 选项

| backend | 需要账号 | 说明 |
|---------|---------|------|
| `localhost` | ❌ | `ssh -R` 到 localhost.run，**推荐先试** |
| `bore` | ❌ | bore.pub（236 出站需能连 bore.pub） |
| `ngrok` | ✅ 免费注册 | 最稳定，Secret `NGROK_AUTHTOKEN` |

### 以后想用 ngrok（可选，5 分钟）

1. 注册 https://ngrok.com
2. 复制 Authtoken → Gitea Secret **`NGROK_AUTHTOKEN`**
3. workflow `backend=ngrok`

---

## 排查

```bash
# workflow action=status
systemctl status viknow-reverse-tunnel
tail -f /etc/viknow/reverse-tunnel/tunnel.log
```
