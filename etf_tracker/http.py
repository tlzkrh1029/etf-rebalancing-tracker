"""Minimal HTTP client for the data layer (standard library only).

urllib honours HTTP(S)_PROXY / NO_PROXY from the environment, which is what
both this container and GitHub Actions runners need. TLS verification is
never disabled.

Design notes
- Issuer edges are sensitive to the User-Agent. Invesco's Fastly edge returns a
  synthetic 406 to any UA starting with ``Mozilla/5.0`` (and to an empty UA);
  iShares/BlackRock return 403 to crawler UAs such as Googlebot. A neutral
  project UA works for both, so it is the default here.
- ``get`` never raises on HTTP errors: callers inspect ``Response.status`` so
  that a 406/404 body can be logged and classified.
"""
from __future__ import annotations

import gzip
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field

DEFAULT_USER_AGENT = (
    "etf-rebalancing-tracker/0.2 (+https://github.com/tlzkrh1029/etf-rebalancing-tracker)"
)
DEFAULT_TIMEOUT = 60.0


@dataclass
class Response:
    """Result of one HTTP GET."""

    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error: str | None = None
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")

    def looks_like_html(self) -> bool:
        head = self.body[:512].lstrip().lower()
        return head.startswith(b"<!doctype") or head.startswith(b"<html") or b"<html" in head


def _decode_body(raw: bytes, headers: dict[str, str]) -> bytes:
    enc = headers.get("content-encoding", "").lower()
    try:
        if enc == "gzip":
            return gzip.decompress(raw)
        if enc == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return raw
    return raw


def get(
    url: str,
    *,
    user_agent: str | None = DEFAULT_USER_AGENT,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
    backoff_s: float = 2.0,
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> Response:
    """GET ``url`` and return a :class:`Response`.

    ``user_agent=None`` sends no User-Agent header at all (urllib then sends
    its own ``Python-urllib/x.y``). Transient failures (network errors and the
    statuses in ``retry_statuses``) are retried with linear backoff.
    """
    hdrs = {"Accept": "*/*", "Accept-Encoding": "gzip, deflate"}
    if user_agent is not None:
        hdrs["User-Agent"] = user_agent
    if headers:
        hdrs.update(headers)
    last: Response | None = None
    for attempt in range(retries + 1):
        started = time.monotonic()
        req = urllib.request.Request(url, headers=hdrs, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                rh = {k.lower(): v for k, v in resp.headers.items()}
                last = Response(url, resp.status, rh, _decode_body(raw, rh), None, time.monotonic() - started)
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            rh = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
            last = Response(url, e.code, rh, _decode_body(raw, rh), None, time.monotonic() - started)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = Response(url, 0, {}, b"", f"{type(e).__name__}: {e}", time.monotonic() - started)
        if last.error is None and last.status not in retry_statuses:
            return last
        if attempt < retries:
            time.sleep(backoff_s * (attempt + 1))
    assert last is not None
    return last
