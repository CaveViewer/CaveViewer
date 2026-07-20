"""Worker-safe texture decoding, inspection, and budget policy.

This module intentionally contains no Tk, OpenGL, or GPU-object ownership.
Worker threads may use :class:`TextureDecodeCache` to inspect and decode image
files into immutable CPU-side bytes. The GUI/render layer is responsible for
turning those bytes into OpenGL textures on the context-owning thread.
"""

from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

from PIL import Image

from caveviewer.core.diagnostics.logging import get_logger


# Pillow's default decompression-bomb guard rejects images above ~179 million
# pixels. Photogrammetry texture atlases can legitimately exceed that, but a
# billion-pixel image is still outside what this runtime path should decode.
Image.MAX_IMAGE_PIXELS = 1_000_000_000

_LOG = get_logger("TextureDecode")

TEXTURE_MAX_SIZE_ENV_VAR = "CAVEVIEWER_MAX_TEXTURE_SIZE"
TEXTURE_BUDGET_SHARE = 0.80
TEXTURE_BYTES_PER_PIXEL_WITH_MIPS = 4.0 * (4.0 / 3.0)
AUTO_TEXTURE_DIMENSION_STEPS = (16384, 8192, 4096, 2048, 1024, 512)
MAX_TEXTURE_DIMENSION_LIMIT = AUTO_TEXTURE_DIMENSION_STEPS[0]
MIN_TEXTURE_DIMENSION_LIMIT = 512
DEFAULT_DECODE_CACHE_BYTES = 256 * 1024 ** 2
MIN_DECODE_CACHE_BYTES = 32 * 1024 ** 2
MAX_DECODE_CACHE_BYTES = 512 * 1024 ** 2
DECODE_CACHE_AVAILABLE_RAM_FRACTION = 0.05
DEFAULT_RESIDENT_TEXTURE_CACHE_BYTES = 512 * 1024 ** 2
MIN_RESIDENT_TEXTURE_CACHE_BYTES = 16 * 1024 ** 2
MAX_DECODED_TEXTURE_BYTES = (
    MAX_TEXTURE_DIMENSION_LIMIT
    * MAX_TEXTURE_DIMENSION_LIMIT
    * 3
)


@dataclass
class DecodedImage:
    """Plain CPU-side texture payload, safe to pass between Python threads."""

    size: tuple[int, int]
    components: int
    data: bytes


def texture_cache_key(file_or_bytes) -> object | None:
    if not file_or_bytes:
        return None
    if isinstance(file_or_bytes, bytes):
        return ("embedded", len(file_or_bytes), hash(file_or_bytes))
    return ("file", str(file_or_bytes))


def resolve_texture_path(textures_dir: str, filename: str) -> str:
    """Resolve a material texture path without escaping ``textures_dir``."""
    raw_filename = str(filename)
    normalized = os.path.normpath(raw_filename)
    if (
        not raw_filename.strip()
        or os.path.isabs(normalized)
        or normalized == os.pardir
        or normalized.startswith(os.pardir + os.sep)
    ):
        raise ValueError(f"Unsafe texture path: {raw_filename!r}")

    root = os.path.abspath(textures_dir)
    resolved = os.path.abspath(os.path.join(root, normalized))
    try:
        common = os.path.commonpath((root, resolved))
    except ValueError as exc:
        raise ValueError(f"Unsafe texture path: {raw_filename!r}") from exc
    if common != root:
        raise ValueError(f"Unsafe texture path: {raw_filename!r}")
    return resolved


def parse_texture_dimension_limit(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    text = raw_value.strip()
    if not text:
        return None
    try:
        value = int(float(text))
    except ValueError:
        return None
    return max(
        MIN_TEXTURE_DIMENSION_LIMIT,
        min(MAX_TEXTURE_DIMENSION_LIMIT, value),
    )


def format_texture_size(size: tuple[int, int] | None) -> str:
    if size is None:
        return "unknown"
    return f"{size[0]}x{size[1]}"


def recommend_max_texture_dimension(
    material_to_file: dict,
    gpu_memory_bytes: int | None,
    gpu_target_fraction: float,
) -> int | None:
    """Choose a decode-time texture cap for the whole map."""
    explicit_raw_value = os.environ.get(TEXTURE_MAX_SIZE_ENV_VAR)
    explicit_limit = parse_texture_dimension_limit(explicit_raw_value)
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
        for key in (texture_cache_key(value) for value in material_to_file.values())
        if key is not None
    }
    if not unique_texture_keys:
        _LOG.info(
            "Texture max dimension cap not selected because the map has no "
            "unique texture files."
        )
        return None

    target_fraction = max(0.01, min(0.80, float(gpu_target_fraction)))
    texture_budget_bytes = gpu_memory_bytes * target_fraction * TEXTURE_BUDGET_SHARE
    bytes_per_texture = texture_budget_bytes / max(1, len(unique_texture_keys))
    max_pixels = bytes_per_texture / TEXTURE_BYTES_PER_PIXEL_WITH_MIPS
    if max_pixels <= 0:
        return MIN_TEXTURE_DIMENSION_LIMIT

    raw_dimension = int(math.sqrt(max_pixels))
    for index, step in enumerate(AUTO_TEXTURE_DIMENSION_STEPS):
        # Treat values close to a common texture size as that size. This avoids
        # useless 4096 -> 3990 resizes when the budget is only a few percent
        # below the original estimate.
        if raw_dimension >= int(step * 0.875):
            next_larger_step = (
                AUTO_TEXTURE_DIMENSION_STEPS[index - 1]
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
                TEXTURE_BUDGET_SHARE * 100.0,
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
        MIN_TEXTURE_DIMENSION_LIMIT,
        gpu_memory_bytes / (1024 ** 3),
        target_fraction * 100.0,
        TEXTURE_BUDGET_SHARE * 100.0,
        texture_budget_bytes / (1024 ** 3),
        len(unique_texture_keys),
        bytes_per_texture / (1024 ** 2),
        raw_dimension,
    )
    return MIN_TEXTURE_DIMENSION_LIMIT


def recommend_resident_texture_cache_bytes(
    gpu_memory_bytes: int | None,
    gpu_target_fraction: float,
) -> int:
    """Choose the resident GPU texture cache budget."""
    if gpu_memory_bytes is None or gpu_memory_bytes <= 0:
        return DEFAULT_RESIDENT_TEXTURE_CACHE_BYTES
    target_fraction = max(0.01, min(0.80, float(gpu_target_fraction)))
    return max(
        MIN_RESIDENT_TEXTURE_CACHE_BYTES,
        int(gpu_memory_bytes * target_fraction * TEXTURE_BUDGET_SHARE),
    )


def recommend_decoded_cache_bytes(available_ram_bytes: int | None) -> int:
    """Choose a CPU-side texture predecode cache cap from available RAM."""
    if available_ram_bytes is None or available_ram_bytes <= 0:
        return DEFAULT_DECODE_CACHE_BYTES
    return max(
        MIN_DECODE_CACHE_BYTES,
        min(
            MAX_DECODE_CACHE_BYTES,
            int(available_ram_bytes * DECODE_CACHE_AVAILABLE_RAM_FRACTION),
        ),
    )


def normalize_decoded_cache_limit(max_decoded_cache_bytes: int | None) -> int:
    if max_decoded_cache_bytes is None:
        return DEFAULT_DECODE_CACHE_BYTES
    try:
        value = int(max_decoded_cache_bytes)
    except (TypeError, ValueError):
        return DEFAULT_DECODE_CACHE_BYTES
    return max(1, value)


def normalize_resident_texture_cache_limit(
    max_resident_texture_bytes: int | None,
) -> int:
    if max_resident_texture_bytes is None:
        return DEFAULT_RESIDENT_TEXTURE_CACHE_BYTES
    try:
        value = int(max_resident_texture_bytes)
    except (TypeError, ValueError):
        return DEFAULT_RESIDENT_TEXTURE_CACHE_BYTES
    return max(1, value)


def estimate_gpu_texture_bytes(size: tuple[int, int] | None) -> int:
    if size is None:
        return 4
    width, height = size
    return max(
        4,
        int(math.ceil(width * height * TEXTURE_BYTES_PER_PIXEL_WITH_MIPS)),
    )


class TextureDecodeCache:
    """Thread-safe CPU texture decode cache.

    ``decode_for_material()`` is safe for streaming workers. It may block on a
    condition when another worker is already decoding the same source, and
    ``shutdown()`` wakes those waiters. No method in this class creates,
    modifies, or releases OpenGL resources.
    """

    def __init__(
        self,
        textures_dir: str,
        material_to_file: dict,
        max_texture_dimension: int | None = None,
        max_decoded_cache_bytes: int | None = None,
        max_resident_texture_bytes: int | None = None,
    ):
        self.textures_dir = textures_dir
        # Own a stable snapshot. Background predecode workers read this map
        # while the render thread may upload/release textures; retaining the
        # caller's mutable dict would make that boundary depend on outside
        # synchronization.
        self.material_to_file = dict(material_to_file)
        self.max_texture_dimension = parse_texture_dimension_limit(
            str(max_texture_dimension) if max_texture_dimension else None
        )
        self.max_decoded_cache_bytes = normalize_decoded_cache_limit(
            max_decoded_cache_bytes
        )
        self.max_resident_texture_bytes = normalize_resident_texture_cache_limit(
            max_resident_texture_bytes
        )
        self._texture_downscale_logged = False
        self._decode_cache_limit_logged = False
        self._state_lock = threading.RLock()
        self._shutdown = False
        self._resident_sources: set[object] = set()
        self._texture_size_cache: dict[object, tuple[int, int] | None] = {}

        # Decoded-but-not-yet-uploaded images, populated by background
        # workers and consumed by the render-thread texture manager.
        self._decode_cache: dict[object, DecodedImage] = {}
        self._decode_cache_bytes = 0
        self._decode_inflight_reserved_bytes = 0
        self._decode_inflight: set[object] = set()
        self._decode_cache_lock = threading.Condition(self._state_lock)
        _LOG.info(
            "Texture predecode cache cap active: %.1f MB. Oversized or "
            "over-budget textures will decode on demand at original resolution.",
            self.max_decoded_cache_bytes / (1024 ** 2),
        )
        if self.max_texture_dimension is not None:
            if self.max_texture_dimension >= MAX_TEXTURE_DIMENSION_LIMIT:
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

    def source_for_material(self, material_name: str):
        return self.material_to_file.get(material_name)

    def decode_for_material(self, material_name: str) -> None:
        """
        Decode the image for ``material_name`` into CPU bytes if useful.

        This method is worker-thread safe. It returns without decoding when the
        source is already decoded, currently being decoded by another worker,
        or marked resident by the render-thread texture manager.
        """
        file_or_bytes = self.source_for_material(material_name)
        if not file_or_bytes:
            return
        with self._state_lock:
            if self._shutdown:
                return
            if file_or_bytes in self._resident_sources:
                return
        estimated_bytes = self._estimate_decoded_image_bytes(file_or_bytes)
        skipped_for_decode_cache_limit = False
        while True:
            with self._decode_cache_lock:
                if self._shutdown:
                    return
                if file_or_bytes in self._decode_cache:
                    return
                if file_or_bytes in self._decode_inflight:
                    self._decode_cache_lock.wait()
                    continue
                if file_or_bytes in self._resident_sources:
                    return
                if estimated_bytes is None:
                    estimated_bytes = self.max_decoded_cache_bytes
                committed_decode_bytes = (
                    self._decode_cache_bytes
                    + self._decode_inflight_reserved_bytes
                )
                if (
                    estimated_bytes > self.max_decoded_cache_bytes
                    or committed_decode_bytes + estimated_bytes
                    > self.max_decoded_cache_bytes
                ):
                    skipped_for_decode_cache_limit = True
                    break
                self._decode_inflight_reserved_bytes += estimated_bytes
                self._decode_inflight.add(file_or_bytes)
                break

        if skipped_for_decode_cache_limit:
            self._log_decode_cache_skip(file_or_bytes, estimated_bytes)
            return

        decoded = None
        try:
            decoded = self.decode_source(file_or_bytes)
        except Exception as exc:
            label = (
                "<embedded texture>"
                if isinstance(file_or_bytes, bytes)
                else str(file_or_bytes)
            )
            _LOG.warning("texture predecode failed unexpectedly for %r: %s", label, exc)
        finally:
            # Always clear inflight state and notify waiters. This prevents one
            # decoder exception from leaving workers parked forever.
            with self._decode_cache_lock:
                self._decode_inflight.discard(file_or_bytes)
                self._decode_inflight_reserved_bytes = max(
                    0,
                    self._decode_inflight_reserved_bytes - estimated_bytes,
                )
                if decoded is not None:
                    actual_bytes = len(decoded.data)
                    # If the render thread uploaded while this worker decoded,
                    # discard the redundant CPU payload.
                    if (
                        file_or_bytes not in self._decode_cache
                        and file_or_bytes not in self._resident_sources
                        and not self._shutdown
                        and actual_bytes <= self.max_decoded_cache_bytes
                        and (
                            self._decode_cache_bytes
                            + self._decode_inflight_reserved_bytes
                            + actual_bytes
                            <= self.max_decoded_cache_bytes
                        )
                    ):
                        self._decode_cache[file_or_bytes] = decoded
                        self._decode_cache_bytes += actual_bytes
                self._decode_cache_lock.notify_all()

    def pop_decoded(self, file_or_bytes) -> DecodedImage | None:
        """Return and remove a decoded CPU payload for render-thread upload."""
        with self._decode_cache_lock:
            decoded = self._decode_cache.pop(file_or_bytes, None)
            if decoded is not None:
                self._decode_cache_bytes = max(
                    0,
                    self._decode_cache_bytes - len(decoded.data),
                )
                self._decode_cache_lock.notify_all()
            return decoded

    def mark_texture_resident(self, file_or_bytes) -> None:
        """Tell workers this source already has a GPU-resident texture."""
        if not file_or_bytes:
            return
        with self._decode_cache_lock:
            self._resident_sources.add(file_or_bytes)
            decoded = self._decode_cache.pop(file_or_bytes, None)
            if decoded is not None:
                self._decode_cache_bytes = max(
                    0,
                    self._decode_cache_bytes - len(decoded.data),
                )
            self._decode_cache_lock.notify_all()

    def mark_texture_nonresident(self, file_or_bytes) -> None:
        """Tell workers this source no longer has a GPU-resident texture."""
        if not file_or_bytes:
            return
        with self._decode_cache_lock:
            self._resident_sources.discard(file_or_bytes)
            self._decode_cache_lock.notify_all()

    def estimate_resident_bytes_for_source(self, file_or_bytes) -> int | None:
        size = self.inspect_texture_size(file_or_bytes)
        if size is None:
            return None
        return estimate_gpu_texture_bytes(self._size_after_texture_limit(size))

    def _log_decode_cache_skip(self, file_or_bytes, estimated_bytes: int) -> None:
        with self._state_lock:
            if self._decode_cache_limit_logged:
                return
            available_cache_bytes = max(
                0,
                self.max_decoded_cache_bytes
                - self._decode_cache_bytes
                - self._decode_inflight_reserved_bytes,
            )
            self._decode_cache_limit_logged = True
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
            available_cache_bytes / (1024 ** 2),
        )

    def _estimate_decoded_image_bytes(self, file_or_bytes) -> int | None:
        size = self.inspect_texture_size(file_or_bytes)
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

    def _max_decoded_image_bytes(self) -> int:
        resident_decode_budget = int(
            max(1, self.max_resident_texture_bytes)
            * (3.0 / TEXTURE_BYTES_PER_PIXEL_WITH_MIPS)
        )
        return max(
            1,
            min(
                MAX_DECODED_TEXTURE_BYTES,
                max(self.max_decoded_cache_bytes, resident_decode_budget),
            ),
        )

    def _prepare_image_for_decode(
        self,
        image: Image.Image,
        source_label: str,
    ) -> Image.Image | None:
        target_width, target_height = self._size_after_texture_limit(image.size)
        decoded_bytes = target_width * target_height * 3
        max_decoded_bytes = self._max_decoded_image_bytes()
        if decoded_bytes > max_decoded_bytes:
            _LOG.warning(
                "Skipping texture %r: decoded RGB size after configured "
                "downscale would be %.1f MB, above the %.1f MB runtime "
                "safety limit.",
                source_label,
                decoded_bytes / (1024 ** 2),
                max_decoded_bytes / (1024 ** 2),
            )
            return None
        return self._apply_texture_dimension_limit(image, source_label)

    def _apply_texture_dimension_limit(
        self, image: Image.Image, source_label: str
    ) -> Image.Image:
        limit = self.max_texture_dimension
        if limit is None or max(image.size) <= limit:
            return image

        original_size = image.size
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        image.thumbnail((limit, limit), resampling)
        with self._state_lock:
            should_log_downscale = not self._texture_downscale_logged
            if should_log_downscale:
                self._texture_downscale_logged = True
        if should_log_downscale:
            _LOG.info(
                "Downscaling oversized textures to fit GPU budget; first resize "
                "%r: %dx%d -> %dx%d.",
                source_label,
                original_size[0],
                original_size[1],
                image.size[0],
                image.size[1],
            )
        return image

    def decode_source(self, file_or_bytes) -> Optional[DecodedImage]:
        """
        Decode one texture source into vertically flipped RGB bytes.

        A ``str`` value is treated as a filename relative to ``textures_dir``;
        ``bytes`` is treated as embedded image data from formats such as GLB.
        """
        if isinstance(file_or_bytes, bytes):
            return self._decode_from_bytes(file_or_bytes)
        return self._decode_from_disk(file_or_bytes)

    def _decode_from_bytes(self, raw_bytes: bytes) -> Optional[DecodedImage]:
        try:
            with Image.open(BytesIO(raw_bytes)) as image:
                prepared = self._prepare_image_for_decode(
                    image,
                    "<embedded texture>",
                )
                if prepared is None:
                    return None
                img = prepared.convert("RGB")
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
            return DecodedImage(size=img.size, components=3, data=img.tobytes())
        except Exception as e:
            _LOG.warning(f"failed to decode an embedded texture: {e}")
            return None

    def _decode_from_disk(self, filename: str) -> Optional[DecodedImage]:
        try:
            path = resolve_texture_path(self.textures_dir, filename)
        except ValueError as exc:
            _LOG.warning("%s", exc)
            return None
        if not os.path.exists(path):
            _LOG.warning(f"texture file missing: {path}")
            return None

        try:
            with Image.open(path) as image:
                prepared = self._prepare_image_for_decode(image, filename)
                if prepared is None:
                    return None
                img = prepared.convert("RGB")
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
            return DecodedImage(size=img.size, components=3, data=img.tobytes())
        except Exception as e:
            # A corrupt, unsupported, truncated, or too-large texture degrades
            # to the render-layer placeholder instead of taking down the viewer.
            _LOG.warning(f"failed to decode texture '{filename}': {e}")
            return None

    def validate_textures(self) -> dict:
        """
        Scan material texture references and log missing/oversized inputs.

        This performs file existence checks and dimension inspection only; it
        does not create or release OpenGL resources.
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
                continue
            texture_key = texture_cache_key(file_or_bytes)
            if texture_key not in inspected_texture_keys:
                inspected_texture_keys.add(texture_key)
                label = (
                    "<embedded texture>"
                    if isinstance(file_or_bytes, bytes)
                    else str(file_or_bytes)
                )
                size = self.inspect_texture_size(file_or_bytes)
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
                continue
            try:
                path = resolve_texture_path(self.textures_dir, file_or_bytes)
            except ValueError as exc:
                _LOG.warning("%s", exc)
                missing.append((mat_name, str(file_or_bytes)))
                continue
            if os.path.exists(path):
                found.append((mat_name, path))
            else:
                missing.append((mat_name, path))
                _LOG.warning(f"VALIDATE: missing texture for '{mat_name}' -> '{path}'")

        _LOG.info(
            f"Validation complete: {len(found)} textures found, {len(missing)} missing."
        )
        self._log_texture_downscale_expectation(
            inspected_count=inspected_count,
            oversized_count=oversized_count,
            largest_label=largest_label,
            largest_size=largest_size,
        )
        return {"found": found, "missing": missing}

    def inspect_texture_size(self, file_or_bytes) -> tuple[int, int] | None:
        """Read image dimensions without performing a full decode."""
        texture_key = texture_cache_key(file_or_bytes)
        with self._state_lock:
            if texture_key is not None and texture_key in self._texture_size_cache:
                return self._texture_size_cache[texture_key]

        size: tuple[int, int] | None = None
        try:
            if isinstance(file_or_bytes, bytes):
                with Image.open(BytesIO(file_or_bytes)) as image:
                    size = image.size
            else:
                path = resolve_texture_path(self.textures_dir, file_or_bytes)
                if os.path.exists(path):
                    with Image.open(path) as image:
                        size = image.size
        except Exception as exc:
            _LOG.warning(
                "VALIDATE: unable to inspect texture dimensions for %r: %s",
                file_or_bytes if not isinstance(file_or_bytes, bytes) else "<embedded texture>",
                exc,
            )
        with self._state_lock:
            if texture_key is None:
                return size
            if texture_key not in self._texture_size_cache:
                self._texture_size_cache[texture_key] = size
            return self._texture_size_cache[texture_key]

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
            f"{format_texture_size(largest_size)}"
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

    def shutdown(self) -> None:
        """Wake workers and discard decoded-but-not-uploaded CPU payloads."""
        with self._decode_cache_lock:
            self._shutdown = True
            self._resident_sources.clear()
            self._decode_cache.clear()
            self._decode_cache_bytes = 0
            self._decode_inflight_reserved_bytes = 0
            self._decode_inflight.clear()
            self._decode_cache_lock.notify_all()

    def stats(self) -> dict:
        with self._decode_cache_lock:
            return {
                "decoded_waiting_for_upload": len(self._decode_cache),
                "decoded_waiting_bytes": self._decode_cache_bytes,
                "decode_inflight_reserved_bytes": self._decode_inflight_reserved_bytes,
            }


# Backwards-compatible private aliases for existing focused tests. They remain
# CPU-only and do not import or expose GUI/OpenGL types.
_texture_cache_key = texture_cache_key
_parse_texture_dimension_limit = parse_texture_dimension_limit
_format_texture_size = format_texture_size
