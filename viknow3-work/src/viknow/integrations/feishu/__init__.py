"""Read-only Feishu OpenAPI adapter for Wiki knowledge discovery."""

from viknow.integrations.feishu.auth import FeishuTenantTokenProvider
from viknow.integrations.feishu.client import FeishuClient
from viknow.integrations.feishu.domain import FeishuSpace, FeishuWikiNode, FeishuWikiNodePage
from viknow.integrations.feishu.errors import (
    FeishuApiError,
    FeishuConfigurationError,
    FeishuIntegrationError,
)

__all__ = [
    "FeishuApiError",
    "FeishuClient",
    "FeishuConfigurationError",
    "FeishuIntegrationError",
    "FeishuSpace",
    "FeishuTenantTokenProvider",
    "FeishuWikiNode",
    "FeishuWikiNodePage",
]
