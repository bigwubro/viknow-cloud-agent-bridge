# ViKnow 3 开发数据栈

与 viknow2-test 共用栈 **隔离**，236 上典型端口：

| 服务 | 容器 / 说明 | 宿主机地址 |
| --- | --- | --- |
| Postgres（access / agent / hyperrag jobs / **viknow_ops**） | `viknow3-ops-postgres` | `127.0.0.1:16432` |
| Postgres（**HyperRAG 知识索引**，ops 表 `postgres_connection`） | 同上实例，库名 `viknow3_knowledge` | `127.0.0.1:16432` |
| Redis | `viknow3-dev-redis`（本 compose） | `127.0.0.1:16479`，密码见 compose |
| Neo4j | `viknow3-dev-neo4j`（本 compose，仅 Bolt） | `neo4j://127.0.0.1:17487` |

```bash
docker compose -f deploy/viknow3-dev-data/docker-compose.yml up -d
```

创建 HyperRAG 知识库（在已有 PG 实例上，只需一次）：

```bash
docker exec viknow3-ops-postgres psql -U viknow_ops -d postgres -c "CREATE DATABASE viknow3_knowledge OWNER viknow_ops;"
```

运维连接写入 `viknow_ops` 的 `postgres_connection` / `redis_connection` / `neo4j_connection`，应用 **不** 维护 YAML。
