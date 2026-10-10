"""On-disk layout of the data layer: raw downloads, normalised snapshots and
the manifest.

Standard library only.

Layout under ``<root>``::

    data/raw/<ETF>/<YYYY-MM-DD><suffix>     bytes exactly as downloaded
    data/normalized/<ETF>/<YYYY-MM-DD>.json  :class:`~etf_tracker.holdings.Snapshot`
    data/manifest.json                       per-ETF bookkeeping (see below)

The manifest is small, deterministic JSON (sorted keys, two-space indent,
trailing newline) so the daily commit produces a readable diff::

    {
      "etfs": {
        "SOXX": {
          "equity_count": 30,
          "last_fetch_utc": "2026-10-09T14:12:03Z",
          "last_message": "stored",
          "last_status": "ok",
          "latest_as_of": "2026-10-08",
          "row_count": 33,
          "source": "blackrock-fund-document-csv"
        }
      },
      "updated_utc": "2026-10-09T14:12:03Z"
    }

:func:`ingest` is the single entry point the pipeline uses: it takes a
:class:`~etf_tracker.sources.FetchResult`, stores the raw bodies and the
normalised snapshot when the result is ``ok`` and the date is not stored
yet (or ``force`` is set), keeps the manifest in step, and reports what
happened as an :class:`IngestOutcome`.

The manifest is rewritten only when an entry *changes*: a date was stored,
a status flipped (``ok`` -> ``error`` -> ``ok``), the message of a failure
changed, or the entry is missing or out of step with the files on disk.
A second run of the day that finds the same date already stored, or a
repeat of the same failure, leaves ``data/`` byte-for-byte untouched, so
the daily job is idempotent and its commit step has nothing to commit.
``last_fetch_utc`` therefore records the fetch that last changed the entry,
not the most recent attempt (the Actions log has that).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from etf_tracker.holdings import (
    Snapshot,
    list_snapshots,
    save_snapshot,
    snapshot_path,
)
from etf_tracker.sources import STATUS_ERROR, STATUS_OK, FetchResult

__all__ = [
    "MANIFEST_FIELDS",
    "REASON_STORED",
    "REASON_FORCED",
    "REASON_ALREADY_STORED",
    "IngestOutcome",
    "data_dir",
    "raw_dir",
    "raw_path",
    "save_raw",
    "has_snapshot",
    "save_normalized",
    "manifest_path",
    "load_manifest",
    "write_manifest",
    "update_manifest",
    "utc_now_iso",
    "ingest",
]

log = logging.getLogger(__name__)

#: Keys kept per ETF in the manifest (anything else passed to
#: :func:`update_manifest` is rejected so typos do not accumulate).
MANIFEST_FIELDS: tuple[str, ...] = (
    "latest_as_of",
    "last_fetch_utc",
    "last_status",
    "last_message",
    "row_count",
    "equity_count",
    "source",
)

REASON_STORED = "stored"
REASON_FORCED = "stored (forced overwrite)"
REASON_ALREADY_STORED = "already stored"

#: A raw-file suffix is ``.csv``, ``.json``, ``.fund.json``...: dot-led,
#: short, and free of path separators or anything else surprising.
_SUFFIX_RE = re.compile(r"(\.[A-Za-z0-9_-]{1,16}){1,3}")
_ETF_RE = re.compile(r"[A-Z0-9]{1,10}")


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def _etf_key(etf: str) -> str:
    key = str(etf).strip().upper()
    if not _ETF_RE.fullmatch(key):
        raise ValueError(f"invalid ETF ticker {etf!r}")
    return key


def _as_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def utc_now_iso(now: datetime | None = None) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ`` for ``now`` (default: the current UTC time)."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def data_dir(root: str | os.PathLike[str]) -> Path:
    """``<root>/data``."""
    return Path(root) / "data"


def raw_dir(root: str | os.PathLike[str], etf: str) -> Path:
    """``<root>/data/raw/<ETF>``."""
    return data_dir(root) / "raw" / _etf_key(etf)


def raw_path(root: str | os.PathLike[str], etf: str, as_of: date | str, suffix: str) -> Path:
    """``<root>/data/raw/<ETF>/<YYYY-MM-DD><suffix>``.

    ``suffix`` must look like a file extension (``'.csv'``, ``'.fund.json'``);
    anything else raises ``ValueError`` because the suffix comes from a
    source module and ends up in a file name.
    """
    if not _SUFFIX_RE.fullmatch(suffix or ""):
        raise ValueError(f"invalid raw-file suffix {suffix!r}")
    return raw_dir(root, etf) / f"{_as_date(as_of).isoformat()}{suffix}"


# --------------------------------------------------------------------------
# Raw and normalised files
# --------------------------------------------------------------------------


def _write_bytes_atomic(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)


def save_raw(
    root: str | os.PathLike[str],
    etf: str,
    as_of: date | str,
    raw_files: Mapping[str, bytes],
) -> list[Path]:
    """Write every ``suffix -> bytes`` pair exactly as received.

    Returns the paths written, in suffix order.  Values must be ``bytes``
    (a source handing over text by mistake is a bug worth surfacing).
    """
    written: list[Path] = []
    for suffix in sorted(raw_files):
        payload = raw_files[suffix]
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError(f"raw file {suffix!r} must be bytes, got {type(payload).__name__}")
        target = raw_path(root, etf, as_of, suffix)
        _write_bytes_atomic(target, bytes(payload))
        written.append(target)
    return written


def has_snapshot(root: str | os.PathLike[str], etf: str, as_of: date | str) -> bool:
    """True when the normalised snapshot for ``as_of`` is already on disk."""
    return snapshot_path(root, _etf_key(etf), _as_date(as_of)).is_file()


def save_normalized(root: str | os.PathLike[str], snapshot: Snapshot) -> Path:
    """Store ``snapshot`` at :func:`etf_tracker.holdings.snapshot_path`."""
    target = snapshot_path(root, _etf_key(snapshot.etf), snapshot.as_of)
    return save_snapshot(snapshot, target)


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


def manifest_path(root: str | os.PathLike[str]) -> Path:
    """``<root>/data/manifest.json``."""
    return data_dir(root) / "manifest.json"


def _empty_manifest() -> dict[str, Any]:
    return {"etfs": {}, "updated_utc": None}


def load_manifest(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Read the manifest; a missing or unreadable file yields an empty one.

    The file is tracked in git and could be hand-edited, so an unparseable
    or oddly shaped manifest is logged and replaced rather than crashing
    the daily job.
    """
    path = manifest_path(root)
    if not path.is_file():
        return _empty_manifest()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("manifest %s unreadable (%s); starting from an empty one", path, exc)
        return _empty_manifest()
    if not isinstance(data, dict) or not isinstance(data.get("etfs"), dict):
        log.warning("manifest %s has an unexpected shape; starting from an empty one", path)
        return _empty_manifest()
    etfs = {str(k): dict(v) for k, v in data["etfs"].items() if isinstance(v, dict)}
    return {"etfs": etfs, "updated_utc": data.get("updated_utc")}


def write_manifest(root: str | os.PathLike[str], manifest: Mapping[str, Any]) -> Path:
    """Write ``manifest`` deterministically (sorted keys, indent 2, newline)."""
    path = manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def update_manifest(
    root: str | os.PathLike[str],
    etf: str,
    *,
    now: datetime | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Merge ``fields`` into the ETF's manifest entry and write the file.

    Only :data:`MANIFEST_FIELDS` are accepted.  ``date`` values are stored
    as ISO strings.  ``updated_utc`` is refreshed.  Returns the manifest.
    """
    unknown = sorted(set(fields) - set(MANIFEST_FIELDS))
    if unknown:
        raise ValueError(f"unknown manifest field(s): {', '.join(unknown)}")
    manifest = load_manifest(root)
    entry = manifest["etfs"].setdefault(_etf_key(etf), {})
    for key, value in fields.items():
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        entry[key] = value
    manifest["updated_utc"] = utc_now_iso(now)
    write_manifest(root, manifest)
    return manifest


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------


@dataclass
class IngestOutcome:
    """What :func:`ingest` did with one :class:`FetchResult`.

    ``stored`` is True only when files were written.  ``status`` is the
    fetch status (``ok``, ``no_data``, ``error``, ``unsupported``) so the
    caller can tell "nothing new" from "something broke"; :attr:`ok` is
    True for an ``ok`` result whether it was stored or already present.
    """

    etf: str
    as_of: date | None
    stored: bool
    reason: str
    status: str = STATUS_OK
    paths: list[Path] = field(default_factory=list)
    source: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def failed(self) -> bool:
        return self.status == STATUS_ERROR

    def to_dict(self) -> dict[str, Any]:
        return {
            "etf": self.etf,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "stored": self.stored,
            "reason": self.reason,
            "status": self.status,
            "paths": [str(p) for p in self.paths],
            "source": self.source,
        }


def _latest_stored(root: str | os.PathLike[str], etf: str) -> date | None:
    dates = list_snapshots(root, etf)
    return dates[-1] if dates else None


#: Manifest keys that may legitimately differ between two runs that found
#: the same state: the timestamp always, and the ok-message when a date
#: stored earlier is merely seen again ("stored" vs "already stored").
_VOLATILE_ALWAYS: frozenset[str] = frozenset({"last_fetch_utc"})


def _entry_changed(old: Mapping[str, Any], new: Mapping[str, Any], ignore: frozenset[str]) -> bool:
    """True when any key of ``new`` (except ``ignore``) differs from ``old``.

    Dates are compared as ISO strings, the form they take in the file.
    """
    for key, value in new.items():
        if key in ignore:
            continue
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        if old.get(key) != value:
            return True
    return False


def ingest(
    root: str | os.PathLike[str],
    result: FetchResult,
    force: bool = False,
    *,
    now: datetime | None = None,
) -> IngestOutcome:
    """Store a fetch result and record it in the manifest.

    * ``result.ok`` and the ``as_of`` date is new (or ``force``): the raw
      bodies go to ``data/raw/<ETF>/`` and the snapshot to
      ``data/normalized/<ETF>/``; ``stored=True``.
    * ``result.ok`` but already stored: nothing is written;
      ``reason='already stored'``.
    * any other status: nothing is written; ``reason`` carries the status
      and the source's message.

    The manifest entry is rewritten only when it changes (see the module
    docstring): ``latest_as_of`` always reflects the newest snapshot actually
    on disk, ``last_status``/``last_message`` the last fetch that changed
    something or failed, ``last_fetch_utc`` the time of that fetch.

    A snapshot whose ``etf`` is not the ETF the result claims to be for is
    refused as ``error`` (it would otherwise land in another ETF's folder).
    """
    etf = _etf_key(result.etf)
    snapshot = result.snapshot
    fetched_at = utc_now_iso(now)
    old_entry = load_manifest(root)["etfs"].get(etf, {})

    status = result.status
    message = result.message or status
    if result.ok and snapshot is not None and _etf_key(snapshot.etf) != etf:
        status = STATUS_ERROR
        message = f"source returned a snapshot for {_etf_key(snapshot.etf)}, not {etf}"
        snapshot = None

    if status != STATUS_OK or snapshot is None:
        reason = f"{status}: {message}" if message != status else status
        fields: dict[str, Any] = {
            "latest_as_of": _latest_stored(root, etf),
            "last_status": status,
            "last_message": message,
        }
        if result.source:
            fields["source"] = result.source
        if _entry_changed(old_entry, fields, _VOLATILE_ALWAYS):
            update_manifest(root, etf, now=now, last_fetch_utc=fetched_at, **fields)
        else:
            log.debug("%s: manifest entry unchanged (%s)", etf, reason)
        log.info("%s: not stored (%s)", etf, reason)
        return IngestOutcome(etf, None, False, reason, status=status, source=result.source)

    as_of = snapshot.as_of
    already = has_snapshot(root, etf, as_of)
    stored = False
    paths: list[Path] = []
    if already and not force:
        reason = REASON_ALREADY_STORED
    else:
        paths = save_raw(root, etf, as_of, result.raw_files)
        paths.append(save_normalized(root, snapshot))
        stored = True
        reason = REASON_FORCED if already else REASON_STORED

    fields = {
        "latest_as_of": _latest_stored(root, etf),
        "last_status": status,
        "last_message": reason,
        "row_count": len(snapshot.holdings),
        "equity_count": len(snapshot.equities()),
        "source": result.source or snapshot.source,
    }
    latest_iso = fields["latest_as_of"].isoformat() if isinstance(fields["latest_as_of"], date) else fields["latest_as_of"]
    if not stored and old_entry.get("latest_as_of") == latest_iso:
        # Nothing was written, so the manifest keeps describing the file on
        # disk, not the re-served document (its bookkeeping rows come and go).
        for key in ("row_count", "equity_count", "source"):
            if old_entry.get(key) is not None:
                fields[key] = old_entry[key]
    ignore = _VOLATILE_ALWAYS
    if not stored and old_entry.get("last_status") == STATUS_OK:
        # "stored" earlier today, "already stored" now: the same state.
        ignore = ignore | {"last_message"}
    if stored or _entry_changed(old_entry, fields, ignore):
        update_manifest(root, etf, now=now, last_fetch_utc=fetched_at, **fields)
    else:
        log.debug("%s %s: manifest entry unchanged", etf, as_of.isoformat())
    log.info("%s %s: %s", etf, as_of.isoformat(), reason)
    return IngestOutcome(etf, as_of, stored, reason, status=status, paths=paths, source=result.source)
