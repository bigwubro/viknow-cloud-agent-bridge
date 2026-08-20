忽略本跳板仓库的本地搭建。不要在本仓库写业务代码。

**先读** `.cursor/skills/viknow-236-ssh-proxy/SKILL.md`（或 `AGENTS.md`）。  
**禁止直连** `36.103.198.236`（机房入站丢包，:22 必 timeout）。

## 连接 236（经阿里云跳板 8.145.57.129）

Secrets：`VIKNOW_236_SSH_KEY`、`VIKNOW_BASTION_SSH_PASSWORD`

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
printf '%s\n' "$VIKNOW_236_SSH_KEY" | sed '/./,$!d' > ~/.ssh/id_ed25519
chmod 600 ~/.ssh/id_ed25519
command -v sshpass >/dev/null || sudo apt-get install -y sshpass

ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
  -o ProxyCommand="sshpass -p \"$VIKNOW_BASTION_SSH_PASSWORD\" ssh -o StrictHostKeyChecking=no -W %h:%p root@8.145.57.129" \
  -p 22 cursor-agent@36.103.198.236 'echo ok && hostname && whoami'
```

期望：`ok` / `36-103-198-236` / `cursor-agent`。通过后再继续。

## 开发流程

1. `docker exec viknow2-test-dev bash`（或非交互 `docker exec ... bash -lc`）
2. `cd /opt/viknow`（容器内工作目录，不是 `/workspace`）
3. `git fetch origin && git checkout -B agent/<topic> origin/main`
4. 完成任务：<在这里写具体需求>
5. 在 test-dev 冒烟（容器内 :8000 或宿主机 :5176）
6. commit 后 `git push -u origin agent/<topic>`
7. 汇报分支名、相对 origin/main 的 log/diff --stat

禁止：直连 236 公网 IP；推 main；动 viknow2-test / private；把密钥写入提交。
