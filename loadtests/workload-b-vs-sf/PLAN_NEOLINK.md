# Neolink（Token Switch）工作负载 B 准备

等你说开始再打量。**不打 :8500。** key 只在 236 `/home/cursor-agent/.secrets/neolink.key`，不进仓库。

文档：https://neolink.com/docs/instruction-manual/01-overview  
Qwen OpenAI 兼容：https://neolink.com/docs/instruction-manual/chat/04-qwen(openai)

## 接口

| 项 | 值 |
|---|---|
| Base URL | `https://neolink.com/api/v1` |
| Chat | `POST /chat/completions` |
| 模型列表 | `GET /v1/models`（已通，200，146 个） |
| 鉴权 | `Authorization: Bearer <API Key>` |
| thinking | `enable_thinking: false` |
| 数据集 | 和硅基同一套 B：共享 16500 + 独特 13500，目标 3 万，`max_tokens=184` |

## 模型：没有 35B-A3B

这个 key 下列出的 Qwen3.6 只有三个，短请求都 200：

| 模型 ID | 短请求 |
|---|---|
| `qwen3.6-flash` | 200（对标 35B-A3B） |
| `qwen3.6-plus` | 200（闭源中档，不对标） |
| `qwen3.6-max-preview` | 200（旗舰，不对标） |

`Qwen/Qwen3.6-35B-A3B` 这个 HuggingFace 名在 Neolink 上 404。百炼把开源 **35B-A3B（35B/激活 3B）** 挂在服务名 **`qwen3.6-flash`** 上；`plus` / `max` 是另一档闭源。  
默认改成 **`qwen3.6-flash`**。网关仍是百炼托管（还带视觉、1M 上下文），236 本地是 FP8 纯文本，账单和限流能比，serving 形态仍不完全同构。

## 价格

`GET /backend/api/open/models/list-prices` 要登录 token，API key 返回 401。  
脚本默认单价 0，只记 `usage.prompt_tokens` / `completion_tokens`。确认刊例价后设：

```bash
export NL_CNY_PER_M_IN=...
export NL_CNY_PER_M_OUT=...
```

百炼 CN `qwen3.6-flash` 参考（不是 Neolink 牌价）：输入 $0.165 / M，输出 $0.99 / M（≤256k）。

## 跑法（等你点头）

```bash
# 默认 qwen3.6-flash，1/8/32/80 × 10 分钟，同一套 3 万 B
NL_MODEL=qwen3.6-flash ./run.sh neolink
```

输出目录建议：`out/nl-b-30k-<date>/`。
