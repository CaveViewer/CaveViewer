"""Neutral HTTPS payload transfer with cancellation and cleanup guarantees."""

from __future__ import annotations

import hashlib
import os
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from caveviewer.core.diagnostics.logging import get_logger


_LOG = get_logger("DownloadTransport")
_TRANSFER_TIMEOUT_SECONDS = 30
_TRANSFER_CHUNK_SIZE = 65_536
_HASH_CHUNK_SIZE = 1_024 * 1_024


class DownloadCancelled(Exception):
    """Raised when a caller cooperatively cancels an in-progress transfer."""


def download_file(
    download_url: str,
    expected_size_bytes: int | None,
    dest_path: str,
    expected_sha256: str | None = None,
    progress_cb: Callable[[int, int | None], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    *,
    user_agent: str,
    ssl_context: ssl.SSLContext,
    label: str = "payload",
    urlopen: Callable[..., Any] | None = None,
) -> None:
    """Transfer one explicitly configured file without owning release policy.

    The caller supplies its own user agent and normally-verifying TLS context,
    keeping transport separate from update or map-catalog configuration. A
    cancelled or failed started transfer removes only its partial destination;
    a pre-existing destination remains untouched if opening the request fails.
    """
    request = urllib.request.Request(
        download_url,
        headers={"User-Agent": user_agent},
    )
    active_urlopen = urlopen or urllib.request.urlopen
    _LOG.info(
        "Downloading %s: url=%s, expected_size=%s, sha256_expected=%s",
        label,
        download_url,
        expected_size_bytes,
        bool(expected_sha256),
    )
    download_started = False

    def remove_partial_download() -> None:
        if not download_started:
            return
        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
        except OSError as cleanup_exc:
            _LOG.warning(
                "could not remove partial %s %s: %s",
                label,
                dest_path,
                cleanup_exc,
            )

    def raise_if_cancelled() -> None:
        if cancel_cb and cancel_cb():
            raise DownloadCancelled("Download cancelled")

    try:
        raise_if_cancelled()
        with active_urlopen(
            request,
            timeout=_TRANSFER_TIMEOUT_SECONDS,
            context=ssl_context,
        ) as response:
            total = (
                expected_size_bytes
                or int(response.headers.get("Content-Length", 0))
                or None
            )
            downloaded = 0

            raise_if_cancelled()
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as file_obj:
                download_started = True
                while True:
                    raise_if_cancelled()
                    chunk = response.read(_TRANSFER_CHUNK_SIZE)
                    if not chunk:
                        raise_if_cancelled()
                        break
                    raise_if_cancelled()
                    file_obj.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total)
                    raise_if_cancelled()
            raise_if_cancelled()
    except DownloadCancelled:
        remove_partial_download()
        _LOG.info("Download cancelled; removed partial %s: %s", label, dest_path)
        raise
    except urllib.error.HTTPError as exc:
        remove_partial_download()
        _LOG.warning(
            "%s download failed with HTTP %s: %s",
            label,
            exc.code,
            download_url,
        )
        raise
    except urllib.error.URLError as exc:
        remove_partial_download()
        _LOG.warning("%s download failed: %s", label, exc)
        raise
    except OSError as exc:
        remove_partial_download()
        _LOG.warning(
            "%s download failed while writing %s: %s",
            label,
            dest_path,
            exc,
        )
        raise

    try:
        if phase_cb:
            phase_cb("verifying")
        raise_if_cancelled()

        actual_size = os.path.getsize(dest_path)
        _LOG.info("Downloaded %s: bytes=%d, path=%s", label, actual_size, dest_path)
        if expected_size_bytes is not None and actual_size != expected_size_bytes:
            _LOG.warning(
                "%s security check failed: size mismatch actual=%d expected=%d",
                label,
                actual_size,
                expected_size_bytes,
            )
            os.remove(dest_path)
            raise IOError(
                f"Downloaded file size ({actual_size} bytes) doesn't match the "
                f"expected size ({expected_size_bytes} bytes) -- the download may "
                f"have been interrupted or corrupted. Please try again."
            )

        if expected_sha256:
            _LOG.info("Verifying %s SHA-256.", label)
            sha256 = hashlib.sha256()
            with open(dest_path, "rb") as file_obj:
                for chunk in iter(lambda: file_obj.read(_HASH_CHUNK_SIZE), b""):
                    raise_if_cancelled()
                    sha256.update(chunk)
            raise_if_cancelled()
            actual_sha = sha256.hexdigest().lower()
            if actual_sha != expected_sha256.strip().lower():
                _LOG.warning(
                    "%s security check failed: SHA-256 mismatch actual=%s expected=%s",
                    label,
                    actual_sha,
                    expected_sha256.strip().lower(),
                )
                os.remove(dest_path)
                raise IOError(
                    "Downloaded file hash doesn't match the expected SHA-256. "
                    "The download may be corrupted or tampered."
                )
            _LOG.info("%s security check passed: SHA-256 verified.", label)
        else:
            _LOG.warning(
                "%s security check skipped: no expected SHA-256 provided.",
                label,
            )
    except DownloadCancelled:
        # Verification may hold the payload open when cancellation is first
        # observed. This handler runs after that file handle is closed, which
        # also makes cleanup reliable on Windows.
        remove_partial_download()
        _LOG.info("Verification cancelled; removed %s: %s", label, dest_path)
        raise
