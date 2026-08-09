"""Fetch the source datasets.

Downloads are staged to a ``.part`` file and renamed only once complete, so an
interrupted run never leaves a half-file that looks finished. Nothing here is
re-fetched if a good copy already exists -- these are multi-gigabyte files and the
pipeline is expected to be re-run many times against the same inputs.
"""

from __future__ import annotations

import csv
import re
import shutil
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from . import config, logs, translink

log = logs.get("acquire")


# The GTFS members the pipeline actually reads. shapes.txt is deliberately absent:
# roughly half of operators supply no geometry, and a feed without it is degraded
# rather than broken.
REQUIRED_GTFS = ("stop_times.txt", "trips.txt", "routes.txt", "stops.txt")

# A feed_version shaped like this identifies a publication and describes nothing
# about it -- see `feed_version`.
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class Invalid(Exception):
    """The transfer completed but the bytes are wrong.

    Kept apart from network faults on purpose. A server that hands back a
    complete-but-unusable file will hand back the same bytes next time, so
    retrying costs a full re-download and changes nothing. Retries are for
    connections that dropped, not for content that is wrong.
    """


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    filename: str
    min_bytes: int = 0
    check: Callable[[Path], None] | None = None
    # Whether the host honours Range requests, so an interrupted transfer can
    # continue instead of starting over. Measured, not assumed -- of our three
    # sources only Geofabrik does, and it is also much the largest.
    resumable: bool = False


def check_gtfs(path: Path) -> None:
    """Reject a truncated or bogus GTFS bundle.

    A zip stores its central directory at the *end*, so a download cut short
    cannot be opened at all -- which makes this a reliable completeness test in
    the absence of a Content-Length, and one that works at any feed size.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = {Path(n).name for n in zf.namelist()}
    except zipfile.BadZipFile as exc:
        raise OSError(f"not a readable zip ({exc}); download was truncated") from exc
    if missing := [n for n in REQUIRED_GTFS if n not in names]:
        raise OSError(f"GTFS bundle is missing {', '.join(missing)}")


def _part_sources(feed: config.Feed) -> list[Source]:
    """The datasets an assembled feed is built from.

    This resolves each one through CKAN, so it makes network calls where the rest
    of `sources` only builds URLs. That is unavoidable and belongs here rather
    than in `download`: OpenDataNI moves both the resource id and the filename on
    every publication, so there is no URL to write down.
    """
    out = []
    for part in feed.parts:
        res = translink.resource(part.dataset)
        log.info("%s: %s -> %s", part.name, part.dataset, res.filename)
        out.append(
            Source(part.name, res.url, res.filename, config.MIN_PART_BYTES, resumable=True)
        )
    return out


def sources(region: str | None = None, with_osm: bool = False) -> list[Source]:
    region = region or config.BODS_REGION
    feed = config.feed(region)
    out = (
        _part_sources(feed)
        if feed.parts
        else [
            Source(
                "gtfs",
                feed.url,
                feed.filename,
                config.MIN_GTFS_BYTES,
                check=check_gtfs,
                resumable=feed.resumable,
            )
        ]
    )
    # NaPTAN is the GB stop register, so a region outside GB skips it rather than
    # downloading 102 MB of stops none of its services call at.
    if feed.stop_register:
        out.append(Source("naptan", config.NAPTAN_URL, "naptan.csv", 10 << 20))
    if with_osm:
        url = config.osm_url(region)
        # Geofabrik answers Range requests with a 206; BODS and NaPTAN both ignore
        # the header and resend the whole file. Verified against every host.
        out.append(Source("osm", url, url.rsplit("/", 1)[-1], 50 << 20, resumable=True))
    return out


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
                raise Invalid(
                    f"{src.name}: got {size} bytes, expected at least {src.min_bytes}"
                )
            if src.check:
                try:
                    src.check(part)
                except OSError as exc:
                    raise Invalid(f"{src.name}: {exc}") from exc
            part.replace(dest)
            log.info("%s: fetched %s (%.2f GB)", src.name, dest.name, _gb(dest))
            return dest

        except Invalid:
            # Complete but wrong. Re-fetching gets the same bytes, so stop now
            # rather than spending four more full transfers to prove it.
            part.unlink(missing_ok=True)
            raise

        except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
            last_error = exc
            # A partial file is only worth keeping if the host will let us
            # continue from where it stopped. Otherwise it is dead weight that
            # would be mistaken for progress.
            if src.resumable:
                log.warning("%s: keeping %.2f GB already fetched", src.name, _gb(part))
            else:
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
    have = part.stat().st_size if (src.resumable and part.exists()) else 0
    if have:
        headers["Range"] = f"bytes={have}-"

    with requests.get(src.url, headers=headers, stream=True, timeout=(30, 300)) as r:
        r.raise_for_status()
        # Asking to resume is not the same as being allowed to. A 206 carries the
        # remainder and is appended; anything else carries the whole file from byte
        # zero, so the partial must be discarded or the two get concatenated into
        # a corrupt file that is the right shape and the wrong size.
        resumed = have > 0 and r.status_code == 206
        if have and not resumed:
            log.info("%s: server ignored the range request; starting over", src.name)
            have = 0

        # BODS sends no Content-Length, so progress is reported in absolute bytes
        # rather than as a percentage. Every other host does send one -- see the
        # completeness check below, which is the cheap version of check_gtfs.
        total = int(r.headers.get("Content-Length") or 0) + have
        # requests transparently decodes a compressed body, so the bytes written
        # are not the bytes declared and the check below would fire on a perfectly
        # good transfer.
        declared = total if not r.headers.get("Content-Encoding") else 0
        written = have
        next_report = written
        if resumed:
            log.info("%s: resuming from %.2f GB", src.name, have / 1e9)

        with part.open("ab" if resumed else "wb") as fh:
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

    # A host that declares a length has told us what complete means, so a short
    # read is knowable here rather than three stages later. This is a dropped
    # connection and not bad content, so it raises OSError and is retried -- Invalid
    # is for bytes that would come back the same next time.
    if declared and written != declared:
        raise OSError(
            f"{src.name}: got {written} bytes of a declared {declared}; "
            "the transfer was cut short"
        )


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


def feed_info(gtfs_dir: Path) -> dict[str, str]:
    """The single row of feed_info.txt, or nothing if the feed omits the file."""
    fi = gtfs_dir / "feed_info.txt"
    if not fi.exists():
        return {}
    # utf-8-sig because a byte order mark on the first header name silently renames
    # the column it belongs to, and csv rather than str.split because a publisher
    # name is free text and may hold a comma.
    with fi.open(encoding="utf-8-sig", newline="") as fh:
        rows = csv.reader(fh)
        header = next(rows, None)
        row = next(rows, None)
    if not header or not row:
        return {}
    return {k.strip(): v.strip() for k, v in zip(header, row, strict=False)}


def feed_version(gtfs_dir: Path) -> str:
    """The label every incremental stage keys its work on.

    BODS stamps a build timestamp, which sorts and compares. The NTA stamps a GUID,
    which does neither: two of them cannot be ordered, and nothing in the string
    says whether a database holds this fortnight's timetable or last year's. So
    where the version is opaque the date the feed declares it starts leads, and
    eight hex digits of the GUID follow it -- the date is what makes it readable
    and sortable, the digits are what keep two publications inside one validity
    window distinct.

    Distinctness is the half that matters. Every consumer of `patterns` filters on
    `last_seen`, so a version that fails to change between feeds leaves withdrawn
    services looking live and reports no churn at all, which is a wrong answer that
    looks like a quiet month.
    """
    fields = feed_info(gtfs_dir)
    version = fields.get("feed_version", "")
    if not _GUID.fullmatch(version):
        return version or "unknown"
    start = fields.get("feed_start_date", "")
    return f"{start}_{version[:8].lower()}" if start else version.lower()


def _gb(p: Path) -> float:
    return p.stat().st_size / 1e9


def assemble(feed: config.Feed, parts: dict[str, Path], force: bool = False) -> Path:
    """Build the GTFS bundle for a feed that is published as several datasets.

    The result lands in WORK rather than RAW: RAW is what was fetched, and this is
    derived from it. It is rebuilt only when one of the parts has changed, because
    the alternative is a minute of XML on every `patterns` run -- and the manifest
    is the parts' sizes and timestamps rather than their contents, since re-reading
    130 MB to decide whether to re-read it saves nothing.
    """
    dest = config.WORK / feed.filename
    manifest_path = dest.with_suffix(dest.suffix + ".manifest")
    manifest = translink.parts_manifest(parts)
    if (
        dest.exists()
        and not force
        and manifest_path.exists()
        and manifest_path.read_text() == manifest
    ):
        log.info("gtfs: already assembled at %s", dest)
        return dest

    kinds = {p.name: p.kind for p in feed.parts}
    built = translink.build_gtfs(
        [v for k, v in sorted(parts.items()) if kinds.get(k) == "timetable"],
        [v for k, v in sorted(parts.items()) if kinds.get(k) == "geometry"],
        dest,
    )
    check_gtfs(built)
    manifest_path.write_text(manifest)
    return built


def acquire_all(
    region: str | None = None, force: bool = False, with_osm: bool = False
) -> dict[str, Path]:
    config.ensure_dirs()
    feed = config.feed(region)
    # Printed every run, cache hit or not. The Republic's feed is CC BY 4.0 where
    # every other source here is OGL, so attribution is a condition of using it and
    # the run that fetches it is the last point at which nobody has yet forgotten.
    log.info(
        "gtfs: %s, %s (%s)",
        feed.url or " + ".join(p.dataset for p in feed.parts),
        feed.licence,
        feed.attribution,
    )
    out: dict[str, Path] = {}
    for src in sources(region, with_osm=with_osm):
        out[src.name] = download(src, force=force)
    if feed.parts:
        names = {p.name for p in feed.parts}
        out["gtfs"] = assemble(feed, {k: v for k, v in out.items() if k in names}, force)
    out["gtfs_dir"] = unpack_gtfs(out["gtfs"], force=force)
    return out
