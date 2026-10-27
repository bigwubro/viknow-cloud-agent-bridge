"""Import the actio package, including a source checkout that is not installed."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType


def load_actio(module_name: str) -> ModuleType:
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError:
        repo_root = Path(__file__).resolve().parents[4]
        actio_src = repo_root / "external" / "actio" / "src"
        if actio_src.is_dir() and str(actio_src) not in sys.path:
            sys.path.insert(0, str(actio_src))
        return importlib.import_module(module_name)
