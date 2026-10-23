"""Application service for safe, read-only Feishu Wiki selection preview."""

from __future__ import annotations

import asyncio
import mimetypes
import re
from enum import StrEnum
from io import BytesIO
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zipfile import BadZipFile, ZipFile

import httpx
from pydantic import BaseModel, ConfigDict, Field

from viknow.core.config import FeishuKnowledgeConfig
from viknow.core.knowledge.feishu.domain import FeishuTrackingMode
from viknow.core.knowledge.uploads.domain import KnowledgeUploadRecord, KnowledgeUploadSource
from viknow.core.knowledge.uploads.manager import KnowledgeUploadManager
from viknow.integrations.feishu import (
    FeishuApiError,
    FeishuClient,
    FeishuConfigurationError,
    FeishuTenantTokenProvider,
    FeishuWikiNode,
)

_NODE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
HYPERRAG_FILE_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".txt",
        ".md",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsx",
        ".png",
        ".jpg",
        ".jpeg",
        ".bmp",
        ".tiff",
        ".tif",
        ".gif",
        ".webp",
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".flv",
        ".webm",
    }
)
_OOXML_REQUIRED_MEMBER = {
    ".docx": "word/document.xml",
    ".pptx": "ppt/presentation.xml",
    ".xlsx": "xl/workbook.xml",
}
_OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
_OOXML_TO_LEGACY_EXTENSION = {
    ".docx": ".doc",
    ".pptx": ".ppt",
    ".xlsx": ".xls",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeishuKnowledgeStatus(_StrictModel):
    enabled: bool
    configured: bool
    auth_mode: str = "tenant_access_token"
    space_id: str | None = None
    tenant_host: str | None = None
    missing_env_names: list[str] = Field(default_factory=list)
    default_sync_interval_seconds: int


class FeishuKnowledgeNodePage(_StrictModel):
    space_id: str
    space_name: str
    items: list[FeishuWikiNode]
    has_more: bool = False
    page_token: str | None = None


class FeishuImportResult(_StrictModel):
    space_id: str
    library_id: str
    uploads: list[KnowledgeUploadRecord]


class FeishuDiscoveryResult(_StrictModel):
    nodes: list[FeishuWikiNode]
    missing_node_tokens: list[str] = Field(default_factory=list)


class FeishuMaterializedDocument(_StrictModel):
    node: FeishuWikiNode
    filename: str
    content: bytes
    canonical_url: str | None = None


class FeishuPreviewStatus(StrEnum):
    VALID = "valid"
    DUPLICATE = "duplicate"
    UNSUPPORTED = "unsupported"
    CROSS_SPACE = "cross_space"
    PERMISSION_DENIED = "permission_denied"
    INVALID = "invalid"


class FeishuPreviewItem(_StrictModel):
    node_token: str
    status: FeishuPreviewStatus
    title: str | None = None
    space_id: str | None = None
    obj_token: str | None = None
    obj_type: str | None = None
    canonical_url: str | None = None
    message: str


class FeishuPreviewResult(_StrictModel):
    space_id: str
    items: list[FeishuPreviewItem]

    @property
    def valid_count(self) -> int:
        return sum(item.status == FeishuPreviewStatus.VALID for item in self.items)


class FeishuKnowledgeService:
    """Validate one configured Space and materialize supported documents for indexing."""

    def __init__(
        self,
        *,
        config: FeishuKnowledgeConfig,
        client: FeishuClient | None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._http_client = http_client

    def status(self) -> FeishuKnowledgeStatus:
        enabled = self._config.is_enabled()
        missing = self._config.missing_env_names() if enabled else []
        return FeishuKnowledgeStatus(
            enabled=enabled,
            configured=enabled and not missing and self._client is not None,
            space_id=self._config.resolve_space_id() if enabled else None,
            tenant_host=self._config.resolve_tenant_host() if enabled else None,
            missing_env_names=missing,
            default_sync_interval_seconds=self._config.default_sync_interval_seconds,
        )

    async def list_nodes(
        self,
        *,
        parent_node_token: str | None = None,
        page_token: str | None = None,
    ) -> FeishuKnowledgeNodePage:
        client, space_id = self._require_ready()
        if parent_node_token and not _NODE_TOKEN_RE.fullmatch(parent_node_token):
            raise ValueError("parent_node_token is invalid")
        space, page = await asyncio.gather(
            client.get_space(space_id),
            client.list_nodes(
                space_id=space_id,
                parent_node_token=parent_node_token,
                page_token=page_token,
                page_size=self._config.page_size,
            ),
        )
        return FeishuKnowledgeNodePage(
            space_id=space_id,
            space_name=space.name or "飞书知识库",
            items=page.items,
            has_more=page.has_more,
            page_token=page.page_token,
        )

    async def preview(self, node_tokens: list[str]) -> FeishuPreviewResult:
        client, space_id = self._require_ready()
        if not node_tokens:
            raise ValueError("node_tokens must not be empty")
        if len(node_tokens) > 100:
            raise ValueError("a batch may contain at most 100 nodes")

        seen: set[str] = set()
        semaphore = asyncio.Semaphore(5)

        async def validate(node_token: str) -> FeishuPreviewItem:
            normalized = node_token.strip()
            if normalized in seen:
                return FeishuPreviewItem(
                    node_token=normalized,
                    status=FeishuPreviewStatus.DUPLICATE,
                    message="同一篇文档在本批次中重复",
                )
            seen.add(normalized)
            if not _NODE_TOKEN_RE.fullmatch(normalized):
                return FeishuPreviewItem(
                    node_token=normalized,
                    status=FeishuPreviewStatus.INVALID,
                    message="飞书知识库节点标识格式无效",
                )
            try:
                async with semaphore:
                    node = await client.get_node(normalized)
            except FeishuApiError as exc:
                status = (
                    FeishuPreviewStatus.PERMISSION_DENIED
                    if exc.permission_denied
                    else FeishuPreviewStatus.INVALID
                )
                message = "应用无权读取该文档" if exc.permission_denied else "无法读取该文档"
                return FeishuPreviewItem(
                    node_token=normalized,
                    status=status,
                    message=message,
                )
            return self._preview_node(node, expected_space_id=space_id)

        items = await asyncio.gather(*(validate(token) for token in node_tokens))
        return FeishuPreviewResult(space_id=space_id, items=list(items))

    async def import_nodes(
        self,
        node_tokens: list[str],
        *,
        upload_manager: KnowledgeUploadManager,
    ) -> FeishuImportResult:
        """Download validated documents and persist/index them in the Space library."""

        preview = await self.preview(node_tokens)
        rejected = [item for item in preview.items if item.status != FeishuPreviewStatus.VALID]
        if rejected:
            raise ValueError(rejected[0].message)

        client, space_id = self._require_ready()
        library_id = self.library_id()
        uploads: list[KnowledgeUploadRecord] = []
        for selected in preview.items:
            upload_id = str(uuid5(NAMESPACE_URL, f"feishu:{space_id}:{selected.node_token}"))
            existing = upload_manager.get_upload(upload_id)
            if existing is not None:
                uploads.append(
                    await upload_manager.retry_index(
                        upload_id,
                        index_params=self._index_params(
                            space_id=space_id,
                            node_token=selected.node_token,
                            canonical_url=selected.canonical_url,
                        ),
                        source=self._upload_source(
                            space_id=space_id,
                            node_token=selected.node_token,
                            canonical_url=selected.canonical_url,
                        ),
                    )
                )
                continue

            node = await client.get_node(selected.node_token)
            revalidated = self._preview_node(node, expected_space_id=space_id)
            if revalidated.status != FeishuPreviewStatus.VALID:
                raise ValueError(revalidated.message)
            if node.obj_type == "docx":
                content = await client.export_docx(node.obj_token)
                filename = self._docx_filename(node.title)
            else:
                content = await client.download_file(node.obj_token)
                filename = Path(node.title).name
            filename = self._normalize_download_filename(filename, content)
            self._validate_download(filename, content)

            uploads.append(
                await upload_manager.upload_stream(
                    filename=filename,
                    stream=BytesIO(content),
                    size_bytes=len(content),
                    content_type=mimetypes.guess_type(filename)[0],
                    upload_id=upload_id,
                    library_id=library_id,
                    index_params=self._index_params(
                        space_id=space_id,
                        node_token=node.node_token,
                        canonical_url=revalidated.canonical_url,
                    ),
                    source=self._upload_source(
                        space_id=space_id,
                        node_token=node.node_token,
                        canonical_url=revalidated.canonical_url,
                    ),
                )
            )
        return FeishuImportResult(space_id=space_id, library_id=library_id, uploads=uploads)

    async def discover_for_sync(
        self,
        node_tokens: list[str],
        *,
        tracking_mode: FeishuTrackingMode,
    ) -> FeishuDiscoveryResult:
        """Discover a fully scanned selected-node or subtree synchronization scope."""

        client, space_id = self._require_ready()
        normalized_tokens = (
            []
            if tracking_mode == FeishuTrackingMode.SPACE
            else self._normalized_node_tokens(node_tokens)
        )
        discovered: dict[str, FeishuWikiNode] = {}
        missing: list[str] = []
        queue: list[str] = list(normalized_tokens)

        if tracking_mode == FeishuTrackingMode.SPACE:
            page_token: str | None = None
            while True:
                page = await client.list_nodes(
                    space_id=space_id,
                    parent_node_token=None,
                    page_token=page_token,
                    page_size=self._config.page_size,
                )
                for node in page.items:
                    if node.space_id != space_id:
                        raise ValueError("飞书知识空间扫描返回了其他知识空间的节点")
                    discovered[node.node_token] = node
                    if node.has_child:
                        queue.append(node.node_token)
                if not page.has_more:
                    break
                if not page.page_token:
                    raise FeishuApiError("飞书知识库分页响应缺少下一页标识")
                page_token = page.page_token
        else:
            for node_token in normalized_tokens:
                try:
                    node = await client.get_node(node_token)
                except FeishuApiError as exc:
                    if exc.not_found:
                        missing.append(node_token)
                        queue.remove(node_token)
                        continue
                    raise
                if node.space_id != space_id:
                    raise ValueError("所选文档不属于已配置的飞书知识空间")
                discovered[node.node_token] = node

        if tracking_mode == FeishuTrackingMode.SELECTED:
            return FeishuDiscoveryResult(
                nodes=list(discovered.values()),
                missing_node_tokens=missing,
            )

        expanded: set[str] = set()
        while queue:
            parent_token = queue.pop(0)
            if parent_token in expanded:
                continue
            expanded.add(parent_token)
            parent = discovered.get(parent_token)
            if parent is None or not parent.has_child:
                continue
            page_token: str | None = None
            while True:
                page = await client.list_nodes(
                    space_id=space_id,
                    parent_node_token=parent_token,
                    page_token=page_token,
                    page_size=self._config.page_size,
                )
                for child in page.items:
                    if child.space_id != space_id:
                        raise ValueError("飞书子树扫描返回了其他知识空间的节点")
                    if child.node_token not in discovered:
                        discovered[child.node_token] = child
                        if child.has_child:
                            queue.append(child.node_token)
                if not page.has_more:
                    break
                if not page.page_token:
                    raise FeishuApiError("飞书知识库分页响应缺少下一页标识")
                page_token = page.page_token
        return FeishuDiscoveryResult(
            nodes=list(discovered.values()),
            missing_node_tokens=missing,
        )

    async def materialize_for_sync(self, node_token: str) -> FeishuMaterializedDocument:
        """Revalidate and download one supported node for the shared upload pipeline."""

        client, space_id = self._require_ready()
        node = await client.get_node(node_token)
        validated = self._preview_node(node, expected_space_id=space_id)
        if validated.status != FeishuPreviewStatus.VALID:
            raise ValueError(validated.message)
        if node.obj_type == "docx":
            content = await client.export_docx(node.obj_token)
            filename = self._docx_filename(node.title)
        else:
            content = await client.download_file(node.obj_token)
            filename = Path(node.title).name
        filename = self._normalize_download_filename(filename, content)
        self._validate_download(filename, content)
        return FeishuMaterializedDocument(
            node=node,
            filename=filename,
            content=content,
            canonical_url=validated.canonical_url,
        )

    def list_imports(self, upload_manager: KnowledgeUploadManager) -> list[KnowledgeUploadRecord]:
        if not self.status().enabled or not self._config.resolve_space_id():
            return []
        return upload_manager.list_uploads(library_id=self.library_id())

    def library_id(self) -> str:
        space_id = self._config.resolve_space_id()
        if not space_id:
            raise FeishuConfigurationError("飞书知识库配置缺少 Space ID")
        return f"{self._config.library_id_prefix}-{space_id}"

    def index_params_for(
        self,
        *,
        node_token: str,
        canonical_url: str | None,
        source_id: str | None = None,
        sync_run_id: str | None = None,
        sync_trigger: str | None = None,
    ) -> dict[str, str | None]:
        _, space_id = self._require_ready()
        params = self._index_params(
            space_id=space_id,
            node_token=node_token,
            canonical_url=canonical_url,
        )
        params.update(
            source_id=source_id,
            sync_run_id=sync_run_id,
            sync_trigger=sync_trigger,
        )
        return params

    def upload_source_for(
        self,
        *,
        node_token: str,
        canonical_url: str | None,
    ) -> KnowledgeUploadSource:
        _, space_id = self._require_ready()
        return self._upload_source(
            space_id=space_id,
            node_token=node_token,
            canonical_url=canonical_url,
        )

    @staticmethod
    def selection_support(node: FeishuWikiNode) -> tuple[bool, str]:
        if node.obj_type == "docx":
            return True, "飞书原生新版文档"
        if node.obj_type == "file":
            extension = Path(node.title).suffix.lower()
            if extension in HYPERRAG_FILE_EXTENSIONS:
                return True, f"HyperRAG 支持的 {extension} 上传文件"
        return False, f"暂不支持 {node.obj_type} 类型或该文件格式"

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()

    def _preview_node(self, node: FeishuWikiNode, *, expected_space_id: str) -> FeishuPreviewItem:
        common = {
            "node_token": node.node_token,
            "title": node.title or "未命名文档",
            "space_id": node.space_id,
            "obj_token": node.obj_token,
            "obj_type": node.obj_type,
            "canonical_url": self._canonical_url(node.node_token),
        }
        if node.space_id != expected_space_id:
            return FeishuPreviewItem(
                **common,
                status=FeishuPreviewStatus.CROSS_SPACE,
                message="所选文档不属于已配置的飞书知识空间",
            )
        supported, reason = self.selection_support(node)
        if not supported:
            return FeishuPreviewItem(
                **common,
                status=FeishuPreviewStatus.UNSUPPORTED,
                message=reason,
            )
        return FeishuPreviewItem(
            **common,
            status=FeishuPreviewStatus.VALID,
            message="校验通过，可加入同步批次",
        )

    @staticmethod
    def _docx_filename(title: str) -> str:
        name = Path(title or "未命名飞书文档").name
        return name if Path(name).suffix.lower() == ".docx" else f"{name}.docx"

    @staticmethod
    def _normalize_download_filename(filename: str, content: bytes) -> str:
        """Correct a modern Office suffix when Feishu stores a legacy binary file.

        Feishu file nodes expose the uploaded display name rather than an independently
        verified media type. Some existing files therefore carry a ``.pptx``/``.docx``/
        ``.xlsx`` name while their bytes are the corresponding legacy OLE document.
        Preserve the Feishu source and correct only the ViKnow persisted filename.
        """

        extension = Path(filename).suffix.lower()
        legacy_extension = _OOXML_TO_LEGACY_EXTENSION.get(extension)
        if legacy_extension is None or not content.startswith(_OLE_MAGIC):
            return filename
        return str(Path(filename).with_suffix(legacy_extension))

    @staticmethod
    def _validate_download(filename: str, content: bytes) -> None:
        extension = Path(filename).suffix.lower()
        if extension not in HYPERRAG_FILE_EXTENSIONS:
            raise ValueError(f"不支持的飞书上传文件格式：{extension or '无扩展名'}")
        if not content:
            raise ValueError("飞书文件下载结果为空")
        required_member = _OOXML_REQUIRED_MEMBER.get(extension)
        if required_member is not None and not FeishuKnowledgeService._is_ooxml(
            content, required_member=required_member
        ):
            label = extension.removeprefix(".").upper()
            legacy_hint = "（可能是旧版 .doc）" if extension == ".docx" else ""
            raise ValueError(f"文件扩展名为 {extension}，但下载内容实际不是 {label}{legacy_hint}")
        if extension == ".pdf" and not content.startswith(b"%PDF-"):
            raise ValueError("文件扩展名为 .pdf，但下载内容实际不是 PDF")
        if extension in {".doc", ".ppt", ".xls"} and not content.startswith(_OLE_MAGIC):
            raise ValueError(f"文件扩展名为 {extension}，但下载内容不是有效的旧版 Office 文件")

    @staticmethod
    def _is_ooxml(content: bytes, *, required_member: str) -> bool:
        try:
            with ZipFile(BytesIO(content)) as archive:
                names = set(archive.namelist())
        except (BadZipFile, OSError):
            return False
        return "[Content_Types].xml" in names and required_member in names

    @staticmethod
    def _index_params(
        *,
        space_id: str,
        node_token: str,
        canonical_url: str | None,
    ) -> dict[str, str | None]:
        return {
            "source_type": "feishu",
            "source_url": canonical_url,
            "source_node_token": node_token,
            "source_space_id": space_id,
        }

    @staticmethod
    def _upload_source(
        *,
        space_id: str,
        node_token: str,
        canonical_url: str | None,
    ) -> KnowledgeUploadSource:
        return KnowledgeUploadSource(
            type="feishu",
            url=canonical_url,
            node_token=node_token,
            space_id=space_id,
        )

    def _canonical_url(self, node_token: str) -> str | None:
        host = self._config.resolve_tenant_host()
        return f"https://{host}/wiki/{node_token}" if host else None

    @staticmethod
    def _normalized_node_tokens(node_tokens: list[str]) -> list[str]:
        if not node_tokens:
            raise ValueError("node_tokens must not be empty")
        if len(node_tokens) > 100:
            raise ValueError("a source may contain at most 100 root nodes")
        normalized: list[str] = []
        for node_token in node_tokens:
            cleaned = node_token.strip()
            if not _NODE_TOKEN_RE.fullmatch(cleaned):
                raise ValueError("飞书知识库节点标识格式无效")
            if cleaned not in normalized:
                normalized.append(cleaned)
        return normalized

    def _require_ready(self) -> tuple[FeishuClient, str]:
        status = self.status()
        if not status.enabled:
            raise FeishuConfigurationError("飞书知识库接入尚未启用")
        if not status.configured or self._client is None or not status.space_id:
            names = ", ".join(status.missing_env_names)
            suffix = f"：{names}" if names else ""
            raise FeishuConfigurationError(f"飞书知识库配置不完整{suffix}")
        return self._client, status.space_id


def build_feishu_knowledge_service(config: FeishuKnowledgeConfig) -> FeishuKnowledgeService:
    """Build a disabled status service or a real read-only OpenAPI client."""

    if not config.is_enabled() or config.missing_env_names():
        return FeishuKnowledgeService(config=config, client=None)
    config.resolve_tenant_host()
    app_id = config.resolve_app_id()
    app_secret = config.resolve_app_secret()
    if app_id is None or app_secret is None:
        return FeishuKnowledgeService(config=config, client=None)
    http_client = httpx.AsyncClient(timeout=config.request_timeout_seconds)
    token_provider = FeishuTenantTokenProvider(
        client=http_client,
        api_base_url=config.api_base_url,
        app_id=app_id,
        app_secret=app_secret,
        client_factory=lambda: httpx.AsyncClient(timeout=config.request_timeout_seconds),
    )
    client = FeishuClient(
        client=http_client,
        token_provider=token_provider,
        api_base_url=config.api_base_url,
    )
    return FeishuKnowledgeService(config=config, client=client, http_client=http_client)
