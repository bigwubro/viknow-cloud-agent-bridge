# deploy/

本目录 **仅服务 viknow3** 的三种部署/依赖，互不替代：

| 目录 | 产物 | 端口（236 典型） |
|------|------|------------------|
| [`236/`](236/README.md) | Playground 容器 `viknow3-playground` | UI 5198，API 5218 |
| [`demo/`](demo/redeploy.sh) | 接入层容器 `viknow3-demo` | 5197 |
| [`viknow3-dev-data/`](viknow3-dev-data/README.md) | 开发 PG / Redis / Neo4j compose | 16432 / 16479 / 17487 |

边界说明见 [`docs/project-boundary.md`](../docs/project-boundary.md)。
