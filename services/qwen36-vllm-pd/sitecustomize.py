"""Force NIXL UCX to allow intra-node sm AM so GPU READ can stay on cuda_ipc.

vLLM's NixlConnector only forwards num_threads into create_backend. The UCX
plugin default is ucx_error_handling_mode=peer, which rejects sm/posix/sysv
("no peer failure handler") and leaves tcp as the only AM transport. tcp then
carries VRAM via cuda_copy at ~330MB/s. Setting mode=none lets sm provide AM.
run-pd-1p1d.sh keeps tcp out of UCX_TLS. cuda_copy stays only so UCX can
detect VRAM; the device path should still be cuda_ipc.

Also: vLLM's NIXL handshake ROUTER thread unpacks recv_multipart() as
(identity, empty, msg). A 2-frame junk message kills the thread and unbinds
the side channel. P1 hit that on :5601; D then failed every pull from that P.

Mooncake Store on 0.29.0 is missing three upstream commits. This file
backports them: #50388 (hybrid invalid-block unpack), #54643 (skip
MultiConnector-rejected loads), #54870 (missing save table is skip, not
assert). #54853 is larger (lazy block-state API) and is not copied.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import logging
import sys

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


_NIXL_SCHEDULER = "vllm.distributed.kv_transfer.kv_connector.v1.nixl.base_scheduler"
_VLLM_SCHEDULER = "vllm.v1.core.sched.scheduler"
_MOONCAKE_STORE_SCHEDULER = (
    "vllm.distributed.kv_transfer.kv_connector.v1.mooncake.store.scheduler"
)


def _patch_handshake_listener(mod) -> None:
    cls = getattr(mod, "NixlBaseConnectorScheduler", None)
    if cls is None or getattr(cls, "_viknow_hs_guard", False):
        return
    orig = cls._nixl_handshake_listener

    def _guarded(encoded_data, ready_event, stop_event, host, port):
        while not stop_event.is_set():
            try:
                orig(encoded_data, ready_event, stop_event, host, port)
                return
            except ValueError as exc:
                _emit(
                    f"NIXL handshake listener unpack error on {host}:{port}: {exc}; rebound"
                )

    cls._nixl_handshake_listener = staticmethod(_guarded)
    cls._viknow_hs_guard = True
    _emit(f"patched {cls.__name__}._nixl_handshake_listener to survive 2-frame ZMQ")


def _patch_hybrid_invalid_blocks(mod) -> None:
    """vLLM #50388 (merged 2026-09-13): hybrid get_block_ids must not unpack as 1."""
    cls = getattr(mod, "Scheduler", None)
    if cls is None or getattr(cls, "_viknow_hybrid_invalid", False):
        return
    orig = cls._update_requests_with_invalid_blocks

    def _wrapped(self, requests, invalid_block_ids, num_scheduled_tokens, evict_blocks):
        reqs = list(requests)
        if not reqs:
            return orig(self, reqs, invalid_block_ids, num_scheduled_tokens, evict_blocks)
        first_groups = self.kv_cache_manager.get_block_ids(reqs[0].request_id)
        if len(first_groups) <= 1:
            return orig(self, reqs, invalid_block_ids, num_scheduled_tokens, evict_blocks)

        affected_req_ids: set[str] = set()
        total_affected_tokens = 0
        blocks_to_evict: set[int] = set()
        marked_invalid_block_ids: set[int] = set()
        null_block_id = self.kv_cache_manager.block_pool.null_block.block_id
        for request in reqs:
            req_id = request.request_id
            req_block_ids_per_group = self.kv_cache_manager.get_block_ids(req_id)
            req_num_computed_tokens = request.num_computed_tokens - num_scheduled_tokens.get(
                req_id, 0
            )
            if len(req_block_ids_per_group) <= 1:
                (req_block_ids,) = req_block_ids_per_group
                is_affected = False
                marked_invalid_block = False
                req_num_computed_blocks = (
                    req_num_computed_tokens + self.block_size - 1
                ) // self.block_size
                for idx, block_id in zip(range(req_num_computed_blocks), req_block_ids):
                    if block_id not in invalid_block_ids:
                        continue
                    is_affected = True
                    if block_id in marked_invalid_block_ids:
                        continue
                    marked_invalid_block_ids.add(block_id)
                    if marked_invalid_block:
                        continue
                    marked_invalid_block = True
                    request.num_computed_tokens = idx * self.block_size
                    total_affected_tokens += (
                        req_num_computed_tokens - request.num_computed_tokens
                    )
                    if evict_blocks:
                        blocks_to_evict.update(req_block_ids[idx:])
                if is_affected:
                    if not marked_invalid_block:
                        total_affected_tokens += (
                            request.num_computed_tokens - req_num_computed_tokens
                        )
                        request.num_computed_tokens = req_num_computed_tokens
                    affected_req_ids.add(req_id)
                continue
            request_invalid_block_ids = {
                block_id
                for group_ids in req_block_ids_per_group
                for block_id in group_ids
                if block_id != null_block_id and block_id in invalid_block_ids
            }
            if request_invalid_block_ids:
                marked_invalid_block_ids |= request_invalid_block_ids
                total_affected_tokens += req_num_computed_tokens
                request.num_computed_tokens = 0
                if evict_blocks:
                    for group_ids in req_block_ids_per_group:
                        blocks_to_evict.update(
                            block_id
                            for block_id in group_ids
                            if block_id != null_block_id
                        )
                affected_req_ids.add(req_id)
        return affected_req_ids, total_affected_tokens, blocks_to_evict

    cls._update_requests_with_invalid_blocks = _wrapped
    cls._viknow_hybrid_invalid = True
    _emit("patched Scheduler._update_requests_with_invalid_blocks from vLLM #50388")


def _patch_mooncake_store_scheduler(mod) -> None:
    """vLLM #54643 + #54870: do not crash EngineCore on missing save tables."""
    cls = getattr(mod, "MooncakeStoreScheduler", None)
    if cls is None or getattr(cls, "_viknow_mooncake_store", False):
        return

    def _apply_current_save_block_ids(self, meta, scheduler_output):
        save_metas = [req_meta for req_meta in meta.requests if req_meta.can_save]
        if not save_metas:
            return
        block_state = getattr(scheduler_output, "kv_connector_block_state", None)
        if block_state is None:
            for req_meta in save_metas:
                req_meta.can_save = False
            _emit("Mooncake save skipped: no kv_connector_block_state")
            return
        getter = getattr(block_state, "get_block_ids", None)
        mapping = getattr(block_state, "block_ids", None)
        for req_meta in save_metas:
            if getter is not None:
                block_ids = getter(req_meta.req_id)
            elif mapping is not None:
                block_ids = mapping.get(req_meta.req_id)
            else:
                block_ids = None
            if block_ids is None:
                _emit(
                    f"Mooncake save skipped for {req_meta.req_id}: "
                    "no current block table (#54870)"
                )
                req_meta.can_save = False
                continue
            req_meta.block_ids = block_ids

    orig_build = cls.build_connector_meta

    def build_connector_meta(self, scheduler_output):
        for req_id, spec in list(getattr(self, "load_specs", {}).items()):
            if spec is not None and not getattr(spec, "can_load", True):
                self.load_specs.pop(req_id, None)
        return orig_build(self, scheduler_output)

    cls._apply_current_save_block_ids = _apply_current_save_block_ids
    cls.build_connector_meta = build_connector_meta
    cls._viknow_mooncake_store = True
    _emit("patched MooncakeStoreScheduler from vLLM #54643/#54870")


class _ViknowPatchFinder(importlib.abc.MetaPathFinder):
    _PATCHERS = {
        _NIXL_SCHEDULER: (_patch_handshake_listener,),
        _VLLM_SCHEDULER: (_patch_hybrid_invalid_blocks,),
        _MOONCAKE_STORE_SCHEDULER: (_patch_mooncake_store_scheduler,),
    }

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        patchers = self._PATCHERS.get(fullname)
        if patchers is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        orig_exec = spec.loader.exec_module

        def exec_module(module):
            orig_exec(module)
            for patch in patchers:
                patch(module)

        spec.loader.exec_module = exec_module  # type: ignore[method-assign]
        return spec


if not any(isinstance(x, _ViknowPatchFinder) for x in sys.meta_path):
    sys.meta_path.insert(0, _ViknowPatchFinder())
