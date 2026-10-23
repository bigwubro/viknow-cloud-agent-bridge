from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from viknow3.application.hyperrag.actio_materialize import resolve_viknow3_repo_root


class ActioRepoRootTests(unittest.TestCase):
    def test_resolve_repo_root_ignores_missing_viknow3_root(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with mock.patch.dict(
            os.environ,
            {
                "VIKNOW3_ROOT": "/opt/viknow3",
                "VIKNOW_REPO_ROOT": "",
                "VIKNOW_HYPERRAG_REPO_ROOT": "",
            },
            clear=False,
        ):
            self.assertEqual(resolve_viknow3_repo_root(), repo)

    def test_resolve_repo_root_from_hyperrag_env(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        hyperrag = repo / "external" / "HyperRAG"
        with mock.patch.dict(
            os.environ,
            {
                "VIKNOW3_ROOT": "/opt/viknow3",
                "VIKNOW_HYPERRAG_REPO_ROOT": str(hyperrag),
            },
            clear=False,
        ):
            self.assertEqual(resolve_viknow3_repo_root(), repo)


if __name__ == "__main__":
    unittest.main()
