# ViKnow 对象存储 — S3 SDK 应用接入（摘要）

**S3 SDK 应用接入仅在飞书维护（唯一入口）：**  
[ViKnow 应用端接口文档（5175 实测版）](https://castmeta.feishu.cn/docx/X1lEdLPwnoSzNDxVpudchA9wnbe) → **§1 对象存储（S3 SDK 应用接入）**（含 §1.6 预签名 TTL、§1.9 维护说明）

本文件仅为跳板仓摘要，勿与飞书正文分叉；变更请只改飞书并写 §9 变更记录。

- 标准 **boto3 / AWS S3 SDK** + 自定义 Endpoint，无 ViKnow 存储 SDK
- Bucket `viknow`，**path-style**，SigV4
- 私有 Key：`knowledge/libraries/{library_id}/{asset_id}.{ext}`（上传与存储唯一路径）
- 对外打开文件：**服务端** `generate_presigned_url` → `access_url` + `expires_at`（§1.6），**不使用** `public/` 固定外链
- 删除：仅 `DeleteObject(source_key)`（§1.7）
