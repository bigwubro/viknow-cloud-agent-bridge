"""Playground injected config: browser hits storage/API directly."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ui.playground_config import browser_storage_url, playground_config_js


class PlaygroundConfigTests(unittest.TestCase):
    def test_browser_url_prefers_explicit_public(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VIKNOW_STORAGE_BROWSER_URL": "http://36.103.198.236:8090",
                "VIKNOW_STORAGE_URL": "http://127.0.0.1:8090",
            },
            clear=False,
        ):
            self.assertEqual(browser_storage_url(), "http://36.103.198.236:8090")

    def test_does_not_expose_loopback_storage_to_browser(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VIKNOW_STORAGE_BROWSER_URL": "",
                "VIKNOW_STORAGE_PUBLIC_URL": "",
                "VIKNOW_STORAGE_PUBLIC_HOST": "",
                "VIKNOW_STORAGE_URL": "http://127.0.0.1:8090",
            },
            clear=False,
        ):
            self.assertEqual(browser_storage_url(), "")

    def test_playground_config_js_uses_browser_storage(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VIKNOW_PLAYGROUND_API_URL": "http://36.103.198.236:5218",
                "VIKNOW_STORAGE_BROWSER_URL": "http://36.103.198.236:8090",
                "VIKNOW_STORAGE_TOKEN": "tok",
            },
            clear=False,
        ):
            js = playground_config_js()
        self.assertIn('window.VIKNOW_API_URL = "http://36.103.198.236:5218"', js)
        self.assertIn('window.VIKNOW_STORAGE_URL = "http://36.103.198.236:8090"', js)
        self.assertNotIn("5198/viknow-storage", js)


if __name__ == "__main__":
    unittest.main()
