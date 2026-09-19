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
            params.setdefault("num_workers", "4")
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


def _patch_lmcache_kv_layout() -> None:
    """vLLM 0.29 dropped get_kv_cache_layout(); Nixl reports LBHNC.

    LMCache 0.5.4 still asks for NHD/HND. LBHNC is the HND alias. Default HND
    so GDN unified-block edits can register under MultiConnector.
    """
    try:
        from lmcache.integration.vllm import utils as lmc_utils
    except Exception as exc:  # noqa: BLE001
        _emit(f"lmcache layout patch skip: {exc}")
        return
    if getattr(lmc_utils, "_viknow_layout_patched", False):
        return

    alias = {"LBHNC": "HND", "HND": "HND", "LBNHC": "NHD", "NHD": "NHD", "BLHNC": "HND"}

    def try_get_vllm_kv_cache_layout():
        raw = None
        try:
            from vllm.v1.attention.backends.utils import get_kv_connector_cache_layout

            raw = get_kv_connector_cache_layout()
        except Exception:
            raw = None
        if raw is not None and hasattr(raw, "name"):
            raw = raw.name
        mapped = alias.get(str(raw) if raw is not None else "")
        if mapped:
            _emit(f"lmcache kv_layout raw={raw} -> {mapped}")
            return mapped
        _emit(f"lmcache kv_layout raw={raw} default HND")
        return "HND"

    lmc_utils.try_get_vllm_kv_cache_layout = try_get_vllm_kv_cache_layout
    lmc_utils._viknow_layout_patched = True
    _emit("patched lmcache try_get_vllm_kv_cache_layout for v0.29 LBHNC")


_patch_lmcache_kv_layout()
