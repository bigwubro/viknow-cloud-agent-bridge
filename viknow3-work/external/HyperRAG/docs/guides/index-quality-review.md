# 索引质量审查使用说明

本文档说明如何用 CSV 一键执行特定文档的索引质量审查。

## 适用范围

- 仅支持文档审查，不支持视频索引审查。
- 审查对象必须是索引状态已经完成的文档。
- 审查流程不依赖 Viknow2 服务启动，但依赖数据库连接、索引存储和 LLM/VLM Web API。

## 前置条件

运行前需要准备好以下条件：

1. 文档已经完成索引，`asset_status.status` 必须是 `completed`。
2. 本地可以访问 HyperRAG 所依赖的 PostgreSQL、Neo4j 等存储服务。
3. 需要可用的文本 LLM 服务与视觉模型服务。
4. CSV 文件中必须包含 `doc_id` 列，可选 `workspace` 列；如果没有 `workspace`，脚本会使用命令行传入的默认 workspace。

## CSV 格式

推荐格式如下：

```csv
workspace,doc_id
default,2026072218364317078836
default,20260721104211be9be397
```

如果只有一个 workspace，也可以省略 `workspace` 列，只保留：

```csv
doc_id
2026072218364317078836
20260721104211be9be397
```

## 命令示例

先准备环境变量，再执行审查脚本：

```bash
cd /home/zzz/viknow2/external/HyperRAG
POSTGRES_PORT=15432 \
POSTGRES_USER=postgres \
POSTGRES_PASSWORD='***' \
POSTGRES_DB=viknow_tmp \
HYPERRAG_WORKING_DIR=/data/dev/app_data/KBIndex2 \
VIKNOW_PRIVATE_BASE_URL='https://ai.qingxiang.tech:4000/v1' \
VIKNOW_PRIVATE_API_KEY='***' \
VIKNOW_PRIVATE_MODEL='vllm/Qwen/Qwen3.6-35B-A3B-FP8' \
uv run python scripts/review_index_quality.py \
  --config /home/zzz/viknow2/config/local/hyperrag.yml \
  --workspace default \
  --csv /path/to/review_assets.csv \
  --judge-source siliconflow \
  --judge-model zai-org/GLM-5.2 \
  --vision-source hyperrag \
  --vision-model Qwen/Qwen3.6-35B-A3B-FP8
```
(在viknow2的虚拟环境下不需要输入uv run...之前的环境变量)

CSV 模式下不需要 `--asset-id`；脚本会逐行读取 `doc_id` 并批量执行。

运行后会生成：

- `batch_report.json`：所有文档的 JSON 汇总
- `batch_report.md`：所有文档的 Markdown 汇总

## 输出说明

批量汇总会展示每篇文档的：

- 信息留存得分
- 平均语义完整性得分
- chunk 加权语义完整性
- 总体状态

同时会汇总两类 bad case：

- 解析侧 bad case
- 分块侧 bad case

## 设计说明

- 审查入口是 read-only 的，不会启动 Viknow2 的在线服务流程。
- 脚本会在执行前检查 CSV 中的 `doc_id` 是否对应 `completed` 状态的资产；不是 `completed` 会直接报错。
- 批量模式是“人为选定文档集”的评测流程，适合做优化前后回归对比。
