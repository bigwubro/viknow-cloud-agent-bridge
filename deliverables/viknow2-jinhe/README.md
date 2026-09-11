# ViKnow jinhe 客户交付包

由 236 `viknow2-test`（:5175）反推，改为 **容器网络 + jinhe 配置**。

## 文件

| 文件 | 说明 |
|------|------|
| `docker-compose.yml` | 挂 external data 网络；保留 privileged/cgroup（JuiceFS） |
| `deploy.env` | 中间件用 compose 服务名；**行尾禁止 `#` 注释** |

## 与 236 viknow2-test 差异

| viknow2-test | 客户 jinhe |
|--------------|------------|
| `host.docker.internal:16379` 等 | `redis:6379` 等（同 Docker 网络） |
| `config/test/app.yaml` | `config/jinhe/app.yaml`（镜像内） |
| Langfuse + Token Gateway 开 | 关闭，直连 LiteLLM |
| 网络 token-gateway-real-host | external data 网络 |

## 启动

```bash
export VIKNOW_DATA_NETWORK=你的data栈网络名
export VIKNOW_IMAGE=viknow2-app:jinhe-51742-b497cc365b67
# 编辑 deploy.env 填密钥与服务名
docker compose up -d
```

## 查网络名

```bash
docker inspect <redis容器> --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}'
```

## 236 联调

5177 仍用 `/home/cursor-agent/docker-compose.viknow2-test.manual.yml`（host.docker.internal）。
