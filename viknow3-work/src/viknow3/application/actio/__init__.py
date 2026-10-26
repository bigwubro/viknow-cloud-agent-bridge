"""Shell-owned Actio client. This package is the only place that imports actio."""

from viknow3.application.actio.client import get_actio_client, normalize_global_context

__all__ = ["get_actio_client", "normalize_global_context"]
