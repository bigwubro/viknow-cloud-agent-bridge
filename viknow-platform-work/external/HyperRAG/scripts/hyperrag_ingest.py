#!/usr/bin/env python3
"""
使用 HyperRAG 启动文件解析/索引的命令行脚本。

示例:
  python scripts/hyperrag_ingest.py --input ./tests/test_document.md \
    --working-dir ./hyperrag_storage --workspace default
"""

import argparse
import asyncio
import sys
from pathlib import Path

import numpy as np
from loguru import logger

try:
    from hyperrag import HyperRAGConfig, HyperRAGV2
    from hyperrag.asset_status import AssetStatus
except ModuleNotFoundError:
    # 允许脚本在未作为包安装时运行：将项目 src 目录加入 sys.path 后重试导入
    _REPO_ROOT = Path(__file__).resolve().parents[1]
    _SRC_DIR = _REPO_ROOT / "src"
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from hyperrag import HyperRAGConfig, HyperRAGV2  # noqa: E402
    from hyperrag.asset_status import AssetStatus  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通过 HyperRAG 解析并索引文档")
    parser.add_argument(
        "--input",
        required=True,
        type=str,
        help="输入文件路径",
    )
    parser.add_argument(
        "--working-dir",
        required=True,
        type=str,
        help="HyperRAG 工作目录（原生运行时数据存储根目录）",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        type=str,
        help="Workspace 名称 (用于隔离不同实例的数据)",
    )
    parser.add_argument(
        "--enable-indexing",
        action="store_true",
        help="是否执行原生索引（默认关闭，仅解析与分块）",
    )
    # MinerU 相关可选参数
    parser.add_argument("--method", type=str, default="auto", help="MinerU 解析方法: auto/txt/ocr")
    parser.add_argument("--lang", type=str, default=None, help="文档语言代码")
    parser.add_argument("--device", type=str, default="cpu", help="推理设备: cpu/cuda/mps")
    parser.add_argument("--backend", type=str, default=None, help="MinerU 后端")
    parser.add_argument("--no-formula", action="store_true", help="禁用公式解析")
    parser.add_argument("--no-table", action="store_true", help="禁用表格解析")
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        logger.error(f"文件不存在: {input_path}")
        return 1

    # 内置最小可用的 embedding 与 LLM 函数，满足原生运行时初始化要求。
    async def _dummy_embed_batch(batch):
        if isinstance(batch, str):
            batch = [batch]
        dim = 8
        out = np.zeros((len(batch), dim), dtype=np.float32)
        for i, text in enumerate(batch):
            # 简单哈希到 one-hot 维度
            h = sum(ord(c) for c in text) % dim
            out[i, h] = 1.0
        return out

    async def _dummy_llm(*_args, **_kwargs) -> str:
        return "OK"

    config = HyperRAGConfig(
        working_dir=args.working_dir,
        workspace=args.workspace,
        llm_model_func=_dummy_llm,
        embed_model_func=_dummy_embed_batch,
        enable_indexing=bool(args.enable_indexing),
        mineru_method=args.method,
        mineru_lang=args.lang,
        mineru_device=args.device,
        mineru_backend=args.backend,
        mineru_formula=not args.no_formula,
        mineru_table=not args.no_table,
    )

    logger.info(
        "启动 HyperRAG 解析: "
        f"input={input_path} working_dir={config.working_dir} workspace={config.workspace}"
    )

    rag = HyperRAGV2(config=config)
    try:
        try:
            asset_id = input_path.stem
            job_id = await rag.aadd_asset(
                workspace=config.workspace,
                asset_id=asset_id,
                asset_path=str(input_path),
                params={},
            )
            logger.info(f"任务已入队，asset_id={asset_id} job_id={job_id}")
        except Exception as e:
            logger.exception(f"入队失败: {e}")
            return 1

        terminal_status = {AssetStatus.COMPLETED, AssetStatus.FAILED, AssetStatus.CANCELLED}
        last_status = None

        while True:
            try:
                status = await rag.aget_asset_status(config.workspace, asset_id)
            except Exception as e:
                logger.exception(f"查询状态失败: {e}")
                return 1

            if status is None:
                await asyncio.sleep(0.5)
                continue

            if last_status != status.status:
                logger.info(
                    "状态更新: "
                    f"asset_id={asset_id} status={status.status.value} "
                    f"progress={status.progress:.2f}"
                )
                last_status = status.status

            if status.status in terminal_status:
                if status.status == AssetStatus.COMPLETED:
                    logger.info("✅ 处理完成")
                    return 0
                elif status.status == AssetStatus.FAILED:
                    logger.error(f"❌ 处理失败: {status.error_message}")
                    return 2
                else:
                    logger.warning("⚠️ 处理被取消")
                    return 3

            await asyncio.sleep(1.0)
    finally:
        await rag.shutdown()


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
