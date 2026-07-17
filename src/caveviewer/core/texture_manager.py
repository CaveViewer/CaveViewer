"""
caveviewer.core.texture_manager

Lazy GPU texture loading/eviction keyed by material name, kept in lockstep
with chunk streaming. This is the second half of the VRAM-saving strategy
(the first half is geometry chunking in chunker.py / streaming_world.py):
even though geometry chunks reference materials, we don't want to upload
every texture tile in the whole cave map upfront -- only the tiles actually
touched by currently-loaded chunks.

Reference-counted: a texture is only evicted once no currently-loaded chunk
still references it. This is necessary because multiple chunks can (and do)
share a texture tile.

Decode vs upload are deliberately split into two separate steps:
  - decode_from_disk() does JPEG decoding (Pillow) and pixel manipulation
    (numpy) only -- no OpenGL calls at all, so it is SAFE to run on a
    background worker thread. This is the expensive, variable-cost part
    (can take anywhere from <1ms to 10+ms depending on image size).
  - upload_decoded() takes already-decoded pixel bytes and does the actual
    ctx.texture(...) GPU call -- this MUST happen on the main/render thread
    (an OpenGL/driver requirement), but it's comparatively fast and
    consistent once decoding is already done.

This split exists because chunk streaming runs texture decode on background
worker threads (see caveviewer.core.streaming_world's _worker_loop, which now also
pre-decodes textures alongside loading geometry) so that by the time a
chunk reaches the main thread for GPU upload, the slow/unpredictable part
(JPEG decode) is already finished -- only the fast, predictable GPU upload
remains on the main thread, which is what keeps frame times smooth during
a fast flythrough that streams in many never-before-seen textures at once.
"""

from __future__ import annotations

import math
import os
import threading
import time
from io import BytesIO
from dataclasses import dataclass
from typing import Optional

from PIL import Image
import numpy as np
from caveviewer.core.logging_utils import get_logger

# Pillow's default decompression-bomb guard rejects images above ~179
# million pixels, as a safety measure against maliciously crafted image
# files designed to exhaust memory when decoded. Photogrammetry/3D-scan
# texture atlases (including some downloaded from sites like Sketchfab)
# can legitimately exceed that -- a single 16000x16000 texture tile is
# 256 million pixels and is a completely normal thing for a textured
# scan to ship with, not an attack. Raise the limit rather than disable
# the check outright, so genuinely absurd files (a 1,000,000,000+ pixel
# image, which no legitimate texture tile would ever be) still get
# caught; this project's own decode path also wraps every Image.open()
# call in a try/except (see _decode_from_disk below) as a second layer
# of protection regardless of where the threshold ends up sitting.
Image.MAX_IMAGE_PIXELS = 1_000_000_000

_LOG = get_logger("TextureManager")

TEXTURE_MAX_SIZE_ENV_VAR = "CAVEVIEWER_MAX_TEXTURE_SIZE"
_TEXTURE_BUDGET_SHARE = 0.80
_TEXTURE_BYTES_PER_PIXEL_WITH_MIPS = 4.0 * (4.0 / 3.0)
_AUTO_TEXTURE_DIMENSION_STEPS = (16384, 8192, 4096, 2048, 1024, 512)
_MAX_TEXTURE_DIMENSION_LIMIT = _AUTO_TEXTURE_DIMENSION_STEPS[0]
_MIN_TEXTURE_DIMENSION_LIMIT = 512
_DEFAULT_DECODE_CACHE_BYTES = 256 * 1024 ** 2
_MIN_DECODE_CACHE_BYTES = 32 * 1024 ** 2
_MAX_DECODE_CACHE_BYTES = 512 * 1024 ** 2
_DECODE_CACHE_AVAILABLE_RAM_FRACTION = 0.05


@dataclass
class DecodedImage:
    """Result of decode_from_disk(): plain CPU-side data, no GPU objects,
    safe to pass between threads."""
    size: tuple[int, int]
    components: int
    data: bytes


@dataclass
class LoadedTexture:
    moderngl_texture: object   # the moderngl.Texture instance
    ref_count: int = 0


def _texture_cache_key(file_or_bytes) -> object | None:
    if not file_or_bytes:
        return None
    if isinstance(file_or_bytes, bytes):
        return ("embedded", len(file_or_bytes), hash(file_or_bytes))
    return ("file", str(file_or_bytes))


def _parse_texture_dimension_limit(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    text = raw_value.strip()
    if not text:
        return None
    try:
        value = int(float(text))
    except ValueError:
        return None
    return max(_MIN_TEXTURE_DIMENSION_LIMIT, min(_MAX_TEXTURE_DIMENSION_LIMIT, value))


def _format_texture_size(size: tuple[int, int] | None) -> str:
    if size is None:
        return "unknown"
    return f"{size[0]}x{size[1]}"


class TextureManager:
    def __init__(
        self,
        gl_context,
        textures_dir: str,
        material_to_file: dict,
        max_texture_dimension: int | None = None,
        max_decoded_cache_bytes: int | None = None,
    ):
        """
        gl_context: a moderngl.Context (or any object exposing .texture(size, components, data))
        textures_dir: folder containing texture files referenced by filename
            (ignored for any material whose value below is raw bytes
            rather than a filename, since there's nothing on disk to
            look up in that case).
        material_to_file: {material_name: value}, where value is one of:
            - a str filename, relative to textures_dir (OBJ/.mtl's
              convention -- a separate image file on disk)
            - raw image bytes (e.g. JPEG/PNG file bytes) -- used for
              formats like GLB/glTF, which commonly embed texture image
              data directly inside the model file rather than as
              separate files alongside it. The bytes themselves serve as
              the cache key (they're hashable), so two materials sharing
              the exact same embedded image are still deduplicated the
              same way two materials sharing one filename already are.
            - None -- no texture for this material (placeholder used)
        """
        self.ctx = gl_context
        self.textures_dir = textures_dir
        self.material_to_file = material_to_file
        self.max_texture_dimension = _parse_texture_dimension_limit(
            str(max_texture_dimension) if max_texture_dimension else None
        )
        self.max_decoded_cache_bytes = self._normalize_decoded_cache_limit(
            max_decoded_cache_bytes
        )
        self._texture_downscale_logged = False
        self._decode_cache_limit_logged = False
        self._loaded: dict[str, LoadedTexture] = {}  # keyed by material name
        # multiple materials can point at the same physical jpg (rare, but
        # cheap to dedupe so we don't decode the same file twice) -- or,
        # for embedded textures, the same raw bytes object/value
        self._file_cache: dict[object, object] = {}  # filename-or-bytes -> moderngl.Texture

        # Decoded-but-not-yet-uploaded images, populated by background
        # worker threads via decode_from_disk(), consumed on the main
        # thread via upload_decoded(). Guarded by a lock since multiple
        # worker threads may populate it concurrently.
        self._decode_cache: dict[object, DecodedImage] = {}
        self._decode_cache_bytes = 0
        self._decode_inflight: set[object] = set()
        self._decode_cache_lock = threading.Lock()
        _LOG.info(
            "Texture predecode cache cap active: %.1f MB. Oversized or "
            "over-budget textures will decode on demand at original resolution.",
            self.max_decoded_cache_bytes / (1024 ** 2),
        )
        if self.max_texture_dimension is not None:
            if self.max_texture_dimension >= _MAX_TEXTURE_DIMENSION_LIMIT:
                _LOG.info(
                    "Texture budget allows the maximum configured texture "
                    "dimension: %d px. Textures at or below this size will "
                    "upload without downscaling.",
                    self.max_texture_dimension,
                )
            else:
                _LOG.info(
                    "Texture max dimension cap active: %d px. Oversized textures "
                    "will be downscaled before GPU upload.",
                    self.max_texture_dimension,
                )

    @staticmethod
    def recommend_max_texture_dimension(
        material_to_file: dict,
        gpu_memory_bytes: int | None,
        gpu_target_fraction: float,
    ) -> int | None:
        """Choose a decode-time texture cap for the whole map.

        The streaming scheduler must keep geometry visible even when a map has
        more texture data than the GPU can hold at full resolution.  This cap is
        the texture-side safety valve: many huge texture tiles become lower-res
        GPU textures instead of forcing the streamer to reject geometry chunks.
        """
        explicit_raw_value = os.environ.get(TEXTURE_MAX_SIZE_ENV_VAR)
        explicit_limit = _parse_texture_dimension_limit(explicit_raw_value)
        if explicit_limit is not None:
            _LOG.info(
                "Texture max dimension cap selected from %s=%r: %d px.",
                TEXTURE_MAX_SIZE_ENV_VAR,
                explicit_raw_value,
                explicit_limit,
            )
            return explicit_limit
        if explicit_raw_value is not None and explicit_raw_value.strip():
            _LOG.warning(
                "Ignoring invalid %s=%r; using automatic texture cap selection.",
                TEXTURE_MAX_SIZE_ENV_VAR,
                explicit_raw_value,
            )

        if gpu_memory_bytes is None or gpu_memory_bytes <= 0:
            _LOG.info(
                "Texture max dimension cap not selected because GPU memory "
                "budget is unavailable."
            )
            return None

        unique_texture_keys = {
            key
            for key in (_texture_cache_key(value) for value in material_to_file.values())
            if key is not None
        }
        if not unique_texture_keys:
            _LOG.info(
                "Texture max dimension cap not selected because the map has no "
                "unique texture files."
            )
            return None

        target_fraction = max(0.01, min(0.80, float(gpu_target_fraction)))
        texture_budget_bytes = gpu_memory_bytes * target_fraction * _TEXTURE_BUDGET_SHARE
        bytes_per_texture = texture_budget_bytes / max(1, len(unique_texture_keys))
        max_pixels = bytes_per_texture / _TEXTURE_BYTES_PER_PIXEL_WITH_MIPS
        if max_pixels <= 0:
            return _MIN_TEXTURE_DIMENSION_LIMIT

        raw_dimension = int(math.sqrt(max_pixels))
        for index, step in enumerate(_AUTO_TEXTURE_DIMENSION_STEPS):
            # Treat values close to a common texture size as that size.  This
            # avoids useless 4096 -> 3990 resizes when the budget is only a few
            # percent below the original estimate.
            if raw_dimension >= int(step * 0.875):
                next_larger_step = (
                    _AUTO_TEXTURE_DIMENSION_STEPS[index - 1]
                    if index > 0
                    else None
                )
                next_larger_reason = (
                    "highest configured step accepted"
                    if next_larger_step is None
                    else (
                        f"next larger {next_larger_step} px step requires "
                        f"raw limit >= {int(next_larger_step * 0.875)} px"
                    )
                )
                _LOG.info(
                    "Texture max dimension cap auto-selected: %d px "
                    "(GPU budget %.1f GB, target %.0f%%, texture share %.0f%% "
                    "=> %.1f GB for %d unique textures; %.1f MB/texture; "
                    "raw square limit %d px; %s).",
                    step,
                    gpu_memory_bytes / (1024 ** 3),
                    target_fraction * 100.0,
                    _TEXTURE_BUDGET_SHARE * 100.0,
                    texture_budget_bytes / (1024 ** 3),
                    len(unique_texture_keys),
                    bytes_per_texture / (1024 ** 2),
                    raw_dimension,
                    next_larger_reason,
                )
                return step
        _LOG.info(
            "Texture max dimension cap auto-selected: %d px "
            "(GPU budget %.1f GB, target %.0f%%, texture share %.0f%% "
            "=> %.1f GB for %d unique textures; %.1f MB/texture; "
            "raw square limit %d px below configured steps).",
            _MIN_TEXTURE_DIMENSION_LIMIT,
            gpu_memory_bytes / (1024 ** 3),
            target_fraction * 100.0,
            _TEXTURE_BUDGET_SHARE * 100.0,
            texture_budget_bytes / (1024 ** 3),
            len(unique_texture_keys),
            bytes_per_texture / (1024 ** 2),
            raw_dimension,
        )
        return _MIN_TEXTURE_DIMENSION_LIMIT

    @staticmethod
    def recommend_decoded_cache_bytes(available_ram_bytes: int | None) -> int:
        """Choose a CPU-side texture predecode cache cap from available RAM.

        This does not resize textures.  It only limits how many fully decoded
        RGB images background workers may keep waiting for main-thread upload.
        If the cap is too small for the next texture, that texture is decoded
        on demand during upload at its original selected resolution.
        """
        if available_ram_bytes is None or available_ram_bytes <= 0:
            return _DEFAULT_DECODE_CACHE_BYTES
        return max(
            _MIN_DECODE_CACHE_BYTES,
            min(
                _MAX_DECODE_CACHE_BYTES,
                int(available_ram_bytes * _DECODE_CACHE_AVAILABLE_RAM_FRACTION),
            ),
        )

    @staticmethod
    def _normalize_decoded_cache_limit(max_decoded_cache_bytes: int | None) -> int:
        if max_decoded_cache_bytes is None:
            return _DEFAULT_DECODE_CACHE_BYTES
        try:
            value = int(max_decoded_cache_bytes)
        except (TypeError, ValueError):
            return _DEFAULT_DECODE_CACHE_BYTES
        return max(1, value)

    def _placeholder_texture(self):
        """1x1 magenta texture used when a material's image file is missing,
        so a bad texture reference degrades visibly (obviously wrong color)
        instead of crashing the whole viewer mid-flythrough."""
        data = np.array([[255, 0, 255, 255]], dtype=np.uint8).tobytes()
        tex = self.ctx.texture((1, 1), 4, data)
        return tex

    def _apply_texture_dimension_limit(self, image: Image.Image, source_label: str) -> Image.Image:
        limit = self.max_texture_dimension
        if limit is None or max(image.size) <= limit:
            return image

        original_size = image.size
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        image.thumbnail((limit, limit), resampling)
        if not self._texture_downscale_logged:
            _LOG.info(
                "Downscaling oversized textures to fit GPU budget; first resize "
                "%r: %dx%d -> %dx%d.",
                source_label,
                original_size[0],
                original_size[1],
                image.size[0],
                image.size[1],
            )
            self._texture_downscale_logged = True
        return image

    # -- background-thread-safe decode step ----------------------------------

    def decode_for_material(self, material_name: str) -> None:
        """
        Safe to call from any thread (no OpenGL calls). Decodes the image
        for `material_name`'s texture (whether that's a file on disk or
        embedded raw bytes -- see __init__'s docstring), if not already
        decoded or already uploaded, and stashes the raw pixel data for
        upload_decoded() to pick up later on the main thread. No-op if the
        texture is already GPU-resident or already sitting decoded-and-
        waiting.
        """
        file_or_bytes = self.material_to_file.get(material_name)
        if not file_or_bytes:
            return  # no texture for this material; placeholder path handles it on upload
        if file_or_bytes in self._file_cache:
            return  # already GPU-resident
        estimated_bytes = self._estimate_decoded_image_bytes(file_or_bytes)
        with self._decode_cache_lock:
            if file_or_bytes in self._decode_cache:
                return  # already decoded, waiting for main-thread upload
            if file_or_bytes in self._decode_inflight:
                return  # another worker is already decoding/reserving this texture
            if estimated_bytes is None:
                estimated_bytes = self.max_decoded_cache_bytes
            if (
                estimated_bytes > self.max_decoded_cache_bytes
                or self._decode_cache_bytes + estimated_bytes > self.max_decoded_cache_bytes
            ):
                self._log_decode_cache_skip(file_or_bytes, estimated_bytes)
                return
            self._decode_cache_bytes += estimated_bytes
            self._decode_inflight.add(file_or_bytes)

        decoded = self._decode_image(file_or_bytes)
        with self._decode_cache_lock:
            self._decode_inflight.discard(file_or_bytes)
            self._decode_cache_bytes = max(
                0,
                self._decode_cache_bytes - estimated_bytes,
            )
            if decoded is None:
                return
            actual_bytes = len(decoded.data)
            # double check another thread didn't upload or decode it in the
            # meantime; if so, just discard our redundant decode rather than
            # overwrite.  If the actual decoded payload is larger than the
            # estimate/cap, keep memory bounded and fall back to on-demand
            # decode during upload.
            if (
                file_or_bytes not in self._decode_cache
                and file_or_bytes not in self._file_cache
                and actual_bytes <= self.max_decoded_cache_bytes
                and self._decode_cache_bytes + actual_bytes <= self.max_decoded_cache_bytes
            ):
                self._decode_cache[file_or_bytes] = decoded
                self._decode_cache_bytes += actual_bytes

    def _log_decode_cache_skip(self, file_or_bytes, estimated_bytes: int) -> None:
        if self._decode_cache_limit_logged:
            return
        label = (
            "<embedded texture>"
            if isinstance(file_or_bytes, bytes)
            else str(file_or_bytes)
        )
        _LOG.info(
            "Texture predecode cache is limiting background decode memory; "
            "%r needs about %.1f MB while %.1f MB is available in the "
            "predecode cache. It will decode on demand at upload time.",
            label,
            estimated_bytes / (1024 ** 2),
            max(0, self.max_decoded_cache_bytes - self._decode_cache_bytes)
            / (1024 ** 2),
        )
        self._decode_cache_limit_logged = True

    def _estimate_decoded_image_bytes(self, file_or_bytes) -> int | None:
        size = self._inspect_texture_size(file_or_bytes)
        if size is None:
            return None
        width, height = self._size_after_texture_limit(size)
        return max(1, int(width) * int(height) * 3)

    def _size_after_texture_limit(self, size: tuple[int, int]) -> tuple[int, int]:
        limit = self.max_texture_dimension
        width, height = size
        if limit is None or max(width, height) <= limit:
            return width, height
        scale = float(limit) / float(max(width, height))
        return max(1, int(round(width * scale))), max(1, int(round(height * scale)))

    def _decode_image(self, file_or_bytes) -> Optional[DecodedImage]:
        """
        Dispatches to the disk-file decode path or the in-memory-bytes
        decode path depending on the type of `file_or_bytes` -- str means
        "filename relative to textures_dir" (the existing OBJ/.mtl
        convention), bytes means "raw embedded image data" (the GLB/
        glTF convention). Both ultimately go through the same Pillow
        Image.open() + vertical-flip + RGB-conversion logic; only how the
        bytes are obtained differs.
        """
        if isinstance(file_or_bytes, bytes):
            return self._decode_from_bytes(file_or_bytes)
        return self._decode_from_disk(file_or_bytes)

    def _decode_from_bytes(self, raw_bytes: bytes) -> Optional[DecodedImage]:
        """Same decode logic as _decode_from_disk, but for image data
        that's already in memory (embedded in the model file) rather
        than a separate file to open from disk -- see GLB/glTF support
        in caveviewer.core.glb_parser."""
        try:
            img = Image.open(BytesIO(raw_bytes)).convert("RGB")
            img = self._apply_texture_dimension_limit(img, "<embedded texture>")
            # same vertical flip as the disk-file path, for the same
            # reason: OBJ/OpenGL UV origin is bottom-left, most image
            # libraries decode top-left first.
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
            return DecodedImage(size=img.size, components=3, data=img.tobytes())
        except Exception as e:
            _LOG.warning(f"failed to decode an embedded texture: {e}")
            return None

    def _decode_from_disk(self, filename: str) -> Optional[DecodedImage]:
        path = os.path.join(self.textures_dir, filename)
        if not os.path.exists(path):
            _LOG.warning(f"texture file missing: {path}")
            return None

        try:
            img = Image.open(path).convert("RGB")
            img = self._apply_texture_dimension_limit(img, filename)
            # flip vertically: OBJ/OpenGL UV origin is bottom-left, most
            # image libraries decode top-left first -- skipping this
            # produces an upside-down or mirrored-look texture that's
            # easy to misdiagnose as a UV bug in the OBJ export itself.
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
            return DecodedImage(size=img.size, components=3, data=img.tobytes())
        except Exception as e:
            # Any decode failure -- a corrupt file, an unsupported/
            # unrecognized format, a truncated download, an image that's
            # still too large even after raising MAX_IMAGE_PIXELS above,
            # or anything else Pillow might raise -- degrades to a
            # missing-texture placeholder instead of taking down the
            # whole viewer. A wrong-looking (magenta) texture on one
            # chunk is recoverable; a crashed app mid-flythrough is not.
            _LOG.warning(f"failed to decode texture '{filename}': {e}")
            return None

    # -- main-thread-only GPU upload step ------------------------------------

    def acquire(self, material_name: str) -> object:
        tex, _timing = self.acquire_with_timing(material_name)
        return tex

    def acquire_with_timing(self, material_name: str) -> tuple[object, dict]:
        """
        Increment refcount for `material_name`'s texture, uploading it to
        the GPU on first use. MUST be called from the main/render thread.

        If decode_for_material() was already called for this material on a
        background thread (the normal streaming path), this just does the
        fast GPU upload of already-decoded pixels. If not (e.g. a texture
        needed before its background decode finished, or this manager is
        used standalone), it falls back to decoding synchronously here --
        slower, but still correct.
        """
        timing = {
            "material": material_name,
            "total_ms": 0.0,
            "material_cache_hit": False,
            "file_cache_hit": False,
            "decoded_cache_hit": False,
            "sync_decode": False,
            "placeholder": False,
            "decode_ms": 0.0,
            "texture_ms": 0.0,
            "mipmap_ms": 0.0,
            "image_bytes": 0,
            "image_size": None,
        }
        start = time.perf_counter()
        if material_name in self._loaded:
            entry = self._loaded[material_name]
            entry.ref_count += 1
            timing["material_cache_hit"] = True
            timing["total_ms"] = (time.perf_counter() - start) * 1000.0
            return entry.moderngl_texture, timing

        file_or_bytes = self.material_to_file.get(material_name)
        if file_or_bytes and file_or_bytes in self._file_cache:
            tex = self._file_cache[file_or_bytes]
            timing["file_cache_hit"] = True
        else:
            tex = self._upload_for_material(
                material_name,
                file_or_bytes,
                timing=timing,
            )
            if file_or_bytes:
                self._file_cache[file_or_bytes] = tex

        self._loaded[material_name] = LoadedTexture(moderngl_texture=tex, ref_count=1)
        timing["total_ms"] = (time.perf_counter() - start) * 1000.0
        return tex, timing

    def _upload_for_material(self, material_name: str, file_or_bytes,
                             *, timing: dict | None = None) -> object:
        if not file_or_bytes:
            if timing is not None:
                timing["placeholder"] = True
            return self._placeholder_texture()

        decoded = None
        with self._decode_cache_lock:
            decoded = self._decode_cache.pop(file_or_bytes, None)
            if decoded is not None:
                self._decode_cache_bytes = max(
                    0,
                    self._decode_cache_bytes - len(decoded.data),
                )
        if decoded is not None and timing is not None:
            timing["decoded_cache_hit"] = True

        if decoded is None:
            # fallback: decode synchronously on the main thread. Slower
            # (this is the case we're trying to avoid via pre-decoding),
            # but correctness matters more than speed here.
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
            timing["texture_ms"] = (time.perf_counter() - t_texture) * 1000.0
        if hasattr(tex, "build_mipmaps"):
            t_mipmap = time.perf_counter()
            tex.build_mipmaps()
            if timing is not None:
                timing["mipmap_ms"] = (time.perf_counter() - t_mipmap) * 1000.0
        return tex

    def release(self, material_name: str) -> None:
        """Decrement refcount; free the GPU texture once it hits zero."""
        entry = self._loaded.get(material_name)
        if entry is None:
            return
        entry.ref_count -= 1
        if entry.ref_count <= 0:
            del self._loaded[material_name]
            # only release the underlying GPU texture if no other material
            # alias still points at the same file
            filename = self.material_to_file.get(material_name)
            if not filename:
                tex = entry.moderngl_texture
                if hasattr(tex, "release"):
                    tex.release()
                return
            still_used = any(
                self.material_to_file.get(m) == filename
                for m in self._loaded
            )
            if filename and not still_used and filename in self._file_cache:
                tex = self._file_cache.pop(filename)
                if hasattr(tex, "release"):
                    tex.release()

    def shutdown(self) -> None:
        """Best-effort full cleanup for window shutdown / map teardown."""
        # Release textures tracked by material refcounts.
        for mat_name in list(self._loaded.keys()):
            # Force release regardless of current count so shutdown is deterministic.
            self._loaded[mat_name].ref_count = 1
            self.release(mat_name)

        # Release any remaining deduplicated file-cache textures.
        for tex in list(self._file_cache.values()):
            if hasattr(tex, "release"):
                tex.release()
        self._file_cache.clear()

        # Drop decoded-but-not-uploaded CPU images.
        with self._decode_cache_lock:
            self._decode_cache.clear()
            self._decode_cache_bytes = 0
            self._decode_inflight.clear()

    def validate_textures(self) -> dict:
        """
        Scan all materials in material_to_file and report which texture
        files are present on disk and which are missing. Safe to call from
        any thread (no OpenGL calls). Intended to be called once after the
        manager is created, before rendering starts.

        Returns a dict with keys:
            'found'   : list of (material_name, filepath) for existing files
            'missing' : list of (material_name, filepath) for missing files
        """
        found = []
        missing = []
        inspected_texture_keys = set()
        inspected_count = 0
        oversized_count = 0
        largest_size = None
        largest_label = None
        for mat_name, file_or_bytes in self.material_to_file.items():
            if file_or_bytes is None:
                continue  # no texture assigned to this material
            texture_key = _texture_cache_key(file_or_bytes)
            if texture_key not in inspected_texture_keys:
                inspected_texture_keys.add(texture_key)
                label = (
                    "<embedded texture>"
                    if isinstance(file_or_bytes, bytes)
                    else str(file_or_bytes)
                )
                size = self._inspect_texture_size(file_or_bytes)
                if size is not None:
                    inspected_count += 1
                    if largest_size is None or max(size) > max(largest_size):
                        largest_size = size
                        largest_label = label
                    if (
                        self.max_texture_dimension is not None
                        and max(size) > self.max_texture_dimension
                    ):
                        oversized_count += 1
            if isinstance(file_or_bytes, bytes):
                continue  # embedded texture, no file to check
            path = os.path.join(self.textures_dir, file_or_bytes)
            if os.path.exists(path):
                found.append((mat_name, path))
            else:
                missing.append((mat_name, path))
                _LOG.warning(f"VALIDATE: missing texture for '{mat_name}' -> '{path}'")

        _LOG.info(f"Validation complete: {len(found)} textures found, {len(missing)} missing.")
        self._log_texture_downscale_expectation(
            inspected_count=inspected_count,
            oversized_count=oversized_count,
            largest_label=largest_label,
            largest_size=largest_size,
        )
        return {"found": found, "missing": missing}

    def _inspect_texture_size(self, file_or_bytes) -> tuple[int, int] | None:
        """Read image dimensions without performing a full decode."""
        try:
            if isinstance(file_or_bytes, bytes):
                with Image.open(BytesIO(file_or_bytes)) as image:
                    return image.size
            path = os.path.join(self.textures_dir, file_or_bytes)
            if not os.path.exists(path):
                return None
            with Image.open(path) as image:
                return image.size
        except Exception as exc:
            _LOG.warning(
                "VALIDATE: unable to inspect texture dimensions for %r: %s",
                file_or_bytes if not isinstance(file_or_bytes, bytes) else "<embedded texture>",
                exc,
            )
            return None

    def _log_texture_downscale_expectation(
        self,
        *,
        inspected_count: int,
        oversized_count: int,
        largest_label: str | None,
        largest_size: tuple[int, int] | None,
    ) -> None:
        """Log whether the current map is expected to downscale textures."""
        if inspected_count <= 0:
            _LOG.info(
                "Texture downscale check skipped: no inspectable texture "
                "dimensions were available."
            )
            return

        largest_detail = (
            f"largest source texture {largest_label!r} is "
            f"{_format_texture_size(largest_size)}"
        )
        if self.max_texture_dimension is None:
            _LOG.info(
                "Texture downscaling is not configured for this map; %d "
                "inspected texture(s) will upload at source dimensions "
                "(%s).",
                inspected_count,
                largest_detail,
            )
            return

        if oversized_count == 0:
            _LOG.info(
                "No texture downscaling will be applied for this map: GPU "
                "resources allow all %d inspected texture(s) to upload at "
                "source resolution (%s; selected limit %d px).",
                inspected_count,
                largest_detail,
                self.max_texture_dimension,
            )
            return

        _LOG.info(
            "Texture downscaling will be applied to %d of %d inspected "
            "texture(s): source textures larger than the selected %d px "
            "limit are resized before GPU upload (%s).",
            oversized_count,
            inspected_count,
            self.max_texture_dimension,
            largest_detail,
        )

    def loaded_count(self) -> int:
        return len(self._file_cache)

    def stats(self) -> dict:
        with self._decode_cache_lock:
            n_decoded_waiting = len(self._decode_cache)
            decoded_waiting_bytes = self._decode_cache_bytes
        return {
            "unique_materials_loaded": len(self._loaded),
            "unique_files_resident": len(self._file_cache),
            "decoded_waiting_for_upload": n_decoded_waiting,
            "decoded_waiting_bytes": decoded_waiting_bytes,
        }
