"""One-process worker for a single isolated SkillOpt candidate rollout."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from starlette.testclient import TestClient

from viknow.api.eval_only import create_eval_only_app
from viknow.core.eval.skillopt_eval_service import build_skillopt_experiment_config
from viknow.core.eval.skillopt_experiment import load_skillopt_experiment_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()

    os.environ["VIKNOW_CONFIG_RELOAD"] = "0"
    manifest = load_skillopt_experiment_manifest(args.manifest)
    config = build_skillopt_experiment_config(
        manifest,
        expected_target_skill_sha256=args.candidate_sha256,
    )
    request = json.loads(args.request.read_text(encoding="utf-8"))
    with TestClient(create_eval_only_app(config)) as client:
        response = client.post("/api/v1/eval/runs", json=request)
        if response.status_code != 200:
            raise RuntimeError(
                f"isolated SkillOpt eval failed with HTTP {response.status_code}: "
                f"{response.text[:1000]}"
            )
        temporary_response = args.response.with_suffix(f"{args.response.suffix}.tmp")
        temporary_response.write_text(
            json.dumps(response.json(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_response, args.response)


if __name__ == "__main__":
    main()
