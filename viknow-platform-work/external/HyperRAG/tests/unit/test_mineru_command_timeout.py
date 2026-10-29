"""MinerU subprocess must time out instead of hanging the API process."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from hyperrag.parser import MinerUParser, mineru_timeout_seconds


def test_mineru_timeout_seconds_rejects_non_positive(monkeypatch):
    monkeypatch.setenv("HYPERRAG_MINERU_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="must be > 0"):
        mineru_timeout_seconds()


def test_mineru_timeout_seconds_reads_env(monkeypatch):
    monkeypatch.setenv("HYPERRAG_MINERU_TIMEOUT_SECONDS", "12")
    assert mineru_timeout_seconds() == 12.0


def test_run_mineru_command_kills_process_on_timeout():
    proc = MagicMock()
    proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["mineru"], timeout=1)
    proc.poll.return_value = None
    proc.pid = 4242
    proc.returncode = None

    with (
        patch("hyperrag.parser.subprocess.Popen", return_value=proc) as popen,
        patch("hyperrag.parser._kill_mineru_process") as kill,
        patch("hyperrag.parser.platform.system", return_value="Linux"),
    ):
        with pytest.raises(TimeoutError, match="exceeded 1s"):
            MinerUParser._run_mineru_command(
                input_path="/tmp/doc.pdf",
                output_dir="/tmp/out",
                timeout=1,
            )

    popen.assert_called_once()
    assert popen.call_args.kwargs.get("start_new_session") is True
    kill.assert_called_once_with(proc)
