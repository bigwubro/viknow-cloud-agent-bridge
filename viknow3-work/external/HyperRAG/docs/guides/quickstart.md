# HyperRAG 快速开始

## 5分钟上手指南

### 前置要求

- Python 3.12+
- uv 包管理器

### 1. 项目设置

```bash
# 克隆项目
git clone <repository-url>
cd HyperRAG

# 安装依赖
uv sync

# 激活虚拟环境（可选）
source .venv/bin/activate
```

### 2. 基础使用

#### 初始化 HyperRAG

```python
from hyperrag import HyperRAGConfig, HyperRAGV2

# 创建 HyperRAG 实例
runtime = HyperRAGV2(HyperRAGConfig(working_dir="./rag_storage"))
```

#### 插入多模态内容

```python
# 将文本保存为文件后加入 workspace
job = runtime.add_asset(
    workspace="default",
    asset_id="guide-text",
    asset_path="./data/guide.txt",
)

# 插入图像（通过文件路径）
job = runtime.add_asset(
    workspace="default",
    asset_id="guide-image",
    asset_path="./data/diagram.jpg",
)

# 插入混合内容
job = runtime.add_asset(
    workspace="default",
    asset_id="guide-document",
    asset_path="./data/architecture.md",
)
```

#### 查询系统

```python
# 基础查询
result = runtime.query("default", "什么是机器学习？")
print(result)

# 高级查询（如果实现了增强查询接口）
# result = rag.enhanced_query(
#     "机器学习的应用",
#     filters={
#         "language": "zh",
#         "modality": ["text", "mixed"],
#         "min_quality": 0.7
#     }
# )
```

### 3. 验证安装

运行测试脚本验证功能：

```bash
# 运行增强分块测试
uv run python test_enhanced_chunking.py

# 查看测试结果
# 应该看到成功的多模态处理输出
```

### 4. 项目结构

```
HyperRAG/
├── src/hyperrag/           # 核心代码
│   ├── __init__.py
│   └── enhanced_chunking.py
├── docs/                   # 文档
├── test_enhanced_chunking.py
├── pyproject.toml
└── CLAUDE.md
```

## 下一步

- 📖 阅读 [多模态处理指南](./multimodal-guide.md)
- 🔧 查看 [高级配置选项](./advanced-config.md)
- 🏗️ 了解 [架构设计](../architecture/multimodal-architecture.md)

## 常见问题

### Q: 如何配置 LLM 函数？
A: 参考 [SingleLayerKGService 文档](../reference/single-layer-kg-service.md) 了解运行时边界。

### Q: 支持哪些图像格式？
A: 目前支持常见格式检测（jpg, png, gif等），具体处理需要实现对应的处理器。

### Q: 如何添加新的模态类型？
A: 参考 [多模态处理指南](./multimodal-guide.md) 中的扩展说明。

## 故障排除

### 依赖问题
```bash
# 重新安装依赖
uv sync --reinstall
```

### 导入错误
```bash
# 检查 Python 路径
uv run python -c "import sys; print(sys.path)"
```

### 性能问题
- 检查 chunk 大小设置
- 调整并发参数
- 参考 [性能优化指南](./advanced-config.md#性能优化)