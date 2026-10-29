"""Pytest: optional test Postgres DSN defaults before viknow3 imports."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["VIKNOW_USE_SCRIPT_MODEL"] = "1"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from pg_test_env import apply_if_configured

apply_if_configured()
