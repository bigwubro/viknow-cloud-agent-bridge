#!/usr/bin/env python3
"""
文档解析脚本

使用MinerUParser解析文档并保存Assets和Segments到JSON文件。

用法:
    python parse_document.py <file_path> <output_dir> [--mineru-dir <dir>]

示例:
    python parse_document.py document.pdf ./output
    python parse_document.py document.docx ./output --mineru-dir ./mineru_cache
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from loguru import logger
from src.hyperrag.parser import MinerUParser
from src.hyperrag.schema import Asset, Segment


def parse_args():
    parser = argparse.ArgumentParser(description='解析文档并保存为JSON格式')
    parser.add_argument('file_path', type=str, help='要解析的文件路径')
    parser.add_argument('output_dir', type=str, help='输出目录')
    parser.add_argument('--mineru-dir', type=str, help='MinerU缓存目录 (默认使用临时目录)')
    parser.add_argument('--method', choices=['auto', 'txt', 'ocr'], 
                       default='auto', help='解析方法')
    parser.add_argument('--lang', type=str, help='文档语言代码')
    
    return parser.parse_args()


def save_asset_to_json(asset: Asset, output_file: Path):
    """保存Asset到JSON文件"""
    asset_dict = {
        'doc_id': asset.doc_id,
        'source_uri': asset.source_uri,
        'modality': asset.modality,
        'file_hash': asset.file_hash,
        'created_at': asset.created_at,
        'segments_count': asset.segments_count,
        'title': asset.title,
        'language': asset.language,
        'extra': asset.extra
    }
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(asset_dict, f, ensure_ascii=False, indent=2)


def save_segments_to_json(segments: list[Segment], output_file: Path):
    """保存Segments到JSON文件"""
    segments_list = []
    
    for segment in segments:
        segment_dict = {
            'segment_id': segment.segment_id,
            'doc_id': segment.doc_id,
            'order': segment.order,
            'type': segment.type,
            'text': segment.text,
            'location': segment.location,
            'extra': segment.extra
        }
        segments_list.append(segment_dict)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(segments_list, f, ensure_ascii=False, indent=2)


def main():
    args = parse_args()
    
    # 验证输入文件
    file_path = Path(args.file_path)
    if not file_path.exists():
        logger.error(f"文件不存在: {file_path}")
        sys.exit(1)
    
    # 创建输出目录
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 设置MinerU目录
    if args.mineru_dir:
        mineru_dir = Path(args.mineru_dir)
    else:
        mineru_dir = Path(tempfile.mkdtemp(prefix="mineru_"))
        logger.info(f"使用临时MinerU目录: {mineru_dir}")
    
    # 生成文档ID
    doc_id = file_path.stem
    
    logger.info(f"开始解析文档: {file_path}")
    logger.info(f"文档ID: {doc_id}")
    logger.info(f"输出目录: {output_dir}")
    
    try:
        # 初始化解析器
        parser = MinerUParser()
        
        # 解析文档
        asset, segments = parser.parse_document(
            doc_id=doc_id,
            doc_path=file_path,
            mineru_dir=mineru_dir,
            method=args.method,
            lang=args.lang
        )
        
        # 保存结果
        asset_file = output_dir / f"{doc_id}_asset.json"
        segments_file = output_dir / f"{doc_id}_segments.json"
        
        save_asset_to_json(asset, asset_file)
        save_segments_to_json(segments, segments_file)
        
        logger.info(f"✅ 解析完成!")
        logger.info(f"📄 Asset保存到: {asset_file}")
        logger.info(f"📋 Segments保存到: {segments_file}")
        logger.info(f"📊 共解析出 {len(segments)} 个segments")
        
        # 显示简要统计
        segment_types = {}
        for segment in segments:
            segment_type = segment.type
            segment_types[segment_type] = segment_types.get(segment_type, 0) + 1
        
        logger.info("📈 Segments类型统计:")
        for seg_type, count in segment_types.items():
            logger.info(f"  {seg_type}: {count}")
            
    except Exception as e:
        logger.error(f"❌ 解析失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()