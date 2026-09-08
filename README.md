# viknow-cloud-agent-bridge

Cursor Cloud Agent **跳板仓库**。

真实开发在 `36.103.198.236` 的 `viknow2-test-dev`（公网 `:5176`），业务代码在 Gitea `castmeta-research/viknow2`。

## 为什么需要这个仓库

Cursor Cloud Agents 只能从 GitHub / GitLab / Azure DevOps / Bitbucket 选仓库，不能直接选 Gitea。  
所以用本仓库启动 Cloud Agent，再 SSH 进 236 干活。

## 启动前准备

1. Cursor Dashboard → Cloud Agents → Secrets  
   - 名称：`VIKNOW_236_SSH_KEY`  
   - 值：236 上 `cursor-agent` 用户的 SSH 私钥（或 base64）
2. Cursor 连接 GitHub，并能选中本仓库 `bigwubro/viknow-cloud-agent-bridge`
3. 阅读 `AGENTS.md`（Cloud Agent 会读）

## 启动 Cloud Agent 时选什么

- **Repository**：`bigwubro/viknow-cloud-agent-bridge`
- **Prompt**：见 `prompts/smoke.md` 或 `prompts/feature.md`

## 云操作台账

对阿里云 / AWS / ACK / ACR 的每次操作与变更，记在 [`docs/cloud-ops-log.md`](docs/cloud-ops-log.md)。不要把密码或 AccessKey 写进去。

## 成功标准

- SSH 登录 `cursor-agent@36.103.198.236` 成功
- `docker exec` 进入 `viknow2-test-dev`
- 在 Gitea 推送 `agent/<topic>`（冒烟可用 `agent/cloud-smoke`）
- **没有**改本跳板仓库的业务逻辑；**没有**动正式 `viknow2-test`
