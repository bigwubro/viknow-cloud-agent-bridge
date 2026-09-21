# ViKnow S3 网关 — Agent 接入说明（test / 236）

面向 **Cursor Cloud Agent** 与其他自动化：知识库原文件走 **JuiceFS S3 Gateway**（S3 兼容 API），不是 ViKnow HTTP 上传接口，也不是底层 MinIO `:19000` 管理口。

**规范正文（变更请只改飞书 + §9）**：[ViKnow 应用端接口文档（5175 实测版）](https://castmeta.feishu.cn/docx/X1lEdLPwnoSzNDxVpudchA9wnbe) → **§1 对象存储（S3 SDK 应用接入）**。

跳板仓摘要：`viknow2-s3-public/APP-S3-SDK.md`。

---

## 1. 它是什么

| 组件 | 说明 |
|------|------|
| **viknow-juicefs-s3-gateway** | 236 上容器，镜像 `juicedata/mount:ce-v1.2.4`，进程为 `juicefs gateway` |
| **协议** | **MinIO S3 Gateway 兼容** REST，签名 **AWS SigV4** |
| **数据** | 文件落在 JuiceFS 卷上；元数据在 Postgres `search_path=viknow_juicefs`（与业务库同实例、不同 schema） |
| **Bucket** | 固定 **`viknow`** |
| **应用读写口** | **`http://<host>:19191`**（test：`36.103.198.236:19191`） |

```text
应用 / Agent (boto3)
        │  PutObject / GetObject / HeadObject / DeleteObject
        │  generate_presigned_url (应用服务端)
        ▼
:19191  JuiceFS S3 Gateway  ──►  JuiceFS 文件 + PG 元数据
        │
        │  （不要对应用暴露）
        ▼
:19000  viknow-test-data-minio   ← 底层对象存储，仅运维/网关内部
```

**和 ViKnow API 的分工**

| 需求 | 做法 |
|------|------|
| 上传/覆盖知识库原文件 | S3 `PutObject` → `knowledge/libraries/...` |
| 登记索引 | `POST /api/v1/knowledge/index-jobs`，`asset_path` = `/data/viknow/{source_key}` |
| 给用户浏览器 **限时**打开原文件 | 应用服务端 **`generate_presigned_url`**（ViKnow **没有**此 HTTP 接口） |
| 索引后的页图、视频切片、排版 PDF | `GET /api/v1/knowledge/media/...`（算法只读，见飞书 §6） |
| 只删检索索引 | `DELETE /api/v1/knowledge/indexes`（**不删** S3 对象） |
| 删资产 | 应用先 `DeleteObject(source_key)`，再删库表/索引 |

---

## 2. 连接参数（test / 5175 环境）

| 参数 | test（236）值 |
|------|----------------|
| **Endpoint** | `http://36.103.198.236:19191` |
| **Bucket** | `viknow` |
| **Region** | `us-east-1`（boto3 必填，与网关配置一致即可） |
| **寻址** | **path-style**（必须关闭 virtual-host） |
| **Access Key / Secret** | 运维下发，与网关 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` 一致；**仅应用服务端**，勿写入 git |

容器内 ViKnow 读本地路径时，挂载根为 **`/data/viknow`**，与 Object Key 一一对应：

```text
Object Key:  knowledge/libraries/{library_id}/{asset_id}.{ext}
asset_path:  /data/viknow/knowledge/libraries/{library_id}/{asset_id}.{ext}
```

**已废弃**：`public/` 前缀、匿名读、`setup-public-anonymous.sh` 产品方案；新接入 **禁止**使用。

---

## 3. boto3 客户端（标准写法）

无 ViKnow 存储 SDK，直接用 **boto3** + 自定义 `endpoint_url`。

```python
import boto3
from botocore.client import Config


def make_s3_client(access_key: str, secret_key: str, endpoint: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint.rstrip("/"),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )
```

官方参考：[boto3 S3](https://docs.aws.amazon.com/boto3/latest/reference/services/s3.html)、[预签名 URL](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/s3-presigned-urls.html)。

---

## 4. 常用操作

### 4.1 上传（覆盖）

```python
def upload_knowledge_object(s3, library_id: str, asset_id: str, ext: str, body: bytes, content_type: str):
    ext = ext.lstrip(".")
    source_key = f"knowledge/libraries/{library_id}/{asset_id}.{ext}"
    s3.put_object(Bucket="viknow", Key=source_key, Body=body, ContentType=content_type)
    asset_path = f"/data/viknow/{source_key}"
    return source_key, asset_path
```

上传后调用 **`POST /api/v1/knowledge/index-jobs`**（`asset_path` 为上面的绝对路径）。

### 4.2 是否存在

```python
s3.head_object(Bucket="viknow", Key=source_key)
```

404 / `ClientError` 表示不存在。

### 4.3 下载（服务端）

```python
resp = s3.get_object(Bucket="viknow", Key=source_key)
body = resp["Body"].read()
```

支持 **Range**（大文件、视频按需拉流）。

### 4.4 删除

```python
s3.delete_object(Bucket="viknow", Key=source_key)
```

删索引接口 **不会**删 S3；删资产由应用负责 `DeleteObject`。

### 4.5 预签名 GET（给用户临时链接）

对象始终在私有 Key；由 **持有 AK/SK 的应用服务端** 签发，`ExpiresIn` 为秒（SigV4 常见上限约 **7 天**）。

```python
from datetime import datetime, timezone, timedelta


def presigned_get_url(s3, library_id: str, asset_id: str, ext: str, ttl_seconds: int = 3600) -> dict:
    ext = ext.lstrip(".")
    source_key = f"knowledge/libraries/{library_id}/{asset_id}.{ext}"
    params = {"Bucket": "viknow", "Key": source_key, "ResponseContentDisposition": "inline"}
    if ext.lower() == "pdf":
        params["ResponseContentType"] = "application/pdf"
    elif ext.lower() in ("mp4", "mov", "webm"):
        params["ResponseContentType"] = (
            f"video/{ext.lower()}" if ext.lower() != "mov" else "video/quicktime"
        )
    url = s3.generate_presigned_url("get_object", Params=params, ExpiresIn=ttl_seconds)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    return {
        "object_key": source_key,
        "access_url": url,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "ttl_seconds": ttl_seconds,
    }
```

返回给前端的 `access_url` 在 TTL 内等同临时凭证，宜 **60–120 分钟**或更短。

**注意**：签发预签名时使用的 `endpoint_url` 必须与用户浏览器访问的 host **一致**（test 用 `http://36.103.198.236:19191`；生产用运维提供的 HTTPS 域名）。

---

## 5. 支持的 S3 API（5175 实测范围）

| API / 方法 | 用途 |
|------------|------|
| `PutObject` | 上传 / 覆盖 |
| `HeadObject` | 是否存在、元数据 |
| `GetObject` | 下载（含 Range） |
| `DeleteObject` | 删除 |
| `generate_presigned_url` | 限时 GET 链接 |

列表桶、多段上传等未作为应用契约保证；按飞书 §1.4 为准。

---

## 6. Agent 在 236 上怎么查 / 冒烟

1. SSH：`cursor-agent@36.103.198.236`（密钥 `VIKNOW_236_SSH_KEY`，见 `AGENTS.md`）。
2. 网关健康（无需 AK/SK）：

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:19191/minio/health/live
# 期望 200
```

3. 容器与编排：

```bash
docker ps --filter name=viknow-juicefs-s3-gateway
# 编排目录（业务仓）：/home/cursor-agent/work/viknow2/deploy/juicefs-s3-gateway/
```

4. 有 AK/SK 时（从 Secret/运维获取，**不要提交到 git**）：

```bash
export AWS_ACCESS_KEY_ID='…'
export AWS_SECRET_ACCESS_KEY='…'
aws --endpoint-url http://127.0.0.1:19191 s3 ls s3://viknow/knowledge/libraries/ --region us-east-1
# 或 python boto3 head_object 探测已知 source_key
```

5. **不要**用 `:19000`（`viknow-test-data-minio`）做应用上传；那是底层 MinIO，与网关凭证/策略不同。

---

## 7. Agent 禁止事项（与 `AGENTS.md` 一致）

- 不要把真实 **Access Key / Secret** 写进跳板仓或 Gitea 提交。
- 不要为验证业务而改 `viknow-test` 容器或热补丁；S3 网关容器名 `viknow-juicefs-s3-gateway` 亦勿随意停删（除非运维任务明确授权）。
- 文档变更：**飞书 §1 + §9** 为唯一产品契约；本文件与 `viknow2-s3-public/` 为 Agent 速查，重大变更请同步更新本节并改飞书。

---

## 8. 相关路径

| 位置 | 内容 |
|------|------|
| 飞书 §1 | 完整参数、示例 JSON、与 `/media` 分工 |
| `viknow2-s3-public/README.md` | JuiceFS gateway 运维摘要 |
| `viknow2` 仓 `deploy/juicefs-s3-gateway/` | `docker-compose.yml`、`env.example` |
| ViKnow test `config/test/deploy.env` | 容器内 `MINIO_ENDPOINT=host.docker.internal:19000` 为**底层** MinIO，应用 S3 SDK 仍指 **:19191** |

---

## 9. 变更记录

| 日期 | 说明 |
|------|------|
| 2026-09-21 | 初版：汇总飞书 §1 + 236 `viknow-juicefs-s3-gateway` 拓扑，供 Cloud Agent 速查 |
