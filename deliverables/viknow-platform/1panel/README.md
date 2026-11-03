# 1Panel 安装 ViKnow Platform

## 前置

- 1Panel 已安装，Docker 可用；GPU 栈需宿主机 NVIDIA 驱动 + `nvidia-container-toolkit`。
- 离线：在 **主机终端** 先 `bash scripts/load-all-images.sh`，再在 1Panel 里建编排（面板内拉不到外网镜像）。

## 建栈顺序

| 步骤 | 1Panel 路径 | 源目录 | 说明 |
|------|-------------|--------|------|
| 1 | 容器 → 编排 → 创建 | `stacks/01-data` | 上传 `docker-compose.yml`；环境变量从 `.env.example` 复制为 `.env` 并改密 |
| 2 | 同上 | `stacks/02-inference` | 可选；启用 `--profile inference` 需在 1Panel 高级/命令里等价配置，或 CLI 首次 up |
| 3 | 同上 | `stacks/03-actio` | 可选；见该目录 README |
| 4 | 同上 | `stacks/04-viknow` | `deploy.env` + `.env`；**两套文件都要填** |

等待 01 中 postgres healthcheck 通过后再起 04。

## 环境变量：两套文件

```
编排目录/
├── docker-compose.yml
├── .env              ← Compose ${VAR} 插值（VIKNOW_IMAGE、VIKNOW_DATA_NETWORK…）
└── deploy.env        ← 仅 04-viknow；env_file 进容器（POSTGRES_*、模型 URL…）
```

1Panel「环境变量」页通常对应 **`.env`**。`deploy.env` 建议用文件挂载/编辑文件方式维护（或 SFTP 改完后在面板 **重建** 容器）。

**不要**在 `KEY=value` 同一行写 `#` 注释（deploy.env）。

## 网络

- 01-data 创建的网络名必须为 **`viknow-platform_data`**（或与 `.env` 中 `VIKNOW_PLATFORM_NETWORK` 一致）。
- 02/03/04 的 compose 已写 `external: true`，无需在 1Panel 里手工「连接网络」，只要名字一致。

## 改配置后怎么生效

| 改了什么 | 操作 |
|----------|------|
| `.env`（镜像 tag、端口） | 编排 → 重建 |
| `deploy.env` | **`up -d --force-recreate --no-deps`**（1Panel 等价「强制重建」）；仅重启不够 |
| 仅 data 栈密码 | 同步改 04 的 deploy.env + 重建 ViKnow |

## 验证

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/docs
docker logs --tail 80 viknow2-platform
```

## 常见坑

1. **ViKnow 起不来但中间件正常** — `VIKNOW_DATA_NETWORK` 写错或未 export 到 compose `.env`。
2. **ASR 400 invalid audio** — Whisper 缺 `soundfile`；应换带依赖的镜像，不要依赖容器内临时 pip。
3. **视频索引 500 temporal_factor** — hyperrag `local_video_mode: first_frame_image`，并确认 `.state` 物化后重建索引任务。
4. **JuiceFS** — 必须 `privileged: true`；换 ViKnow 实例见 jinhe README。
