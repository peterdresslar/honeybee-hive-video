from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest import mock

from hive_video import download

PAYLOAD = b"bee video bytes\n"
FILENAME = "start47__20190731_184423_side1_top.mp4"


def remote_file() -> download.RemoteFile:
    return download.RemoteFile(
        file_id=237822,
        filename=FILENAME,
        size=len(PAYLOAD),
        md5=hashlib.md5(PAYLOAD).hexdigest(),
        start=47,
        date="20190731",
        time="184423",
        side=1,
        panel="top",
    )


def day22_file() -> download.RemoteFile:
    return replace(
        remote_file(),
        file_id=2200,
        filename="start22__20190731_184423_side0_top.mp4",
        start=22,
        side=0,
    )


def manifest_entries(*recordings: download.RemoteFile) -> list[dict]:
    return [
        {
            "dataFile": {
                "id": remote.file_id,
                "filename": remote.filename,
                "filesize": remote.size,
                "checksum": {"type": "MD5", "value": remote.md5},
            }
        }
        for remote in recordings
    ]


def write_manifest(path: Path) -> None:
    path.write_text(json.dumps(manifest_entries(remote_file())))


def response(payload: bytes, status: int = 200) -> io.BytesIO:
    stream = io.BytesIO(payload)
    stream.status = status
    return stream


class DownloadApiTests(unittest.TestCase):
    def test_direct_download_defaults_select_start_id_and_reuse_verified_copy(self) -> None:
        manifest = json.dumps(
            {"status": "OK", "data": manifest_entries(remote_file(), day22_file())}
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}),
                mock.patch.object(
                    download,
                    "_http_get",
                    side_effect=[response(manifest), response(PAYLOAD)],
                ) as get,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                destination = download.download_video(
                    day=22, side=0, panel="top", target=root / "raw"
                )
                reused = download.download_video(day=22, side=0, panel="top", target=root / "raw")
            self.assertEqual(destination, root / "raw" / "start22__20190731_184423_side0_top.mp4")
            self.assertEqual(reused, destination)
            self.assertEqual(destination.read_bytes(), PAYLOAD)
            self.assertFalse(destination.with_suffix(".mp4.part").exists())
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                [call.args[:3] for call in get.call_args_list],
                [
                    (
                        "https://edmond.mpg.de/api/datasets/:persistentId/versions/:latest/files"
                        "?persistentId=doi:10.17617%2F3.LLWRWR",
                        120.0,
                        None,
                    ),
                    ("https://edmond.mpg.de/api/access/datafile/2200", 120.0, {}),
                ],
            )
            self.assertTrue(all(call.args[3].check_hostname for call in get.call_args_list))

    def test_direct_download_uses_explicit_manifest_and_repairs_same_size_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "provided-manifest.json"
            cache.write_text(json.dumps(manifest_entries(day22_file())))
            destination = root / day22_file().filename
            destination.write_bytes(b"x" * len(PAYLOAD))
            messages = []
            with (
                mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "unused-cache")}),
                mock.patch.object(download, "_http_get", return_value=response(PAYLOAD)) as get,
            ):
                result = download.download_video(
                    day=22,
                    side=0,
                    panel="top",
                    target=root,
                    manifest_cache=cache,
                    on_message=messages.append,
                )
            self.assertEqual(result, destination)
            self.assertEqual(result.read_bytes(), PAYLOAD)
            self.assertIn(
                "existing file does not match the archive checksum; re-downloading", messages
            )
            self.assertIn("MD5 OK", messages)
            get.assert_called_once()
            self.assertEqual(
                get.call_args.args[0], "https://edmond.mpg.de/api/access/datafile/2200"
            )
            self.assertFalse((root / "unused-cache").exists())

    def test_direct_download_defaults_stop_after_eight_short_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "manifest.json"
            cache.write_text(json.dumps(manifest_entries(day22_file())))
            with (
                mock.patch.object(
                    download, "_http_get", side_effect=lambda *_, **__: response(b"")
                ) as get,
                self.assertRaisesRegex(RuntimeError, "after 8 attempts"),
            ):
                download.download_video(
                    day=22, side=0, panel="top", target=root / "raw", manifest_cache=cache
                )
            self.assertEqual(get.call_count, 8)
            self.assertFalse((root / "raw" / day22_file().filename).exists())

    def test_direct_download_invalid_selection_and_conflicts_have_no_side_effects(self) -> None:
        selection = {"day": 22, "side": 0, "panel": "top"}
        invalid_requests = [
            {},
            {"day": 22},
            {"day": 22, "side": 0},
            *(selection | {"day": day} for day in (0, -1, True, 22.0, "22")),
            *(selection | {"side": side} for side in (-1, 2, True, 0.0, "0")),
            selection | {"panel": "left"},
            selection | {"refresh_manifest": "yes"},
            selection | {"timeout": 0},
            selection | {"retries": 0},
            *(
                {"remote": remote_file()} | conflict
                for conflict in (
                    {"day": 22},
                    {"side": 0},
                    {"panel": "top"},
                    {"manifest_cache": "unused.json"},
                    {"doi": "doi:10.9999/OTHER"},
                    {"refresh_manifest": True},
                    {"refresh_manifest": "yes"},
                )
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for request in invalid_requests:
                with (
                    self.subTest(request=request),
                    mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}),
                    mock.patch.object(download, "build_ssl_context") as context,
                    mock.patch.object(download, "_http_get") as get,
                    self.assertRaises(ValueError),
                ):
                    download.download_video(target=root / "raw", **request)
                context.assert_not_called()
                get.assert_not_called()
                self.assertEqual(list(root.iterdir()), [])

    def test_direct_download_routes_and_separates_cached_archive_identities(self) -> None:
        archives = (
            ("https://archive.example.invalid", "doi:10.9999/ONE", "doi:10.9999%2FONE"),
            ("https://other.example.invalid", "doi:10.9999/ONE", "doi:10.9999%2FONE"),
            ("https://archive.example.invalid", "doi:10.9999/TWO", "doi:10.9999%2FTWO"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = []
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}):
                for index, (server, doi, encoded_doi) in enumerate(archives):
                    recording = replace(day22_file(), file_id=2200 + index)
                    manifest = json.dumps(
                        {"status": "OK", "data": manifest_entries(recording)}
                    ).encode()
                    with mock.patch.object(
                        download,
                        "_http_get",
                        side_effect=[response(manifest), response(PAYLOAD)],
                    ) as get:
                        result = download.download_video(
                            day=22,
                            side=0,
                            panel="top",
                            target=root / f"raw-{index}",
                            server=server,
                            doi=doi,
                        )
                    self.assertEqual(result.read_bytes(), PAYLOAD)
                    self.assertEqual(
                        [call.args[0] for call in get.call_args_list],
                        [
                            f"{server}/api/datasets/:persistentId/versions/:latest/files"
                            f"?persistentId={encoded_doi}",
                            f"{server}/api/access/datafile/{recording.file_id}",
                        ],
                    )
                    results.append(result)
                with mock.patch.object(download, "_http_get") as get:
                    for index, (server, doi, _encoded_doi) in enumerate(archives):
                        result = download.download_video(
                            day=22,
                            side=0,
                            panel="top",
                            target=root / f"raw-{index}",
                            server=server,
                            doi=doi,
                        )
                        self.assertEqual(result, results[index])
                    get.assert_not_called()

    def test_resolve_cached_video_is_quiet_and_never_requests_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "manifest.json"
            write_manifest(cache)
            with (
                mock.patch.object(download, "_http_get") as get,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                remote = download.resolve_video(
                    locator="start47_side1_top",
                    manifest_cache=cache,
                    refresh_manifest=False,
                    timeout=1,
                )
            self.assertEqual(remote, remote_file())
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            get.assert_not_called()

    def test_archive_defaults_and_overrides_reach_manifest_and_media_requests(self) -> None:
        cases = (
            ({}, "https://edmond.mpg.de", "doi:10.17617%2F3.LLWRWR"),
            (
                {"server": "https://archive.example.invalid", "doi": "doi:10.9999/OTHER"},
                "https://archive.example.invalid",
                "doi:10.9999%2FOTHER",
            ),
        )
        for archive_args, server, encoded_doi in cases:
            with (
                self.subTest(archive_args=archive_args),
                tempfile.TemporaryDirectory() as directory,
            ):
                cache = Path(directory) / "manifest.json"
                write_manifest(cache)
                manifest_response = json.dumps(
                    {"status": "OK", "data": json.loads(cache.read_text())}
                ).encode()
                with mock.patch.object(
                    download,
                    "_http_get",
                    side_effect=[response(manifest_response), response(PAYLOAD)],
                ) as get:
                    recording = download.resolve_video(
                        locator="start47_side1_top",
                        manifest_cache=cache,
                        refresh_manifest=True,
                        timeout=1,
                        **archive_args,
                    )
                    transfer_args = {"server": archive_args["server"]} if archive_args else {}
                    destination = download.download_video(
                        recording,
                        target=Path(directory) / "raw",
                        timeout=1,
                        retries=1,
                        progress_seconds=60,
                        verify=True,
                        force=False,
                        **transfer_args,
                    )
                self.assertEqual(recording, remote_file())
                self.assertEqual(destination.read_bytes(), PAYLOAD)
                self.assertEqual(
                    [call.args[:3] for call in get.call_args_list],
                    [
                        (
                            f"{server}/api/datasets/:persistentId/versions/:latest/files"
                            f"?persistentId={encoded_doi}",
                            1,
                            None,
                        ),
                        (f"{server}/api/access/datafile/237822", 1, {}),
                    ],
                )

    def test_resolve_exact_filename_and_explicit_triple(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "manifest.json"
            write_manifest(cache)
            for selection in ({"filename": FILENAME}, {"start": 47, "side": 1, "panel": "top"}):
                with self.subTest(selection=selection):
                    remote = download.resolve_video(
                        **selection,
                        manifest_cache=cache,
                        server=download.DEFAULT_SERVER,
                        doi=download.DEFAULT_DOI,
                        refresh_manifest=False,
                        timeout=1,
                    )
                    self.assertEqual(remote, remote_file())

    def test_missing_or_invalid_selection_fails_before_loading_manifest(self) -> None:
        for selection in ({}, {"locator": "day47_side1_top"}, {"start": 47, "side": 1}):
            with (
                self.subTest(selection=selection),
                mock.patch.object(download, "load_manifest") as load,
                self.assertRaises(ValueError),
            ):
                download.resolve_video(
                    **selection,
                    manifest_cache="unused.json",
                    server=download.DEFAULT_SERVER,
                    doi=download.DEFAULT_DOI,
                    refresh_manifest=False,
                    timeout=1,
                )
            load.assert_not_called()

    def test_empty_or_ambiguous_manifest_selection_is_an_ordinary_exception(self) -> None:
        for files, expected in (([], "No video files"), ([remote_file()] * 2, "Ambiguous")):
            with self.subTest(files=files), self.assertRaisesRegex(ValueError, expected):
                download.select_file(files, filename=None, start=47, side=1, panel="top")

    def test_download_api_writes_verified_bytes_and_is_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(download, "_http_get", return_value=response(PAYLOAD)) as get,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                destination = download.download_video(
                    remote_file(),
                    target=directory,
                    timeout=1,
                    retries=1,
                    progress_seconds=60,
                    verify=True,
                    force=False,
                )
            self.assertEqual(destination, Path(directory) / FILENAME)
            self.assertEqual(destination.read_bytes(), PAYLOAD)
            self.assertFalse(destination.with_suffix(".mp4.part").exists())
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                get.call_args.args[:3],
                (
                    "https://edmond.mpg.de/api/access/datafile/237822",
                    1,
                    {},
                ),
            )
            self.assertTrue(get.call_args.args[3].check_hostname)

    def test_download_api_resumes_existing_partial_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / FILENAME
            destination.with_suffix(".mp4.part").write_bytes(PAYLOAD[:4])
            with mock.patch.object(
                download, "_http_get", return_value=response(PAYLOAD[4:], 206)
            ) as get:
                result = download.download_video(
                    remote_file(),
                    target=directory,
                    server=download.DEFAULT_SERVER,
                    timeout=1,
                    retries=1,
                    progress_seconds=60,
                    verify=True,
                    force=False,
                )
            self.assertEqual(result.read_bytes(), PAYLOAD)
            self.assertEqual(get.call_args.args[2], {"Range": "bytes=4-"})

    def test_verified_existing_copy_is_reused_without_media_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / FILENAME
            destination.write_bytes(PAYLOAD)
            messages = []
            with mock.patch.object(download, "_http_get") as get:
                result = download.download_video(
                    remote_file(),
                    target=directory,
                    server=download.DEFAULT_SERVER,
                    timeout=1,
                    retries=1,
                    progress_seconds=60,
                    verify=True,
                    force=False,
                    on_message=messages.append,
                )
            self.assertEqual(result, destination)
            self.assertIn("already present and MD5 matches, skipping", messages)
            get.assert_not_called()

    def test_force_download_preserves_original_redownload_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / FILENAME
            destination.write_bytes(PAYLOAD)
            with mock.patch.object(download, "_http_get", return_value=response(PAYLOAD)) as get:
                download.download_video(
                    remote_file(),
                    target=directory,
                    server=download.DEFAULT_SERVER,
                    timeout=1,
                    retries=1,
                    progress_seconds=60,
                    verify=True,
                    force=True,
                )
            get.assert_called_once()
            self.assertEqual(destination.read_bytes(), PAYLOAD)

    def test_md5_mismatch_is_failure_and_never_reports_done(self) -> None:
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    download, "_http_get", return_value=response(b"x" * len(PAYLOAD))
                ),
                self.assertRaisesRegex(RuntimeError, "MD5 mismatch"),
            ):
                download.download_video(
                    remote_file(),
                    target=directory,
                    server=download.DEFAULT_SERVER,
                    timeout=1,
                    retries=1,
                    progress_seconds=60,
                    verify=True,
                    force=False,
                    on_message=messages.append,
                )
            self.assertFalse(any(message.startswith("done:") for message in messages))
            # The established transfer publishes before MD5 verification; the
            # preserved mismatch remains available for diagnosis and re-download.
            self.assertEqual((Path(directory) / FILENAME).read_bytes(), b"x" * len(PAYLOAD))

    def test_no_verify_explicitly_skips_existing_checksum_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / FILENAME
            destination.write_bytes(b"unchecked")
            with mock.patch.object(download, "_http_get") as get:
                result = download.download_video(
                    remote_file(),
                    target=directory,
                    server=download.DEFAULT_SERVER,
                    timeout=1,
                    retries=1,
                    progress_seconds=60,
                    verify=False,
                    force=False,
                )
            self.assertEqual(result.read_bytes(), b"unchecked")
            get.assert_not_called()

    def test_invalid_transfer_settings_fail_before_tls_or_transfer(self) -> None:
        settings = dict(timeout=1, retries=1, progress_seconds=60, verify=True, force=False)
        for invalid in (
            {"timeout": 0},
            {"timeout": float("nan")},
            {"retries": 0},
            {"retries": True},
            {"progress_seconds": -1},
            {"verify": "yes"},
        ):
            with (
                self.subTest(invalid=invalid),
                mock.patch.object(download, "build_ssl_context") as context,
                self.assertRaises(ValueError),
            ):
                download.download_video(
                    remote_file(),
                    target="unused",
                    server=download.DEFAULT_SERVER,
                    **(settings | invalid),
                )
            context.assert_not_called()

    def test_short_read_retries_keep_range_resume_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                download,
                "_http_get",
                side_effect=[response(PAYLOAD[:4]), response(PAYLOAD[4:], 206)],
            ) as get:
                result = download.download_video(
                    remote_file(),
                    target=directory,
                    server=download.DEFAULT_SERVER,
                    timeout=1,
                    retries=2,
                    progress_seconds=60,
                    verify=True,
                    force=False,
                )
            self.assertEqual(result.read_bytes(), PAYLOAD)
            self.assertEqual(get.call_args_list[1].args[2], {"Range": "bytes=4-"})

    def test_media_dns_failure_retains_retry_and_resume_behavior(self) -> None:
        dns_error = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / FILENAME
            part = destination.with_suffix(".mp4.part")
            part.write_bytes(PAYLOAD[:4])
            messages: list[str] = []
            with (
                mock.patch.object(download.urllib.request, "build_opener") as build_opener,
                mock.patch.object(download.time, "sleep") as sleep,
            ):
                opener = build_opener.return_value
                opener.open.side_effect = [dns_error, response(PAYLOAD[4:], 206)]
                result = download.download_video(
                    remote_file(), target=directory, retries=2, on_message=messages.append
                )
            self.assertEqual(result.read_bytes(), PAYLOAD)
            self.assertFalse(part.exists())
            self.assertEqual(opener.open.call_count, 2)
            for call in opener.open.call_args_list:
                self.assertEqual(call.args[0].get_header("Range"), "bytes=4-")
            sleep.assert_called_once_with(2.0)
            self.assertTrue(
                any("DNS/name resolution failed during media download" in m for m in messages)
            )
            self.assertIn("MD5 OK", messages)

    def test_exhausted_dns_retries_preserve_partial_without_completing(self) -> None:
        dns_error = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / FILENAME
            part = destination.with_suffix(".mp4.part")
            part.write_bytes(PAYLOAD[:4])
            messages: list[str] = []
            with (
                mock.patch.object(download.urllib.request, "build_opener") as build_opener,
                mock.patch.object(download.time, "sleep") as sleep,
            ):
                opener = build_opener.return_value
                opener.open.side_effect = dns_error
                with self.assertRaisesRegex(urllib.error.URLError, "during media download"):
                    download.download_video(
                        remote_file(), target=directory, retries=2, on_message=messages.append
                    )
            self.assertEqual(opener.open.call_count, 2)
            sleep.assert_called_once_with(2.0)
            self.assertEqual(part.read_bytes(), PAYLOAD[:4])
            self.assertFalse(destination.exists())
            self.assertFalse(any(m.startswith("done:") for m in messages))


class DownloadCliTests(unittest.TestCase):
    def test_dns_failures_identify_manifest_or_probe_and_return_nonzero(self) -> None:
        for cached, mode, operation in (
            (False, [], "archive manifest"),
            (True, ["--resolve-only", "--refresh-manifest"], "archive manifest"),
            (True, ["--probe-only"], "media probe"),
        ):
            with self.subTest(cached=cached, mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cache = root / "manifest.json"
                target = root / "raw"
                if cached:
                    write_manifest(cache)
                    original_cache = cache.read_bytes()
                dns_error = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
                with (
                    mock.patch.object(download.urllib.request, "build_opener") as build_opener,
                    mock.patch.object(download.time, "sleep") as sleep,
                    contextlib.redirect_stdout(io.StringIO()) as stdout,
                    contextlib.redirect_stderr(io.StringIO()) as stderr,
                ):
                    build_opener.return_value.open.side_effect = dns_error
                    result = download.main(
                        [
                            "--locator",
                            "start47_side1_top",
                            "--target",
                            str(target),
                            "--manifest-cache",
                            str(cache),
                            *mode,
                        ]
                    )
                self.assertEqual(result, 1)
                self.assertIn(f"DNS/name resolution failed during {operation}", stderr.getvalue())
                self.assertIn("edmond.mpg.de", stderr.getvalue())
                self.assertIn("--probe-only --refresh-manifest", stderr.getvalue())
                self.assertNotIn("probe OK", stdout.getvalue())
                build_opener.return_value.open.assert_called_once()
                sleep.assert_not_called()
                self.assertFalse(target.exists())
                if cached:
                    self.assertEqual(cache.read_bytes(), original_cache)
                else:
                    self.assertFalse(cache.exists())

    def test_resolve_sh_contract_is_exact_and_does_not_mutate_argv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "manifest.json"
            write_manifest(cache)
            target = root / "a bee's files"
            original_argv = sys.argv[:]
            with (
                mock.patch.object(download, "_http_get") as get,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                result = download.main(
                    [
                        "--locator",
                        "start47_side1_top",
                        "--target",
                        str(target),
                        "--manifest-cache",
                        str(cache),
                        "--resolve-only",
                        "--format",
                        "sh",
                    ]
                )
            expected = {
                "KEY": "start47_20190731_184423_side1_top",
                "LOCATOR": "start47_side1_top",
                "FILENAME": FILENAME,
                "PATH": str(target / FILENAME),
                "DIRNAME": "reseq_start47_20190731_184423_side1_top",
                "FILE_ID": "237822",
                "SIZE": str(len(PAYLOAD)),
                "MD5": hashlib.md5(PAYLOAD).hexdigest(),
            }
            self.assertEqual(
                stdout.getvalue(),
                "".join(f"RESEQ_{key}={shlex.quote(value)}\n" for key, value in expected.items()),
            )
            self.assertEqual(result, 0)
            self.assertEqual(sys.argv, original_argv)
            self.assertIn("TLS verification roots:", stderr.getvalue())
            self.assertFalse(target.exists())
            get.assert_not_called()

    def test_root_cli_and_download_module_resolve_identically_from_another_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "manifest.json"
            write_manifest(cache)
            options = [
                "--locator",
                "start47_side1_top",
                "--target",
                "relative-downloads",
                "--manifest-cache",
                str(cache),
                "--resolve-only",
                "--format",
                "sh",
            ]
            cli_result = subprocess.run(
                [sys.executable, "-m", "hive_video", "download", *options],
                cwd=directory,
                capture_output=True,
                text=True,
                check=True,
            )
            module_result = subprocess.run(
                [sys.executable, "-m", "hive_video.download", *options],
                cwd=directory,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(cli_result.stdout, module_result.stdout)
            self.assertIn("RESEQ_PATH=relative-downloads/", module_result.stdout)
            self.assertFalse((Path(directory) / "relative-downloads").exists())

    def test_cli_selection_error_is_nonzero_without_library_system_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "manifest.json"
            cache.write_text("[]")
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                result = download.main(
                    [
                        "--locator",
                        "start47_side1_top",
                        "--manifest-cache",
                        str(cache),
                        "--resolve-only",
                    ]
                )
            self.assertEqual(result, 1)
            self.assertIn("No video files", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
