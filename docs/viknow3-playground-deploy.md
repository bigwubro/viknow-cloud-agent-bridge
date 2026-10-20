# Playground UI 快速部署（236 宿主机）

避免每次改前端都走 Docker 重建或多次 SCP。代码在 `castmeta-research/viknow3`。

## 236 上一键重启（改完代码已在盘上时）

```bash
cd /home/cursor-agent/src/viknow3
./scripts/deploy-playground-ui.sh restart   # 默认 ~1s，只杀 pidfile 里的 UI 进程
./scripts/deploy-playground-ui.sh status
```

环境由脚本读取：

- `VIKNOW3_ENV_FILE`：`pg-demo.env` + `~/.viknow3/ops.env`（Postgres + S3 配置）
- `VIKNOW_UI_PORT`：默认 `5198`

## 开发机 → 236 同步（推荐：一条 tar 流）

在 viknow3 仓库根目录：

```bash
./scripts/push-playground-to-236.sh
```

内部只做 **UI + knowledge BFF + access knowledge 路由** 的 rsync/等价同步，然后远程执行 `deploy-playground-ui.sh restart`。

若本机没有 rsync，可用与 Cloud Agent 相同的一流式 tar（约数秒）：

```bash
tar czf - ui/playground_routes.py ui/server.py ui/static/playground \
  src/viknow3/access/knowledge src/viknow3/storage src/viknow3/server.py \
  scripts/deploy-playground-ui.sh \
  | ssh cursor-agent@36.103.198.236 'cd /home/cursor-agent/src/viknow3 && tar xzf - && ./scripts/deploy-playground-ui.sh restart'
```

## 不要再用

- `pkill -f ui.server`（可能误伤 SSH 会话，表现为部署「卡住/失败」）
- 全仓库 tarball + 手工 `nohup`（慢且易漏 env）

## 访问

http://36.103.198.236:5198/playground → 登录后 **知识库** → 创建 library → 进入库内 S3 预签名上传 + 创建 asset。
