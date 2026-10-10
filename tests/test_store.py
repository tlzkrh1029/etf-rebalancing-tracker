"""Tests for etf_tracker.store: raw files, normalised snapshots, manifest, ingest."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from etf_tracker.holdings import CASH, EQUITY, Holding, Snapshot, load_snapshot, snapshot_path
from etf_tracker.sources import STATUS_ERROR, STATUS_NO_DATA, STATUS_OK, STATUS_UNSUPPORTED, FetchResult
from etf_tracker.store import (
    MANIFEST_FIELDS,
    REASON_ALREADY_STORED,
    REASON_FORCED,
    REASON_STORED,
    IngestOutcome,
    has_snapshot,
    ingest,
    load_manifest,
    manifest_path,
    raw_dir,
    raw_path,
    save_normalized,
    save_raw,
    update_manifest,
    utc_now_iso,
    write_manifest,
)

AS_OF = date(2026, 10, 8)
NOW = datetime(2026, 10, 9, 14, 12, 3, tzinfo=timezone.utc)


def _snapshot(etf: str = "SOXX", as_of: date = AS_OF, source: str = "test-source") -> Snapshot:
    rows = [
        ("AMD", 0.5, EQUITY),
        ("NVDA", 0.3, EQUITY),
        ("INTC", 0.15, EQUITY),
        ("USD", 0.05, CASH),
    ]
    holdings = [
        Holding(etf, as_of, t, f"{t} name", ac, shares=100.0, price=10.0, market_value=1000.0 * w, weight=w)
        for t, w, ac in rows
    ]
    return Snapshot(etf=etf, as_of=as_of, source=source, holdings=holdings, meta={"shares_outstanding": 1000.0})


def _ok_result(etf: str = "SOXX", as_of: date = AS_OF, raw: bytes = b"a,b\n1,2\n") -> FetchResult:
    return FetchResult(etf, STATUS_OK, _snapshot(etf, as_of), {".csv": raw}, "parsed", "test-source", as_of.isoformat())


# ---------------------------------------------------------------------------
# Paths and raw files
# ---------------------------------------------------------------------------


def test_raw_dir_and_raw_path_layout(tmp_path: Path):
    assert raw_dir(tmp_path, "soxx") == tmp_path / "data" / "raw" / "SOXX"
    assert raw_path(tmp_path, "SOXX", AS_OF, ".csv") == tmp_path / "data" / "raw" / "SOXX" / "2026-10-08.csv"
    assert raw_path(tmp_path, "QQQ", "2026-10-08", ".fund.json").name == "2026-10-08.fund.json"


@pytest.mark.parametrize("suffix", ["", "csv", "../x", ".c/sv", ".json\n", ". json", ".a" * 10])
def test_raw_path_rejects_unsafe_suffixes(tmp_path: Path, suffix: str):
    with pytest.raises(ValueError):
        raw_path(tmp_path, "SOXX", AS_OF, suffix)


@pytest.mark.parametrize("etf", ["", "SO XX", "../SOXX", "soxx/evil"])
def test_raw_dir_rejects_unsafe_tickers(tmp_path: Path, etf: str):
    with pytest.raises(ValueError):
        raw_dir(tmp_path, etf)


def test_save_raw_writes_bytes_exactly_as_received(tmp_path: Path):
    payload = b"\xef\xbb\xbfFund,\r\n\"Oct 08, 2026\"\r\n\x00binary"
    paths = save_raw(tmp_path, "SOXX", AS_OF, {".json": b"{}", ".csv": payload})
    assert [p.name for p in paths] == ["2026-10-08.csv", "2026-10-08.json"]
    assert paths[0].read_bytes() == payload
    assert paths[1].read_bytes() == b"{}"
    assert not list(raw_dir(tmp_path, "SOXX").glob("*.tmp"))


def test_save_raw_rejects_text(tmp_path: Path):
    with pytest.raises(TypeError):
        save_raw(tmp_path, "SOXX", AS_OF, {".csv": "text"})  # type: ignore[dict-item]


def test_has_snapshot_and_save_normalized(tmp_path: Path):
    assert has_snapshot(tmp_path, "SOXX", AS_OF) is False
    path = save_normalized(tmp_path, _snapshot())
    assert path == snapshot_path(tmp_path, "SOXX", AS_OF)
    assert has_snapshot(tmp_path, "soxx", "2026-10-08") is True
    assert load_snapshot(path).holdings[0].ticker == "AMD"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_load_manifest_missing_file_is_empty(tmp_path: Path):
    assert load_manifest(tmp_path) == {"etfs": {}, "updated_utc": None}


def test_update_manifest_writes_deterministic_json(tmp_path: Path):
    update_manifest(tmp_path, "soxx", now=NOW, latest_as_of=AS_OF, last_status="ok", row_count=34)
    text = manifest_path(tmp_path).read_text(encoding="utf-8")
    assert text.endswith("\n") and not text.endswith("\n\n")
    expected = json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert text == expected
    data = json.loads(text)
    assert data["updated_utc"] == "2026-10-09T14:12:03Z"
    assert data["etfs"]["SOXX"] == {"latest_as_of": "2026-10-08", "last_status": "ok", "row_count": 34}


def test_update_manifest_merges_and_keeps_other_etfs(tmp_path: Path):
    update_manifest(tmp_path, "SOXX", now=NOW, latest_as_of=AS_OF, source="a")
    update_manifest(tmp_path, "QQQ", now=NOW, latest_as_of=AS_OF)
    manifest = update_manifest(tmp_path, "SOXX", now=NOW, last_status="error")
    assert manifest["etfs"]["SOXX"] == {"latest_as_of": "2026-10-08", "source": "a", "last_status": "error"}
    assert manifest["etfs"]["QQQ"] == {"latest_as_of": "2026-10-08"}


def test_update_manifest_rejects_unknown_field(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown manifest field"):
        update_manifest(tmp_path, "SOXX", latest_as_off=AS_OF)
    assert set(MANIFEST_FIELDS) >= {"latest_as_of", "last_fetch_utc", "last_status", "last_message", "row_count", "equity_count", "source"}


def test_load_manifest_recovers_from_corrupt_or_odd_file(tmp_path: Path):
    path = manifest_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_manifest(tmp_path) == {"etfs": {}, "updated_utc": None}
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_manifest(tmp_path) == {"etfs": {}, "updated_utc": None}
    write_manifest(tmp_path, {"etfs": {"SOXX": {"row_count": 1}, "BAD": 5}, "updated_utc": "x"})
    assert load_manifest(tmp_path)["etfs"] == {"SOXX": {"row_count": 1}}


def test_utc_now_iso_format():
    assert utc_now_iso(NOW) == "2026-10-09T14:12:03Z"
    assert utc_now_iso(datetime(2026, 10, 9, 16, 0, tzinfo=timezone(timedelta(hours=2)))) == "2026-10-09T14:00:00Z"


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def test_ingest_stores_raw_and_normalized_and_updates_manifest(tmp_path: Path):
    outcome = ingest(tmp_path, _ok_result(), now=NOW)
    assert outcome.stored is True
    assert outcome.ok and not outcome.failed
    assert outcome.reason == REASON_STORED
    assert outcome.as_of == AS_OF
    assert outcome.status == STATUS_OK
    raw = raw_path(tmp_path, "SOXX", AS_OF, ".csv")
    assert raw.read_bytes() == b"a,b\n1,2\n"
    assert has_snapshot(tmp_path, "SOXX", AS_OF)
    assert set(outcome.paths) == {raw, snapshot_path(tmp_path, "SOXX", AS_OF)}
    entry = load_manifest(tmp_path)["etfs"]["SOXX"]
    assert entry == {
        "latest_as_of": "2026-10-08",
        "last_fetch_utc": "2026-10-09T14:12:03Z",
        "last_status": "ok",
        "last_message": REASON_STORED,
        "row_count": 4,
        "equity_count": 3,
        "source": "test-source",
    }
    json.dumps(outcome.to_dict())


def test_ingest_is_idempotent_and_force_restores(tmp_path: Path):
    ingest(tmp_path, _ok_result(raw=b"first"), now=NOW)
    second = ingest(tmp_path, _ok_result(raw=b"second"), now=NOW)
    assert second.stored is False
    assert second.ok is True
    assert second.reason == REASON_ALREADY_STORED
    assert second.paths == []
    assert raw_path(tmp_path, "SOXX", AS_OF, ".csv").read_bytes() == b"first"
    # the manifest still describes the fetch that stored the data
    assert load_manifest(tmp_path)["etfs"]["SOXX"]["last_message"] == REASON_STORED

    forced = ingest(tmp_path, _ok_result(raw=b"third"), force=True, now=NOW)
    assert forced.stored is True
    assert forced.reason == REASON_FORCED
    assert raw_path(tmp_path, "SOXX", AS_OF, ".csv").read_bytes() == b"third"


def test_ingest_latest_as_of_tracks_newest_snapshot_on_disk(tmp_path: Path):
    ingest(tmp_path, _ok_result(as_of=date(2026, 10, 8)), now=NOW)
    ingest(tmp_path, _ok_result(as_of=date(2026, 10, 6)), now=NOW)  # backfilling an older date
    assert load_manifest(tmp_path)["etfs"]["SOXX"]["latest_as_of"] == "2026-10-08"


@pytest.mark.parametrize("status", [STATUS_NO_DATA, STATUS_ERROR, STATUS_UNSUPPORTED])
def test_ingest_non_ok_results_store_nothing_but_record_status(tmp_path: Path, status: str):
    ingest(tmp_path, _ok_result(), now=NOW)
    result = FetchResult("SOXX", status, None, {".csv": b"<html>"}, "boom", "", "2026-10-09")
    outcome = ingest(tmp_path, result, now=NOW)
    assert outcome.stored is False
    assert outcome.status == status
    assert outcome.as_of is None
    assert outcome.reason == f"{status}: boom"
    assert outcome.failed is (status == STATUS_ERROR)
    assert not raw_path(tmp_path, "SOXX", date(2026, 10, 9), ".csv").exists()
    entry = load_manifest(tmp_path)["etfs"]["SOXX"]
    assert entry["last_status"] == status
    assert entry["last_message"] == "boom"
    assert entry["latest_as_of"] == "2026-10-08"  # untouched
    assert entry["source"] == "test-source"  # a failed fetch does not blank the source
    assert entry["row_count"] == 4


def test_ingest_ok_result_without_snapshot_is_not_stored(tmp_path: Path):
    outcome = ingest(tmp_path, FetchResult("SOXX", STATUS_OK, None, {}, "no snapshot"), now=NOW)
    assert outcome.stored is False and outcome.status == STATUS_OK


def test_ingest_outcome_to_dict_is_jsonable():
    outcome = IngestOutcome("QQQ", AS_OF, True, REASON_STORED, paths=[Path("x/y.json")], source="s")
    data = json.loads(json.dumps(outcome.to_dict()))
    assert data == {
        "etf": "QQQ",
        "as_of": "2026-10-08",
        "stored": True,
        "reason": REASON_STORED,
        "status": STATUS_OK,
        "paths": ["x/y.json"],
        "source": "s",
    }


# ---------------------------------------------------------------------------
# Idempotency of the manifest (the daily job runs twice a day)
# ---------------------------------------------------------------------------


def _manifest_bytes(root: Path) -> bytes:
    return manifest_path(root).read_bytes()


def test_ingest_already_stored_leaves_manifest_file_untouched(tmp_path: Path):
    """Second run of the day: nothing new -> data/ must not change at all."""
    ingest(tmp_path, _ok_result(), now=NOW)
    before = _manifest_bytes(tmp_path)
    later = NOW + timedelta(hours=4)
    outcome = ingest(tmp_path, _ok_result(), now=later)
    assert outcome.reason == REASON_ALREADY_STORED
    assert _manifest_bytes(tmp_path) == before
    entry = load_manifest(tmp_path)["etfs"]["SOXX"]
    assert entry["last_fetch_utc"] == "2026-10-09T14:12:03Z"
    assert load_manifest(tmp_path)["updated_utc"] == "2026-10-09T14:12:03Z"


def test_ingest_already_stored_keeps_the_manifest_describing_the_file_on_disk(tmp_path: Path):
    """The re-served document differs in bookkeeping rows and source: nothing is written,
    so the manifest keeps the row counts and source of the snapshot on disk."""
    ingest(tmp_path, _ok_result(), now=NOW)
    before = _manifest_bytes(tmp_path)
    snap = _snapshot()
    trimmed = Snapshot(etf=snap.etf, as_of=snap.as_of, source="fallback-source", holdings=snap.holdings[:-1], meta=dict(snap.meta))
    reserved = FetchResult("SOXX", STATUS_OK, trimmed, {".csv": b"a,b\n1,2\n"}, "parsed", "fallback-source", AS_OF.isoformat())
    outcome = ingest(tmp_path, reserved, now=NOW + timedelta(hours=4))
    assert outcome.reason == REASON_ALREADY_STORED
    assert _manifest_bytes(tmp_path) == before
    entry = load_manifest(tmp_path)["etfs"]["SOXX"]
    assert entry["row_count"] == 4 and entry["equity_count"] == 3 and entry["source"] == "test-source"
    assert len(load_snapshot(snapshot_path(tmp_path, "SOXX", AS_OF)).holdings) == 4


def test_ingest_repeated_identical_failure_leaves_manifest_untouched(tmp_path: Path):
    ingest(tmp_path, _ok_result(), now=NOW)
    failure = FetchResult("SOXX", STATUS_ERROR, None, {}, "HTTP 503", "test-source", "2026-10-09")
    ingest(tmp_path, failure, now=NOW + timedelta(hours=1))
    before = _manifest_bytes(tmp_path)
    assert load_manifest(tmp_path)["etfs"]["SOXX"]["last_status"] == STATUS_ERROR
    ingest(tmp_path, failure, now=NOW + timedelta(hours=5))
    assert _manifest_bytes(tmp_path) == before


def test_ingest_status_changes_are_recorded(tmp_path: Path):
    ingest(tmp_path, _ok_result(), now=NOW)
    failure = FetchResult("SOXX", STATUS_ERROR, None, {}, "HTTP 503", "test-source", "2026-10-09")
    ingest(tmp_path, failure, now=NOW + timedelta(hours=1))
    entry = load_manifest(tmp_path)["etfs"]["SOXX"]
    assert entry["last_status"] == STATUS_ERROR and entry["last_message"] == "HTTP 503"
    assert entry["last_fetch_utc"] == "2026-10-09T15:12:03Z"
    assert entry["latest_as_of"] == "2026-10-08"
    # recovery: the same date is served again (already stored) -> status flips back to ok
    ingest(tmp_path, _ok_result(), now=NOW + timedelta(hours=2))
    entry = load_manifest(tmp_path)["etfs"]["SOXX"]
    assert entry["last_status"] == STATUS_OK and entry["last_message"] == REASON_ALREADY_STORED
    assert entry["last_fetch_utc"] == "2026-10-09T16:12:03Z"


def test_ingest_repairs_a_missing_manifest_entry(tmp_path: Path):
    ingest(tmp_path, _ok_result(), now=NOW)
    manifest_path(tmp_path).unlink()
    outcome = ingest(tmp_path, _ok_result(), now=NOW + timedelta(hours=1))
    assert outcome.reason == REASON_ALREADY_STORED
    entry = load_manifest(tmp_path)["etfs"]["SOXX"]
    assert entry["latest_as_of"] == "2026-10-08" and entry["row_count"] == 4
    assert entry["last_status"] == STATUS_OK


def test_ingest_force_rewrites_and_records_the_time(tmp_path: Path):
    ingest(tmp_path, _ok_result(), now=NOW)
    ingest(tmp_path, _ok_result(raw=b"again"), force=True, now=NOW + timedelta(hours=1))
    entry = load_manifest(tmp_path)["etfs"]["SOXX"]
    assert entry["last_message"] == REASON_FORCED
    assert entry["last_fetch_utc"] == "2026-10-09T15:12:03Z"


# ---------------------------------------------------------------------------
# A source handing back the wrong ETF must not land in another ETF's folder
# ---------------------------------------------------------------------------


def test_ingest_rejects_snapshot_of_another_etf(tmp_path: Path):
    result = FetchResult("IGV", STATUS_OK, _snapshot("SOXX"), {".csv": b"x"}, "parsed", "test-source")
    outcome = ingest(tmp_path, result, now=NOW)
    assert outcome.stored is False
    assert outcome.failed is True
    assert outcome.status == STATUS_ERROR
    assert "SOXX" in outcome.reason and "IGV" in outcome.reason
    assert not has_snapshot(tmp_path, "SOXX", AS_OF) and not has_snapshot(tmp_path, "IGV", AS_OF)
    assert not raw_path(tmp_path, "IGV", AS_OF, ".csv").exists()
    assert load_manifest(tmp_path)["etfs"]["IGV"]["last_status"] == STATUS_ERROR
