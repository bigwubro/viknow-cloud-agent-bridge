# 历史索引对象一次性搬运

把原来放在 `/data/viknow` 下的索引文件搬到现行对象键 `knowledge/assets/{asset_id}/...`。运行时继续只认新键，不增加旧路径兼容。`/data/test` 不参与。

事实来源：2026-10-10 在 236 的 `viknow2-test` 上核对 `hyperrag_asset_status`、`hyperrag_kg` 和 JuiceFS 网关桶 `viknow`。网关对象键等于去掉挂载前缀 `/data/viknow/` 后的路径。

## 1. 搬运范围

只处理 `hyperrag_asset_status.file_path` 以 `/data/viknow/` 开头的资产，共 1682 条。

| 旧路径 | 条数 | 新位置 |
| --- | --- | --- |
| `/data/viknow/knowledge/uploads/...` | 1527 | `knowledge/assets/{id}/upload/{id}{扩展名}` |
| `/data/viknow/knowledge/libraries/...` 下的源文件 | 67 | 同上 |
| `/data/viknow/knowledge/subtitle_reindex_sources_20260904/...` | 83 | 同上 |
| `/data/viknow/tmp/...` | 3 | 同上 |
| `/data/viknow/experiments/...`、`/data/viknow/KBStore/...` | 2 | 同上 |

同一资产如果在 `/data/viknow/knowledge/libraries/{库}/mineru_cache/{id}/` 或 `video_cache/{id}/` 下还有目录，整棵复制到：

- `knowledge/assets/{id}/mineru_cache/V0/`
- `knowledge/assets/{id}/video_cache/V0/`

`{id}_origin.pdf` 再复制一份为 `knowledge/assets/{id}/mineru_cache/V0/{id}.pdf`。没有这些目录就只搬源文件。

不搬：

- `/data/test`，包括 `KBIndex`、`KBStore`
- `/data/datasets`、`/home` 以及其他不在 `/data/viknow` 下的 `file_path`（约 971 条）
- `/data/viknow/logs`、`/data/viknow/ops`、`/data/viknow/t2`，它们不是索引文件

已经是 `*.normalized.mp4` 的源文件，新文件名保持 ` {id}.normalized.mp4`。

## 2. 单个资产的顺序

一个资产失败就停在该资产，不改它的数据库记录，不删它的旧对象。

1. 源对象从旧键复制到新的 upload 键。旧键是 `file_path` 去掉 `/data/viknow/`。
2. 该资产在 `/data/viknow` 内的 `mineru_cache`、`video_cache` 复制到上面的 `V0` 前缀。
3. 对新键做 `stat`，大小与源对象一致。
4. 更新仍指向旧绝对路径的 `file_path`：
   - `public.hyperrag_asset_status`
   - `hyperrag_kg.kg_chunk_vectors`
   - `hyperrag_kg.kg_entity_vectors`
   - `hyperrag_kg.kg_relation_vectors`
5. 删除该资产对应的旧对象。

向量表只替换与该资产旧 `file_path` 完全相等的行。不按前缀批量替换。

## 3. 实验

先只做第 1 步和第 3 步，用来确认网关内复制可行。不改数据库，不删旧对象，也不搬缓存。

| 项 | 值 |
| --- | --- |
| 资产 | `f20a2812-db00-4f4b-8599-f93eb98a036d` |
| 库 | `default` |
| 类型 | `document` |
| 旧路径 | `/data/viknow/knowledge/uploads/default/f20a2812-db00-4f4b-8599-f93eb98a036d.pdf` |
| 旧键 | `knowledge/uploads/default/f20a2812-db00-4f4b-8599-f93eb98a036d.pdf` |
| 新键 | `knowledge/assets/f20a2812-db00-4f4b-8599-f93eb98a036d/upload/f20a2812-db00-4f4b-8599-f93eb98a036d.pdf` |

实验前已确认旧键在桶 `viknow` 中存在，新键不存在。该资产的 `file_path` 不在 `knowledge/libraries/*/mineru_cache` 下，这次不复制缓存。

2026-10-10 在 `viknow2-test` 内用网关 `copy_object` 完成复制。

| 检查 | 结果 |
| --- | --- |
| 复制前新键 | 不存在 |
| 源对象大小 | 200379 字节 |
| 新对象大小 | 200379 字节 |
| 大小一致 | 是 |
| 旧键 | 仍在 |
| 数据库 | 未改 |
| 旧对象 | 未删 |

这条资产的索引状态是 `deleted`，没有分块，所以后面的全量搬运没有包含它。

## 4. 全量执行

2026-10-10 对 `status = completed` 且 `file_path` 以 `/data/viknow/` 开头的资产执行了复制和数据库改写。旧对象没有删除。

| 结果 | 数量 |
| --- | --- |
| 符合条件的资产 | 1279 |
| 已复制并改写 `file_path` | 1253 |
| 源对象在网关上不存在，数据库未改 | 26 |
| 随资产复制的缓存目录 | 0 |

`file_path` 已改为 `knowledge/assets/{id}/upload/{id}{扩展名}` 的表：

- `public.hyperrag_asset_status`
- `hyperrag_kg.kg_chunk_vectors`
- `hyperrag_kg.kg_entity_vectors`
- `hyperrag_kg.kg_relation_vectors`

分块 `metadata` 里的旧绝对路径也换成了同一条新键。抽查 `20260728141552eebf2666`：3415 条分块都指向 `knowledge/assets/20260728141552eebf2666/upload/20260728141552eebf2666.pdf`，网关对象与应用读回的文件都是 125663333 字节，分块元数据里不再含 `/data/viknow/`。

26 条未搬是因为旧键已经不在桶里。其中 17 条在 `knowledge/uploads/feishu-7674904628516883756/`。这些行的 `file_path` 仍是 `/data/viknow/...`。

`/data/viknow/knowledge/libraries` 下的 `mineru_cache` 和 `video_cache` 属于状态不是 `completed`，或 `file_path` 在 `/data/source/viknow/` 下的资产，这次没有搬。
