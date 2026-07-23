"""Render-thread ownership for streamed chunk GPU uploads.

The OpenGL viewer owns the context and decides when upload work may advance.
This module owns the per-chunk upload state machine, GPU object bookkeeping,
and cleanup path used by both normal streaming unloads and map teardown.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
import time

import numpy as np

from caveviewer.core.chunking import builder as chunker
from caveviewer.gui import render_upload
from caveviewer.gui import streaming_frame_timing


class ChunkUploadManager:
    """Advance render-thread chunk uploads and release their GPU resources."""

    def __init__(
        self,
        *,
        ctx,
        program,
        texture_manager,
        smooth_shading_enabled: Callable[[], bool],
        gpu_objects: dict[tuple, list] | None = None,
        upload_states: dict[tuple, dict] | None = None,
        normal_cache: dict[tuple, list] | None = None,
        aabbs: dict[tuple, tuple] | None = None,
        upload_operations_per_chunk: int = 1,
        upload_time_budget_ms: float = 3.0,
        vbo_upload_slice_bytes: int = render_upload.RENDER_UPLOAD_INITIAL_SLICE_BYTES,
        texture_upload_slice_bytes: int = render_upload.RENDER_UPLOAD_INITIAL_SLICE_BYTES,
    ) -> None:
        self.ctx = ctx
        self.program = program
        self.texture_manager = texture_manager
        self.smooth_shading_enabled = smooth_shading_enabled
        self.gpu_objects = {} if gpu_objects is None else gpu_objects
        self.upload_states = {} if upload_states is None else upload_states
        self.normal_cache = {} if normal_cache is None else normal_cache
        self.aabbs = {} if aabbs is None else aabbs
        self.upload_operations_per_chunk = max(1, int(upload_operations_per_chunk))
        self.upload_time_budget_ms = max(0.5, float(upload_time_budget_ms))
        self.vbo_upload_slice_bytes = int(vbo_upload_slice_bytes)
        self.texture_upload_slice_bytes = int(texture_upload_slice_bytes)
        self._streaming_frame_timing: dict | None = None

    def set_frame_limits(
        self,
        *,
        operations_per_chunk: int,
        time_budget_ms: float,
        timing: dict | None,
    ) -> None:
        """Set the upload budget that applies to callbacks in the current frame."""
        self.upload_operations_per_chunk = max(1, int(operations_per_chunk))
        self.upload_time_budget_ms = max(0.5, float(time_budget_ms))
        self._streaming_frame_timing = timing

    def clear_frame_timing(self) -> None:
        """Detach per-frame diagnostics after the ready queue has been drained."""
        self._streaming_frame_timing = None

    def record_upload_slice_sizes(self, timing: dict | None) -> None:
        """Record the current adaptive VBO and texture upload slice sizes."""
        render_upload.record_upload_slice_sizes(
            timing,
            render_upload.UploadSliceState(
                vbo_upload_slice_bytes=self.vbo_upload_slice_bytes,
                texture_upload_slice_bytes=self.texture_upload_slice_bytes,
            ),
        )

    def adapt_upload_slice_size(
        self,
        *,
        kind: str,
        elapsed_ms: float,
        byte_count: int,
        timing: dict | None,
    ) -> None:
        """Apply adaptive upload-slice policy after a measured operation."""
        decision = render_upload.adapt_upload_slice_size(
            kind=kind,
            elapsed_ms=elapsed_ms,
            byte_count=byte_count,
            target_ms=self.upload_time_budget_ms,
            state=render_upload.UploadSliceState(
                vbo_upload_slice_bytes=self.vbo_upload_slice_bytes,
                texture_upload_slice_bytes=self.texture_upload_slice_bytes,
            ),
            timing=timing,
        )
        self.vbo_upload_slice_bytes = decision.state.vbo_upload_slice_bytes
        self.texture_upload_slice_bytes = decision.state.texture_upload_slice_bytes

    def _cancel_chunk_group_upload_job(self, job: dict | None) -> None:
        if not job:
            return
        texture_task = job.get("texture_task")
        cancel_acquire_task = getattr(self.texture_manager, "cancel_acquire_task", None)
        if texture_task is not None and callable(cancel_acquire_task):
            cancel_acquire_task(texture_task)
        if job.get("texture") is not None:
            self._release_material_texture(job["group"].material_name)
            job["texture"] = None
        pending_vbo = job.get("pending_vbo")
        if pending_vbo is not None and hasattr(pending_vbo, "release"):
            pending_vbo.release()
        job["pending_vbo"] = None
        job["pending_vbo_payload"] = None

    def _release_material_texture(self, material_name: str) -> None:
        timing = self._streaming_frame_timing
        before_stats = (
            self.texture_manager.stats()
            if timing is not None and hasattr(self.texture_manager, "stats")
            else None
        )
        self.texture_manager.release(material_name)
        if timing is None or before_stats is None:
            return
        after_stats = self.texture_manager.stats()
        evicted = max(
            0,
            int(before_stats.get("unique_files_resident", 0))
            - int(after_stats.get("unique_files_resident", 0)),
        )
        evicted_bytes = max(
            0,
            int(before_stats.get("resident_texture_bytes", 0))
            - int(after_stats.get("resident_texture_bytes", 0)),
        )
        timing["texture_evictions"] += evicted
        timing["texture_evicted_bytes"] += evicted_bytes

    def _advance_texture_upload_job(
        self,
        job: dict,
        counters: dict,
        timing: dict | None,
    ) -> bool:
        """Return True when the current render-upload job has a texture."""
        if job.get("texture") is not None:
            return True

        texture_task = job.get("texture_task")
        begin_acquire = getattr(self.texture_manager, "begin_acquire_with_timing", None)
        advance_acquire = getattr(
            self.texture_manager,
            "advance_acquire_with_timing",
            None,
        )
        if texture_task is None and callable(begin_acquire) and callable(advance_acquire):
            t_texture_begin = time.perf_counter()
            texture_task = begin_acquire(job["group"].material_name)
            counters["texture_ms"] += (
                time.perf_counter() - t_texture_begin
            ) * 1000.0
            job["texture_task"] = texture_task
            if texture_task.complete:
                job["texture"] = texture_task.result_texture
                job["texture_task"] = None
                render_upload.add_texture_timing_counters(
                    counters,
                    texture_task.timing,
                    timing,
                )
                return True

        if texture_task is not None and callable(advance_acquire):
            texture, texture_timing, complete = advance_acquire(
                texture_task,
                max_upload_bytes=max(1, int(self.texture_upload_slice_bytes)),
            )
            counters["texture_ms"] += texture_timing.get("total_ms", 0.0)
            render_upload.add_texture_timing_counters(counters, texture_timing, timing)
            self.adapt_upload_slice_size(
                kind="texture",
                elapsed_ms=texture_timing.get("total_ms", 0.0),
                byte_count=int(texture_timing.get("image_bytes", 0)),
                timing=timing,
            )
            if complete:
                job["texture"] = texture
                job["texture_task"] = None
                return True
            return False

        t_texture = time.perf_counter()
        acquire_with_timing = getattr(
            self.texture_manager,
            "acquire_with_timing",
            None,
        )
        texture_timing = None
        if callable(acquire_with_timing):
            texture, texture_timing = acquire_with_timing(job["group"].material_name)
        else:
            texture = self.texture_manager.acquire(job["group"].material_name)
        counters["texture_ms"] += (time.perf_counter() - t_texture) * 1000.0
        if texture_timing is not None:
            render_upload.add_texture_timing_counters(counters, texture_timing, timing)
            self.adapt_upload_slice_size(
                kind="texture",
                elapsed_ms=texture_timing.get(
                    "total_ms",
                    texture_timing.get("texture_ms", 0.0),
                ),
                byte_count=int(texture_timing.get("image_bytes", 0)),
                timing=timing,
            )
        job["texture"] = texture
        return True

    def _append_chunk_vbo_slice(
        self,
        job: dict,
        chunk_state: dict,
        vbo,
        start_vertex: int,
        end_vertex: int,
        counters: dict,
    ) -> bool:
        group = job["group"]
        t_vao = time.perf_counter()
        vao = self.ctx.vertex_array(
            self.program, [(vbo, "3f 2f 3f", "in_position", "in_uv", "in_normal")]
        )
        counters["vao_ms"] += (time.perf_counter() - t_vao) * 1000.0

        chunk_state["vao_list"].append(
            (vao, vbo, group.material_name, job["texture"])
        )
        chunk_state["normal_cache_entry"].append(
            (
                group.material_name,
                group.positions[start_vertex:end_vertex],
                group.uvs[start_vertex:end_vertex],
                group.smooth_normals[start_vertex:end_vertex],
            )
        )
        job["texture"] = None
        job["next_vertex_index"] = end_vertex
        if end_vertex >= len(group.positions):
            counters["groups"] += 1
            return True
        return False

    def _complete_pending_vbo_upload_job(
        self,
        job: dict,
        chunk_state: dict,
        counters: dict,
        timing: dict | None,
    ) -> bool:
        vbo = job["pending_vbo"]
        payload = job["pending_vbo_payload"]
        start_vertex = int(job["pending_vbo_start_vertex"])
        end_vertex = int(job["pending_vbo_end_vertex"])
        byte_count = len(payload)

        t_buffer = time.perf_counter()
        vbo.write(payload)
        elapsed_ms = (time.perf_counter() - t_buffer) * 1000.0
        counters["buffer_ms"] += elapsed_ms
        counters["buffer_write_ms"] += elapsed_ms
        counters["buffer_write_bytes"] += byte_count
        counters["vertices"] += end_vertex - start_vertex
        counters["bytes"] += byte_count
        self.adapt_upload_slice_size(
            kind="vbo",
            elapsed_ms=elapsed_ms,
            byte_count=byte_count,
            timing=timing,
        )

        complete = self._append_chunk_vbo_slice(
            job,
            chunk_state,
            vbo,
            start_vertex,
            end_vertex,
            counters,
        )
        job["pending_vbo"] = None
        job["pending_vbo_payload"] = None
        job["pending_vbo_start_vertex"] = 0
        job["pending_vbo_end_vertex"] = 0
        return complete

    def _advance_chunk_group_upload_job(
        self,
        job: dict,
        chunk_state: dict,
        counters: dict,
        timing: dict | None,
    ) -> bool:
        """
        Advance one render-thread upload operation for a material group.

        A group is deliberately split into a resumable texture acquire and
        triangle-aligned VBO slices. This keeps the streaming frame budget from
        starting a single large ``ctx.texture`` or ``ctx.buffer`` call that can
        monopolize the render thread.
        """
        if not self._advance_texture_upload_job(job, counters, timing):
            return False

        if job.get("pending_vbo") is not None:
            return self._complete_pending_vbo_upload_job(
                job,
                chunk_state,
                counters,
                timing,
            )

        group = job["group"]
        start_vertex = int(job["next_vertex_index"])
        vertex_count = len(group.positions)
        if start_vertex >= vertex_count:
            return True

        end_vertex = min(
            vertex_count,
            start_vertex + render_upload.render_upload_slice_vertices(
                self.vbo_upload_slice_bytes
            ),
        )
        if end_vertex < vertex_count:
            end_vertex -= (end_vertex - start_vertex) % 3
            if end_vertex <= start_vertex:
                end_vertex = min(vertex_count, start_vertex + 3)

        used_prepacked = group.has_prepacked_vertex_bytes(
            smooth_shading=job["smooth_shading"]
        )
        t_pack = time.perf_counter()
        if used_prepacked:
            byte_start = start_vertex * render_upload.RENDER_UPLOAD_VERTEX_BYTES
            byte_end = end_vertex * render_upload.RENDER_UPLOAD_VERTEX_BYTES
            active_bytes = memoryview(group.prepacked_vertex_bytes)[
                byte_start:byte_end
            ]
        else:
            active_bytes = chunker.vertex_bytes_for_shading(
                group.positions[start_vertex:end_vertex],
                group.uvs[start_vertex:end_vertex],
                group.smooth_normals[start_vertex:end_vertex],
                smooth_shading=job["smooth_shading"],
            )
        counters["vertex_pack_ms"] += (time.perf_counter() - t_pack) * 1000.0
        if used_prepacked:
            counters["prepacked_groups"] += 1
        else:
            counters["fallback_pack_groups"] += 1
        byte_count = len(active_bytes)
        counters["buffer_alloc_bytes"] += byte_count

        t_buffer = time.perf_counter()
        try:
            vbo = self.ctx.buffer(reserve=byte_count)
        except TypeError:
            t_buffer = time.perf_counter()
            vbo = self.ctx.buffer(active_bytes)
            elapsed_ms = (time.perf_counter() - t_buffer) * 1000.0
            counters["buffer_ms"] += elapsed_ms
            counters["buffer_alloc_ms"] += elapsed_ms
            counters["buffer_write_ms"] += elapsed_ms
            counters["buffer_write_bytes"] += byte_count
            counters["vertices"] += end_vertex - start_vertex
            counters["bytes"] += byte_count
            self.adapt_upload_slice_size(
                kind="vbo",
                elapsed_ms=elapsed_ms,
                byte_count=byte_count,
                timing=timing,
            )
            try:
                return self._append_chunk_vbo_slice(
                    job,
                    chunk_state,
                    vbo,
                    start_vertex,
                    end_vertex,
                    counters,
                )
            except Exception:
                if hasattr(vbo, "release"):
                    vbo.release()
                raise

        elapsed_ms = (time.perf_counter() - t_buffer) * 1000.0
        counters["buffer_ms"] += elapsed_ms
        counters["buffer_alloc_ms"] += elapsed_ms
        self.adapt_upload_slice_size(
            kind="vbo",
            elapsed_ms=elapsed_ms,
            byte_count=byte_count,
            timing=timing,
        )
        job["pending_vbo"] = vbo
        job["pending_vbo_payload"] = active_bytes
        job["pending_vbo_start_vertex"] = start_vertex
        job["pending_vbo_end_vertex"] = end_vertex
        return False

    def _publish_chunk_upload_state(self, chunk_data, state: dict) -> None:
        """Make completed upload slices drawable before the whole chunk is done."""
        if not state.get("vao_list"):
            return
        self.gpu_objects[chunk_data.cell] = state["vao_list"]
        self.normal_cache[chunk_data.cell] = state["normal_cache_entry"]
        self.aabbs[chunk_data.cell] = (
            chunk_data.bounds_min.astype(np.float32, copy=False),
            chunk_data.bounds_max.astype(np.float32, copy=False),
        )

    def on_chunk_ready(self, chunk_data) -> bool:
        """Advance render-thread upload work for one ready chunk."""
        timing = self._streaming_frame_timing
        chunk_start = time.perf_counter()
        frame_counters = render_upload.new_chunk_upload_counters()

        state = self.upload_states.get(chunk_data.cell)
        if state is None:
            upload_groups = chunk_data.upload_groups
            if upload_groups is None:
                t_prepare = time.perf_counter()
                chunker.prepare_chunk_upload_groups(chunk_data)
                frame_counters["chunk_prepare_ms"] = (
                    time.perf_counter() - t_prepare
                ) * 1000.0
                upload_groups = chunk_data.upload_groups or []

            state = {
                "upload_groups": upload_groups or [],
                "next_group_index": 0,
                "active_group_job": None,
                "vao_list": [],
                "normal_cache_entry": [],
                "smooth_shading": bool(self.smooth_shading_enabled()),
            }
            self.upload_states[chunk_data.cell] = state

        operations_this_call = 0
        upload_groups = state["upload_groups"]

        while state["next_group_index"] < len(upload_groups):
            if operations_this_call >= self.upload_operations_per_chunk:
                break
            if (
                operations_this_call > 0
                and (time.perf_counter() - chunk_start) * 1000.0
                >= self.upload_time_budget_ms
            ):
                break

            group_job = state.get("active_group_job")
            if group_job is None:
                group_job = render_upload.new_chunk_group_upload_job(
                    upload_groups[state["next_group_index"]],
                    state["smooth_shading"],
                )
                state["active_group_job"] = group_job

            group_counters = render_upload.new_chunk_upload_counters()
            group_complete = self._advance_chunk_group_upload_job(
                group_job,
                state,
                group_counters,
                timing,
            )
            operations_this_call += 1
            render_upload.add_chunk_upload_counters(frame_counters, group_counters)
            self._publish_chunk_upload_state(chunk_data, state)
            if group_complete:
                state["active_group_job"] = None
                state["next_group_index"] += 1

        completed = state["next_group_index"] >= len(upload_groups)
        if completed:
            t_book = time.perf_counter()
            self._publish_chunk_upload_state(chunk_data, state)
            frame_counters["chunk_bookkeeping_ms"] += (
                time.perf_counter() - t_book
            ) * 1000.0
            del self.upload_states[chunk_data.cell]
            if state["smooth_shading"] != bool(self.smooth_shading_enabled()):
                self.apply_shading_toggle_to_cell(chunk_data.cell)

        chunk_ms = (time.perf_counter() - chunk_start) * 1000.0
        streaming_frame_timing.record_chunk_upload_timing(
            timing,
            frame_counters,
            chunk_ms=chunk_ms,
            cell=chunk_data.cell,
            completed=completed,
        )
        return completed

    def on_chunk_unload(self, cell) -> None:
        """Release complete or partial GPU resources for one chunk cell."""
        t_unload = time.perf_counter()
        partial_state = self.upload_states.pop(cell, None)
        partial_was_published = (
            partial_state is not None
            and self.gpu_objects.get(cell)
            is partial_state.get("vao_list")
        )
        if partial_state is not None:
            self._cancel_chunk_group_upload_job(
                partial_state.get("active_group_job")
            )
            for vao, vbo, mat_name, _texture in partial_state.get("vao_list", []):
                vao.release()
                vbo.release()
                self._release_material_texture(mat_name)
        if partial_was_published:
            self.gpu_objects.pop(cell, None)
        else:
            vao_list = self.gpu_objects.pop(cell, [])
            for vao, vbo, mat_name, _texture in vao_list:
                vao.release()
                vbo.release()
                self._release_material_texture(mat_name)
        self.normal_cache.pop(cell, None)
        self.aabbs.pop(cell, None)
        timing = self._streaming_frame_timing
        if timing is not None:
            timing["unload_ms"] += (time.perf_counter() - t_unload) * 1000.0
            timing["chunks_unloaded"] += 1

    def unload_all(self) -> None:
        """Release every complete or partial chunk upload owned by this manager."""
        for cell in list(self.upload_states.keys()):
            self.on_chunk_unload(cell)
        for cell in list(self.gpu_objects.keys()):
            self.on_chunk_unload(cell)
        self.gpu_objects.clear()
        self.upload_states.clear()
        self.normal_cache.clear()
        self.aabbs.clear()

    def apply_shading_toggle_to_cell(self, cell) -> None:
        """Rewrite one loaded chunk's VBO normal data for the current shade mode."""
        smooth = bool(self.smooth_shading_enabled())
        vao_list = self.gpu_objects.get(cell)
        cache_entries = self.normal_cache.get(cell)
        if not vao_list or not cache_entries or len(cache_entries) != len(vao_list):
            return
        for (
            _vao,
            vbo,
            _mat_name,
            _texture,
        ), (
            _cached_mat,
            positions,
            uvs,
            smooth_normals,
        ) in zip(vao_list, cache_entries):
            vbo.write(
                chunker.vertex_bytes_for_shading(
                    positions,
                    uvs,
                    smooth_normals,
                    smooth_shading=smooth,
                )
            )

    def apply_shading_toggle(self, *, world=None) -> None:
        """
        Rewrite loaded VBOs after a shade-mode change without reloading chunks.

        The streaming world is notified first so future worker-prepacked bytes
        match the visible mode; currently resident VBOs are then updated in
        place.
        """
        if world is not None and hasattr(world, "set_prepack_smooth_shading"):
            world.set_prepack_smooth_shading(bool(self.smooth_shading_enabled()))
        for cell in list(self.gpu_objects.keys()):
            self.apply_shading_toggle_to_cell(cell)
