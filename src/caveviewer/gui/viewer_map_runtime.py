"""Render-thread ownership for one loaded viewer map.

The native viewer window publishes one instance at a time. The runtime owns
map-scoped streaming, texture, minimap, upload, culling, and validation state
and releases it in a deterministic order on the render thread. It does not
import or call back through the window adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from caveviewer.gui import view_culling


@dataclass
class ViewerMapRuntime:
    """Own the mutable resources associated with one viewer map."""

    cache_dir: str | None = None
    textures_dir: str | None = None
    map_root: str | None = None
    manifest: Any = None
    world: Any = None
    camera: Any = None
    minimap: Any = None
    texture_manager: Any = None
    chunk_upload_manager: Any = None
    chunk_gpu_objects: dict[tuple, list] = field(default_factory=dict)
    chunk_upload_states: dict[tuple, dict] = field(default_factory=dict)
    chunk_normal_cache: dict[tuple, list] = field(default_factory=dict)
    chunk_aabbs: dict[tuple, tuple] = field(default_factory=dict)
    view_culling_cache: view_culling.FrustumCullingCache = field(
        default_factory=view_culling.FrustumCullingCache
    )
    chunk_visibility_generation: int = 0
    texture_validation_executor: ThreadPoolExecutor | None = None
    texture_validation_future: Future | None = None
    texture_validation_manager: Any = None
    texture_validation_cache_dir: str | None = None
    texture_validation_started_at: float | None = None
    loaded: bool = False

    @classmethod
    def for_map(
        cls,
        *,
        cache_dir: str,
        textures_dir: str,
        map_root: str | None,
        manifest: Any,
    ) -> "ViewerMapRuntime":
        """Create an unpublished runtime for a map being initialized."""
        return cls(
            cache_dir=cache_dir,
            textures_dir=textures_dir,
            map_root=map_root,
            manifest=manifest,
        )

    def cancel_texture_validation(self) -> None:
        """Detach and stop the map's CPU-side texture-validation task."""
        future = self.texture_validation_future
        executor = self.texture_validation_executor
        self.texture_validation_executor = None
        self.texture_validation_future = None
        self.texture_validation_manager = None
        self.texture_validation_cache_dir = None
        self.texture_validation_started_at = None
        if future is not None:
            future.cancel()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def start_texture_validation(
        self,
        *,
        executor_factory: Callable[..., ThreadPoolExecutor],
        perf_counter: Callable[[], float],
        logger: Any,
    ) -> bool:
        """Start nonblocking texture validation for this map."""
        texture_manager = self.texture_manager
        if texture_manager is None:
            return False

        self.cancel_texture_validation()
        executor: ThreadPoolExecutor | None = None
        try:
            executor = executor_factory(
                max_workers=1,
                thread_name_prefix="caveviewer-texture-validate",
            )
            future = executor.submit(texture_manager.validate_textures)
        except Exception as error:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            logger.warning(
                "Could not start background texture validation: %s", error
            )
            return False

        self.texture_validation_executor = executor
        self.texture_validation_future = future
        self.texture_validation_manager = texture_manager
        self.texture_validation_cache_dir = self.cache_dir
        self.texture_validation_started_at = perf_counter()
        return True

    def finish_texture_validation(
        self,
        *,
        perf_counter: Callable[[], float],
        logger: Any,
    ) -> bool:
        """Publish a completed validation result when it still belongs here."""
        future = self.texture_validation_future
        if future is None or not future.done():
            return False

        executor = self.texture_validation_executor
        texture_manager = self.texture_validation_manager
        cache_dir = self.texture_validation_cache_dir
        started_at = self.texture_validation_started_at
        self._clear_texture_validation_state()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

        if texture_manager is not self.texture_manager or cache_dir != self.cache_dir:
            return True

        elapsed_s = (
            max(0.0, perf_counter() - started_at)
            if started_at is not None
            else None
        )
        try:
            result = future.result()
        except Exception as error:
            logger.warning("Background texture validation failed: %s", error)
            return True

        found = len(result.get("found", ())) if isinstance(result, dict) else None
        missing = len(result.get("missing", ())) if isinstance(result, dict) else None
        if elapsed_s is None:
            logger.info(
                "Background texture validation completed "
                "(found=%s missing=%s).",
                found,
                missing,
            )
        else:
            logger.info(
                "Background texture validation completed in %.2fs "
                "(found=%s missing=%s).",
                elapsed_s,
                found,
                missing,
            )
        return True

    def _clear_texture_validation_state(self) -> None:
        self.texture_validation_executor = None
        self.texture_validation_future = None
        self.texture_validation_manager = None
        self.texture_validation_cache_dir = None
        self.texture_validation_started_at = None

    def release(self, *, streaming_shutdown_timeout: float, logger: Any) -> None:
        """Release all map resources and raise the first cleanup error."""
        errors: list[Exception] = []
        try:
            self.cancel_texture_validation()
        except Exception as error:
            errors.append(error)

        if self.world is not None:
            try:
                self.world.shutdown(timeout=streaming_shutdown_timeout)
            except Exception as error:
                errors.append(error)

        if self.chunk_upload_manager is not None:
            try:
                self.chunk_upload_manager.unload_all()
            except Exception as error:
                errors.append(error)
        else:
            try:
                self._release_orphaned_uploads(logger)
            except Exception as error:
                errors.append(error)

        if self.texture_manager is not None:
            try:
                self.texture_manager.shutdown()
            except Exception as error:
                errors.append(error)

        if self.minimap is not None:
            try:
                self.minimap.release()
            except Exception as error:
                logger.warning("Could not release viewer minimap: %s", error)

        self._clear()
        if errors:
            raise errors[0]

    def _release_orphaned_uploads(self, logger: Any) -> None:
        """Best-effort cleanup for a failure before the upload owner exists."""
        released_pairs: set[tuple[int, int]] = set()
        vao_lists = list(self.chunk_gpu_objects.values())
        vao_lists.extend(
            state.get("vao_list", [])
            for state in self.chunk_upload_states.values()
            if isinstance(state, dict)
        )
        for vao_list in vao_lists:
            for vao, vbo, material_name, _texture in vao_list:
                resource_key = (id(vao), id(vbo))
                if resource_key in released_pairs:
                    continue
                released_pairs.add(resource_key)
                for resource in (vao, vbo):
                    try:
                        resource.release()
                    except Exception as error:
                        logger.warning(
                            "Could not release partial chunk resource: %s", error
                        )
                if self.texture_manager is not None:
                    try:
                        self.texture_manager.release(material_name)
                    except Exception as error:
                        logger.warning(
                            "Could not release partial chunk texture: %s", error
                        )

    def _clear(self) -> None:
        self.cache_dir = None
        self.textures_dir = None
        self.map_root = None
        self.manifest = None
        self.world = None
        self.camera = None
        self.minimap = None
        self.texture_manager = None
        self.chunk_upload_manager = None
        self.chunk_gpu_objects.clear()
        self.chunk_upload_states.clear()
        self.chunk_normal_cache.clear()
        self.chunk_aabbs.clear()
        self.view_culling_cache = view_culling.FrustumCullingCache()
        self.chunk_visibility_generation = 0
        self.loaded = False
