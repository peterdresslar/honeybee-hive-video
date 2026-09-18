"""Download raw 2019 hive videos from the Edmond (Dataverse) archive.

The source dataset is "Videos for honey bee lifetime tracking data 2019",
published at doi:10.17617/3.LLWRWR. Every file in it is public, so no API
token is required.

Files are named on the archive like::

    start47__20190731_184423_side1_top.mp4

This module addresses them either by the archive filename or by the
``start`` / ``side`` / ``panel`` triple, where ``start`` is the archive's
sequential capture identifier (``--start 47`` selects ``start47``). It also
exposes a canonical *key* used everywhere downstream::

    start47_20190731_184423_side1_top

That key is the naming pattern the resequencing outputs follow
(``reseq_<key>``), so slurm scripts can resolve a locator once and derive
every later path from it.

Examples::

    # Download one file into the current directory.
    hive-video download --start 4 --side 1 --panel top

    # Download by archive filename into an explicit target.
    hive-video download \\
        --filename start47__20190731_184423_side1_top.mp4 \\
        --target /scratch/pdressla/honey-bee/downloads

    # Resolve a slurm locator to its key and local path without downloading.
    hive-video download --locator start47_side1_top --resolve-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import certifi

DEFAULT_SERVER = "https://edmond.mpg.de"
DEFAULT_DOI = "doi:10.17617/3.LLWRWR"

# start47__20190731_184423_side1_top.mp4
FILENAME_RE = re.compile(
    r"^start(?P<start>\d+)__(?P<date>\d{8})_(?P<time>\d{6})_"
    r"side(?P<side>\d)_(?P<panel>[a-z]+)\.mp4$"
)
# start47_side1_top
LOCATOR_RE = re.compile(r"^start(?P<start>\d+)_side(?P<side>[01])_(?P<panel>top|bottom)$")

CHUNK_BYTES = 8 * 1024 * 1024
USER_AGENT = "hive-video-download-raw/1.0"
SYSTEM_CA_BUNDLE_CANDIDATES = (
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/ssl/cert.pem"),
)


@dataclass(frozen=True)
class RemoteFile:
    """One file in the Edmond dataset."""

    file_id: int
    filename: str
    size: int
    md5: str
    start: int
    date: str
    time: str
    side: int
    panel: str

    @property
    def key(self) -> str:
        """Canonical key, e.g. ``start47_20190731_184423_side1_top``."""
        return f"start{self.start:02d}_{self.date}_{self.time}_side{self.side}_{self.panel}"

    @property
    def locator(self) -> str:
        """Compact slurm-friendly locator, e.g. ``start47_side1_top``."""
        return f"start{self.start}_side{self.side}_{self.panel}"

    @property
    def reseq_dirname(self) -> str:
        """Output folder name used by the resequencing pipeline."""
        return f"reseq_{self.key}"


class HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect before contacting a non-HTTPS destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme.lower() != "https":
            fp.close()
            raise RuntimeError(
                "Refused a non-HTTPS redirect from the Edmond archive before contacting it."
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_cache_path(doi: str, server: str = DEFAULT_SERVER) -> Path:
    """Keep file IDs from different archive servers and datasets in separate caches."""
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "hive_video"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", doi).strip("_")
    identity = json.dumps([server.rstrip("/"), doi]).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return root / f"edmond_manifest_{slug}_{digest}.json"


def build_ssl_context(
    ca_bundle: Path | None = None,
) -> tuple[ssl.SSLContext, tuple[Path, ...]]:
    """Build one verifying TLS context for manifest and media requests.

    uv-managed standalone Python builds do not always discover a Linux
    distribution's CA bundle. Keep Python's default roots, then add certifi,
    the common system bundle locations, and any explicitly configured bundle.
    """
    configured: list[tuple[Path, bool]] = []
    if ca_bundle is not None:
        configured.append((ca_bundle.expanduser(), True))
    environment_bundle = os.environ.get("SSL_CERT_FILE")
    if environment_bundle:
        configured.append((Path(environment_bundle).expanduser(), True))
    configured.append((Path(certifi.where()), True))
    configured.extend((candidate, False) for candidate in SYSTEM_CA_BUNDLE_CANDIDATES)

    for candidate, required in configured:
        if required and not candidate.is_file():
            raise FileNotFoundError(f"Configured CA bundle is missing or not a file: {candidate}")

    context = ssl.create_default_context()
    loaded: list[Path] = []
    seen: set[Path] = set()
    for candidate, _required in configured:
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        context.load_verify_locations(cafile=str(resolved))
        loaded.append(resolved)
        seen.add(resolved)
    return context, tuple(loaded)


def _is_certificate_verification_error(error: urllib.error.URLError) -> bool:
    return isinstance(error.reason, ssl.SSLCertVerificationError)


def _http_get(
    url: str,
    timeout: float,
    headers: dict[str, str] | None,
    ssl_context: ssl.SSLContext,
    *,
    operation: str = "archive request",
):
    if urllib.parse.urlparse(url).scheme.lower() != "https":
        raise ValueError("Refused a non-HTTPS archive request.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    opener = urllib.request.build_opener(
        HTTPSOnlyRedirectHandler(),
        urllib.request.HTTPSHandler(context=ssl_context),
    )
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.URLError as error:
        if isinstance(error.reason, socket.gaierror):
            # The opener may be resolving a redirect or proxy, not the original
            # server. Omit URL credentials and queries (including signed URLs).
            host = urllib.parse.urlparse(url).hostname
            raise urllib.error.URLError(
                f"DNS/name resolution failed during {operation} "
                f"(requested host: {host!r}; socket.gaierror errno={error.reason.errno}). "
                "The lookup may involve this server, a redirect destination, or a configured proxy. "
                "Check DNS, network access, and proxy settings on the node running this command. "
                "This is not a TLS certificate error; changing CA bundles will not fix it. "
                "Use --probe-only --refresh-manifest to check archive and media access "
                "without downloading the video."
            ) from error
        if _is_certificate_verification_error(error):
            raise RuntimeError(
                "TLS certificate verification failed for the Edmond archive. "
                "The downloader kept verification enabled and loaded Python, certifi, "
                "and available system roots. If the cluster requires another trust root, "
                "provide it with --ca-bundle or SSL_CERT_FILE."
            ) from error
        raise


def fetch_manifest(
    server: str,
    doi: str,
    timeout: float,
    ssl_context: ssl.SSLContext,
) -> list[dict]:
    """Fetch the dataset file listing from the Dataverse native API."""
    url = (
        f"{server}/api/datasets/:persistentId/versions/:latest/files"
        f"?persistentId={urllib.parse.quote(doi, safe=':.')}"
    )
    with _http_get(url, timeout, None, ssl_context, operation="archive manifest") as response:
        payload = json.load(response)
    if payload.get("status") != "OK":
        raise RuntimeError(f"Dataverse API returned status {payload.get('status')!r} for {doi}")
    return payload["data"]


def load_manifest(
    server: str,
    doi: str,
    cache_path: Path,
    refresh: bool,
    timeout: float,
    ssl_context: ssl.SSLContext | None = None,
) -> list[RemoteFile]:
    """Load the file listing, using a local cache unless ``refresh`` is set.

    The published dataset is immutable, so caching avoids one API round trip
    per array task.
    """
    raw: list[dict] | None = None
    if cache_path.exists() and not refresh:
        try:
            raw = json.loads(cache_path.read_text())
            if not isinstance(raw, list):
                raw = None
        except (OSError, json.JSONDecodeError):
            raw = None
    if raw is None:
        if ssl_context is None:
            ssl_context, _ca_files = build_ssl_context()
        raw = fetch_manifest(server, doi, timeout, ssl_context)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=cache_path.parent,
                prefix=f".{cache_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(raw, handle)
                tmp = Path(handle.name)
            tmp.replace(cache_path)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    files: list[RemoteFile] = []
    for entry in raw:
        data_file = entry["dataFile"]
        name = data_file["filename"]
        match = FILENAME_RE.match(name)
        if match is None:
            continue  # non-video assets, e.g. the camera diagram PNG
        files.append(
            RemoteFile(
                file_id=int(data_file["id"]),
                filename=name,
                size=int(data_file.get("filesize", 0)),
                md5=str(data_file.get("md5") or data_file.get("checksum", {}).get("value", "")),
                start=int(match.group("start")),
                date=match.group("date"),
                time=match.group("time"),
                side=int(match.group("side")),
                panel=match.group("panel"),
            )
        )
    return files


def select_file(
    files: list[RemoteFile],
    filename: str | None,
    start: int | None,
    side: int | None,
    panel: str | None,
) -> RemoteFile:
    """Resolve a single archive file, failing loudly when the request is ambiguous."""
    if filename is not None:
        for candidate in files:
            if candidate.filename == filename:
                return candidate
        raise ValueError(f"No archive file named {filename!r} in this dataset.")

    if start is None or side is None or panel is None:
        raise ValueError("Specify a filename or all of start, side, and panel.")

    matches = [f for f in files if f.start == start and f.side == side and f.panel == panel]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        for_start = [f for f in files if f.start == start]
        if not for_start:
            starts = sorted({f.start for f in files})
            if not starts:
                raise ValueError("No video files were found in the archive manifest.")
            raise ValueError(
                f"No start{start:02d} in this dataset. Available start identifiers: "
                f"{starts[0]}-{starts[-1]} ({len(starts)} captures)."
            )
        sides = sorted({f.side for f in for_start})
        panels = sorted({f.panel for f in for_start})
        raise ValueError(
            f"No start{start:02d} side{side} {panel!r}. For start{start:02d} the archive has "
            f"sides {sides} and panels {panels}."
        )
    names = ", ".join(f.filename for f in matches)
    raise ValueError(f"Ambiguous selection, matched several files: {names}")


def md5sum(path: Path, chunk_bytes: int = CHUNK_BYTES) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(block)
    return digest.hexdigest()


def _format_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(count) < 1024.0:
            return f"{count:.1f}{unit}"
        count /= 1024.0
    return f"{count:.1f}PB"


def _report(on_message: Callable[[str], None] | None, message: str) -> None:
    if on_message is not None:
        on_message(message)


def download(
    remote: RemoteFile,
    destination: Path,
    server: str,
    timeout: float,
    retries: int,
    progress_seconds: float,
    ssl_context: ssl.SSLContext,
    *,
    on_message: Callable[[str], None] | None = None,
) -> None:
    """Download ``remote`` to ``destination``, resuming a partial ``.part`` file.

    Edmond redirects large files to presigned S3 URLs that honour HTTP range
    requests, so an interrupted transfer resumes rather than restarting.
    """
    part = destination.with_suffix(destination.suffix + ".part")
    url = f"{server}/api/access/datafile/{remote.file_id}"

    for attempt in range(1, retries + 1):
        have = part.stat().st_size if part.exists() else 0
        if remote.size and have > remote.size:
            _report(
                on_message,
                f"  partial file is larger than expected ({have} > {remote.size}); restarting",
            )
            part.unlink()
            have = 0
        if remote.size and have == remote.size:
            break

        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with _http_get(
                url, timeout, headers, ssl_context, operation="media download"
            ) as response:
                if have and response.status != 206:
                    # Server ignored the range request; start over rather than corrupt.
                    _report(
                        on_message,
                        f"  server returned {response.status} for a range request; restarting",
                    )
                    part.unlink(missing_ok=True)
                    have = 0
                mode = "ab" if have else "wb"
                started = time.monotonic()
                last_report = started
                written = have
                with part.open(mode) as handle:
                    while True:
                        block = response.read(CHUNK_BYTES)
                        if not block:
                            break
                        handle.write(block)
                        written += len(block)
                        now = time.monotonic()
                        if now - last_report >= progress_seconds:
                            rate = (written - have) / max(now - started, 1e-6)
                            pct = f"{100.0 * written / remote.size:5.1f}%" if remote.size else "?"
                            _report(
                                on_message,
                                f"  {pct} {_format_bytes(written)}"
                                f"/{_format_bytes(remote.size)} at {_format_bytes(rate)}/s",
                            )
                            last_report = now
            if not remote.size or part.stat().st_size == remote.size:
                break
            _report(on_message, f"  short read ({part.stat().st_size}/{remote.size}); retrying")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            if attempt == retries:
                raise
            backoff = min(60.0, 2.0**attempt)
            _report(
                on_message,
                f"  attempt {attempt}/{retries} failed ({error}); retrying in {backoff:.0f}s",
            )
            time.sleep(backoff)
    else:
        raise RuntimeError(f"Giving up on {remote.filename} after {retries} attempts.")

    part.replace(destination)


def probe_download(
    remote: RemoteFile,
    server: str,
    timeout: float,
    ssl_context: ssl.SSLContext,
) -> None:
    """Verify the media endpoint and its redirects while reading only one byte."""
    url = f"{server}/api/access/datafile/{remote.file_id}"
    with _http_get(
        url, timeout, {"Range": "bytes=0-0"}, ssl_context, operation="media probe"
    ) as response:
        status = getattr(response, "status", None)
        if status != 206:
            raise RuntimeError(
                f"Media probe expected HTTP 206 but received {status} for {remote.filename}"
            )
        content_range = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes 0-0/(\d+)", content_range)
        if match is None:
            raise RuntimeError(
                f"Media probe returned invalid Content-Range {content_range!r} "
                f"for {remote.filename}"
            )
        response_size = int(match.group(1))
        if remote.size and response_size != remote.size:
            raise RuntimeError(
                f"Media probe size mismatch for {remote.filename}: "
                f"manifest={remote.size}, endpoint={response_size}"
            )
        final_url = response.geturl()
        if (
            urllib.parse.urlparse(server).scheme == "https"
            and urllib.parse.urlparse(final_url).scheme != "https"
        ):
            raise RuntimeError(f"Media probe refused an HTTPS downgrade for {remote.filename}")
        if response.read(1) == b"":
            raise RuntimeError(f"Media probe returned no data for {remote.filename}")


def resolve_selection(
    *,
    locator: str | None,
    start: int | None,
    side: int | None,
    panel: str | None,
) -> tuple[int | None, int | None, str | None]:
    """Normalise --locator into the start/side/panel triple."""
    if locator is not None:
        match = LOCATOR_RE.match(locator)
        if match is None:
            raise ValueError(
                f"Could not parse locator {locator!r}. Expected e.g. 'start47_side1_top'."
            )
        return (
            int(match.group("start")),
            int(match.group("side")),
            match.group("panel"),
        )
    return start, side, panel


def resolve_video(
    *,
    manifest_cache: str | Path,
    server: str = DEFAULT_SERVER,
    doi: str = DEFAULT_DOI,
    refresh_manifest: bool,
    timeout: float,
    locator: str | None = None,
    filename: str | None = None,
    start: int | None = None,
    side: int | None = None,
    panel: str | None = None,
    ca_bundle: str | Path | None = None,
) -> RemoteFile:
    """Resolve one archive file without downloading its media.

    Select by locator, exact filename, or complete start/side/panel triple.
    Server and DOI default to the CLI's Edmond archive. A missing or refreshed
    manifest cache fetches the listing using verified HTTPS. The return value
    records the published identity, byte count, and MD5 for ``download_video``.
    """
    start, side, panel = resolve_selection(locator=locator, start=start, side=side, panel=panel)
    if filename is None and (start is None or side is None or panel is None):
        raise ValueError("Specify filename, locator, or all of start, side, and panel.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be finite and positive, observed {timeout!r}")
    context, _ca_files = build_ssl_context(None if ca_bundle is None else Path(ca_bundle))
    files = load_manifest(
        server, doi, Path(manifest_cache).expanduser(), refresh_manifest, timeout, context
    )
    return select_file(files, filename, start, side, panel)


def _validate_download_settings(
    timeout: float, retries: int, progress_seconds: float, verify: bool, force: bool
) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be finite and positive, observed {timeout!r}")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 1:
        raise ValueError(f"retries must be a positive integer, observed {retries!r}")
    if not math.isfinite(progress_seconds) or progress_seconds <= 0:
        raise ValueError(
            f"progress_seconds must be finite and positive, observed {progress_seconds!r}"
        )
    if not isinstance(verify, bool) or not isinstance(force, bool):
        raise ValueError(f"verify and force must be booleans, observed {verify!r}, {force!r}")


def _download_selected(
    remote: RemoteFile,
    *,
    target: Path,
    server: str,
    timeout: float,
    retries: int,
    progress_seconds: float,
    verify: bool,
    force: bool,
    ssl_context: ssl.SSLContext,
    on_message: Callable[[str], None] | None,
) -> Path:
    if Path(remote.filename).name != remote.filename:
        raise ValueError(f"Archive filename must be a basename, observed {remote.filename!r}")
    destination = target.expanduser() / remote.filename
    if destination.exists() and not destination.is_file():
        raise ValueError(f"Download destination exists and is not a file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _report(
        on_message, f"resolved {remote.locator} -> {remote.filename} ({remote.size / 1e9:.1f} GB)"
    )
    _report(on_message, f"target   {destination}")
    if destination.exists() and not force:
        if not verify:
            _report(on_message, "already present, skipping (MD5 not checked)")
            return destination
        if destination.stat().st_size == remote.size and md5sum(destination) == remote.md5:
            _report(on_message, "already present and MD5 matches, skipping")
            return destination
        _report(on_message, "existing file does not match the archive checksum; re-downloading")
    download(
        remote,
        destination,
        server,
        timeout,
        retries,
        progress_seconds,
        ssl_context,
        on_message=on_message,
    )
    if verify:
        _report(on_message, "verifying MD5")
        digest = md5sum(destination)
        if digest != remote.md5:
            raise RuntimeError(f"MD5 mismatch: got {digest}, archive says {remote.md5}")
        _report(on_message, "MD5 OK")
    _report(on_message, f"done: {destination}")
    return destination


def download_video(
    remote: RemoteFile | None = None,
    *,
    target: str | Path,
    day: int | None = None,
    side: int | None = None,
    panel: str | None = None,
    manifest_cache: str | Path | None = None,
    server: str = DEFAULT_SERVER,
    doi: str = DEFAULT_DOI,
    refresh_manifest: bool = False,
    timeout: float = 120.0,
    retries: int = 8,
    progress_seconds: float = 60.0,
    verify: bool = True,
    force: bool = False,
    ca_bundle: str | Path | None = None,
    on_message: Callable[[str], None] | None = None,
) -> Path:
    """Download one recording by day/side/panel or a previously resolved identity.

    ``day`` selects the archive's sequential ``start<N>`` identifier, not an
    elapsed or calendar day. Direct selection requires all three selectors;
    ``day=22, side=0, panel="top"`` selects the Start 22 side-0 top recording.
    The filename timestamp identifies its calendar date. Manifest caching
    defaults to the user cache directory, keyed by server and DOI; an explicit
    ``manifest_cache`` is used as given. Missing or refreshed caches fetch the
    archive listing before the media transfer.

    Alternatively, pass a ``RemoteFile`` from ``resolve_video`` after inspecting
    its identity and size. Do not combine it with selectors or resolution-only
    settings. When overriding the resolution server, pass the same server here.

    Uses the established resumable ``.part`` transfer and archive MD5 check.
    Defaults match the CLI: Edmond, a 120-second timeout, eight attempts, and
    progress messages every 60 seconds when ``on_message`` is supplied. MD5
    verification is enabled and forced downloading is disabled. Verified
    existing files are reused; mismatched files are re-downloaded.
    ``verify=False`` explicitly skips checksum verification. The returned path
    retains the target's relative or absolute form. This API is quiet unless
    ``on_message`` is supplied; errors propagate.
    """
    _validate_download_settings(timeout, retries, progress_seconds, verify, force)
    if not isinstance(refresh_manifest, bool):
        raise ValueError(f"refresh_manifest must be a boolean, observed {refresh_manifest!r}")
    if on_message is not None and not callable(on_message):
        raise ValueError("on_message must be callable or None")
    target = Path(target)
    if remote is None:
        if day is None or side is None or panel is None:
            raise ValueError("Specify all of day, side, and panel, or pass a resolved RemoteFile.")
        if isinstance(day, bool) or not isinstance(day, int) or day < 1:
            raise ValueError(f"day must be a positive integer archive identifier, observed {day!r}")
        if isinstance(side, bool) or not isinstance(side, int) or side not in (0, 1):
            raise ValueError(f"side must be integer 0 or 1, observed {side!r}")
        if panel not in ("top", "bottom"):
            raise ValueError(f"panel must be 'top' or 'bottom', observed {panel!r}")
        cache_path = (
            default_cache_path(doi, server=server)
            if manifest_cache is None
            else Path(manifest_cache).expanduser()
        )
    else:
        if not isinstance(remote, RemoteFile):
            raise ValueError(
                f"remote must be a resolved RemoteFile, observed {type(remote).__name__}"
            )
        if any(value is not None for value in (day, side, panel)):
            raise ValueError("A resolved RemoteFile cannot be combined with day, side, or panel.")
        if manifest_cache is not None or doi != DEFAULT_DOI or refresh_manifest:
            raise ValueError(
                "A resolved RemoteFile cannot be combined with manifest_cache, "
                "a non-default doi, or refresh_manifest."
            )
    context, _ca_files = build_ssl_context(None if ca_bundle is None else Path(ca_bundle))
    if remote is None:
        files = load_manifest(server, doi, cache_path, refresh_manifest, timeout, context)
        remote = select_file(files, None, day, side, panel)
    return _download_selected(
        remote,
        target=target,
        server=server,
        timeout=timeout,
        retries=retries,
        progress_seconds=progress_seconds,
        verify=verify,
        force=force,
        ssl_context=context,
        on_message=on_message,
    )


def build_parser(*, prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Download a raw 2019 hive video from the Edmond archive by start/side/panel "
            "or by archive filename."
        ),
    )
    selector = parser.add_argument_group("file selection")
    selector.add_argument(
        "--start",
        type=int,
        help="Sequential archive capture identifier: --start 47 selects start47.",
    )
    selector.add_argument("--side", type=int, choices=(0, 1), help="Hive side.")
    selector.add_argument(
        "--panel",
        dest="panel",
        choices=("top", "bottom"),
        help="Camera panel.",
    )
    selector.add_argument(
        "--filename",
        help="Exact archive filename, overrides start/side/panel.",
    )
    selector.add_argument(
        "--locator",
        help="Compact locator used by the slurm arrays, e.g. 'start47_side1_top'.",
    )

    parser.add_argument(
        "--target",
        type=Path,
        default=Path.cwd(),
        help="Directory to download into. Defaults to the current directory.",
    )
    parser.add_argument(
        "--resolve-only",
        action="store_true",
        help="Print the resolved key, filename and local path, then exit without downloading.",
    )
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help=(
            "Resolve the selection, verify the media TLS/redirect path with a one-byte "
            "range request, then exit without creating a download."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("json", "sh"),
        default="json",
        help=(
            "Output shape for --resolve-only. 'sh' emits RESEQ_* shell assignments "
            "suitable for eval in a slurm script."
        ),
    )
    parser.add_argument("--list", action="store_true", help="List archive files and exit.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even when a verified local copy already exists.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the MD5 check. Faster, but a truncated file will go unnoticed.",
    )

    source = parser.add_argument_group("archive source")
    source.add_argument("--server", default=DEFAULT_SERVER)
    source.add_argument("--doi", default=DEFAULT_DOI)
    source.add_argument("--manifest-cache", type=Path, default=None)
    source.add_argument("--refresh-manifest", action="store_true")
    source.add_argument(
        "--ca-bundle",
        type=Path,
        default=None,
        help=(
            "Additional PEM CA bundle. TLS verification remains enabled. "
            "SSL_CERT_FILE is also honored."
        ),
    )
    source.add_argument("--timeout", type=float, default=120.0)
    source.add_argument("--retries", type=int, default=8)
    source.add_argument("--progress-every-seconds", type=float, default=60.0)
    return parser


def _run_cli(args: argparse.Namespace) -> int:
    selected_modes = sum((args.list, args.resolve_only, args.probe_only))
    if selected_modes > 1:
        raise ValueError("Choose only one of --list, --resolve-only, or --probe-only.")
    _validate_download_settings(
        args.timeout, args.retries, args.progress_every_seconds, not args.no_verify, args.force
    )

    ssl_context, ca_files = build_ssl_context(args.ca_bundle)
    print(
        "TLS verification roots: Python defaults plus " + ", ".join(str(path) for path in ca_files),
        file=sys.stderr,
    )
    cache_path = args.manifest_cache or default_cache_path(args.doi, server=args.server)
    files = load_manifest(
        args.server,
        args.doi,
        cache_path,
        args.refresh_manifest,
        args.timeout,
        ssl_context,
    )

    if args.list:
        for entry in sorted(files, key=lambda f: (f.start, f.side, f.panel)):
            print(f"{entry.key}\t{entry.locator}\t{entry.size / 1e9:.1f}GB\t{entry.filename}")
        return 0

    start, side, panel = resolve_selection(
        locator=args.locator, start=args.start, side=args.side, panel=args.panel
    )
    if args.filename is None and (start is None or side is None or panel is None):
        raise ValueError("Specify --filename, or --locator, or all of --start, --side and --panel.")

    remote = select_file(files, args.filename, start, side, panel)
    destination = args.target.expanduser() / remote.filename

    if args.resolve_only:
        resolved = {
            "key": remote.key,
            "locator": remote.locator,
            "filename": remote.filename,
            "path": str(destination),
            "reseq_dirname": remote.reseq_dirname,
            "file_id": remote.file_id,
            "size": remote.size,
            "md5": remote.md5,
        }
        if args.format == "sh":
            for name, value in resolved.items():
                # Avoid RESEQ_RESEQ_DIRNAME; the prefix already carries that meaning.
                suffix = name.removeprefix("reseq_").upper()
                print(f"RESEQ_{suffix}={shlex.quote(str(value))}")
        else:
            print(json.dumps(resolved, indent=2))
        return 0

    if args.probe_only:
        print(f"probing  {remote.locator} -> {remote.filename}")
        probe_download(remote, args.server, args.timeout, ssl_context)
        print("media TLS/redirect probe OK")
        return 0

    _download_selected(
        remote,
        target=args.target,
        server=args.server,
        timeout=args.timeout,
        retries=args.retries,
        progress_seconds=args.progress_every_seconds,
        verify=not args.no_verify,
        force=args.force,
        ssl_context=ssl_context,
        on_message=lambda message: print(message, flush=True),
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser(prog="hive-video download")
    args = parser.parse_args(argv)
    try:
        return _run_cli(args)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"{parser.prog}: error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"{parser.prog}: cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
