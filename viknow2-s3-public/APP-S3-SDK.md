# ViKnow 对象存储 — S3 SDK 应用接入（摘要）

**应用开发请以飞书为准（完整版、实测参数）：**  
[ViKnow 应用端接口文档（5175 实测版）](https://castmeta.feishu.cn/docx/X1lEdLPwnoSzNDxVpudchA9wnbe) → **模块 1：对象存储（S3 SDK 应用接入）**

本文件仅为跳板仓摘要，避免与飞书正文分叉。

- 标准 **boto3 / AWS S3 SDK** + 自定义 Endpoint，无 ViKnow 存储 SDK
- Bucket `viknow`，**path-style**，SigV4
- 私有 Key：`knowledge/libraries/{library_id}/{asset_id}.{ext}`
- 公开 Key / URL：`public/{library_id}/{asset_id}.{ext}` → `{PUBLIC_BASE}/viknow/public/...`
- 上传即公开：`PutObject` 后同请求 `CopyObject`；删除时同步 `DeleteObject` 两个 Key
