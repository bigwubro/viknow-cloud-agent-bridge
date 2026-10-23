# Contextual Retrieval in HyperRAG

HyperRAG 现已支持 **Contextual Retrieval**（上下文检索），这是 Anthropic 提出的一种改进 RAG 系统检索准确性的技术。

## 什么是 Contextual Retrieval？

传统的 RAG 系统在将文档分块（chunking）时，每个块会丢失其在原文档中的上下文信息。Contextual Retrieval 通过为每个块添加上下文说明来解决这个问题：

- 使用 LLM 为每个 chunk 生成 2-3 句话的上下文说明
- 说明解释该 chunk 在文档中的位置、所属部分和主题
- 将上下文与原始 chunk 结合后再进行向量化
- 显著提高检索时的语义匹配准确度

根据 Anthropic 的测试，Contextual Retrieval 可以将检索失败率降低约 **49%**。

## 如何启用

### 1. 配置 HyperRAG

在创建 HyperRAG 实例时，设置相关配置项：

```python
from hyperrag import HyperRAG, HyperRAGConfig

# 定义用于生成上下文的 LLM 函数
def my_context_llm(prompt: str) -> str:
    """
    调用 LLM 生成上下文说明。
    
    Args:
        prompt: 包含文档和 chunk 信息的 prompt
    
    Returns:
        生成的上下文说明文本
    """
    # 示例：使用 OpenAI API
    from openai import OpenAI
    client = OpenAI()
    
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "你是一个帮助为文档片段生成简短上下文说明的助手。"},
            {"role": "user", "content": prompt}
        ],
        max_tokens=150,
        temperature=0.3,
    )
    return response.choices[0].message.content

# 创建配置，启用 Contextual Retrieval
config = HyperRAGConfig(
    working_dir="./hyperrag-storage",
    workspace="my_workspace",
    
    # 启用上下文嵌入
    enable_contextual_embeddings=True,
    contextual_llm_func=my_context_llm,
    contextual_max_tokens=100,  # 生成的上下文最大 token 数
    
    # 其他配置...
    llm_model_func=my_llm_func,
    embed_model_func=my_embed_func,
)

# 初始化 HyperRAG
rag = HyperRAG(config)
```

### 2. 使用不同的 LLM 提供商

#### OpenAI

```python
from openai import OpenAI

def openai_context_llm(prompt: str) -> str:
    client = OpenAI(api_key="your-api-key")
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150,
        temperature=0.3,
    )
    return response.choices[0].message.content
```

#### Anthropic Claude

```python
from anthropic import Anthropic

def claude_context_llm(prompt: str) -> str:
    client = Anthropic(api_key="your-api-key")
    message = client.messages.create(
        model="claude-3-haiku-20240307",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text
```

#### 本地模型（Ollama）

```python
import requests

def ollama_context_llm(prompt: str) -> str:
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": "llama3.2",
            "prompt": prompt,
            "stream": False,
        }
    )
    return response.json()["response"]
```

### 3. 处理文档

启用 Contextual Retrieval 后，正常处理文档即可：

```python
# 处理单个文档
rag.add_document("path/to/document.pdf")

# 或批量处理
rag.add_documents([
    "path/to/doc1.pdf",
    "path/to/doc2.docx",
    "path/to/doc3.txt",
])
```

## 工作原理

### 1. 文档分块

HyperRAG 首先使用 `HyperChunker` 将文档分成多个 chunks（按 token 限制和语义边界）。

### 2. 上下文生成

如果启用了 `enable_contextual_embeddings`，对每个 chunk：

1. 提取文档级元数据（标题、类型、来源等）
2. 构建包含文档上下文和 chunk 内容的 prompt
3. 调用 `contextual_llm_func` 生成上下文说明
4. 将上下文添加到 chunk 内容的开头

生成的 prompt 示例：

```
<document_context>
文档标题: 产品使用手册 | 类型: 文档 | 语言: zh | 来源: manual.pdf
</document_context>

文档标识: manual_v1.2
这是文档中的第 5 个片段。

请为以下文档片段（chunk）生成一段简短的上下文说明（2-3句话），
解释这个片段在整个文档中的位置、所属部分、以及主要讨论的主题。
这段上下文说明将用于帮助后续的信息检索，使检索系统能够更好地理解这个片段的背景和含义。

<chunk>
3. 安全注意事项

在使用本产品之前，请仔细阅读以下安全警告：
- 请勿在潮湿环境中使用
- 避免阳光直射
...
</chunk>

请直接输出上下文说明，不要包含任何前缀（如'上下文说明：'）或其他解释。
上下文说明应该简洁明了，不超过 100 个词。
```

LLM 生成的上下文示例：

```
本片段位于产品使用手册的安全说明部分，是手册的第三章节。
主要内容是关于产品使用时必须遵守的安全注意事项和警告信息，
包括环境要求和使用限制，旨在确保用户安全正确地使用产品。
```

### 3. 最终的 Chunk 内容

生成的 chunk 内容格式：

```
<context>本片段位于产品使用手册的安全说明部分，是手册的第三章节。
主要内容是关于产品使用时必须遵守的安全注意事项和警告信息，
包括环境要求和使用限制，旨在确保用户安全正确地使用产品。</context>

3. 安全注意事项

在使用本产品之前，请仔细阅读以下安全警告：
- 请勿在潮湿环境中使用
- 避免阳光直射
...
```

这样的内容被向量化后，能更好地匹配用户的查询意图。

## 性能考虑

### 1. LLM 调用成本

- 每个 chunk 都会调用一次 LLM 生成上下文
- 对于大型文档集，建议使用成本较低的模型（如 GPT-4o-mini、Claude Haiku）
- 或使用本地模型（如 Ollama）

### 2. 处理时间

- 上下文生成是串行处理的（未来版本可能支持批量处理）
- 会显著增加文档处理时间
- 建议在离线索引构建时使用，而不是在线查询时

### 3. 优化建议

```python
# 1. 使用更快的模型
config = HyperRAGConfig(
    enable_contextual_embeddings=True,
    contextual_llm_func=fast_llm_func,  # 使用快速模型
    contextual_max_tokens=50,  # 限制生成长度
)

# 2. 仅对重要文档启用
# 通过动态配置来控制
if document.is_important:
    rag.config.enable_contextual_embeddings = True
else:
    rag.config.enable_contextual_embeddings = False

rag.add_document(document.path)
```

## 配置参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable_contextual_embeddings` | `bool` | `False` | 是否启用上下文嵌入功能 |
| `contextual_llm_func` | `Callable[[str], str]` | `None` | 用于生成上下文的 LLM 函数 |
| `contextual_max_tokens` | `int` | `100` | 生成的上下文最大 token 数 |

## 实用工具

### 提取上下文和原始内容

```python
from hyperrag.contextual_embedder import ContextualEmbedder

# 从 chunk 中提取上下文
chunk_content = "<context>这是上下文</context>\n\n这是原始内容"
context = ContextualEmbedder.extract_context(chunk_content)
print(context)  # "这是上下文"

# 提取原始内容（不含上下文）
original = ContextualEmbedder.extract_original_content(chunk_content)
print(original)  # "这是原始内容"
```

## 调试和日志

启用 Contextual Retrieval 后，会在日志中看到相关信息：

```
2024-01-01 10:00:00 | INFO | ContextualEmbedder enabled with max_context_tokens=100
2024-01-01 10:00:05 | INFO | HyperChunker: generated 25 chunks for doc_id=manual_v1.2
2024-01-01 10:00:05 | INFO | HyperChunker: applying contextual embeddings to 25 chunks
2024-01-01 10:00:10 | INFO | ContextualEmbedder: generating contexts for 25 chunks
2024-01-01 10:00:30 | INFO | ContextualEmbedder: successfully added contexts to 25/25 chunks
```

## 何时使用

### 适合的场景

✅ **长文档处理**：技术文档、学术论文、法律文件等  
✅ **需要精确检索**：FAQ 系统、技术支持知识库  
✅ **跨章节信息**：需要理解文档结构的应用  
✅ **离线索引构建**：有足够时间预处理文档

### 不适合的场景

❌ **实时处理**：需要快速响应的场景  
❌ **小文档**：推文、短评论等无需额外上下文  
❌ **成本敏感**：预算有限，无法承担 LLM 调用成本  
❌ **简单问答**：基本的关键词匹配就能满足需求

## 参考资料

- [Anthropic: Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval) - 原始技术博客
- HyperRAG 源码：`src/hyperrag/contextual_embedder.py`
- HyperRAG 源码：`src/hyperrag/chunker.py`

## 常见问题

**Q: 上下文嵌入会影响检索性能吗？**  
A: 不会。上下文嵌入只在索引构建时进行，查询时使用标准的向量检索，性能不受影响。

**Q: 可以自定义生成上下文的 prompt 吗？**  
A: 当前版本使用内置 prompt。如需自定义，可以修改 `ContextualEmbedder._build_context_prompt()` 方法。

**Q: 上下文嵌入与原始内容如何组合？**  
A: 上下文被添加在内容开头，用 `<context>...</context>` 标记包裹，便于识别和后续处理。

**Q: 如果 LLM 调用失败怎么办？**  
A: 系统会记录错误日志，该 chunk 会使用原始内容（不含上下文），不会中断整个处理流程。

**Q: 支持批量生成上下文吗？**  
A: 当前版本是串行处理。未来版本计划支持批量 API 调用以提高效率。
