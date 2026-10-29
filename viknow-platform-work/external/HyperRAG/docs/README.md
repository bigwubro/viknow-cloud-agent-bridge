# HyperRAG Documentation

欢迎来到 HyperRAG 文档！HyperRAG 是使用 `SingleLayerKGService` 的多模态 RAG 知识搜索引擎。

## 📚 文档导航

### 🏗️ [架构设计](./architecture/)
- [多模态架构概览](./architecture/multimodal-architecture.md) - 系统整体架构设计
- [自定义字段扩展](./architecture/custom-fields.md) - 如何扩展 chunk 自定义字段
- [数据流分析](./architecture/data-flow.md) - 完整数据处理流程
- [视频与图像处理设计](./architecture/video-design.md) - VideoParser 与 ImageEnhancer 设计

### 📖 [开发指南](./guides/)
- [快速开始](./guides/quickstart.md) - 5分钟上手指南
- [多模态处理](./guides/multimodal-guide.md) - 处理图像、音频、视频
- [高级配置](./guides/advanced-config.md) - 深度定制和优化
- [部署指南](./guides/deployment.md) - 生产环境部署

### 📋 [API 参考](./reference/)
- [SingleLayerKGService](./reference/single-layer-kg-service.md) - 知识图谱运行时参考
- [增强分块函数](./reference/enhanced-chunking.md) - 自定义分块函数 API
- [配置选项](./reference/configuration.md) - 完整配置参数说明
- [变更日志](./reference/changelog.md) - 版本改动与迁移说明

### 🔬 [研究文档](./research/)
- [技术验证](./research/technical-validation.md) - 核心技术可行性验证
- [实现路径](./research/implementation-roadmap.md) - 详细实现计划
- [性能基准](./research/benchmarks.md) - 性能测试和优化

## 🚀 快速开始

1. **环境设置**
   ```bash
   uv sync
   ```

2. **基础使用**
   ```python
   from hyperrag import HyperRAGV2
   
   # 初始化多模态 RAG 系统
   runtime = HyperRAGV2()
   result = runtime.query("default", "你的问题")
   ```

3. **更多信息** → [完整快速开始指南](./guides/quickstart.md)

## 🤝 贡献

欢迎贡献代码和文档！请参考开发指南了解更多信息。

## 📄 许可证

Apache License 2.0