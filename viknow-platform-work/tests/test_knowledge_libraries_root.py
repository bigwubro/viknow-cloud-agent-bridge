from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from viknow_platform.application.hyperrag.ops_bootstrap import _knowledge_libraries_root


class KnowledgeLibrariesRootTests(unittest.TestCase):
    def test_libraries_root_override(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "VIKNOW_LIBRARIES_ROOT": "/tmp/custom-lib-root",
                "VIKNOW_DATA_DIR": "/data/viknow",
            },
            clear=False,
        ):
            self.assertEqual(_knowledge_libraries_root(), Path("/tmp/custom-lib-root").resolve())

    def test_default_under_data_dir(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"VIKNOW_LIBRARIES_ROOT": "", "VIKNOW_DATA_DIR": "/data/viknow"},
            clear=False,
        ):
            self.assertEqual(
                _knowledge_libraries_root(),
                Path("/data/viknow/hyperrag-wd").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
