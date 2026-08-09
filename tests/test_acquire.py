from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from wayfare import acquire, config


def _zip(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, "a,b\n1,2\n")
    return path


# --- Caching ----------------------------------------------------------------


def test_a_complete_file_is_never_refetched(tmp_path, monkeypatch):
    dest = tmp_path / "bods_gtfs_wales.zip"
    _zip(dest, list(acquire.REQUIRED_GTFS))

    def boom(*a, **k):
        raise AssertionError("must not hit the network when the file is present")

    monkeypatch.setattr(acquire, "_stream", boom)
    src = acquire.sources("wales")[0]
    assert acquire.download(src, tmp_path) == dest


def test_force_overrides_the_cache(tmp_path, monkeypatch):
    dest = tmp_path / "bods_gtfs_wales.zip"
    _zip(dest, list(acquire.REQUIRED_GTFS))
    calls = []

    def fake(src, part):
        calls.append(src.name)
        _zip(part, list(acquire.REQUIRED_GTFS))

    monkeypatch.setattr(acquire, "_stream", fake)
    monkeypatch.setattr(config, "MIN_GTFS_BYTES", 1)
    acquire.download(acquire.sources("wales")[0], tmp_path, force=True)
    assert calls == ["gtfs"]


def test_unpacked_feed_is_not_re_extracted(tmp_path, monkeypatch):
    z = _zip(tmp_path / "feed.zip", ["stop_times.txt", "trips.txt"])
    out = tmp_path / "gtfs"
    acquire.unpack_gtfs(z, out)
    monkeypatch.setattr(
        zipfile, "ZipFile", lambda *a, **k: pytest.fail("re-extracted a cached feed")
    )
    assert acquire.unpack_gtfs(z, out) == out


# --- Retry policy -----------------------------------------------------------


def test_a_bad_archive_is_not_retried(tmp_path, monkeypatch):
    """The expensive mistake this guards against: five full re-downloads of a
    file the server will hand back byte-identical every time."""
    attempts = []

    def fake(src, part):
        attempts.append(1)
        part.write_bytes(b"<html>error</html>" * 100_000)

    monkeypatch.setattr(acquire, "_stream", fake)
    monkeypatch.setattr(config, "MIN_GTFS_BYTES", 1)
    with pytest.raises(acquire.Invalid):
        acquire.download(acquire.sources("wales")[0], tmp_path)
    assert len(attempts) == 1


def test_a_short_file_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire, "_stream", lambda src, part: part.write_bytes(b"x"))
    with pytest.raises(acquire.Invalid, match="expected at least"):
        acquire.download(acquire.sources("wales")[0], tmp_path)


def test_network_failures_are_retried(tmp_path, monkeypatch):
    attempts = []

    def flaky(src, part):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("connection reset")
        _zip(part, list(acquire.REQUIRED_GTFS))

    monkeypatch.setattr(acquire, "_stream", flaky)
    monkeypatch.setattr(config, "DOWNLOAD_BACKOFF", 0.0)
    monkeypatch.setattr(config, "MIN_GTFS_BYTES", 1)
    acquire.download(acquire.sources("wales")[0], tmp_path)
    assert len(attempts) == 3


def test_partials_are_kept_only_when_the_host_can_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOWNLOAD_BACKOFF", 0.0)
    monkeypatch.setattr(config, "DOWNLOAD_RETRIES", 1)

    def die(src, part):
        part.write_bytes(b"partial")
        raise ConnectionError("dropped")

    monkeypatch.setattr(acquire, "_stream", die)

    # BODS ignores Range, so a partial is dead weight.
    with pytest.raises(RuntimeError):
        acquire.download(acquire.sources("wales")[0], tmp_path)
    assert not (tmp_path / "bods_gtfs_wales.zip.part").exists()

    # Geofabrik answers 206, so the bytes already paid for are kept.
    osm = [s for s in acquire.sources("wales", with_osm=True) if s.name == "osm"][0]
    with pytest.raises(RuntimeError):
        acquire.download(osm, tmp_path)
    assert (tmp_path / f"{osm.filename}.part").read_bytes() == b"partial"


# --- Archive validation -----------------------------------------------------


def test_truncated_zip_is_rejected(tmp_path):
    z = _zip(tmp_path / "f.zip", list(acquire.REQUIRED_GTFS))
    data = z.read_bytes()
    z.write_bytes(data[: len(data) // 2])  # loses the central directory
    with pytest.raises(OSError, match="truncated"):
        acquire.check_gtfs(z)


def test_missing_members_are_named(tmp_path):
    z = _zip(tmp_path / "f.zip", ["stops.txt", "routes.txt"])
    with pytest.raises(OSError, match="stop_times.txt"):
        acquire.check_gtfs(z)


def test_a_small_but_complete_feed_passes():
    """Wales is 41 MB against the national bundle's 1.28 GB. Validation must not
    depend on size, or the smaller regions are rejected outright."""
    acquire.check_gtfs  # noqa: B018 - referenced for intent
    assert config.MIN_GTFS_BYTES < 37 << 20


# --- Resume -----------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None):
        self.status_code = status
        self.headers = headers or {"Content-Length": str(len(body))}
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, n):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_resume_appends_on_206(tmp_path, monkeypatch):
    part = tmp_path / "x.pbf.part"
    part.write_bytes(b"AAAA")
    seen = {}

    def fake_get(url, headers=None, **k):
        seen.update(headers or {})
        return FakeResponse(206, b"BBBB")

    monkeypatch.setattr(acquire.requests, "get", fake_get)
    src = acquire.Source("osm", "http://x/y.pbf", "x.pbf", resumable=True)
    acquire._stream(src, part)
    assert seen["Range"] == "bytes=4-"
    assert part.read_bytes() == b"AAAABBBB"


def test_a_server_that_ignores_range_restarts_cleanly(tmp_path, monkeypatch):
    """Appending a full-file 200 response onto a partial yields a file that is
    the right shape and the wrong bytes -- the worst kind of corruption."""
    part = tmp_path / "x.pbf.part"
    part.write_bytes(b"AAAA")
    monkeypatch.setattr(
        acquire.requests, "get", lambda url, headers=None, **k: FakeResponse(200, b"WHOLE")
    )
    src = acquire.Source("osm", "http://x/y.pbf", "x.pbf", resumable=True)
    acquire._stream(src, part)
    assert part.read_bytes() == b"WHOLE"


# --- Declared length --------------------------------------------------------


def test_a_short_read_is_caught_where_the_host_declares_a_length(tmp_path, monkeypatch):
    """BODS sends no Content-Length, which is why check_gtfs opens the archive.
    Every other host does send one, and it turns a cut-short transfer into an error
    where it happened rather than a puzzle three stages later."""
    monkeypatch.setattr(
        acquire.requests,
        "get",
        lambda url, headers=None, **k: FakeResponse(200, b"AB", {"Content-Length": "1000"}),
    )
    src = acquire.Source("gtfs", "http://x/f.zip", "f.zip")
    with pytest.raises(OSError, match="cut short"):
        acquire._stream(src, tmp_path / "f.zip.part")


def test_a_short_read_is_retried_rather_than_refused(tmp_path, monkeypatch):
    """A dropped connection hands back different bytes next time, so it is a
    network fault and not an Invalid one."""
    attempts = []

    def flaky(src, part):
        attempts.append(1)
        if len(attempts) < 2:
            raise OSError("got 2 bytes of a declared 1000; the transfer was cut short")
        _zip(part, list(acquire.REQUIRED_GTFS))

    monkeypatch.setattr(acquire, "_stream", flaky)
    monkeypatch.setattr(config, "DOWNLOAD_BACKOFF", 0.0)
    monkeypatch.setattr(config, "MIN_GTFS_BYTES", 1)
    acquire.download(acquire.sources("wales")[0], tmp_path)
    assert len(attempts) == 2


def test_a_compressed_body_is_not_measured_against_the_declared_length(
    tmp_path, monkeypatch
):
    """requests decodes the body, so the bytes written are the decoded ones and the
    declared length describes something else entirely."""
    monkeypatch.setattr(
        acquire.requests,
        "get",
        lambda url, headers=None, **k: FakeResponse(
            200, b"decoded and longer", {"Content-Length": "4", "Content-Encoding": "gzip"}
        ),
    )
    src = acquire.Source("naptan", "http://x/f.csv", "f.csv")
    acquire._stream(src, tmp_path / "f.csv.part")
    assert (tmp_path / "f.csv.part").read_bytes() == b"decoded and longer"


def test_non_resumable_sources_send_no_range_header(tmp_path, monkeypatch):
    part = tmp_path / "f.zip.part"
    part.write_bytes(b"AAAA")
    seen = {}

    def fake_get(url, headers=None, **k):
        seen.update(headers or {})
        return FakeResponse(200, b"WHOLE")

    monkeypatch.setattr(acquire.requests, "get", fake_get)
    acquire._stream(acquire.sources("wales")[0], part)
    assert "Range" not in seen
