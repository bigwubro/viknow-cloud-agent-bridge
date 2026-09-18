"""Force NIXL UCX to allow intra-node sm AM so GPU READ can stay on cuda_ipc.

vLLM's NixlConnector only forwards num_threads into create_backend. The UCX
plugin default is ucx_error_handling_mode=peer, which rejects sm/posix/sysv
("no peer failure handler") and leaves tcp as the only AM transport. tcp then
carries VRAM via cuda_copy at ~330MB/s. Setting mode=none lets sm provide AM.
run-pd-1p1d.sh keeps tcp out of UCX_TLS. cuda_copy stays only so UCX can
detect VRAM; the device path should still be cuda_ipc.
"""
from __future__ import annotations

import logging

log = logging.getLogger("nixl_force_cuda_ipc")


def _emit(msg: str) -> None:
    print(f"[nixl_force_cuda_ipc] {msg}", flush=True)
    log.info(msg)


def _wrap_create_backend(api_mod) -> None:
    agent_cls = getattr(api_mod, "nixl_agent", None)
    if agent_cls is None or getattr(agent_cls, "_viknow_ucx_ipc_patched", False):
        return
    orig = agent_cls.create_backend

    def create_backend(self, backend: str, initParams=None):
        params = dict(initParams or {})
        if str(backend).upper() == "UCX":
            params.setdefault("ucx_error_handling_mode", "none")
            _emit(f"NIXL UCX create_backend init={params}")
        handle = orig(self, backend, params)
        try:
            _emit(
                f"NIXL backend {backend} options={self.get_backend_params(backend)} "
                f"mems={getattr(self, 'backend_mems', {}).get(backend)}"
            )
        except Exception as exc:  # noqa: BLE001
            _emit(f"NIXL backend {backend} param dump failed: {exc}")
        return handle

    agent_cls.create_backend = create_backend
    agent_cls._viknow_ucx_ipc_patched = True
    _emit(f"patched {api_mod.__name__}.nixl_agent.create_backend ucx_error_handling_mode=none")


for _mod_name in ("nixl_cu13._api", "nixl._api"):
    try:
        _wrap_create_backend(__import__(_mod_name, fromlist=["nixl_agent"]))
    except Exception as exc:  # noqa: BLE001
        _emit(f"could not patch {_mod_name}: {exc}")
