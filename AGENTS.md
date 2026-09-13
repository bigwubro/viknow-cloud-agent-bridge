# ViKnow Cloud Agent Bridge

本仓库是 **Cursor Cloud Agent 跳板**，不是 ViKnow 业务代码仓。

- 业务仓在 Gitea：`ssh://git@git.qingxiang.tech:2222/castmeta-research/viknow2.git`
- 真实工作区在 236：`viknow2-test-dev` 容器内的 `/workspace`
- Cloud Agent 在本仓库启动后，必须 SSH 到 236 再 `docker exec` 进入开发容器

## Cursor Cloud specific instructions

### 必做

1. 用 Secret `VIKNOW_236_SSH_KEY` 写入 `~/.ssh/id_ed25519`（chmod 600）
2. SSH：`cursor-agent@36.103.198.236`
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
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new cursor-agent@36.103.198.236 'echo ok'
```

### 冒烟命令（连通性）

```bash
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new cursor-agent@36.103.198.236 \
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

### 飞书（浏览器扫码登录）

Cloud Agent 云主机有桌面 Chrome（`DISPLAY=:1`），飞书 **不能** 用 API 密钥代替网页登录读内部文档时，走扫码：

1. 打开 Chrome → 飞书登录页（会跳转到 `castmeta.feishu.cn`）：
   - `https://accounts.feishu.cn/accounts/page/login?app_id=2&redirect_uri=https%3A%2F%2Fcastmeta.feishu.cn%2F`
2. 截图 QR 给用户扫码；过期点 **Refresh QR Code** 再截一张
3. 登录成功后 Cookie 留在云主机 Chrome，可继续打开文档

常用文档：

- 共享存储说明：`https://castmeta.feishu.cn/docx/Q6MrdUryCoPO9zxBlDYcS7Kpnae`

程序读文档（Wiki/导出）仍用 236 容器内 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（与网页登录无关）。

### NAS（Synology DSM / File Station）

**凭证（勿提交 Git）**：读本地文件

```bash
# 仓库内（已 gitignore）
set -a && source /workspace/.secrets/castmeta-credentials.env && set +a
# 或云主机 home（跨会话备份）
set -a && source ~/.config/castmeta/credentials.env && set +a
```

| 项 | 值 |
|---|---|
| 用户名 | `$CASTMETA_NAS_USERNAME`（`zihan.wu@castmeta.cn`） |
| 密码 | `$CASTMETA_NAS_PASSWORD` |
| QuickConnect ID | `castmeta-data` |
| QuickConnect URL | `https://quickconnect.to/castmeta-data` |
| 内网 DSM | `http://10.168.1.222/`（云主机浏览器可访问；236 ping 不通） |

**登录步骤（File Station）**

1. Chrome 打开 QuickConnect URL → DSM 登录页
2. 输入 `$CASTMETA_NAS_USERNAME` / `$CASTMETA_NAS_PASSWORD`
3. 打开 **File Station**，左侧选 `castmeta-data`

**常用路径**

| 用途 | File Station 路径 |
|---|---|
| NFS 共享（客户授权数据） | `P40_NFS` → `/volume2/P40_NFS` |
| 交付包目录 | `P60_SHARE/auto_download/app/viknow2-for-jinhe-911`（当前为空） |
| 历史参考包 | `P60_SHARE/auto_download/app/viknow2-for-jinhe/viknow-deploy-image/` |

**上传大文件注意**

- 236 无法直连写 NAS；大镜像 `docker save` 后需经云主机 File Station 上传
- 旧参考包约 11 GB（app + postgres/redis/minio/neo4j）；仅 app 镜像 tar 约 3.5 GB

**NFS 挂载（服务器侧，文档记载）**

```bash
sudo apt install nfs-common
sudo mount -v 10.168.1.222:/volume2/P40_NFS /mnt/P40_NFS/
# /etc/fstab:
# 10.168.1.222:/volume2/P40_NFS  /mnt/P40_NFS  nfs  rw,hard,timeo=600,rsize=262144,wsize=262144
```

236 上 `/mnt/P40_NFS` 存在但 ping 不通 NAS，需内网或 VPN 才能 mount。
