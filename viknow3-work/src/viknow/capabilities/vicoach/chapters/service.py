"""Learning owns its lifecycle independently of indexing and QA."""

import asyncio

from .frozen import FrozenChapterSource
from .jobs import ChapterJobs
from .source import IndexedChapterSource


class ChapterService:
    def __init__(self, config_provider, adapter_provider):
        self.config_provider, self.adapter_provider = config_provider, adapter_provider
        self.jobs = None
        self.identity = None
        self.lock = asyncio.Lock()
        self.closing = False

    def settings(self):
        app = self.config_provider()
        config = app.capabilities.vicoach.chapters
        if config is None:
            raise RuntimeError("ViCoach learning chapters are not enabled")
        try:
            resolved = config.materialize()
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return app.app.state_dir / "vicoach" / "chapters", resolved

    async def current_jobs(self, *, resolve_source=True):
        async with self.lock:
            if self.closing:
                raise RuntimeError("learning service is closing")
            root, config = self.settings()
            identity = (str(root), config.model_dump_json())
            if identity != self.identity:
                if self.jobs is not None:
                    await self.jobs.close()
                self.jobs = ChapterJobs(root, config, None)
                self.identity = identity
            if resolve_source and self.jobs.source is None:
                self.jobs.source = (
                    FrozenChapterSource(config.source_manifest)
                    if config.source_manifest is not None
                    else IndexedChapterSource(self.adapter_provider())
                )
            return self.jobs

    def read(self, library_id, asset_id):
        root, config = self.settings()
        return ChapterJobs(root, config, None).view(library_id, asset_id)

    async def generate(self, library_id, asset_id, *, publish_course=True):
        return await (await self.current_jobs()).enqueue(
            library_id, asset_id, retry=True, publish_course=publish_course
        )

    async def generate_snapshot(self, library_id, asset_id, snapshot, *, publish_course=True):
        from .source import ChapterSnapshot

        snapshot = ChapterSnapshot.model_validate(snapshot)
        jobs = await self.current_jobs(resolve_source=False)
        return await jobs.enqueue(
            library_id, asset_id, retry=True, snapshot=snapshot, publish_course=publish_course
        )

    async def recover(self):
        if self.config_provider().capabilities.vicoach.chapters is not None:
            root, config = self.settings()
            # Recovery needs only the original job snapshot. It must not initialize
            # a knowledge adapter or require a live source manifest during QA startup.
            self.jobs = ChapterJobs(root, config, None)
            self.identity = (str(root), config.model_dump_json())
            await self.jobs.recover()

    async def close(self):
        self.closing = True
        async with self.lock:
            if self.jobs is not None:
                await self.jobs.close()
