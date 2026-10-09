"""Issuer data sources.

Each source module exposes::

    ETFS: tuple[str, ...]                       # tickers it serves
    fetch(etf, as_of=None, *, http_get=None) -> FetchResult

``as_of`` is the holdings date requested (None = latest available). Sources
that cannot serve history (Invesco) must return status ``unsupported`` when a
past date is requested. ``http_get`` lets tests inject a fake transport with
the signature of :func:`etf_tracker.http.get`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from etf_tracker.holdings import Snapshot

STATUS_OK = "ok"
STATUS_NO_DATA = "no_data"          # endpoint answered but no holdings for that date (non-trading day, not yet published)
STATUS_ERROR = "error"              # transport/HTTP failure or unparseable body
STATUS_UNSUPPORTED = "unsupported"  # e.g. historical date requested from a source without history


@dataclass
class FetchResult:
    etf: str
    status: str
    snapshot: "Snapshot | None" = None
    raw_files: dict[str, bytes] = field(default_factory=dict)  # suffix -> bytes, e.g. {".csv": b"..."} or {".json": ..., ".fund.json": ...}
    message: str = ""
    source: str = ""
    requested_as_of: "str | None" = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK and self.snapshot is not None
