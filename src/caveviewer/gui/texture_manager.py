"""Render-thread OpenGL texture lifecycle management.

The worker-safe Pillow decode/cache policy lives in
``caveviewer.core.textures.decoding``. This module owns only the render-side half:
reference counts, resident texture LRU state, OpenGL texture creation, mipmap
generation, and GPU texture release. Create and use :class:`TextureManager` on
the thread that owns the active OpenGL context.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.textures.decoding import (
    DecodedImage,
    TEXTURE_BYTES_PER_PIXEL_WITH_MIPS,
    TEXTURE_MAX_SIZE_ENV_VAR,
    TextureDecodeCache,
    estimate_gpu_texture_bytes,
    normalize_decoded_cache_limit,
    normalize_resident_texture_cache_limit,
    recommend_decoded_cache_bytes,
    recommend_max_texture_dimension,
    recommend_resident_texture_cache_bytes,
)


_LOG = get_logger("TextureManager")


@dataclass
class LoadedTexture:
    moderngl_texture: object
    ref_count: int = 0


@dataclass
class TextureAcquireTask:
    """Render-thread state for one resumable texture acquire/upload."""

    material_name: str
    file_or_bytes: object
    timing: dict
    started_at: float
    decoded: DecodedImage | None = None
    texture: object | None = None
    next_row: int = 0
    complete: bool = False
    result_texture: object | None = None
    _reported_decode_state: bool = False


class TextureManager:
    """OpenGL texture manager owned by one render/context thread.

    Worker-safe method:
      - ``decode_for_material()``, which delegates to the CPU-only decoder.

    Render-thread-only methods:
      - ``acquire()``, ``acquire_with_timing()``, ``release()``, ``shutdown()``,
        and helpers that create or release OpenGL texture objects.
    """

    recommend_max_texture_dimension = staticmethod(recommend_max_texture_dimension)
    recommend_resident_texture_cache_bytes = staticmethod(
        recommend_resident_texture_cache_bytes
    )
    recommend_decoded_cache_bytes = staticmethod(recommend_decoded_cache_bytes)
    _normalize_decoded_cache_limit = staticmethod(normalize_decoded_cache_limit)
    _normalize_resident_texture_limit = staticmethod(
        normalize_resident_texture_cache_limit
    )

    def __init__(
        self,
        gl_context,
        textures_dir: str,
        material_to_file: dict,
        max_texture_dimension: int | None = None,
        max_decoded_cache_bytes: int | None = None,
        max_resident_texture_bytes: int | None = None,
        *,
        render_thread_id: int | None = None,
    ):
        """
        ``gl_context`` is a moderngl context owned by the creating render
        thread. All OpenGL resource creation/deletion methods assert that same
        thread before touching the context or texture objects.
        """
        self.ctx = gl_context
        self._render_thread_id = (
            threading.get_ident() if render_thread_id is None else render_thread_id
        )
        self.decoder = TextureDecodeCache(
            textures_dir,
            material_to_file,
            max_texture_dimension=max_texture_dimension,
            max_decoded_cache_bytes=max_decoded_cache_bytes,
            max_resident_texture_bytes=max_resident_texture_bytes,
        )
        self.textures_dir = self.decoder.textures_dir
        self.material_to_file = self.decoder.material_to_file
        self.max_texture_dimension = self.decoder.max_texture_dimension
        self.max_decoded_cache_bytes = self.decoder.max_decoded_cache_bytes
        self.max_resident_texture_bytes = self.decoder.max_resident_texture_bytes

        self._state_lock = threading.RLock()
        self._shutdown = False
        self._loaded: dict[str, LoadedTexture] = {}
        # Multiple materials can point at the same physical jpg, or the same
        # embedded bytes. The render cache deduplicates one GPU texture per
        # texture source and keeps idle entries until the resident budget needs
        # room.
        self._file_cache: dict[object, object] = {}
        self._file_cache_bytes: dict[object, int] = {}
        self._file_cache_total_bytes = 0
        self._idle_file_lru: OrderedDict[object, None] = OrderedDict()

        _LOG.info(
            "Texture resident GPU LRU cache cap active: %.1f MB. Released "
            "textures remain resident while idle until this budget needs room.",
            self.max_resident_texture_bytes / (1024 ** 2),
        )

    def _assert_render_thread(self, operation: str) -> None:
        if threading.get_ident() != self._render_thread_id:
            raise RuntimeError(
                f"TextureManager.{operation} must run on the render thread "
                "that created this manager and owns the OpenGL context."
            )

    @staticmethod
    def _new_acquire_timing(material_name: str) -> dict:
        return {
            "material": material_name,
            "total_ms": 0.0,
            "material_cache_hit": False,
            "file_cache_hit": False,
            "decoded_cache_hit": False,
            "sync_decode": False,
            "placeholder": False,
            "decode_ms": 0.0,
            "texture_ms": 0.0,
            "texture_alloc_ms": 0.0,
            "texture_write_ms": 0.0,
            "mipmap_ms": 0.0,
            "image_bytes": 0,
            "image_size": None,
            "texture_evictions": 0,
            "texture_evicted_bytes": 0,
        }

    @staticmethod
    def _new_acquire_step_timing(material_name: str) -> dict:
        timing = TextureManager._new_acquire_timing(material_name)
        timing["image_total_bytes"] = 0
        return timing

    # -- worker-safe CPU decode facade --------------------------------------

    def decode_for_material(self, material_name: str) -> None:
        """Worker-thread-safe texture predecode; performs no OpenGL work."""
        self.decoder.decode_for_material(material_name)

    def validate_textures(self) -> dict:
        """Validate/inspect texture sources without touching OpenGL."""
        return self.decoder.validate_textures()

    # Compatibility wrappers for focused tests and synchronous fallback.
    def _decode_image(self, file_or_bytes) -> DecodedImage | None:
        return self.decoder.decode_source(file_or_bytes)

    def _decode_from_disk(self, filename: str) -> DecodedImage | None:
        return self.decoder._decode_from_disk(filename)

    def _decode_from_bytes(self, raw_bytes: bytes) -> DecodedImage | None:
        return self.decoder._decode_from_bytes(raw_bytes)

    def _apply_texture_dimension_limit(self, image, source_label: str):
        return self.decoder._apply_texture_dimension_limit(image, source_label)

    def _estimate_decoded_image_bytes(self, file_or_bytes) -> int | None:
        return self.decoder._estimate_decoded_image_bytes(file_or_bytes)

    def _estimate_resident_bytes_for_file(self, file_or_bytes) -> int | None:
        return self.decoder.estimate_resident_bytes_for_source(file_or_bytes)

    # -- render-thread-only GPU upload step ---------------------------------

    def _placeholder_texture(self):
        """Create a 1x1 magenta texture for missing/invalid texture sources."""
        self._assert_render_thread("_placeholder_texture")
        return self.ctx.texture((1, 1), 4, bytes((255, 0, 255, 255)))

    def acquire(self, material_name: str) -> object:
        tex, _timing = self.acquire_with_timing(material_name)
        return tex

    def acquire_with_timing(self, material_name: str) -> tuple[object, dict]:
        """
        Increment refcount for one material texture, uploading on first use.

        This method may synchronously decode if worker predecode did not
        complete in time, but all OpenGL texture creation stays on the render
        thread.
        """
        self._assert_render_thread("acquire_with_timing")
        timing = self._new_acquire_timing(material_name)
        start = time.perf_counter()
        file_or_bytes = self.material_to_file.get(material_name)
        with self._state_lock:
            if self._shutdown:
                raise RuntimeError("TextureManager.acquire called after shutdown.")
            if material_name in self._loaded:
                entry = self._loaded[material_name]
                entry.ref_count += 1
                timing["material_cache_hit"] = True
                timing["total_ms"] = (time.perf_counter() - start) * 1000.0
                return entry.moderngl_texture, timing

            if file_or_bytes and file_or_bytes in self._file_cache:
                tex = self._file_cache[file_or_bytes]
                self._idle_file_lru.pop(file_or_bytes, None)
                self._loaded[material_name] = LoadedTexture(
                    moderngl_texture=tex,
                    ref_count=1,
                )
                timing["file_cache_hit"] = True
                timing["total_ms"] = (time.perf_counter() - start) * 1000.0
                return tex, timing

        if file_or_bytes:
            incoming_bytes = self._estimate_resident_bytes_for_file(file_or_bytes)
            textures_to_release: list[object] = []
            sources_to_mark_nonresident: list[object] = []
            with self._state_lock:
                if self._shutdown:
                    raise RuntimeError("TextureManager.acquire called after shutdown.")
                if material_name in self._loaded:
                    entry = self._loaded[material_name]
                    entry.ref_count += 1
                    timing["material_cache_hit"] = True
                    timing["total_ms"] = (time.perf_counter() - start) * 1000.0
                    return entry.moderngl_texture, timing
                if file_or_bytes in self._file_cache:
                    tex = self._file_cache[file_or_bytes]
                    self._idle_file_lru.pop(file_or_bytes, None)
                    self._loaded[material_name] = LoadedTexture(
                        moderngl_texture=tex,
                        ref_count=1,
                    )
                    timing["file_cache_hit"] = True
                    timing["total_ms"] = (time.perf_counter() - start) * 1000.0
                    return tex, timing
                (
                    textures_to_release,
                    sources_to_mark_nonresident,
                ) = self._evict_idle_texture_objects_locked(
                    incoming_bytes=incoming_bytes,
                    timing=timing,
                )
            for source in sources_to_mark_nonresident:
                self.decoder.mark_texture_nonresident(source)
            for texture_to_release in textures_to_release:
                self._release_texture_object(texture_to_release)

        tex = self._upload_for_material(
            material_name,
            file_or_bytes,
            timing=timing,
        )
        uploaded_texture_to_release = None
        textures_to_release = []
        sources_to_mark_nonresident = []
        with self._state_lock:
            if self._shutdown:
                uploaded_texture_to_release = tex
            elif file_or_bytes:
                self._file_cache.setdefault(file_or_bytes, tex)
                cached_tex = self._file_cache[file_or_bytes]
                if cached_tex is tex:
                    previous = self._file_cache_bytes.get(file_or_bytes, 0)
                    byte_count = max(
                        1,
                        int(
                            self._resident_bytes_from_upload_timing(
                                timing,
                                fallback_file_or_bytes=file_or_bytes,
                            )
                        ),
                    )
                    self._file_cache_bytes[file_or_bytes] = byte_count
                    self._file_cache_total_bytes += byte_count - previous
                    (
                        textures_to_release,
                        sources_to_mark_nonresident,
                    ) = self._evict_idle_texture_objects_locked(timing=timing)
                else:
                    uploaded_texture_to_release = tex
                    tex = cached_tex
                    self._idle_file_lru.pop(file_or_bytes, None)

            if not self._shutdown:
                self._loaded[material_name] = LoadedTexture(
                    moderngl_texture=tex,
                    ref_count=1,
                )
                timing["total_ms"] = (time.perf_counter() - start) * 1000.0
        if file_or_bytes and uploaded_texture_to_release is None:
            self.decoder.mark_texture_resident(file_or_bytes)
        for source in sources_to_mark_nonresident:
            self.decoder.mark_texture_nonresident(source)
        self._release_texture_object(uploaded_texture_to_release)
        for texture_to_release in textures_to_release:
            self._release_texture_object(texture_to_release)
        return tex, timing

    def begin_acquire_with_timing(self, material_name: str) -> TextureAcquireTask:
        """
        Start a render-thread texture acquire that may be resumed over frames.

        Cache hits, placeholders, and already-resident textures complete
        immediately. New real textures keep decoded CPU bytes in the returned
        task and upload the image rows through ``advance_acquire_with_timing``.
        """
        self._assert_render_thread("begin_acquire_with_timing")
        timing = self._new_acquire_timing(material_name)
        start = time.perf_counter()
        file_or_bytes = self.material_to_file.get(material_name)
        with self._state_lock:
            if self._shutdown:
                raise RuntimeError("TextureManager.acquire called after shutdown.")
            if material_name in self._loaded:
                entry = self._loaded[material_name]
                entry.ref_count += 1
                timing["material_cache_hit"] = True
                timing["total_ms"] = (time.perf_counter() - start) * 1000.0
                return TextureAcquireTask(
                    material_name=material_name,
                    file_or_bytes=file_or_bytes,
                    timing=timing,
                    started_at=start,
                    complete=True,
                    result_texture=entry.moderngl_texture,
                )

            if file_or_bytes and file_or_bytes in self._file_cache:
                tex = self._file_cache[file_or_bytes]
                self._idle_file_lru.pop(file_or_bytes, None)
                self._loaded[material_name] = LoadedTexture(
                    moderngl_texture=tex,
                    ref_count=1,
                )
                timing["file_cache_hit"] = True
                timing["total_ms"] = (time.perf_counter() - start) * 1000.0
                return TextureAcquireTask(
                    material_name=material_name,
                    file_or_bytes=file_or_bytes,
                    timing=timing,
                    started_at=start,
                    complete=True,
                    result_texture=tex,
                )

        if file_or_bytes:
            incoming_bytes = self._estimate_resident_bytes_for_file(file_or_bytes)
            textures_to_release: list[object] = []
            sources_to_mark_nonresident: list[object] = []
            with self._state_lock:
                if self._shutdown:
                    raise RuntimeError("TextureManager.acquire called after shutdown.")
                if material_name in self._loaded:
                    entry = self._loaded[material_name]
                    entry.ref_count += 1
                    timing["material_cache_hit"] = True
                    timing["total_ms"] = (time.perf_counter() - start) * 1000.0
                    return TextureAcquireTask(
                        material_name=material_name,
                        file_or_bytes=file_or_bytes,
                        timing=timing,
                        started_at=start,
                        complete=True,
                        result_texture=entry.moderngl_texture,
                    )
                if file_or_bytes in self._file_cache:
                    tex = self._file_cache[file_or_bytes]
                    self._idle_file_lru.pop(file_or_bytes, None)
                    self._loaded[material_name] = LoadedTexture(
                        moderngl_texture=tex,
                        ref_count=1,
                    )
                    timing["file_cache_hit"] = True
                    timing["total_ms"] = (time.perf_counter() - start) * 1000.0
                    return TextureAcquireTask(
                        material_name=material_name,
                        file_or_bytes=file_or_bytes,
                        timing=timing,
                        started_at=start,
                        complete=True,
                        result_texture=tex,
                    )
                (
                    textures_to_release,
                    sources_to_mark_nonresident,
                ) = self._evict_idle_texture_objects_locked(
                    incoming_bytes=incoming_bytes,
                    timing=timing,
                )
            for source in sources_to_mark_nonresident:
                self.decoder.mark_texture_nonresident(source)
            for texture_to_release in textures_to_release:
                self._release_texture_object(texture_to_release)

        decoded = None
        if file_or_bytes:
            decoded = self.decoder.pop_decoded(file_or_bytes)
            if decoded is not None:
                timing["decoded_cache_hit"] = True

        if file_or_bytes and decoded is None:
            timing["sync_decode"] = True
            t_decode = time.perf_counter()
            decoded = self._decode_image(file_or_bytes)
            timing["decode_ms"] = (time.perf_counter() - t_decode) * 1000.0

        task = TextureAcquireTask(
            material_name=material_name,
            file_or_bytes=file_or_bytes,
            timing=timing,
            started_at=start,
            decoded=decoded,
        )
        if decoded is None:
            timing["placeholder"] = True
            tex = self._placeholder_texture()
            task.complete = True
            task.result_texture = self._commit_acquired_texture(task, tex)
            return task

        timing["image_size"] = decoded.size
        timing["image_bytes"] = len(decoded.data)
        return task

    def advance_acquire_with_timing(
        self,
        task: TextureAcquireTask,
        *,
        max_upload_bytes: int,
    ) -> tuple[object | None, dict, bool]:
        """Advance one texture acquire/upload task by at most one row band."""
        self._assert_render_thread("advance_acquire_with_timing")
        if task.complete:
            return task.result_texture, task.timing, True
        if task.decoded is None:
            task.complete = True
            return task.result_texture, task.timing, True

        step_timing = self._new_acquire_step_timing(task.material_name)
        step_start = time.perf_counter()
        decoded = task.decoded
        if not task._reported_decode_state:
            for key in (
                "decoded_cache_hit",
                "sync_decode",
                "placeholder",
                "decode_ms",
                "image_size",
                "texture_evictions",
                "texture_evicted_bytes",
            ):
                step_timing[key] = task.timing[key]
            step_timing["image_total_bytes"] = len(decoded.data)
            task._reported_decode_state = True

        if task.texture is None:
            t_texture = time.perf_counter()
            task.texture = self.ctx.texture(decoded.size, decoded.components, None)
            elapsed = (time.perf_counter() - t_texture) * 1000.0
            step_timing["texture_ms"] += elapsed
            step_timing["texture_alloc_ms"] += elapsed
            task.timing["texture_ms"] += elapsed
            task.timing["texture_alloc_ms"] += elapsed
            step_timing["image_size"] = decoded.size
            step_timing["image_total_bytes"] = len(decoded.data)
            step_timing["total_ms"] = (time.perf_counter() - step_start) * 1000.0
            task.timing["total_ms"] = (time.perf_counter() - task.started_at) * 1000.0
            return None, step_timing, False

        width, height = decoded.size
        row_bytes = max(1, int(width) * int(decoded.components))
        upload_budget = max(row_bytes, int(max_upload_bytes))
        rows = max(1, upload_budget // row_bytes)
        next_row = min(int(height), task.next_row + rows)
        start = task.next_row * row_bytes
        end = next_row * row_bytes
        payload = memoryview(decoded.data)[start:end]

        t_texture = time.perf_counter()
        try:
            task.texture.write(
                payload,
                viewport=(0, task.next_row, int(width), next_row - task.next_row),
            )
        except AttributeError:
            self._release_texture_object(task.texture)
            task.texture = self.ctx.texture(decoded.size, decoded.components, decoded.data)
            next_row = int(height)
            payload = memoryview(decoded.data)
        elapsed = (time.perf_counter() - t_texture) * 1000.0
        uploaded_bytes = len(payload)
        step_timing["texture_ms"] += elapsed
        step_timing["texture_write_ms"] += elapsed
        step_timing["image_bytes"] = uploaded_bytes
        step_timing["image_total_bytes"] = len(decoded.data)
        step_timing["image_size"] = decoded.size
        task.timing["texture_ms"] += elapsed
        task.timing["texture_write_ms"] += elapsed
        task.next_row = next_row

        if task.next_row >= int(height):
            if hasattr(task.texture, "build_mipmaps"):
                t_mipmap = time.perf_counter()
                task.texture.build_mipmaps()
                elapsed = (time.perf_counter() - t_mipmap) * 1000.0
                step_timing["mipmap_ms"] += elapsed
                task.timing["mipmap_ms"] += elapsed
            task.result_texture = self._commit_acquired_texture(
                task,
                task.texture,
                timing=step_timing,
            )
            task.texture = None
            task.decoded = None
            task.complete = True

        step_timing["total_ms"] = (time.perf_counter() - step_start) * 1000.0
        task.timing["total_ms"] = (time.perf_counter() - task.started_at) * 1000.0
        return task.result_texture, step_timing, task.complete

    def cancel_acquire_task(self, task: TextureAcquireTask | None) -> None:
        """Cancel an unfinished resumable acquire and release private GL state."""
        self._assert_render_thread("cancel_acquire_task")
        if task is None or task.complete:
            return
        texture = task.texture
        task.texture = None
        task.decoded = None
        task.complete = True
        self._release_texture_object(texture)

    def _resident_bytes_from_upload_timing(
        self,
        timing: dict | None,
        *,
        fallback_file_or_bytes,
    ) -> int:
        if timing is not None:
            image_size = timing.get("image_size")
            if image_size is not None:
                return estimate_gpu_texture_bytes(image_size)
            if timing.get("placeholder"):
                return 4
        estimated_bytes = self._estimate_resident_bytes_for_file(fallback_file_or_bytes)
        if estimated_bytes is not None:
            return estimated_bytes
        return 4

    def _commit_acquired_texture(
        self,
        task: TextureAcquireTask,
        tex: object,
        *,
        timing: dict | None = None,
    ) -> object:
        """Publish a freshly acquired texture into material/file caches."""
        file_or_bytes = task.file_or_bytes
        uploaded_texture_to_release = None
        textures_to_release = []
        sources_to_mark_nonresident = []
        with self._state_lock:
            if self._shutdown:
                uploaded_texture_to_release = tex
            elif task.material_name in self._loaded:
                entry = self._loaded[task.material_name]
                entry.ref_count += 1
                task.timing["material_cache_hit"] = True
                uploaded_texture_to_release = tex
                tex = entry.moderngl_texture
            elif file_or_bytes:
                self._file_cache.setdefault(file_or_bytes, tex)
                cached_tex = self._file_cache[file_or_bytes]
                if cached_tex is tex:
                    previous = self._file_cache_bytes.get(file_or_bytes, 0)
                    byte_count = max(
                        1,
                        int(
                            self._resident_bytes_from_upload_timing(
                                task.timing,
                                fallback_file_or_bytes=file_or_bytes,
                            )
                        ),
                    )
                    self._file_cache_bytes[file_or_bytes] = byte_count
                    self._file_cache_total_bytes += byte_count - previous
                    (
                        textures_to_release,
                        sources_to_mark_nonresident,
                    ) = self._evict_idle_texture_objects_locked(timing=timing)
                else:
                    uploaded_texture_to_release = tex
                    tex = cached_tex
                    self._idle_file_lru.pop(file_or_bytes, None)

            if not self._shutdown:
                self._loaded[task.material_name] = LoadedTexture(
                    moderngl_texture=tex,
                    ref_count=1,
                )
                task.timing["total_ms"] = (
                    time.perf_counter() - task.started_at
                ) * 1000.0
        if file_or_bytes and uploaded_texture_to_release is None:
            self.decoder.mark_texture_resident(file_or_bytes)
        for source in sources_to_mark_nonresident:
            self.decoder.mark_texture_nonresident(source)
        self._release_texture_object(uploaded_texture_to_release)
        for texture_to_release in textures_to_release:
            self._release_texture_object(texture_to_release)
        return tex

    def _release_texture_object(self, tex: object | None) -> None:
        self._assert_render_thread("_release_texture_object")
        if tex is not None and hasattr(tex, "release"):
            tex.release()

    def _pop_cached_texture_locked(self, file_or_bytes) -> object | None:
        self._idle_file_lru.pop(file_or_bytes, None)
        tex = self._file_cache.pop(file_or_bytes, None)
        self._file_cache_total_bytes = max(
            0,
            self._file_cache_total_bytes - self._file_cache_bytes.pop(file_or_bytes, 0),
        )
        return tex

    def _release_cached_texture(self, file_or_bytes) -> None:
        self._assert_render_thread("_release_cached_texture")
        with self._state_lock:
            tex = self._pop_cached_texture_locked(file_or_bytes)
        self.decoder.mark_texture_nonresident(file_or_bytes)
        self._release_texture_object(tex)

    def _evict_idle_texture_objects_locked(
        self,
        incoming_bytes: int | None = None,
        *,
        timing: dict | None = None,
    ) -> tuple[list[object], list[object]]:
        textures_to_release: list[object] = []
        sources_to_mark_nonresident: list[object] = []
        evicted_bytes = 0
        projected_bytes = self._file_cache_total_bytes + max(0, incoming_bytes or 0)
        while (
            projected_bytes > self.max_resident_texture_bytes
            and self._idle_file_lru
        ):
            file_or_bytes, _ = self._idle_file_lru.popitem(last=False)
            released_bytes = self._file_cache_bytes.get(file_or_bytes, 0)
            evicted_bytes += released_bytes
            tex = self._pop_cached_texture_locked(file_or_bytes)
            sources_to_mark_nonresident.append(file_or_bytes)
            if tex is not None:
                textures_to_release.append(tex)
            projected_bytes = max(0, projected_bytes - released_bytes)
        if timing is not None and sources_to_mark_nonresident:
            timing["texture_evictions"] = int(
                timing.get("texture_evictions", 0)
            ) + len(sources_to_mark_nonresident)
            timing["texture_evicted_bytes"] = int(
                timing.get("texture_evicted_bytes", 0)
            ) + evicted_bytes
        return textures_to_release, sources_to_mark_nonresident

    def _upload_for_material(
        self,
        material_name: str,
        file_or_bytes,
        *,
        timing: dict | None = None,
    ) -> object:
        self._assert_render_thread("_upload_for_material")
        if not file_or_bytes:
            if timing is not None:
                timing["placeholder"] = True
            return self._placeholder_texture()

        decoded = self.decoder.pop_decoded(file_or_bytes)
        if decoded is not None and timing is not None:
            timing["decoded_cache_hit"] = True

        if decoded is None:
            if timing is not None:
                timing["sync_decode"] = True
            t_decode = time.perf_counter()
            decoded = self._decode_image(file_or_bytes)
            if timing is not None:
                timing["decode_ms"] = (time.perf_counter() - t_decode) * 1000.0

        if decoded is None:
            if timing is not None:
                timing["placeholder"] = True
            return self._placeholder_texture()

        if timing is not None:
            timing["image_size"] = decoded.size
            timing["image_bytes"] = len(decoded.data)

        t_texture = time.perf_counter()
        tex = self.ctx.texture(decoded.size, decoded.components, decoded.data)
        if timing is not None:
            elapsed = (time.perf_counter() - t_texture) * 1000.0
            timing["texture_ms"] = elapsed
            # The synchronous path asks ModernGL to allocate and upload in one
            # call. Attribute that combined operation to the write/upload side
            # so diagnostics remain comparable with resumable row-band writes.
            timing["texture_write_ms"] = elapsed
        if hasattr(tex, "build_mipmaps"):
            t_mipmap = time.perf_counter()
            tex.build_mipmaps()
            if timing is not None:
                timing["mipmap_ms"] = (time.perf_counter() - t_mipmap) * 1000.0
        return tex

    def release(self, material_name: str) -> None:
        """Decrement refcount and move unused real textures into the idle LRU."""
        self._assert_render_thread("release")
        texture_to_release = None
        textures_to_release: list[object] = []
        sources_to_mark_nonresident: list[object] = []
        with self._state_lock:
            entry = self._loaded.get(material_name)
            if entry is None:
                return
            entry.ref_count -= 1
            if entry.ref_count <= 0:
                del self._loaded[material_name]
                filename = self.material_to_file.get(material_name)
                if not filename:
                    texture_to_release = entry.moderngl_texture
                else:
                    still_used = any(
                        self.material_to_file.get(m) == filename
                        for m in self._loaded
                    )
                    if filename and not still_used and filename in self._file_cache:
                        self._idle_file_lru[filename] = None
                        self._idle_file_lru.move_to_end(filename)
                        (
                            textures_to_release,
                            sources_to_mark_nonresident,
                        ) = self._evict_idle_texture_objects_locked()
        for source in sources_to_mark_nonresident:
            self.decoder.mark_texture_nonresident(source)
        self._release_texture_object(texture_to_release)
        for texture in textures_to_release:
            self._release_texture_object(texture)

    def shutdown(self) -> None:
        """Release all GPU textures and wake/discard pending CPU decodes."""
        self._assert_render_thread("shutdown")
        with self._state_lock:
            self._shutdown = True
            loaded_materials = list(self._loaded.keys())

        # Wake workers early; GPU release below remains render-thread-only.
        self.decoder.shutdown()

        for mat_name in loaded_materials:
            with self._state_lock:
                if mat_name in self._loaded:
                    self._loaded[mat_name].ref_count = 1
            self.release(mat_name)

        with self._state_lock:
            resident_sources = list(self._file_cache.keys())
        for file_or_bytes in resident_sources:
            self._release_cached_texture(file_or_bytes)
        with self._state_lock:
            self._file_cache.clear()
            self._file_cache_bytes.clear()
            self._file_cache_total_bytes = 0
            self._idle_file_lru.clear()

    def loaded_count(self) -> int:
        with self._state_lock:
            return len(self._file_cache)

    def stats(self) -> dict:
        with self._state_lock:
            gpu_stats = {
                "unique_materials_loaded": len(self._loaded),
                "unique_files_resident": len(self._file_cache),
                "idle_files_resident": len(self._idle_file_lru),
                "resident_texture_bytes": self._file_cache_total_bytes,
                "resident_texture_budget_bytes": self.max_resident_texture_bytes,
            }
        gpu_stats.update(self.decoder.stats())
        return gpu_stats

    def resident_texture_sources(self) -> tuple[object, ...]:
        """Return the exact texture source keys currently resident on the GPU."""
        with self._state_lock:
            return tuple(self._file_cache.keys())


__all__ = [
    "DecodedImage",
    "TEXTURE_BYTES_PER_PIXEL_WITH_MIPS",
    "TEXTURE_MAX_SIZE_ENV_VAR",
    "TextureManager",
]
