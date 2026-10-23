from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from hyperrag.config_loader import HyperRAGConfig, load_config_from_yaml
from hyperrag.hyperrag_v2 import HyperRAGV2

logger = logging.getLogger(__name__)

DEFAULT_APPLICATION_CONFIG = Path(__file__).resolve().parents[3] / "config" / "local" / "app.yaml"


def load_application_config(application_config_path: str | None) -> dict[str, Any]:
    path = Path(application_config_path or DEFAULT_APPLICATION_CONFIG)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        payload = yaml.safe_load(file) or {}
    if not isinstance(payload, dict):
        return {}
    return payload


def apply_hyperrag_env(application_config: dict[str, Any]) -> None:
    hyperrag_env = ((application_config.get("hyperrag") or {}).get("env")) or {}
    if not isinstance(hyperrag_env, dict):
        return
    for key, value in hyperrag_env.items():
        os.environ[str(key)] = str(value)


def build_experiment_config(
    *,
    config_path: str,
    application_config_path: str | None,
    workspace: str | None,
) -> HyperRAGConfig:
    if config_path and Path(config_path).exists():
        config = load_config_from_yaml(config_path)
    else:
        app_config = load_application_config(application_config_path)
        apply_hyperrag_env(app_config)
        if config_path:
            logger.warning(
                'Config path not found: %s. Falling back to application config.',
                config_path,
            )
        fallback = (
            app_config.get("hyperrag") if isinstance(app_config.get("hyperrag"), dict) else {}
        )
        config = HyperRAGConfig(
            working_dir=str(fallback.get("working_dir") or "./hyperrag-storage"),
            workspace=str(workspace or fallback.get("workspace") or "default"),
            llm_model=str(fallback.get("llm_model") or "gpt-4o-mini"),
            llm_base_url=str(fallback.get("llm_base_url") or "https://api.openai.com/v1"),
            llm_api_key=str(fallback.get("llm_api_key") or os.environ.get("OPENAI_API_KEY") or ""),
            embedding_model=str(fallback.get("embedding_model") or "text-embedding-3-small"),
            embedding_base_url=str(
                fallback.get("embedding_base_url") or "https://api.openai.com/v1"
            ),
            embedding_api_key=str(
                fallback.get("embedding_api_key") or os.environ.get("OPENAI_API_KEY") or ""
            ),
            embedding_dim=int(fallback.get("embedding_dim") or 1024),
        )

    if workspace:
        config.workspace = workspace
    return config


class ExperimentRuntime:
    def __init__(self, config: HyperRAGConfig):
        self.config = config
        self.hyperrag = HyperRAGV2(config=config)

    async def initialize(self, *, ensure_rw: bool = True) -> None:
        await self.hyperrag.initialize()
        if not ensure_rw:
            return
        if self.hyperrag.current_mode == "read_write":
            return
        mode_ok = await self.hyperrag.aswitch_mode("read_write")
        if not mode_ok or self.hyperrag.current_mode != "read_write":
            raise RuntimeError("failed to switch HyperRAG instance to read_write mode")

    async def shutdown(self) -> None:
        await self.hyperrag.shutdown()

    async def wait_job_terminal(
        self,
        job_id: str,
        *,
        timeout_s: int = 1800,
        poll_interval_s: int = 2,
    ) -> dict[str, Any]:
        elapsed = 0
        while elapsed < timeout_s:
            status = await self.hyperrag.aget_job_status(job_id)
            if status is None:
                await asyncio.sleep(poll_interval_s)
                elapsed += poll_interval_s
                continue

            state = str(status.get("status") or "").lower()
            if state in {"completed", "failed", "cancelled"}:
                return status

            await asyncio.sleep(poll_interval_s)
            elapsed += poll_interval_s

        raise TimeoutError(f"job timeout: job_id={job_id}, timeout_s={timeout_s}")

    async def wait_index_task_running(
        self,
        job_id: str,
        *,
        timeout_s: int = 900,
        poll_interval_s: int = 2,
    ) -> dict[str, Any]:
        elapsed = 0
        while elapsed < timeout_s:
            job_status = await self.hyperrag.aget_job_status(job_id)
            if job_status:
                for task_id in job_status.get("task_ids", []) or []:
                    task = await self.hyperrag.aget_task_status(task_id)
                    if not task:
                        continue
                    task_type = str(task.get("task_type") or "")
                    task_state = str(task.get("status") or "").lower()
                    if task_type == "kg_index" and task_state == "running":
                        return task
                    if task_type == "kg_index" and task_state in {
                        "failed",
                        "completed",
                    }:
                        message = (
                            "index task ended before running: "
                            f"task_id={task_id} status={task_state}"
                        )
                        raise RuntimeError(message)
            await asyncio.sleep(poll_interval_s)
            elapsed += poll_interval_s
        raise TimeoutError(f"wait index running timeout: job_id={job_id}, timeout_s={timeout_s}")

    def make_asset_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:10]}"

    @staticmethod
    def now() -> float:
        return time.perf_counter()
