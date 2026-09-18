"""Force NIXL UCX to allow intra-node sm AM so GPU READ can stay on cuda_ipc.

vLLM's NixlConnector only forwards num_threads into create_backend. The UCX
plugin default is ucx_error_handling_mode=peer, which rejects sm/posix/sysv
("no peer failure handler") and leaves tcp as the only AM transport. tcp then
carries VRAM via cuda_copy at ~330MB/s. Setting mode=none lets sm provide AM;
run-pd-1p1d.sh keeps tcp and cuda_copy out of UCX_TLS so the data plane cannot
fall back to host.
"""
from __future__ import annotations

import logging

log = logging.getLogger("nixl_force_cuda_ipc")


def _wrap_create_backend(api_mod) -> None:
    agent_cls = getattr(api_mod, "nixl_agent", None)
    if agent_cls is None or getattr(agent_cls, "_viknow_ucx_ipc_patched", False):
        return
    orig = agent_cls.create_backend

    def create_backend(self, backend: str, initParams=None):
        params = dict(initParams or {})
        if str(backend).upper() == "UCX":
            params.setdefault("ucx_error_handling_mode", "none")
            log.info("NIXL UCX create_backend init=%s", params)
        handle = orig(self, backend, params)
        try:
            log.info(
                "NIXL backend %s options=%s mems=%s",
                backend,
                self.get_backend_params(backend),
                getattr(self, "backend_mems", {}).get(backend),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("NIXL backend %s param dump failed: %s", backend, exc)
        return handle

    agent_cls.create_backend = create_backend
    agent_cls._viknow_ucx_ipc_patched = True
    log.info("patched %s.nixl_agent.create_backend ucx_error_handling_mode=none", api_mod.__name__)


for _mod_name in ("nixl_cu13._api", "nixl._api"):
    try:
        _wrap_create_backend(__import__(_mod_name, fromlist=["nixl_agent"]))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not patch %s: %s", _mod_name, exc)
