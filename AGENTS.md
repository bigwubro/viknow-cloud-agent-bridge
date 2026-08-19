# ViKnow Cloud Agent Bridge

本仓库是 **Cursor Cloud Agent 跳板**，不是 ViKnow 业务代码仓。

- 业务仓在 Gitea：`ssh://git@git.qingxiang.tech:2222/castmeta-research/viknow2.git`
- 真实工作区在 236：`viknow2-test-dev` 容器内的 `/workspace`
- Cloud Agent 在本仓库启动后，必须 SSH 到 236 再 `docker exec` 进入开发容器

## Cursor Cloud specific instructions

### 必做

1. 用 Secret `VIKNOW_236_SSH_KEY` 写入 `~/.ssh/id_ed25519`（chmod 600）  
   - 私钥见 Gitea Actions run **#602** 日志 `CLOUD_AGENT_CURSOR_SSH_KEY` 段（指纹 `SHA256:CoXNRO8C48uDlZe0GQGdr/M59XVf8BfwhQcZWPX5V4g`）
2. SSH（**方案 A**，不要直连 `36.103.198.236`）：`ssh -p 2223 cursor-agent@git.qingxiang.tech`  
   - 236 上 host `sshd` 已在 `:2223` 就绪（run #602）；**网关 `101.71.223.113` 须把 `:2223` 转发到 236**（见下方一次性配置）
3. 进入容器：`docker exec -it viknow2-test-dev bash`（非交互可用 `docker exec viknow2-test-dev bash -lc '...'`）
4. 在 `/workspace` 开发：
   - `git fetch origin`
   - `git checkout -B agent/<topic> origin/main`（或 rebase 到最新 `origin/main`）
   - 改代码、跑测试、`:5176` / 容器内 `:8000` 冒烟
   - `git commit` 后 `git push -u origin agent/<topic>`
5. 结束时汇报：分支名、`git log --oneline origin/main..HEAD`、`git diff --stat origin/main...HEAD`

### 禁止

- 不要在本跳板仓库里实现 ViKnow 业务功能
- 不要改、停、删 `viknow2-test` / private 相关容器
- 不要 `git push` 到 `main`
- 不要把密钥、密码、license 写进提交
- 不要假设正式 test 镜像已经等于最新 `origin/main`

### SSH 密钥落地（Secret）

Secret 名：`VIKNOW_236_SSH_KEY`

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# 若 Secret 是 OpenSSH 私钥原文：
printf '%s\n' "$VIKNOW_236_SSH_KEY" > ~/.ssh/id_ed25519
# 若 Secret 是 base64：
# echo "$VIKNOW_236_SSH_KEY" | base64 -d > ~/.ssh/id_ed25519
chmod 600 ~/.ssh/id_ed25519
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new -p 2223 cursor-agent@git.qingxiang.tech 'echo ok'
```

### 方案 A：网关一次性端口转发（`101.71.223.113`）

236 无法自动 SSH 到网关，需在 **Gitea 公网机** 上执行一次（目标 IP 以 run #604 日志为准，当前为 `172.30.57.94` 或可达的 236 内网/公网 IP）：

```bash
sudo apt-get install -y socat
sudo tee /etc/systemd/system/viknow-cursor-agent-ssh-forward.service <<'EOF'
[Unit]
Description=Forward cursor-agent SSH :2223 to 236
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP-LISTEN:2223,fork,reuseaddr,bind=0.0.0.0 TCP:172.30.57.94:2223
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now viknow-cursor-agent-ssh-forward
sudo ss -tlnp | grep 2223
```

若 `172.30.57.94` 从网关不可达，改为 `36.103.198.236` 或 236 在网关侧可见的内网 IP。

### 冒烟命令（连通性）

```bash
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new -p 2223 cursor-agent@git.qingxiang.tech \
  'docker exec viknow2-test-dev bash -lc "
    cd /workspace &&
    git fetch origin &&
    git checkout -B agent/cloud-smoke origin/main &&
    git commit --allow-empty -m \"chore: cloud agent smoke\" &&
    git push -u origin agent/cloud-smoke &&
    git log -1 --oneline &&
    git status -sb
  "'
```

成功标志：Gitea `castmeta-research/viknow2` 出现分支 `agent/cloud-smoke`。
