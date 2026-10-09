#!/usr/bin/env bash
# Probe whether the ETF issuer holdings downloads work from this network.
# Writes probe/summary.txt and saves bodies that look like CSV under probe/.
set -u
OUT=probe
mkdir -p "$OUT"
SUMMARY="$OUT/summary.txt"
: > "$SUMMARY"
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

log() { echo "$*" | tee -a "$SUMMARY"; }

log "probe run (UTC): $STAMP"
log "runner egress ip: $(curl -sS --max-time 15 https://api.ipify.org || echo unknown)"
log ""

classify() {
  # prints HTML or CSV or EMPTY based on the first bytes of a file
  local f=$1
  if [ ! -s "$f" ]; then echo EMPTY; return; fi
  if head -c 400 "$f" | tr -d '\0' | grep -qi '<!doctype\|<html'; then echo HTML; else echo CSV; fi
}

probe() {
  # probe <label> <url> [extra curl args...]
  local label=$1 url=$2; shift 2
  local body="$OUT/.$label.body" hdr="$OUT/.$label.hdr"
  local code
  code=$(curl -sS -L --compressed --max-time 60 -A "$UA" \
    -H 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8' \
    -H 'Accept-Language: en-US,en;q=0.9' \
    -o "$body" -D "$hdr" -w '%{http_code}' "$@" "$url" 2>>"$OUT/.curl.err" || echo "000")
  local ctype size kind
  ctype=$(grep -i '^content-type:' "$hdr" | tail -1 | cut -d' ' -f2- | tr -d '\r')
  size=$(stat -c %s "$body" 2>/dev/null || echo 0)
  kind=$(classify "$body")
  log "== $label"
  log "   url:   $url"
  log "   http:  $code  type: ${ctype:-?}  bytes: $size  looks-like: $kind"
  if [ "$kind" = HTML ]; then
    log "   title: $(grep -o -i '<title>[^<]*</title>' "$body" | head -1 | cut -c1-120)"
  elif [ "$kind" = CSV ]; then
    log "   first lines:"
    head -n 14 "$body" | cut -c1-200 | sed 's/^/     | /' | tee -a "$SUMMARY"
    log "   total lines: $(wc -l < "$body")"
    cp "$body" "$OUT/${label}_${STAMP}.csv"
  fi
  log ""
}

probe SOXX_ishares "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/1467271812596.ajax?fileType=csv&fileName=SOXX_holdings&dataType=fund"
probe IGV_ishares  "https://www.ishares.com/us/products/239771/ishares-north-american-tech-software-etf/1467271812596.ajax?fileType=csv&fileName=IGV_holdings&dataType=fund"
probe QQQ_invesco  "https://www.invesco.com/us/financial-products/etfs/holdings/main/holdings/0?audienceType=Investor&action=download&ticker=QQQ"
probe QQQ_invesco_page "https://www.invesco.com/us/en/etf/invesco-qqq-trust-series-1.html"
probe QQQ_qqqsite_page "https://www.invesco.com/qqq-etf/en/holdings.html"

log "== SOXX via python urllib (client fingerprint comparison)"
python3 - <<'PY' 2>&1 | tee -a "$SUMMARY"
import urllib.request, ssl
url = "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/1467271812596.ajax?fileType=csv&fileName=SOXX_holdings&dataType=fund"
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Accept": "text/csv,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        head = body[:400].decode("utf-8", "replace").lower()
        kind = "HTML" if ("<!doctype" in head or "<html" in head) else "CSV"
        print(f"   http: {r.status}  type: {r.headers.get('Content-Type')}  bytes: {len(body)}  looks-like: {kind}")
        if kind == "CSV":
            for line in body.decode("utf-8", "replace").splitlines()[:6]:
                print("     | " + line[:200])
except Exception as e:
    print(f"   error: {e!r}")
PY
log ""
log "done"
rm -f "$OUT"/.*.body "$OUT"/.*.hdr
exit 0
