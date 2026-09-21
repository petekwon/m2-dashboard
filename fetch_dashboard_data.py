#!/usr/bin/env python3
"""
Fetch the M2 Bull Dashboard inputs that a browser can't pull directly (no CORS,
or an API key that must stay server-side) and write dashboard-data.json.

Standard library only. Environment variables:
  FRED_API_KEY      required for us10y and m2YoY   (free: https://fredaccount.stlouisfed.org)
  SOSO_API_KEY      required for btcEtf5d / ethEtf5d (SoSoValue OpenAPI)
  SOSO_ETF_URL      optional override of the SoSoValue ETF history endpoint
  M2_MODE           "us_ea" (default: US + Euro area in USD) or "us" (US M2 only)
  DASHBOARD_DATA_OUT  output path (default: dashboard-data.json)

A metric that fails keeps its previous value from the existing file, and the
failure is recorded under "errors" so the dashboard can flag it.
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.parse
import urllib.request

OUT = os.environ.get("DASHBOARD_DATA_OUT", "dashboard-data.json")
FRED_KEY = os.environ.get("FRED_API_KEY", "")
SOSO_KEY = os.environ.get("SOSO_API_KEY", "")
SOSO_URL = os.environ.get("SOSO_ETF_URL", "https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart")
M2_MODE = os.environ.get("M2_MODE", "us_ea")
UA = "m2-bull-dashboard/5"


def http(url, method="GET", headers=None, body=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json, text/csv;q=0.9"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


# ---------------------------------------------------------------- FRED
def fred(series_id, limit=40):
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY not set")
    q = urllib.parse.urlencode({
        "series_id": series_id, "api_key": FRED_KEY, "file_type": "json",
        "sort_order": "desc", "limit": limit,
    })
    j = json.loads(http("https://api.stlouisfed.org/fred/series/observations?" + q))
    return [(o["date"], float(o["value"])) for o in j["observations"] if o["value"] not in (".", "")]


def get_us10y():
    date, value = fred("DGS10", 10)[0]
    return {"value": round(value, 2), "asOf": date, "source": "FRED DGS10"}


# ---------------------------------------------------------------- M2
def ecb_euro_area_m2():
    """Euro area M2 outstanding amounts, EUR millions, seasonally adjusted (ECB BSI)."""
    url = ("https://data-api.ecb.europa.eu/service/data/BSI/"
           "M.U2.Y.V.M20.X.1.U2.2300.Z01.E?format=csvdata&lastNObservations=30")
    rows = csv.DictReader(io.StringIO(http(url)))
    return {r["TIME_PERIOD"] + "-01": float(r["OBS_VALUE"]) for r in rows if r.get("OBS_VALUE")}


def year_earlier(month_iso):
    y, m, _ = month_iso.split("-")
    return f"{int(y) - 1}-{m}-01"


def yoy(series, month):
    prior = year_earlier(month)
    if prior not in series:
        raise RuntimeError(f"no observation for {prior}")
    return (series[month] / series[prior] - 1) * 100


def get_m2():
    us = dict(fred("M2SL", 30))  # USD billions, SA, monthly
    if M2_MODE == "us":
        m = max(us)
        return {"value": round(yoy(us, m), 2), "asOf": m, "source": "FRED M2SL (US only)"}
    try:
        ea = ecb_euro_area_m2()              # EUR millions
        fx = dict(fred("EXUSEU", 30))        # USD per EUR, monthly average
        months = sorted(set(us) & set(ea) & set(fx))
        total = {m: us[m] + ea[m] * fx[m] / 1000 for m in months}  # USD billions
        m = months[-1]
        return {"value": round(yoy(total, m), 2), "asOf": m, "source": "FRED M2SL + ECB M2 in USD"}
    except Exception as e:  # degrade to US-only rather than going dark
        m = max(us)
        print(f"  m2: Euro area leg failed ({e}); using US only", file=sys.stderr)
        return {"value": round(yoy(us, m), 2), "asOf": m, "source": "FRED M2SL (US only, EA leg failed)"}


# ---------------------------------------------------------------- ETF flows
def soso_5_session(etf_type):
    if not SOSO_KEY:
        raise RuntimeError("SOSO_API_KEY not set")
    raw = json.loads(http(SOSO_URL, "POST", {"x-soso-api-key": SOSO_KEY}, {"type": etf_type}))
    if isinstance(raw, dict) and str(raw.get("code", 0)) not in ("0", "200"):
        raise RuntimeError(f"SoSoValue code {raw.get('code')}: {raw.get('msg')}")
    rows = raw.get("data", raw) if isinstance(raw, dict) else raw
    if isinstance(rows, dict):
        rows = rows.get("list") or rows.get("data") or []
    recs = []
    for r in rows:
        d, v = r.get("date"), r.get("totalNetInflow")
        if d is None or v is None:
            continue
        if isinstance(d, (int, float)):
            d = dt.datetime.fromtimestamp(d / 1000 if d > 1e11 else d, dt.timezone.utc).date().isoformat()
        recs.append((str(d)[:10], float(v)))
    recs.sort()
    if len(recs) < 5:
        raise RuntimeError(f"only {len(recs)} daily rows returned")
    last5 = recs[-5:]
    return {"value": round(sum(v for _, v in last5) / 1e9, 3), "asOf": last5[-1][0],
            "source": f"SoSoValue {etf_type}, sessions {last5[0][0]}..{last5[-1][0]}"}


# ---------------------------------------------------------------- 200W MA
def get_ma200w():
    url = "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=1w&limit=202"
    rows = json.loads(http(url))
    now_ms = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
    closed = [r for r in rows if int(r[6]) < now_ms]
    if len(closed) < 200:
        raise RuntimeError(f"{len(closed)} closed weeks")
    last = closed[-200:]
    avg = sum(float(r[4]) for r in last) / 200
    as_of = dt.datetime.fromtimestamp(int(last[-1][6]) / 1000, dt.timezone.utc).date().isoformat()
    return {"value": round(avg), "asOf": as_of, "source": "Binance.US weekly closes (server)"}


# ---------------------------------------------------------------- main
JOBS = {
    "us10y": get_us10y,
    "m2YoY": get_m2,
    "btcEtf5d": lambda: soso_5_session("us-btc-spot"),
    "ethEtf5d": lambda: soso_5_session("us-eth-spot"),
    "ma200w": get_ma200w,
}


def main():
    try:
        with open(OUT) as f:
            previous = json.load(f).get("metrics", {})
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}

    metrics, errors = {}, {}
    retrieved = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for key, job in JOBS.items():
        try:
            m = job()
            m["retrievedAt"] = retrieved
            metrics[key] = m
            print(f"  {key:9} {m['value']:>10}  as of {m['asOf']}  ({m['source']})")
        except Exception as e:
            errors[key] = f"{type(e).__name__}: {e}"[:200]
            if key in previous:
                metrics[key] = previous[key]  # carry forward; its asOf lets the page flag staleness
            print(f"  {key:9} FAILED: {errors[key]}", file=sys.stderr)

    doc = {"generatedAt": retrieved, "metrics": metrics, "errors": errors}
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, OUT)

    # Non-zero exit only when nothing worked, so one flaky source doesn't fail the whole job.
    return 1 if len(errors) == len(JOBS) else 0


if __name__ == "__main__":
    sys.exit(main())
