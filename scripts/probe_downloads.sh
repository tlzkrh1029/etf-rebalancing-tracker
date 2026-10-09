#!/usr/bin/env bash
# Probe (v2) whether the current issuer data endpoints work from this network.
# Writes probe/summary.txt. Legacy endpoints (ishares .ajax CSV, invesco action=download) are retired: see probe history.
set -u
OUT=probe; mkdir -p "$OUT"; SUMMARY="$OUT/summary.txt"; : > "$SUMMARY"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
log() { echo "$*" | tee -a "$SUMMARY"; }
log "probe v2 run (UTC): $STAMP"
log "runner egress ip: $(curl -sS --max-time 15 https://api.ipify.org || echo unknown)"
log "curl: $(curl --version | head -1)"
log ""
classify() { local f=$1; if [ ! -s "$f" ]; then echo EMPTY; elif head -c 400 "$f" | tr -d '\0' | grep -qi '<!doctype\|<html'; then echo HTML; elif head -c 2 "$f" | grep -q '^[\[{]'; then echo JSON; else echo CSV; fi; }
probe() { # probe <label> <url> [curl args...]
  local label=$1 url=$2; shift 2
  local body="$OUT/.$label.body" hdr="$OUT/.$label.hdr" code
  code=$(curl -sS -L --compressed --max-time 60 -o "$body" -D "$hdr" -w '%{http_code}' "$@" "$url" 2>>"$OUT/.curl.err" || echo "000")
  local ctype size kind; ctype=$(grep -i '^content-type:' "$hdr" | tail -1 | cut -d' ' -f2- | tr -d '\r'); size=$(stat -c %s "$body" 2>/dev/null || echo 0); kind=$(classify "$body")
  log "== $label"; log "   http: $code  type: ${ctype:-?}  bytes: $size  looks-like: $kind"
  case $kind in
    HTML) log "   title: $(grep -o -i '<title>[^<]*</title>' "$body" | head -1 | cut -c1-120)";;
    CSV)  log "   as-of line: $(sed -n '2p' "$body" | cut -c1-80)"; log "   holding rows: $(awk 'f&&NF{c++} /^Ticker,Name/{f=1} END{print c+0}' "$body")"; head -n 11 "$body" | tail -n 2 | cut -c1-160 | sed 's/^/     | /' | tee -a "$SUMMARY";;
    JSON) log "   head: $(head -c 300 "$body" | tr '\n' ' ')";;
  esac
  log ""
}
BR='https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&userType=individual&component=holdings'
probe SOXX_blackrock_fund_document "$BR&portfolioId=239705" -A 'etf-rebalancing-tracker/1.0'
probe IGV_blackrock_fund_document  "$BR&portfolioId=239771" -A 'etf-rebalancing-tracker/1.0'
probe SOXX_blackrock_fund_document_asof20261001 "$BR&portfolioId=239705&asOfDate=20261001" -A 'etf-rebalancing-tracker/1.0'
probe SOXX_ishares_product_data_json 'https://www.ishares.com/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data?appSubType=ISHARES&appType=PRODUCT_PAGE&component=holdings.all&locale=en_US&portfolioId=239705&targetSite=us-ishares&userType=individual&excludeContent=true&includeConfig=true' -A 'etf-rebalancing-tracker/1.0'
probe QQQ_dng_api_default_ua "https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/fund?idType=ticker&productType=ETF&cb=$(date +%s)"
probe QQQ_dng_api_project_ua "https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/fund?idType=ticker&productType=ETF&cb=$(date +%s)1" -A 'etf-rebalancing-tracker/1.0'
probe QQQ_dng_api_mozilla_ua "https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/fund?idType=ticker&productType=ETF&cb=$(date +%s)2" -A 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
probe QQQ_dng_api_fundDetails "https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ?idType=ticker&variationType=fundDetails&productType=ETF&cb=$(date +%s)3"
log "== QQQ via python urllib default UA"
python3 - <<'PY' 2>&1 | tee -a "$SUMMARY"
import urllib.request, json, time
url=f"https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/fund?idType=ticker&productType=ETF&cb={int(time.time())}9"
try:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as r:
        b=r.read(); d=json.loads(b)
        print(f"   http: {r.status} bytes: {len(b)} effectiveDate: {d.get('effectiveDate')} holdings: {len(d.get('holdings',[]))}")
except Exception as e: print("   error:", repr(e))
PY
log ""; log "done"
rm -f "$OUT"/.*.body "$OUT"/.*.hdr "$OUT"/.curl.err
exit 0
