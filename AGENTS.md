# ViKnow Cloud Agent Bridge

本仓库是 **Cursor Cloud Agent 跳板**，不是 ViKnow 业务代码仓。

- 业务仓在 Gitea：`ssh://git@git.qingxiang.tech:2222/castmeta-research/viknow2.git`
- 真实工作区在 236：`viknow2-test-dev` 容器内（多为 `/opt/viknow`）
- Cloud Agent 启动后，**必须经阿里云跳板 SSH 正向代理** 到 236，再 `docker exec` 进开发容器

## Cursor Cloud specific instructions

> **⚠️ 不要直连 `36.103.198.236`**（任意端口均 timeout）。必须经跳板 `8.145.57.129`。  
> 若你正在执行 `prompts/feature.md` 或 `prompts/smoke.md`，其中已包含跳板命令；勿使用旧版「SSH 到 cursor-agent@36.103.198.236」直连写法。

### 必读 Skill

连接 236 的完整步骤见：**`.cursor/skills/viknow-236-ssh-proxy/SKILL.md`**  
Agent **必须先 Read 该文件**，不要跳过。`.cursor/skills/` 不会自动注入，需主动读取。

### 必做（摘要）

1. Cursor Secrets：
   - `VIKNOW_236_SSH_KEY` — `cursor-agent` 私钥（去掉 `-----BEGIN` 前空行）
   - `VIKNOW_BASTION_SSH_PASSWORD` — 跳板 `root@8.145.57.129` 密码
2. **不要直连** `36.103.198.236`；经跳板 ProxyCommand 连 `cursor-agent@36.103.198.236:22`
3. `docker exec viknow2-test-dev bash` 进入开发环境
4. 在容器内 `git fetch` / `checkout -B agent/<topic>` / 开发 / `git push -u origin agent/<topic>`
5. 结束时汇报：分支名、`git log --oneline origin/main..HEAD`、`git diff --stat origin/main...HEAD`

### 禁止

- 不要在本跳板仓库里实现 ViKnow 业务功能
- 不要改、停、删 `viknow2-test` / private 相关容器
- 不要 `git push` 到 `main`
- 不要把密钥、密码、license 写进提交
- 不要在 236 上生成新 SSH 密钥或改 `authorized_keys`
- 不要假设正式 test 镜像已经等于最新 `origin/main`

### 冒烟（经跳板）

```bash
# 详见 .cursor/skills/viknow-236-ssh-proxy/SKILL.md
ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
  -o ProxyCommand="sshpass -p \"$VIKNOW_BASTION_SSH_PASSWORD\" ssh -o StrictHostKeyChecking=no -W %h:%p root@8.145.57.129" \
  -p 22 cursor-agent@36.103.198.236 'echo ok && hostname && whoami'
```

成功标志：`ok` / `36-103-198-236` / `cursor-agent`。
