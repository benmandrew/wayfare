"""Fetch the source datasets.

Downloads are staged to a ``.part`` file and renamed only once complete, so an
interrupted run never leaves a half-file that looks finished. Nothing here is
re-fetched if a good copy already exists -- these are multi-gigabyte files and the
pipeline is expected to be re-run many times against the same inputs.
"""

from __future__ import annotations

import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from . import config, logs

log = logs.get("acquire")


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    filename: str
    min_bytes: int = 0


def sources(region: str | None = None) -> list[Source]:
    region = region or config.BODS_REGION
    return [
        Source(
            "gtfs",
            config.BODS_GTFS_URL.format(region=region),
            f"bods_gtfs_{region}.zip",
            config.MIN_GTFS_BYTES,
        ),
        Source("osm_gb", config.OSM_GB_URL, "great-britain.osm.pbf", 1 << 30),
        Source("naptan", config.NAPTAN_URL, "naptan.csv", 10 << 20),
    ]


def download(src: Source, dest_dir: Path | None = None, force: bool = False) -> Path:
    """Fetch one source. Returns the path to the complete file."""
    dest_dir = dest_dir or config.RAW
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.filename
    part = dest.with_suffix(dest.suffix + ".part")

    if dest.exists() and not force:
        log.info("%s: already have %s (%.1f GB)", src.name, dest.name, _gb(dest))
        return dest

    last_error: Exception | None = None
    for attempt in range(1, config.DOWNLOAD_RETRIES + 1):
        try:
            _stream(src, part)
            size = part.stat().st_size
            if size < src.min_bytes:
                raise OSError(
                    f"{src.name}: got {size} bytes, expected at least {src.min_bytes}"
                )
            part.replace(dest)
            log.info("%s: fetched %s (%.2f GB)", src.name, dest.name, _gb(dest))
            return dest
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
            last_error = exc
            part.unlink(missing_ok=True)
            if attempt < config.DOWNLOAD_RETRIES:
                wait = config.DOWNLOAD_BACKOFF * attempt
                log.warning(
                    "%s: attempt %d failed (%s); retrying in %.0fs",
                    src.name,
                    attempt,
                    exc,
                    wait,
                )
                time.sleep(wait)

    raise RuntimeError(
        f"{src.name}: gave up after {config.DOWNLOAD_RETRIES} attempts"
    ) from last_error


def _stream(src: Source, part: Path) -> None:
    headers = {"User-Agent": config.USER_AGENT}
    with requests.get(src.url, headers=headers, stream=True, timeout=(30, 300)) as r:
        r.raise_for_status()
        # BODS sends no Content-Length, so progress is reported in absolute bytes
        # rather than as a percentage.
        total = int(r.headers.get("Content-Length") or 0)
        written = 0
        next_report = 0
        with part.open("wb") as fh:
            for chunk in r.iter_content(config.DOWNLOAD_CHUNK):
                fh.write(chunk)
                written += len(chunk)
                if written >= next_report:
                    log.info(
                        "%s: %.2f GB%s",
                        src.name,
                        written / 1e9,
                        f" of {total / 1e9:.2f}" if total else "",
                    )
                    next_report = written + (250 << 20)


def unpack_gtfs(zip_path: Path, dest: Path | None = None, force: bool = False) -> Path:
    """Extract the GTFS bundle.

    The national bundle is 7.8 GB unpacked, with a 5.1 GB ``stop_times.txt``. It is
    extracted rather than streamed because DuckDB reads CSV files from disk and does
    the large group-by out of core -- which is the whole reason for using it.
    """
    dest = dest or (config.WORK / "gtfs")
    marker = dest / "stop_times.txt"
    if marker.exists() and not force:
        log.info("gtfs: already unpacked at %s", dest)
        return dest

    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".txt")]
        log.info("gtfs: unpacking %d files to %s", len(names), dest)
        for name in names:
            target = dest / Path(name).name
            with zf.open(name) as fin, target.open("wb") as fout:
                shutil.copyfileobj(fin, fout, config.DOWNLOAD_CHUNK)
            log.info("gtfs:   %s (%.2f GB)", target.name, _gb(target))
    return dest


def feed_version(gtfs_dir: Path) -> str:
    """BODS stamps the build into feed_info.txt; used to label the output."""
    fi = gtfs_dir / "feed_info.txt"
    if not fi.exists():
        return "unknown"
    with fi.open() as fh:
        header = fh.readline().rstrip("\n").split(",")
        row = fh.readline().rstrip("\n").split(",")
    fields = dict(zip(header, row, strict=False))
    return fields.get("feed_version") or "unknown"


def _gb(p: Path) -> float:
    return p.stat().st_size / 1e9


def acquire_all(region: str | None = None, force: bool = False) -> dict[str, Path]:
    config.ensure_dirs()
    out: dict[str, Path] = {}
    for src in sources(region):
        out[src.name] = download(src, force=force)
    out["gtfs_dir"] = unpack_gtfs(out["gtfs"], force=force)
    return out
