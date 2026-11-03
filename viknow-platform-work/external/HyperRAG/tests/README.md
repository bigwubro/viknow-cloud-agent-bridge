# HyperRAG 测试说明

## 测试分类

测试分为两类：

### 1. 单元测试（Unit Tests）
- 不依赖外部服务
- 使用 mock/fake 对象
- 运行速度快
- **默认运行**

### 2. 集成测试（Integration Tests）
- 需要真实外部服务（Redis、PostgreSQL、Neo4j、LLM/Embedding API）
- 测试真实的端到端流程
- 运行较慢
- **默认跳过**，需显式开启

## 运行测试

### 运行所有单元测试（默认）
```bash
cd /home/dtong/code/HyperRAG
pytest tests/
```

### 运行集成测试

#### 方式 1：使用命令行选项
```bash
pytest tests/ --run-integration
```

#### 方式 2：使用环境变量
```bash
export HYPERRAG_RUN_INTEGRATION=true
pytest tests/
```

### 运行特定测试文件
```bash
# 只运行队列测试
pytest tests/test_task_queue.py --run-integration

# 只运行调度器测试
pytest tests/test_task_scheduler.py --run-integration
```

## 集成测试依赖的服务

集成测试需要以下真实服务：

### 1. Redis
- **默认连接**: `redis://localhost:6379/15`
- **环境变量**: `HYPERRAG_TEST_REDIS_URI`
- **用途**: 任务队列、分布式协调

### 2. PostgreSQL
- **默认连接**: `localhost:5432/postgres (postgres/postgres)`
- **环境变量**: 
  - `POSTGRES_HOST` (默认: localhost)
  - `POSTGRES_PORT` (默认: 5432)
  - `POSTGRES_USER` (默认: postgres)
  - `POSTGRES_PASSWORD` (默认: postgres)
  - `POSTGRES_DATABASE` (默认: postgres)
- **用途**: HyperRAG 的 KV、Vector、DocStatus 存储

### 3. Neo4j
- **默认连接**: `bolt://localhost:7687 (neo4j/neo4j)`
- **环境变量**:
  - `NEO4J_URI` (默认: bolt://localhost:7687)
  - `NEO4J_USERNAME` (默认: neo4j)
  - `NEO4J_PASSWORD` (默认: neo4j)
- **用途**: HyperRAG 的图存储

### 4. LLM/Embedding API（OpenAI 兼容）
- **默认 Base URL**: `http://dev.ai.qingxiang.tech:8080/v1`
- **默认 API Key**: `QX123456`
- **默认 LLM Model**: `Qwen/Qwen3-VL-30B-A3B-Instruct-FP8`
- **默认 Embed Model**: `Qwen/Qwen3-Embedding-0.6B`
- **默认 Embedding Dim**: `1024`
- **环境变量**:
  - `HYPERRAG_TEST_LLM_BASE_URL`
  - `HYPERRAG_TEST_LLM_API_KEY`
  - `HYPERRAG_TEST_LLM_MODEL`
  - `HYPERRAG_TEST_EMBED_MODEL`
  - `HYPERRAG_TEST_EMBEDDING_DIM`

## 快速启动服务（Docker）

如果本地没有这些服务，可以使用 Docker 快速启动：

### Redis
```bash
docker run -d --name redis-test -p 6379:6379 redis:7-alpine
```

### PostgreSQL with pgvector
```bash
docker run -d --name postgres-test \
  -e POSTGRES_PASSWORD=postgres \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

### Neo4j
```bash
docker run -d --name neo4j-test \
  -e NEO4J_AUTH=neo4j/neo4j \
  -p 7687:7687 -p 7474:7474 \
  neo4j:5
```

## 测试覆盖

### `test_task_queue.py`（队列集成测试）
- 任务入队/出队
- 任务优先级排序
- 任务重新入队
- 任务状态更新

### `test_task_scheduler.py`（调度器集成测试）
- 单 workspace 索引任务
- 多 workspace 并发索引
- 索引进行中同时查询（验证并发安全性）

## 注意事项

1. **数据隔离**: 集成测试使用独特的 workspace 名称（包含 UUID），避免测试间相互影响。
2. **Redis DB**: 默认使用 DB 15，避免影响其他应用数据。
3. **清理**: 测试会自动清理创建的数据（Redis keys、临时文件）。
4. **超时**: 索引任务有合理的超时设置（120-180秒），避免无限等待。
5. **断言宽松**: 对 LLM 生成结果不做严格断言，只验证流程正确性和数据存在性。

## 故障排查

### 测试被跳过
```
SKIPPED [1] conftest.py:XX: 需要 --run-integration 或 HYPERRAG_RUN_INTEGRATION=true 来运行集成测试
```
**解决**: 使用 `--run-integration` 选项或设置环境变量。

### Redis 连接失败
```
redis.exceptions.ConnectionError: Error connecting to Redis
```
**解决**: 确保 Redis 服务在 `localhost:6379` 运行，或设置 `HYPERRAG_TEST_REDIS_URI` 环境变量。

### PostgreSQL 连接失败
```
asyncpg.exceptions.InvalidCatalogNameError
```
**解决**: 确保 PostgreSQL 服务运行且安装了 pgvector 扩展。

### Neo4j 连接失败
```
neo4j.exceptions.ServiceUnavailable
```
**解决**: 确保 Neo4j 服务运行且认证信息正确。

### 任务超时
```
TimeoutError: HyperRAG indexing timeout
```
**解决**: 检查 LLM/Embedding API 是否可访问，或增加测试中的超时时间。

## CI/CD 集成

在 CI 环境中运行集成测试示例：

```yaml
# .github/workflows/test.yml
jobs:
  integration-tests:
    runs-on: ubuntu-latest
    services:
      redis:
        image: redis:7-alpine
        ports:
          - 6379:6379
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_PASSWORD: postgres
        ports:
          - 5432:5432
      neo4j:
        image: neo4j:5
        env:
          NEO4J_AUTH: neo4j/neo4j
        ports:
          - 7687:7687
    
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.10'
      - run: pip install -e .
      - run: pytest tests/ --run-integration
        env:
          HYPERRAG_TEST_LLM_BASE_URL: ${{ secrets.LLM_BASE_URL }}
          HYPERRAG_TEST_LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
```
