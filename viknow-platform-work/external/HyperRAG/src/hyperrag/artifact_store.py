"""
Artifact Store - 统一管理 mineru_cache 和 video_cache 路径

为多个 workspace 提供确定性的 cache 目录路径，消除散落的路径拼装。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from .config_loader import HyperRAGConfig


class ArtifactStore:
    """
    多 workspace 的 artifact 路径管理器
    
    职责：
    - 基于 workspace 计算 mineru_cache/video_cache 根目录
    - 尊重 config 中的 override 配置
    - 提供确定性路径（无扫描回退）
    """
    
    def __init__(self, config: HyperRAGConfig):
        """
        初始化 ArtifactStore
        
        Args:
            config: HyperRAG 配置对象
        """
        self.config = config
        self._working_dir = Path(config.working_dir)
        
        # 缓存已计算的路径，避免重复计算
        self._mineru_cache_roots: Dict[str, Path] = {}
        self._video_cache_roots: Dict[str, Path] = {}
    
    def get_mineru_cache_root(self, workspace: str) -> Path:
        """
        获取指定 workspace 的 mineru_cache 根目录
        
        Args:
            workspace: workspace 名称
            
        Returns:
            mineru_cache 根目录的 Path 对象
        """
        if workspace in self._mineru_cache_roots:
            return self._mineru_cache_roots[workspace]
        
        # 检查是否有 override 配置
        if self.config.mineru_cache_dir:
            # 如果配置了 override，将其视为包含 workspace 的完整路径
            cache_root = Path(self.config.mineru_cache_dir)
        else:
            # 默认路径结构：<working_dir>/<workspace>/mineru_cache
            cache_root = self._working_dir / workspace / "mineru_cache"
        
        # 确保目录存在
        cache_root.mkdir(parents=True, exist_ok=True)
        
        # 缓存结果
        self._mineru_cache_roots[workspace] = cache_root
        return cache_root
    
    def get_video_cache_root(self, workspace: str) -> Path:
        """
        获取指定 workspace 的 video_cache 根目录
        
        Args:
            workspace: workspace 名称
            
        Returns:
            video_cache 根目录的 Path 对象
        """
        if workspace in self._video_cache_roots:
            return self._video_cache_roots[workspace]
        
        # 检查是否有 override 配置
        if self.config.video_cache_dir:
            # 如果配置了 override，将其视为包含 workspace 的完整路径
            cache_root = Path(self.config.video_cache_dir)
        else:
            # 默认路径结构：<working_dir>/<workspace>/video_cache
            cache_root = self._working_dir / workspace / "video_cache"
        
        # 确保目录存在
        cache_root.mkdir(parents=True, exist_ok=True)
        
        # 缓存结果
        self._video_cache_roots[workspace] = cache_root
        return cache_root
    
    def get_mineru_index_json(self, workspace: str, asset_id: str) -> Path:
        """
        获取 mineru 解析产物的 index.json 路径（确定性路径，不做扫描）
        
        Args:
            workspace: workspace 名称
            asset_id: 资产ID
            
        Returns:
            index.json 的 Path 对象
        """
        cache_root = self.get_mineru_cache_root(workspace)
        return cache_root / asset_id / "vlm" / "index.json"
    
    def get_video_index_json(self, workspace: str, asset_id: str) -> Path:
        """
        获取 video 解析产物的 index.json 路径（确定性路径，不做扫描）
        
        Args:
            workspace: workspace 名称
            asset_id: 资产ID（通常为视频文件名的 stem）
            
        Returns:
            index.json 的 Path 对象
        """
        cache_root = self.get_video_cache_root(workspace)
        return cache_root / asset_id / "index.json"

    def get_video_clip_cache_root(self) -> Path:
        """
        获取指定 workspace 的 video_clip_cache 根目录
        
        Args:
            workspace: workspace 名称
            
        Returns:
            video_clip_cache 根目录的 Path 对象
        """
        return self._working_dir / "temp_clips"
