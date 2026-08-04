"""Explicit construction of offline navigation certificates.

Render-cache construction intentionally stops after render chunks, manifest
metadata, and the Guided Dive cache identity.  Navigation analysis is a
separate, potentially long-running developer operation.  Its artifacts are
published below a cache-local ``navigation_certificate`` directory and are
bound to the immutable render-cache identity without mutating ``manifest.json``.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Any, Literal

import numpy as np

from caveviewer.core.chunking.metadata import cache_dir_is_valid, load_manifest
from caveviewer.core.chunking.staging import (
    _atomic_write_json,
    _publish_cache_directory,
)
from caveviewer.core.diagnostics.logging import (
    configure_logging,
    finish_console_progress_line,
    get_logger,
    set_console_progress,
)
from caveviewer.core.json_io import load_bounded_json
from caveviewer.core.map.cache_identity import (
    GuidedDiveCacheIdentity,
    guided_dive_cache_identity_from_manifest,
    parse_guided_dive_cache_identity,
)
from caveviewer.core.map import source_model
from caveviewer.core.map.cache_paths import MANAGED_CACHE_ENV_VAR, MapCacheLocator
from caveviewer.core.mesh import glb as glb_parser
from caveviewer.core.mesh.obj import parse_obj_vertices
from caveviewer.core.navigation.cache_metadata import build_navigation_metadata
from caveviewer.core.navigation.mesh_collision import CachedChunkMeshCollisionGuard
from caveviewer.core.navigation.voxel_cache import (
    NAVIGATION_CERTIFICATE_DIRECTORY_NAME,
    NAVIGATION_VOXEL_CACHE_NAME,
    build_navigation_voxel_cache,
    first_manifest_chunk_center_for_route_contract,
)


_LOG = get_logger("navigation_certificate")

NAVIGATION_CERTIFICATE_MANIFEST_NAME = "certificate.json"
NAVIGATION_CERTIFICATE_VERSION = 1
NAVIGATION_CERTIFICATE_METHOD = "cache_bound_navigation_certificate_v1"
NAVIGATION_CERTIFICATE_MAX_BYTES = 64 * 1024 * 1024
NAVIGATION_CERTIFICATE_IDENTITY_KEY = "render_cache_identity"
NAVIGATION_CERTIFICATE_RENDER_KEY = "render_cache"
NAVIGATION_CERTIFICATE_NAVIGATION_KEY = "navigation"
NAVIGATION_START_SOURCE_FIRST_MANIFEST_CHUNK = "first_manifest_chunk_center_v1"

ProgressCallback = Callable[[str, float], None]


class NavigationCertificateBuildError(RuntimeError):
    """An expected failure to construct an optional navigation certificate."""


@dataclass(frozen=True)
class NavigationCertificateBuildResult:
    """Machine-readable result of one explicit certificate operation."""

    status: Literal["built", "skipped", "unavailable"]
    source_path: str
    source_format: str
    cache_dir: str
    certificate_dir: str
    route_count: int
    recommended_route_id: str | None
    elapsed_seconds: float

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "source": self.source_path,
            "format": self.source_format,
            "cache_dir": self.cache_dir,
            "certificate_dir": self.certificate_dir,
            "route_count": self.route_count,
            "recommended_route_id": self.recommended_route_id,
            "elapsed_seconds": self.elapsed_seconds,
        }


def build_navigation_certificate(
    source_path: str | os.PathLike[str],
    *,
    cache_dir: str | os.PathLike[str],
    source_format: str | None = None,
    force: bool = False,
    progress_cb: ProgressCallback | None = None,
) -> NavigationCertificateBuildResult:
    """Build an optional, cache-identity-bound navigation certificate.

    The selected render cache must already be current and carry a Guided Dive
    identity.  The render manifest is only read: construction writes a private
    certificate staging directory and atomically replaces the previous
    ``navigation_certificate`` directory after all artifacts are complete.
    """
    started_at = time.perf_counter()
    source = os.path.abspath(os.fspath(source_path))
    cache_path = os.path.abspath(os.fspath(cache_dir))
    resolved_format = (
        _source_format_id(source)
        if source_format is None
        else str(source_format).strip().lower()
    )
    certificate_dir = os.path.join(cache_path, NAVIGATION_CERTIFICATE_DIRECTORY_NAME)

    _emit_progress(progress_cb, "validating render cache", 0.0)
    manifest = _require_render_manifest(cache_path, source)
    render_identity = guided_dive_cache_identity_from_manifest(manifest)
    if render_identity is None:
        raise NavigationCertificateBuildError(
            "navigation certificates require a cache with stable Guided Dive "
            "identity; rebuild the map before certifying"
        )
    _validate_source_binding(manifest, source)

    if not force:
        existing_navigation = load_navigation_certificate(
            manifest,
            cache_dir=cache_path,
        )
        if _has_published_navigation_artifacts(existing_navigation, cache_path):
            _emit_progress(progress_cb, "using current navigation certificate", 1.0)
            return NavigationCertificateBuildResult(
                status="skipped",
                source_path=source,
                source_format=resolved_format,
                cache_dir=cache_path,
                certificate_dir=certificate_dir,
                route_count=_certificate_route_count(existing_navigation),
                recommended_route_id=_recommended_route_id(existing_navigation),
                elapsed_seconds=time.perf_counter() - started_at,
            )

    phase_started_at = time.perf_counter()
    _emit_progress(progress_cb, "reading source positions", 0.08)
    surface_positions = _load_surface_positions(
        source,
        resolved_format,
        progress_cb=progress_cb,
    )
    phase_started_at = _log_phase("source positions read", phase_started_at)

    _emit_progress(progress_cb, "building navigation metadata", 0.32)
    navigation_start, navigation_start_anchor = _navigation_start_metadata_for_source(
        source,
        surface_positions,
        manifest_chunks=manifest.get("chunks"),
    )
    try:
        navigation_metadata = build_navigation_metadata(
            manifest,
            surface_positions=surface_positions,
            navigation_start=navigation_start,
            navigation_start_anchor=navigation_start_anchor,
        )
    except Exception as exc:
        raise NavigationCertificateBuildError(
            f"could not build navigation metadata: {exc}"
        ) from exc
    phase_started_at = _log_phase("navigation metadata built", phase_started_at)

    route_count = 0
    recommended_route_id: str | None = None
    status: Literal["built", "unavailable"] = "unavailable"
    navigation_payload: dict[str, object] = {}
    artifact_payload: dict[str, object] | None = None
    chunk_payloads: Mapping[str, Mapping[str, object]] = {}

    if navigation_metadata is not None:
        certificate_manifest = dict(manifest)
        certificate_manifest["navigation"] = navigation_metadata
        mesh_guard = CachedChunkMeshCollisionGuard.from_manifest(
            certificate_manifest,
            cache_dir=cache_path,
        )
        if mesh_guard is None:
            raise NavigationCertificateBuildError(
                "render cache chunks are unavailable for navigation certification"
            )

        def voxel_progress(stage: str, fraction: float) -> None:
            _emit_progress(
                progress_cb,
                stage,
                0.40 + 0.50 * max(0.0, min(1.0, float(fraction))),
            )

        phase_started_at = time.perf_counter()
        _emit_progress(progress_cb, "certifying navigation routes", 0.40)
        try:
            voxel_result = build_navigation_voxel_cache(
                certificate_manifest,
                navigation_metadata,
                triangle_provider=mesh_guard.triangle_meshes_for_bounds,
                mesh_edge_is_clear=lambda first, second: (
                    mesh_guard.segment_collision(first, second) is None
                ),
                mesh_point_has_opposing_support=(
                    lambda point, max_distance_m, minimum_clearance_m: bool(
                        mesh_guard.opposing_axis_support(
                            point,
                            max_distance_m=max_distance_m,
                            minimum_clearance_m=minimum_clearance_m,
                        )
                    )
                ),
                progress_cb=voxel_progress,
            )
        except Exception as exc:
            raise NavigationCertificateBuildError(
                f"navigation route certification failed: {exc}"
            ) from exc
        _log_phase("navigation routes certified", phase_started_at)
        route_count = int(voxel_result.built_route_count)
        recommended_route_id = voxel_result.recommended_route_id
        navigation_payload = _certificate_navigation_metadata(navigation_metadata)
        if route_count:
            if (
                voxel_result.chunked_payload is None
                or not voxel_result.chunk_payloads
            ):
                raise NavigationCertificateBuildError(
                    "navigation route certification did not produce bounded "
                    "chunked certificate artifacts"
                )
            artifact_payload = dict(voxel_result.chunked_payload)
            chunk_payloads = voxel_result.chunk_payloads
            status = "built"
    else:
        _LOG.info("Navigation certificate metadata has no eligible routes.")

    _emit_progress(progress_cb, "publishing navigation certificate", 0.94)
    phase_started_at = time.perf_counter()
    _publish_navigation_certificate(
        cache_path,
        render_identity=render_identity,
        render_manifest=manifest,
        navigation_metadata=navigation_payload,
        artifact_payload=artifact_payload,
        chunk_payloads=chunk_payloads,
        status=status,
    )
    _log_phase("navigation certificate published", phase_started_at)
    _emit_progress(progress_cb, "done", 1.0)

    elapsed_seconds = time.perf_counter() - started_at
    _LOG.info(
        "Navigation certificate %s in %.2fs: %s",
        status,
        elapsed_seconds,
        certificate_dir,
    )
    return NavigationCertificateBuildResult(
        status=status,
        source_path=source,
        source_format=resolved_format,
        cache_dir=cache_path,
        certificate_dir=certificate_dir,
        route_count=route_count,
        recommended_route_id=recommended_route_id,
        elapsed_seconds=elapsed_seconds,
    )


def load_navigation_certificate(
    manifest: Mapping[str, Any] | None,
    *,
    cache_dir: str | os.PathLike[str],
) -> dict[str, object] | None:
    """Load navigation metadata only when it is bound to this render cache.

    A malformed, stale, or missing certificate is deliberately ignored here.
    Render cache validity remains independent from an optional certificate.
    """
    if not isinstance(manifest, Mapping):
        return None
    render_identity = guided_dive_cache_identity_from_manifest(manifest)
    if render_identity is None:
        return None
    path = _certificate_manifest_path(cache_dir)
    try:
        payload = load_bounded_json(
            path,
            max_bytes=NAVIGATION_CERTIFICATE_MAX_BYTES,
            description="navigation certificate",
        )
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    if (
        payload.get("version") != NAVIGATION_CERTIFICATE_VERSION
        or payload.get("method") != NAVIGATION_CERTIFICATE_METHOD
    ):
        return None
    certificate_identity = parse_guided_dive_cache_identity(
        payload.get(NAVIGATION_CERTIFICATE_IDENTITY_KEY)
    )
    if certificate_identity != render_identity:
        return None
    if not _certificate_render_metadata_matches(payload, manifest):
        return None
    navigation = payload.get(NAVIGATION_CERTIFICATE_NAVIGATION_KEY)
    return dict(navigation) if isinstance(navigation, Mapping) else None


def manifest_with_navigation_certificate(
    manifest: Mapping[str, Any],
    *,
    cache_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    """Return an in-memory manifest augmented with a bound certificate.

    Existing legacy manifests that already contain navigation metadata remain
    readable.  New render manifests are never rewritten to carry it.
    """
    effective = dict(manifest)
    if isinstance(effective.get("navigation"), Mapping):
        return effective
    navigation = load_navigation_certificate(effective, cache_dir=cache_dir)
    if navigation is not None:
        effective["navigation"] = navigation
    return effective


def resolve_navigation_certificate_cache_dir(
    source_path: str | os.PathLike[str],
    *,
    cache_root: str | os.PathLike[str] | None = None,
) -> str:
    """Resolve the standard generated cache directory for an input source."""
    source = os.path.abspath(os.fspath(source_path))
    if cache_root is None:
        locator = MapCacheLocator()
    else:
        expanded_root = os.path.expanduser(os.fspath(cache_root))
        if not os.path.isabs(expanded_root):
            raise NavigationCertificateBuildError(
                "--cache-root must be an absolute path"
            )
        root = os.path.abspath(expanded_root)
        environ = dict(os.environ)
        environ[MANAGED_CACHE_ENV_VAR] = root
        locator = MapCacheLocator(environ=environ)
    return os.path.abspath(str(locator.build_cache_dir(source)))


def resolve_navigation_certificate_source(
    value: str | os.PathLike[str],
) -> tuple[str, str]:
    """Resolve a supported source file without requiring import companions."""
    selected = os.path.abspath(os.path.expanduser(os.fspath(value)))
    if os.path.isfile(selected):
        source_format = source_model.source_format_for_path(selected)
        if source_format is None:
            raise NavigationCertificateBuildError(
                f"unsupported source model: {selected}"
            )
        return selected, source_format.id.value
    if not os.path.isdir(selected):
        raise NavigationCertificateBuildError(f"source path does not exist: {selected}")
    candidates = source_model.find_supported_source_files(selected)
    if not candidates:
        raise NavigationCertificateBuildError(
            f"no supported source model found in: {selected}"
        )
    selected_candidate = candidates[0]
    if len(candidates) > 1:
        _LOG.info(
            "Multiple source models found; using %s.",
            selected_candidate.path,
        )
    return (
        os.path.abspath(selected_candidate.path),
        selected_candidate.source_format.id.value,
    )


def _require_render_manifest(cache_dir: str, source_path: str) -> dict[str, Any]:
    manifest = load_manifest(cache_dir)
    if not isinstance(manifest, dict):
        raise NavigationCertificateBuildError(
            f"render cache manifest is missing or unreadable: {cache_dir}"
        )
    if not cache_dir_is_valid(cache_dir, source_path):
        raise NavigationCertificateBuildError(
            "render cache is missing, invalid, or older than its source; "
            "rebuild the map before certifying"
        )
    return manifest


def _validate_source_binding(manifest: Mapping[str, Any], source_path: str) -> None:
    source_name = os.path.basename(source_path)
    cached_name = manifest.get("source_obj")
    if isinstance(cached_name, str) and cached_name and cached_name != source_name:
        raise NavigationCertificateBuildError(
            "selected source does not match the render cache manifest: "
            f"expected {cached_name!r}, got {source_name!r}"
        )


def _load_surface_positions(
    source_path: str,
    source_format: str,
    *,
    progress_cb: ProgressCallback | None,
) -> np.ndarray:
    def source_progress(_stage: str, fraction: float) -> None:
        _emit_progress(
            progress_cb,
            "reading source positions",
            0.08 + 0.20 * max(0.0, min(1.0, float(fraction))),
        )

    if source_format == source_model.SourceFormatId.OBJ.value:
        positions = parse_obj_vertices(
            source_path,
            progress_cb=source_progress,
        ).positions
    elif source_format == source_model.SourceFormatId.GLB.value:
        mesh, _embedded_textures = glb_parser.parse_glb(
            source_path,
            progress_cb=source_progress,
        )
        positions = mesh.positions
    else:
        raise NavigationCertificateBuildError(
            f"unsupported navigation certificate source format: {source_format!r}"
        )
    array = np.asarray(positions)
    if array.ndim != 2 or array.shape[1] != 3 or not len(array):
        raise NavigationCertificateBuildError(
            "source model has no usable XYZ vertex positions"
        )
    if not bool(np.isfinite(array).all()):
        raise NavigationCertificateBuildError(
            "source model has non-finite vertex positions"
        )
    return array


def _navigation_start_metadata_for_source(
    source_path: str,
    surface_positions: np.ndarray | None,
    *,
    manifest_chunks: object = None,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Return authored or source-order entrance evidence for certification."""
    anchor = _obj_navigation_start_anchor(source_path, surface_positions)
    sidecar = _navigation_start_sidecar_for_obj(source_path)
    if sidecar is not None:
        return sidecar, anchor
    if anchor is not None:
        return None, anchor
    position = first_manifest_chunk_center_for_route_contract(manifest_chunks)
    if position is None:
        return None, None
    return {
        "position": [float(value) for value in position],
        "label": "Cave start",
        "source": NAVIGATION_START_SOURCE_FIRST_MANIFEST_CHUNK,
    }, None


def _obj_navigation_start_anchor(
    source_path: str,
    surface_positions: np.ndarray | None,
) -> dict[str, object] | None:
    if os.path.splitext(source_path)[1].casefold() != ".obj":
        return None
    if surface_positions is None:
        return None
    try:
        positions = np.asarray(surface_positions)
        if positions.ndim != 2 or positions.shape[1] != 3 or not len(positions):
            return None
        position = [float(value) for value in positions[0]]
    except (TypeError, ValueError):
        return None
    if not bool(np.isfinite(position).all()):
        return None
    return {
        "position": position,
        "kind": "obj_surface_vertex",
        "source": os.path.basename(source_path),
        "source_vertex_index": 0,
        "source_order": "obj_declaration_order",
        "executable": False,
        "attachment_required": True,
        "attachment_coordinate_space": "xyz",
    }


def _navigation_start_sidecar_for_obj(source_path: str) -> dict[str, object] | None:
    base_path, _extension = os.path.splitext(os.path.abspath(source_path))
    source_dir = os.path.dirname(os.path.abspath(source_path))
    candidate_paths = (
        f"{base_path}.navigation.json",
        os.path.join(source_dir, "navigation.json"),
    )
    seen: set[str] = set()
    for path in candidate_paths:
        if path in seen:
            continue
        seen.add(path)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            _LOG.warning("Could not read navigation sidecar %s: %s", path, exc)
            return None
        if not isinstance(payload, Mapping):
            _LOG.warning("Ignoring navigation sidecar %s: expected a JSON object.", path)
            return None
        result = dict(payload)
        result.setdefault("source", os.path.basename(path))
        return result
    return None


def _certificate_navigation_metadata(
    navigation_metadata: Mapping[str, object],
) -> dict[str, object]:
    """Copy metadata and point voxel artifacts at the certificate directory."""
    result = dict(navigation_metadata)
    descriptor = result.get("voxel_cache")
    if not isinstance(descriptor, Mapping):
        return result
    certificate_descriptor = dict(descriptor)
    certificate_descriptor["path"] = (
        f"{NAVIGATION_CERTIFICATE_DIRECTORY_NAME}/{NAVIGATION_VOXEL_CACHE_NAME}"
    )
    certificate_descriptor["chunk_directory"] = (
        f"{NAVIGATION_CERTIFICATE_DIRECTORY_NAME}/navigation_voxel_chunks"
    )
    result["voxel_cache"] = certificate_descriptor
    return result


def _publish_navigation_certificate(
    cache_dir: str,
    *,
    render_identity: GuidedDiveCacheIdentity,
    render_manifest: Mapping[str, Any],
    navigation_metadata: Mapping[str, object],
    artifact_payload: Mapping[str, object] | None,
    chunk_payloads: Mapping[str, Mapping[str, object]],
    status: str,
) -> None:
    staging_dir = tempfile.mkdtemp(
        prefix=f".{NAVIGATION_CERTIFICATE_DIRECTORY_NAME}.tmp-{os.getpid()}-",
        dir=cache_dir,
    )
    certificate_dir = os.path.join(cache_dir, NAVIGATION_CERTIFICATE_DIRECTORY_NAME)
    try:
        if artifact_payload is not None:
            for relative_path, payload in chunk_payloads.items():
                destination = _safe_certificate_staging_path(staging_dir, relative_path)
                _atomic_write_json(destination, dict(payload))
            _atomic_write_json(
                os.path.join(staging_dir, NAVIGATION_VOXEL_CACHE_NAME),
                dict(artifact_payload),
            )
        certificate_payload = {
            "version": NAVIGATION_CERTIFICATE_VERSION,
            "method": NAVIGATION_CERTIFICATE_METHOD,
            "status": str(status),
            NAVIGATION_CERTIFICATE_IDENTITY_KEY: render_identity.payload(),
            NAVIGATION_CERTIFICATE_RENDER_KEY: _render_metadata_payload(render_manifest),
            NAVIGATION_CERTIFICATE_NAVIGATION_KEY: dict(navigation_metadata),
        }
        _atomic_write_json(
            os.path.join(staging_dir, NAVIGATION_CERTIFICATE_MANIFEST_NAME),
            certificate_payload,
        )
        _publish_cache_directory(staging_dir, certificate_dir)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _safe_certificate_staging_path(staging_dir: str, relative_path: object) -> str:
    if not isinstance(relative_path, str) or not relative_path:
        raise NavigationCertificateBuildError(
            "navigation voxel chunk path is missing or invalid"
        )
    normalized = os.path.normpath(relative_path)
    if (
        os.path.isabs(normalized)
        or normalized == os.pardir
        or normalized.startswith(os.pardir + os.sep)
        or not normalized.startswith("navigation_voxel_chunks" + os.sep)
    ):
        raise NavigationCertificateBuildError(
            f"unsafe navigation voxel chunk path: {relative_path!r}"
        )
    root = os.path.abspath(staging_dir)
    destination = os.path.abspath(os.path.join(root, normalized))
    try:
        if os.path.commonpath((root, destination)) != root:
            raise NavigationCertificateBuildError(
                f"unsafe navigation voxel chunk path: {relative_path!r}"
            )
    except ValueError as exc:
        raise NavigationCertificateBuildError(
            f"unsafe navigation voxel chunk path: {relative_path!r}"
        ) from exc
    return destination


def _certificate_manifest_path(cache_dir: str | os.PathLike[str]) -> str:
    return os.path.join(
        os.path.abspath(os.fspath(cache_dir)),
        NAVIGATION_CERTIFICATE_DIRECTORY_NAME,
        NAVIGATION_CERTIFICATE_MANIFEST_NAME,
    )


def _certificate_render_metadata_matches(
    certificate: Mapping[str, object],
    manifest: Mapping[str, Any],
) -> bool:
    render = certificate.get(NAVIGATION_CERTIFICATE_RENDER_KEY)
    if not isinstance(render, Mapping):
        return False
    expected = _render_metadata_payload(manifest)
    return all(render.get(key) == value for key, value in expected.items())


def _render_metadata_payload(manifest: Mapping[str, Any]) -> dict[str, object]:
    return {
        "source_obj": manifest.get("source_obj"),
        "manifest_version": manifest.get("version"),
        "chunk_size": manifest.get("chunk_size"),
        "triangle_count": manifest.get("triangle_count"),
    }


def _has_published_navigation_artifacts(
    navigation: Mapping[str, object] | None,
    cache_dir: str,
) -> bool:
    if not isinstance(navigation, Mapping):
        return False
    descriptor = navigation.get("voxel_cache")
    if not isinstance(descriptor, Mapping):
        return False
    expected_path = (
        f"{NAVIGATION_CERTIFICATE_DIRECTORY_NAME}/{NAVIGATION_VOXEL_CACHE_NAME}"
    )
    expected_chunks = (
        f"{NAVIGATION_CERTIFICATE_DIRECTORY_NAME}/navigation_voxel_chunks"
    )
    if (
        descriptor.get("path") != expected_path
        or descriptor.get("chunk_directory") != expected_chunks
    ):
        return False
    return (
        os.path.isfile(os.path.join(cache_dir, expected_path))
        and os.path.isdir(os.path.join(cache_dir, expected_chunks))
    )


def _certificate_route_count(navigation: Mapping[str, object] | None) -> int:
    if not isinstance(navigation, Mapping):
        return 0
    descriptor = navigation.get("voxel_cache")
    if not isinstance(descriptor, Mapping):
        return 0
    try:
        return max(0, int(descriptor.get("built_route_count", 0)))
    except (TypeError, ValueError):
        return 0


def _recommended_route_id(navigation: Mapping[str, object] | None) -> str | None:
    if not isinstance(navigation, Mapping):
        return None
    value = navigation.get("recommended_route_id")
    return value if isinstance(value, str) and value else None


def _source_format_id(source_path: str) -> str:
    source_format = source_model.source_format_for_path(source_path)
    if source_format is None:
        raise NavigationCertificateBuildError(
            f"unsupported navigation certificate source: {source_path}"
        )
    return source_format.id.value


def _emit_progress(
    progress_cb: ProgressCallback | None,
    stage: str,
    fraction: float,
) -> None:
    if progress_cb is not None:
        progress_cb(stage, max(0.0, min(1.0, float(fraction))))


def _log_phase(phase: str, started_at: float) -> float:
    _LOG.info(
        "Navigation certificate phase %s completed in %.2fs.",
        phase,
        time.perf_counter() - started_at,
    )
    return time.perf_counter()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build an optional offline navigation certificate for an existing "
            "CaveViewer render cache."
        )
    )
    parser.add_argument(
        "--source",
        required=True,
        help="OBJ/GLB source file, or folder containing one.",
    )
    cache_group = parser.add_mutually_exclusive_group()
    cache_group.add_argument(
        "--cache-dir",
        help="Explicit completed render-cache directory.",
    )
    cache_group.add_argument(
        "--cache-root",
        help="Optional absolute root for managed compiled map caches.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild even when a bound navigation certificate is current.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the explicit navigation-certificate builder CLI."""
    args = _parser().parse_args(argv)
    progress_cb = None if args.json else set_console_progress
    if not args.json:
        configure_logging()
    try:
        source_path, source_format = resolve_navigation_certificate_source(args.source)
        cache_dir = (
            os.path.abspath(os.path.expanduser(args.cache_dir))
            if args.cache_dir
            else resolve_navigation_certificate_cache_dir(
                source_path,
                cache_root=args.cache_root,
            )
        )
        result = build_navigation_certificate(
            source_path,
            cache_dir=cache_dir,
            source_format=source_format,
            force=bool(args.force),
            progress_cb=progress_cb,
        )
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except NavigationCertificateBuildError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if progress_cb is not None:
            finish_console_progress_line()

    if args.json:
        print(json.dumps(result.as_dict(), sort_keys=True))
    else:
        _print_result(result)
    return 0 if result.status in {"built", "skipped"} else 1


def _print_result(result: NavigationCertificateBuildResult) -> None:
    heading = {
        "built": "Navigation certificate built:",
        "skipped": "Navigation certificate is current:",
        "unavailable": "Navigation certificate unavailable:",
    }[result.status]
    print(heading)
    print(f"  Cache: {result.cache_dir}")
    print(f"  Certificate: {result.certificate_dir}")
    print(f"  Source: {result.source_path}")
    print(f"  Certified routes: {result.route_count:,}")
    if result.recommended_route_id:
        print(f"  Recommended route: {result.recommended_route_id}")
    print(f"  Elapsed: {result.elapsed_seconds:.1f}s")


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
