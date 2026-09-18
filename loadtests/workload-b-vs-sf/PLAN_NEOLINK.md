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
| `qwen3.6-plus` | 200 |
| `qwen3.6-flash` | 200 |
| `qwen3.6-max-preview` | 200 |

`Qwen/Qwen3.6-35B-A3B` / `qwen3.6-35b-a3b` / `qwen3.6-35b` 都是 **404 model_not_found**。  
默认先用 **`qwen3.6-plus`**（3.6 里最接近通用对话档）。要换模型设 `NL_MODEL`。

这和硅基的 35B-A3B **不是同一款权重**，延迟/QPS 不能当同构对比，只能比「同一套 B prompt 的账单和限流形态」。

## 价格

`GET /backend/api/open/models/list-prices` 要登录 token，API key 返回 401。  
脚本默认单价 0，只记 `usage.prompt_tokens` / `completion_tokens`。确认刊例价后设：

```bash
export NL_CNY_PER_M_IN=...
export NL_CNY_PER_M_OUT=...
```

百炼 CN 的 qwen3.6-plus 参考（不是 Neolink 牌价）：输入 $0.276 / M，输出 $1.651 / M（≤256k）。

## 跑法（等你点头）

```bash
# 默认 qwen3.6-plus，1/8/32/80 × 10 分钟，同一套 3 万 B
NL_MODEL=qwen3.6-plus ./run.sh neolink

# 或换 flash / max
NL_MODEL=qwen3.6-flash ./run.sh neolink
```

输出目录建议：`out/nl-b-30k-<date>/`。
