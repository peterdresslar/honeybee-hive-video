# Package interface

The `hive-video` distribution contains `hive_video.fragment`, `hive_video.download`, and `hive_video.resequence`. These tools can be installed in another project, including `honey-bee-behavior`. Internal analysis, experiments, and Slurm launchers remain in this repository and are excluded from the distribution.

Version 0.1.0 is the first public package release. The API is still evolving: during 0.x, incompatible interface changes receive a minor version increment and an explanation in the release notes; patch releases contain compatible fixes. Pin the package version and retain your dependency lockfile when reproducing an analysis.

## Installation

Python 3.12 or newer is required. Install the command-line tools with:

```bash
uv tool install hive-video
hive-video --help
```

This installs downloading and fragment extraction. To include the NumPy, OpenCV, and Pillow dependencies for resequencing, use:

```bash
uv tool install 'hive-video[resequence]'
```

`uv` manages an isolated environment for the installed tool and exposes the `hive-video` shell command. For Python API access in a `uv` project, add the package from that project's directory:

```bash
uv add 'hive-video[resequence]'
```

In an already activated virtual environment, use `uv pip install 'hive-video[resequence]'`. Omit `[resequence]` for the base package.

For development or testing a source revision, run `uv tool install .` or `uv tool install '.[resequence]'` from the checkout root. A consuming Python project can use `uv add '/absolute/path/to/honeybee-hive-video[resequence]'` to install that checkout.

The base package includes the downloader's certificate bundle dependency, `portable-ffmpeg==0.3.0` for media executables, and `filelock` to coordinate cache preparation. Fragment extraction and resequencing media stages resolve both `ffmpeg` and `ffprobe` together: explicit absolute `HIVE_VIDEO_FFMPEG` and `HIVE_VIDEO_FFPROBE` paths take precedence, followed by a complete pair on `PATH`, then the provider's cached platform-specific builds. The provider downloads binaries from the FFmpeg 8 family on first use when needed; the minor version depends on the platform and provider build. Help and archive downloading do not need them.

Prepare media executables before offline work:

```bash
hive-video setup-ffmpeg
```

This prints the selected paths, actual executable versions, and SHA-256 checksums as JSON. These reported identities establish which binaries will run. Provider download progress goes to stderr, including first-use setup triggered from a Python API. Pinning the provider package does not freeze its downloadable binaries; [method HV-P002](METHODS.md) records that distinction and the executable provenance policy.

A normal `uv sync --locked` in this repository still installs the development and research dependency groups; consumers of the distribution do not inherit those groups.

## Command line

The command groups are `fragment`, `download`, and `resequence`; `setup-ffmpeg` prepares the media executables.

```bash
hive-video --help
hive-video --version
hive-video fragment --help
hive-video download --help
hive-video resequence --help
hive-video resequence detect --help
hive-video setup-ffmpeg --help
```

`hive-video --version` (available from v0.1.1) prints the package name and version, then exits successfully. `python -m hive_video --version` uses the selected Python interpreter's installation. In Python or a notebook, use `import hive_video; print(hive_video.__version__)` to check the imported package's version. These checks do not prepare FFmpeg or require the resequencing extra.

Extract one zero-based frame, or a 25-frame interval, from a local video:

```bash
hive-video fragment --video source.mp4 --start-frame 100 --out frame100.png
hive-video fragment --video source.mp4 --start-frame 100 --duration-frames 25 --out clip.mp4
```

Fragment extraction retains its bee progress display, with `--progress plain` for logs and `--progress off` to suppress operation progress. First-use binary setup can still report download progress on stderr. The output path is written to stdout and progress to stderr. Existing output files are refused and each derivative has a JSON provenance sidecar. Download and resequencing retain their existing transfer and stage logs.

Resolve an archive locator before transferring media:

```bash
hive-video download --locator start4_side1_top --target /data/raw --resolve-only
hive-video download --locator start4_side1_top --target /data/raw
```

Resolution may fetch the archive manifest; it does not download video. `--manifest-cache` selects an explicit cache, and `--resolve-only --format sh` retains the `RESEQ_*` assignments used by existing cluster scripts. Transfers retain resumable `.part` files, retry settings, and archive MD5 verification. The inherited downloader renames a byte-complete transfer before checking MD5; a checksum failure returns an error but can leave that destination present. A later call with verification enabled checks it again.

### Download name-resolution failures (including Sol)

`<urlopen error [Errno -2] Name or service not known>`, as reported in [issue #14](https://github.com/Collective-Logic-Lab/honeybee-hive-video/issues/14), is a network name-resolution failure. The preceding `TLS verification roots:` line reports which certificate roots were loaded; it does not mean an HTTPS connection succeeded. Changing the target directory or CA bundle does not fix a DNS lookup. Python reports these failures as [`socket.gaierror`](https://docs.python.org/3.12/library/socket.html#socket.gaierror); the error number and wording vary by platform.

The working checkout now identifies whether the failure occurred during archive manifest retrieval, a media probe, or a media download, and reports the requested hostname. A redirect destination or configured proxy can be the host whose lookup failed, so the requested hostname alone does not establish the cause. TLS verification, media retry limits, and partial-file resume behavior are unchanged; manifest retrieval and probes still fail after one attempt.

On the same node and in the same environment that failed, check the archive hostname and then run the small end-to-end probe:

```bash
python -c 'import socket; print(socket.getaddrinfo("edmond.mpg.de", 443, type=socket.SOCK_STREAM))'
hive-video download --start 4 --side 0 --panel top --probe-only --refresh-manifest
```

The first command checks direct hostname resolution. The second refreshes the manifest cache and follows the actual media HTTPS redirects using a one-byte range request; it creates no video or target directory. Refreshing matters because a cached `--resolve-only` result does not test the archive connection. A successful direct lookup does not check redirect hosts or proxies; urllib can use [proxy settings from the environment or operating system](https://docs.python.org/3.12/library/urllib.request.html#urllib.request.ProxyHandler).

If this still fails on Sol, record the hostname, whether it is a login node or a Slurm allocation, the package version (`hive-video --version`), and the diagnostic error. Check the network and proxy configuration for that node with the cluster support team. Keep proxy credentials and signed media URLs out of shared logs. A successful probe establishes connectivity at that time; it does not guarantee an uninterrupted full transfer. After it succeeds, rerun the original download command to resume any partial file.

### Automatic resequencing (working checkout; awaiting release)

Run the unattended 25 fps hive-video profile in a new output directory:

```bash
hive-video resequence run --video source.mp4 --out-dir work/example_01 --profile edmond-2019-v1
```

The command detects cuts, orders segments, and checks joins. An automatic pass writes the archival render and frame map, then the H.264 viewing copy `output/resequenced.mp4`. A flagged result stops with `review/qc_roll_flagged_joins.mp4`. Watch all flagged joins; if acceptable, one command records your approval and finishes:

```bash
hive-video resequence finish --out-dir work/example_01 --reviewer "Your name" --note "What you checked"
```

Python calls use the same path:

```python
from hive_video.resequence.workflow import run_resequence, approve_resequence

result = run_resequence(source, output_dir, profile="edmond-2019-v1")
# Only after watching and accepting every flagged join:
result = approve_resequence(output_dir, reviewer="Your name", note="What you checked")
```

The returned dictionary and `run.json` contain `status` (`complete` or `manual_review_required`), `video`, and `review_video`. Stage logs, settings, software identity, and checksums stay in the run directory. Failures raise and are recorded as `failed`; choose a fresh directory or use the staged tools for recovery. Existing directories are refused. During rendering, write `sea cucumber` to the run's `STOP` file to stop between chunks; this leaves an incomplete run.

Source cuts retain `unreviewed_pilot` provenance, including after join approval. QC checks continuity and ambiguity, not complete source-cut detection or absolute chronology. An unacceptable join needs correction through the [staged workflow](resequencing.md), not approval. The existing renderer omits the nominal final source frame. See [HV-P003](METHODS.md#hv-p003-local-automatic-resequencing-with-one-join-review-boundary) for fixed settings and limits.

Until release, install the working checkout with `python -m pip install -e '.[resequence]'` from its root. Restart an already running notebook kernel after changing installations.

### Individual stages

Each stage accepts the arguments of its existing tool; use its `--help` to inspect them.

| Stage | Purpose | Module under `hive_video.resequence` |
| --- | --- | --- |
| `detect` | Find candidate source discontinuities | `detect_video_discontinuities` |
| `summarize` | Group candidates into jump events | `summarize_jump_events` |
| `prepare-cuts` | Write the editable source-cut proposal | `prepare_cut_review` |
| `build-segments` | Construct source segment definitions | `build_segments_from_jumps` |
| `order` | Score candidate successors and propose an order | `order_video_segments` |
| `qc` | Score joins against the detector baseline | `diagnostics.auto_qc_segment_joins` |
| `review` | Render a join review video | `diagnostics.make_join_review_video` |
| `approve` | Record or check a human review decision | `diagnostics.approve_manual_join_qc` |
| `diagnose` | Inspect discontinuities within segments | `diagnostics.diagnose_segment_discontinuities` |
| `render` | Validate join QC and render from the validated order | `cli` gate, then `reassemble_video_from_segments` |
| `compress` | Create a smaller sharing derivative | `compress_resequenced` |

In the staged workflow, source-cut inspection is a human step between `prepare-cuts` and `build-segments`. The `render` command requires the QC report, exact source, detector metadata, segment table, and complete order:

```bash
hive-video resequence render \
  --qc-summary review/auto_qc.summary.json \
  --video /data/raw/source.mp4 \
  --detector-metadata qc/metadata.json \
  --segments segments/segments.csv \
  --ranked-edges order/ranked_edges.csv \
  --order-csv order/greedy_order.csv \
  --require-complete-order \
  --out review/resequenced.mp4
```

Use the actual paths from the preceding stages. If the report says `manual_review_required`, inspect the flagged review artifacts, record the decision with `resequence approve create`, and supply the resulting `--qc-approval` to `render`. The gate checks the existing input fingerprints, source identity in every segment, complete order, and report-bound approval. It does not establish source-cut inspection or absolute chronology. The scientific rules and limitations remain in [METHODS.md](METHODS.md).

## Python access

Fragment extraction accepts explicit source, output, interval, and units:

```python
from hive_video.fragment import create_fragment

output = create_fragment(
    "source.mp4", "clip.mp4", start=100, duration=25, unit="frames", threads=1
)
```

The downloader accepts a day, side, panel, and destination directory directly and returns the downloaded file's `Path`. `day` is the archive's sequential capture identifier: `day=22` selects `start22`, whose filename supplies its calendar timestamp. The server and dataset DOI default to the CLI's Edmond archive. Calls are quiet unless supplied an `on_message` callback; failures raise ordinary exceptions.

```python
from pathlib import Path
from hive_video.download import download_video

source = download_video(day=22, side=0, panel="top", target=Path("raw"))
```

The archive listing is cached beneath `XDG_CACHE_HOME` (or `~/.cache`), with server and DOI identifying the cache. Set `manifest_cache` to choose its path or `refresh_manifest=True` to fetch the listing again. Transfer defaults are `timeout=120.0`, `retries=8`, `progress_seconds=60.0`, `verify=True`, and `force=False`; verified existing files are reused and partial transfers can resume. Add `on_message=print` for transfer messages. Supply `server` and `doi` to select an alternate archive.

To inspect the published filename, size, and checksum before transferring media, use `resolve_video`. It also supports selection by locator, exact filename, or a complete `start`/`side`/`panel` triple:

```python
from pathlib import Path
from hive_video.download import download_video, resolve_video

recording = resolve_video(
    locator="start22_side0_top",
    manifest_cache=Path("archive-manifest.json"),
    refresh_manifest=False,
    timeout=120.0,
)
print(recording.filename, recording.size, recording.md5)
output = download_video(recording, target=Path("raw"))
```

When resolving from an alternate archive, pass `server` and `doi` to `resolve_video`, then the same `server` to `download_video`. Passing a resolved recording together with `day`, `side`, `panel`, or resolution-only overrides is an error.

Resequencing modules retain their directly importable scientific and artifact helpers. For example, a cut proposal can be inspected before serialization:

```python
from pathlib import Path
from hive_video.resequence.prepare_cut_review import prepare_rows, write_rows

rows = prepare_rows(Path("jump_events.csv"))
write_rows(Path("cut_review.proposed.csv"), rows)
```

Every stage also exposes `main(argv)` for calling its existing command procedure without changing the host process's arguments. `hive_video.resequence.cli.main(["render", ...])` uses the guarded rendering interface. The underlying renderer module remains a lower-level operation; direct callers must validate QC and review inputs themselves, as the Slurm workers do. Resequencing segment tables use inclusive endpoint indices, while fragment intervals exclude their stop index; preserve that distinction when consuming artifacts.

## Cluster callers and provenance

Cluster workers invoke the installed modules with `python -m hive_video.download` and `python -m hive_video.resequence.<stage_module>`. They retain the pinned FFmpeg module and explicit executable pair; scheduled work does not download binaries. Python imports use the same `hive_video` package. The [resequencing workflow](resequencing.md) describes the stages and review artifacts.

The Slurm workers hash the package implementation files in their restart signatures, including the detector used by join QC. Existing completion markers therefore do not match this revision automatically. Prior runs remain associated with their recorded code and artifacts. Launch settings in the versioned zero-argument parent scripts are unchanged; prepare a reviewed plan and environment before scheduled compute. The copy-and-edit template remains `src/pipeline/slurm/resequence/resequence_pipeline_sample.sh`.
