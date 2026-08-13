import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
import pandas as pd
import requests
import yfinance as yf
from io import StringIO
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QPushButton, QTextEdit,
    QVBoxLayout, QWidget, QCheckBox
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_DIRECTORY = os.path.join(BASE_DIR, "pobrane_dane_v4")
CONFIG_PATH = os.path.join(BASE_DIR, "sources_config2.json")
TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
UTC = timezone.utc

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def safe_label(name):
    return (
        str(name).replace("/", " ").replace("!", " ")
        .replace(" ", " ").replace("%", "pct")
    ).replace(":", "_")

def utc_now():
    return datetime.now(UTC)

def iso(dt):
    if dt is None:
        return None
    if isinstance(dt, pd.Timestamp):
        if dt.tzinfo is None:
            dt = dt.tz_localize("UTC")
        else:
            dt = dt.tz_convert("UTC")
        return dt.isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()

def period_to_days(period):
    return {
        "30d": 30,
        "90d": 90,
        "1y": 365,
        "2y": 730,
    }.get(period, 90)

def requested_window(period, interval):
    end = utc_now()
    start = end - timedelta(days=period_to_days(period))
    return start, end

def expected_timestamps(start, end, interval):
    freq = {"1h": "1h", "1d": "1D"}[interval]
    start_ts = pd.Timestamp(start).floor("h" if interval == "1h" else "D")
    end_ts = pd.Timestamp(end).floor("h" if interval == "1h" else "D")
    idx = pd.date_range(
        start=start_ts,
        end=end_ts,
        freq=freq,
        tz="UTC",
    )
    return idx

def normalize_index(df):
    if df is None or df.empty:
        return df
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    idx = pd.to_datetime(df.index, utc=True, errors="coerce")
    df.index = idx
    df = df[~df.index.isna()]
    df = df[~df.index.duplicated(keep="last")]
    return df.sort_index()

def numeric_columns(df):
    for c in df.columns:
        if c not in ("Datetime", "timestamp", "time"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

def dataframe_quality(df, interval, requested_start, requested_end):
    if df is None or df.empty:
        return {
            "status": "NO_DATA",
            "rows": 0,
            "expected_rows": len(expected_timestamps(requested_start, requested_end, interval)),
            "missing_rows": None,
            "missing_ratio": None,
            "duplicate_timestamps": 0,
            "monotonic_timestamps": True,
            "actual_start": None,
            "actual_end": None,
            "requested_start": iso(requested_start),
            "requested_end": iso(requested_end),
            "gaps": [],
        }
    idx = pd.DatetimeIndex(df.index).tz_convert("UTC")
    expected = expected_timestamps(requested_start, requested_end, interval)
    observed = pd.DatetimeIndex(idx).floor("h" if interval == "1h" else "D").unique()
    expected = pd.DatetimeIndex(expected).unique()
    missing = expected.difference(observed)
    dup_count = int(df.index.duplicated().sum())
    gap_list = []
    if len(observed) > 1:
        step = pd.Timedelta(hours=1 if interval == "1h" else 24)
        diffs = pd.Series(observed[1:] - observed[:-1])
        bad = diffs[diffs > step]
        for pos, delta in bad.items():
            i = int(pos)
            gap_list.append({
                "after": iso(observed[i]),
                "gap_hours": round(delta.total_seconds() / 3600, 2),
            })
    if interval == "1d":
        expected_business = pd.bdate_range(
            start=requested_start.date(), end=requested_end.date(), tz="UTC"
        )
        missing_business = expected_business.difference(observed)
        missing_count = int(len(missing_business))
        denominator = max(1, len(expected_business))
    else:
        missing_count = int(len(missing))
        denominator = max(1, len(expected))
    ratio = missing_count / denominator
    status = "GOOD" if ratio <= 0.05 else ("WARN" if ratio <= 0.15 else "POOR")
    return {
        "status": status,
        "rows": int(len(df)),
        "expected_rows": int(denominator),
        "missing_rows": missing_count,
        "missing_ratio": round(ratio, 6),
        "duplicate_timestamps": dup_count,
        "monotonic_timestamps": bool(df.index.is_monotonic_increasing),
        "actual_start": iso(idx.min()),
        "actual_end": iso(idx.max()),
        "requested_start": iso(requested_start),
        "requested_end": iso(requested_end),
        "gaps": gap_list[:200],
    }

def dataframe_to_payload(df, metadata):
    if df is None or df.empty:
        return None
    df = normalize_index(df)
    payload = json.loads(df.to_json(orient="split", date_format="iso"))
    payload["data_type"] = "ohlcv" if {"Open", "High", "Low", "Close", "Volume"}.issubset(df.columns) else "timeseries"
    payload["metadata"] = metadata
    return payload

def request_json(url, params=None, headers=None, retries=4):
    headers = headers or {}
    headers.setdefault("User-Agent", USER_AGENT)
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params or {}, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(15.0 * (attempt + 1))
    raise last

# ----------------------------
# Yahoo / FRED
# ----------------------------
def yahoo_download(ticker, period, interval):
    df = yf.download(
        ticker,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=False,
        prepost=False,
        threads=False,
    )
    return normalize_index(df)

def fred_csv(series, start=None, end=None):
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    params = {"id": series}
    if start:
        params["cosd"] = start.strftime("%Y-%m-%d")
    if end:
        params["coed"] = end.strftime("%Y-%m-%d")
        
    # Dodano mechanizm retry dla FRED, bo potrafi wyrzucać timeouty
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            break
        except Exception as exc:
            if attempt == 3:
                raise
            time.sleep(3.0 * (attempt + 1))
            
    raw = pd.read_csv(StringIO(r.text))
    if "DATE" not in raw.columns or series not in raw.columns:
        raise ValueError(f"Nieoczekiwany format FRED dla {series}")
    raw["DATE"] = pd.to_datetime(raw["DATE"], errors="coerce", utc=True)
    raw[series] = pd.to_numeric(raw[series], errors="coerce")
    raw = raw.dropna(subset=["DATE", series]).set_index("DATE")
    return pd.DataFrame({"Close": raw[series]})

# ----------------------------
# Binance Public Endpoints
# ----------------------------
BINANCE_SPOT = "https://api.binance.com/api/v3/klines"
BINANCE_FUTURES = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
BINANCE_OI = "https://fapi.binance.com/futures/data/openInterestHist"
BINANCE_TAKER = "https://fapi.binance.com/futures/data/takerlongshortRatio"

def binance_klines(url, symbol, interval, start, end, limit=1000):
    rows = []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": limit,
        }
        data = request_json(url, params=params)
        if not data:
            break
        rows.extend(data)
        last_close_time = int(data[-1][6])
        next_start = last_close_time + 1
        if next_start <= start_ms:
            break
        start_ms = next_start
        if len(data) < limit:
            break
        time.sleep(0.1)
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=[
        "Open time", "Open", "High", "Low", "Close", "Volume",
        "Close time", "Quote asset volume", "Number of trades",
        "Taker buy base asset volume", "Taker buy quote asset volume", "Ignore"
    ])
    df["Datetime"] = pd.to_datetime(df["Open time"], unit="ms", utc=True)
    df = df.set_index("Datetime")
    for c in [
        "Open", "High", "Low", "Close", "Volume",
        "Quote asset volume", "Number of trades",
        "Taker buy base asset volume", "Taker buy quote asset volume"
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[[
        "Open", "High", "Low", "Close", "Volume",
        "Quote asset volume", "Number of trades",
        "Taker buy base asset volume", "Taker buy quote asset volume"
    ]]

def binance_funding_rate(symbol, start, end):
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows = []
    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000
        }
        data = request_json(BINANCE_FUNDING, params=params)
        if not data:
            break
        rows.extend(data)
        last_time = int(data[-1]["fundingTime"])
        next_start = last_time + 1
        if next_start <= start_ms or len(data) < 1000:
            break
        start_ms = next_start
        time.sleep(0.1)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["Datetime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    df = df.set_index("Datetime")[["fundingRate"]].rename(columns={"fundingRate": "Close"})
    return normalize_index(df)

def binance_open_interest(symbol, period_interval, start, end):
    thirty_days_ago = utc_now() - timedelta(days=29)
    if start < thirty_days_ago:
        start = thirty_days_ago
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows = []
    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "period": period_interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 500
        }
        data = request_json(BINANCE_OI, params=params)
        if not data:
            break
        rows.extend(data)
        last_time = int(data[-1]["timestamp"])
        next_start = last_time + 1
        if next_start <= start_ms or len(data) < 500:
            break
        start_ms = next_start
        time.sleep(0.1)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["Datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["sumOpenInterestValue"] = pd.to_numeric(df["sumOpenInterestValue"], errors="coerce")
    df = df.set_index("Datetime")[["sumOpenInterestValue"]].rename(columns={"sumOpenInterestValue": "Close"})
    return normalize_index(df)

def binance_taker_ratio(symbol, period_interval, start, end):
    thirty_days_ago = utc_now() - timedelta(days=29)
    if start < thirty_days_ago:
        start = thirty_days_ago
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows = []
    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "period": period_interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 500
        }
        data = request_json(BINANCE_TAKER, params=params)
        if not data:
            break
        rows.extend(data)
        last_time = int(data[-1]["timestamp"])
        next_start = last_time + 1
        if next_start <= start_ms or len(data) < 500:
            break
        start_ms = next_start
        time.sleep(0.1)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["Datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["buySellRatio"] = pd.to_numeric(df["buySellRatio"], errors="coerce")
    df = df.set_index("Datetime")[["buySellRatio"]].rename(columns={"buySellRatio": "Close"})
    return normalize_index(df)

# ----------------------------
# CoinGecko (Free Tier + Resampling)
# ----------------------------
CG_PUBLIC = "https://api.coingecko.com/api/v3"

def coingecko_get(path, params=None):
    headers = {"User-Agent": USER_AGENT}
    api_key = os.getenv("COINGECKO_API_KEY")
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    time.sleep(2.0)  # Zabezpieczenie limitu
    return request_json(CG_PUBLIC + path, params=params, headers=headers)

def coingecko_market_chart_range(coin_id, start, end, field="prices"):
    params = {
        "vs_currency": "usd",
        "from": int(start.timestamp()),
        "to": int(end.timestamp()),
    }
    data = coingecko_get(f"/coins/{coin_id}/market_chart/range", params)
    rows = data.get(field, [])
    if not rows:
        raise ValueError(f"CoinGecko brak danych '{field}' dla {coin_id}")
    
    df = pd.DataFrame(rows, columns=["timestamp_ms", "value"])
    df["Datetime"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    
    if field == "market_caps":
        df = df.rename(columns={"value": "market_cap"})
        df = df.set_index("Datetime")[["market_cap"]]
        return df.resample("1D").last().dropna()
    else:
        df["Close"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.set_index("Datetime")[["Close"]].dropna().sort_index()
        return df

def resample_df(df, interval, start=None, end=None, fill_limit=3):
    if df is None or df.empty:
        return df
    df = normalize_index(df)
    freq = "1h" if interval == "1h" else "1D"
    df = df.resample(freq).last()
    if fill_limit is not None:
        df = df.ffill(limit=fill_limit)
        df = df.bfill(limit=fill_limit)
    df = df.dropna(how="all")
    if start is not None and end is not None:
        df = df.loc[pd.Timestamp(start):pd.Timestamp(end)]
    return df

def coingecko_close_series(coin_id, start, end, interval, field="prices"):
    df = coingecko_market_chart_range(coin_id, start, end, field)
    return resample_df(df, interval=interval, start=start, end=end, fill_limit=3)

def coingecko_ratio_series(base_coin_id, quote_coin_id, start, end, interval):
    base = coingecko_market_chart_range(base_coin_id, start, end, "prices").rename(columns={"Close": "base"})
    quote = coingecko_market_chart_range(quote_coin_id, start, end, "prices").rename(columns={"Close": "quote"})
    df = base.join(quote, how="outer").sort_index()
    df = df.ffill(limit=3)
    quote_safe = df["quote"].replace(0, pd.NA)
    close = df["base"] / quote_safe
    close = close.replace([float("inf"), float("-inf")], pd.NA)
    out = close.to_frame("Close").dropna()
    return resample_df(out, interval=interval, start=start, end=end, fill_limit=3)

def build_global_derived(start, end):
    btc = coingecko_market_chart_range("bitcoin", start, end, "market_caps").rename(columns={"market_cap": "btc_market_cap"})
    eth = coingecko_market_chart_range("ethereum", start, end, "market_caps").rename(columns={"market_cap": "eth_market_cap"})
    sol = coingecko_market_chart_range("solana", start, end, "market_caps").rename(columns={"market_cap": "sol_market_cap"})
    df = btc.join([eth, sol], how="outer").sort_index().ffill()
    df["total_market_cap"] = df["btc_market_cap"] + df["eth_market_cap"] + df["sol_market_cap"]
    df["total_ex_btc"] = df["eth_market_cap"] + df["sol_market_cap"]
    df["btc_dominance"] = df["btc_market_cap"] / df["total_market_cap"] * 100
    df["eth_dominance"] = df["eth_market_cap"] / df["total_market_cap"] * 100
    df["sol_dominance"] = df["sol_market_cap"] / df["total_market_cap"] * 100
    return df

# ----------------------------
# DeFiLlama stablecoin supply
# ----------------------------
LLAMA_STABLECOINS = "https://stablecoins.llama.fi"

def llama_stablecoin_list():
    data = request_json(
        f"{LLAMA_STABLECOINS}/stablecoins",
        params={"includePrices": "true"},
    )
    return data.get("peggedAssets", data if isinstance(data, list) else [])

def llama_find_stablecoin(symbol):
    assets = llama_stablecoin_list()
    matches = [
        x for x in assets
        if str(x.get("symbol", "")).upper() == symbol.upper()
    ]
    if not matches:
        raise ValueError(f"Nie znaleziono stablecoina {symbol} w DeFiLlama")
    return matches[0]

def llama_stablecoin_history(symbol, start, end):
    asset = llama_find_stablecoin(symbol)
    asset_id = asset.get("id")
    if asset_id is None:
        raise ValueError(f"Brak ID stablecoina {symbol}")
    data = request_json(
        f"{LLAMA_STABLECOINS}/stablecoincharts/all",
        params={"stablecoin": asset_id},
    )
    if not isinstance(data, list):
        raise ValueError(f"Nieznany format DeFiLlama dla {symbol}")
    rows = []
    for x in data:
        ts = x.get("date") or x.get("timestamp")
        if ts is None:
            continue
        supply = (
            x.get("totalCirculatingUSD")
            or x.get("totalCirculating")
            or x.get("circulating")
        )
        if isinstance(supply, dict):
            supply = sum(float(v or 0) for v in supply.values())
        if supply is None:
            continue
        rows.append([ts, supply])
    if not rows:
        raise ValueError(f"Brak historii supply dla {symbol}")
    df = pd.DataFrame(rows, columns=["timestamp", "supply_usd"])
    vals = pd.to_numeric(df["timestamp"], errors="coerce")
    unit = "s" if vals.dropna().median() < 10_000_000_000 else "ms"
    df["Datetime"] = pd.to_datetime(vals, unit=unit, utc=True)
    df["supply_usd"] = pd.to_numeric(df["supply_usd"], errors="coerce")
    df = df.dropna().set_index("Datetime").sort_index()
    return df[["supply_usd"]].resample("1D").last().loc[start:end]

# ----------------------------
# Pobieranie pojedynczego instrumentu
# ----------------------------
def download_instrument(inst, period, interval):
    start, end = requested_window(period, interval)
    source = inst["source"]
    
    if source == "YFINANCE":
        df = yahoo_download(inst["ticker"], period, interval)
        return df, start, end
    if source == "FRED_CSV":
        return fred_csv(inst["ticker"], start, end), start, end
    if source == "BINANCE_SPOT_KLINES":
        return binance_klines(BINANCE_SPOT, inst["ticker"], interval, start, end), start, end
    if source == "BINANCE_FUTURES_KLINES":
        return binance_klines(BINANCE_FUTURES, inst["ticker"], interval, start, end), start, end
    if source == "BINANCE_FUNDING":
        return binance_funding_rate(inst["ticker"], start, end), start, end
    if source == "BINANCE_OI":
        return binance_open_interest(inst["ticker"], interval, start, end), start, end
    if source == "BINANCE_TAKER_RATIO":
        return binance_taker_ratio(inst["ticker"], interval, start, end), start, end
        
    if source == "COINGECKO_MARKET_CHART":
        return (
            coingecko_close_series(
                coin_id=inst["coin_id"],
                start=start,
                end=end,
                interval=interval,
                field=inst.get("field", "prices"),
            ),
            start,
            end,
        )
    if source == "COINGECKO_RATIO":
        return (
            coingecko_ratio_series(
                base_coin_id=inst["base_coin_id"],
                quote_coin_id=inst["quote_coin_id"],
                start=start,
                end=end,
                interval=interval,
            ),
            start,
            end,
        )
    if source == "COINGECKO_DERIVED":
        df = build_global_derived(start, end)
        col = inst["field"]
        return df[[col]].rename(columns={col: "Close"}), start, end
        
    if source == "DEFILLAMA_STABLECOIN":
        return llama_stablecoin_history(inst["ticker"], start, end), start, end
        
    raise ValueError(f"Nieznane źródło: {source}")

def save_instrument(name, inst, df, start, end, target_dir):
    os.makedirs(target_dir, exist_ok=True)
    df = normalize_index(df)
    df = numeric_columns(df)
    metadata = dataframe_quality(df, inst["interval"], start, end)
    metadata.update({
        "instrument": name,
        "source": inst.get("source"),
        "source_url": inst.get("source_url"),
        "interval": inst["interval"],
        "period": inst["period"],
        "retrieved_at": iso(utc_now()),
        "timezone": "UTC",
        "notes": inst.get("note"),
    })
    payload = dataframe_to_payload(df, metadata)
    if payload is None:
        raise RuntimeError("brak danych")
    filename = f"{safe_label(name)}_{inst['period']}_{inst['interval']}.json"
    path = os.path.join(target_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path, metadata

# ----------------------------
# Raport jakości
# ----------------------------
def generate_report(config, target_dir):
    report = {
        "generated_at": iso(utc_now()),
        "instruments": {},
        "summary": {"good": 0, "warn": 0, "poor": 0, "missing": 0, "error": 0},
    }
    human = [
        "# RAPORT JAKOŚCI DANYCH v4",
        f"Wygenerowano UTC: {report['generated_at']}",
        "",
    ]
    for universe_key, universe in config.items():
        if universe_key == "meta":
            continue
        human.append(f"## {universe.get('description', universe_key)}")
        for name, inst in universe["instruments"].items():
            filename = f"{safe_label(name)}_{inst['period']}_{inst['interval']}.json"
            path = os.path.join(target_dir, filename)
            if not os.path.exists(path):
                report["summary"]["missing"] += 1
                report["instruments"][name] = {"status": "MISSING_FILE"}
                human.append(f"- {name}: BRAK PLIKU")
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                meta = payload.get("metadata", {})
                status = meta.get("status", "UNKNOWN")
                report["instruments"][name] = {
                    "status": status,
                    "rows": meta.get("rows"),
                    "expected_rows": meta.get("expected_rows"),
                    "missing_rows": meta.get("missing_rows"),
                    "missing_ratio": meta.get("missing_ratio"),
                    "actual_start": meta.get("actual_start"),
                    "actual_end": meta.get("actual_end"),
                    "source": inst.get("source"),
                    "source_url": inst.get("source_url"),
                }
                if status == "GOOD":
                    report["summary"]["good"] += 1
                elif status == "WARN":
                    report["summary"]["warn"] += 1
                elif status == "POOR":
                    report["summary"]["poor"] += 1
                human.append(
                    f"- {name}: {status} | rows={meta.get('rows')} | "
                    f"braki={meta.get('missing_rows')} | "
                    f"missing_ratio={meta.get('missing_ratio')} | "
                    f"{meta.get('actual_start')} → {meta.get('actual_end')}"
                )
            except Exception as exc:
                report["summary"]["error"] += 1
                report["instruments"][name] = {"status": "ERROR", "error": str(exc)}
                human.append(f"- {name}: BŁĄD ODCZYTU: {exc}")
        human.append("")
    with open(os.path.join(target_dir, "raport_jakosci_v4.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(target_dir, "raport_jakosci_v4.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(human))

# ----------------------------
# Worker / GUI
# ----------------------------
class BulkDownloadWorker(QThread):
    finished_signal = pyqtSignal(str, bool)
    progress = pyqtSignal(str)

    def __init__(self, config_data, target_dir, skip_existing=False):
        super().__init__()
        self.config_data = config_data
        self.target_dir = target_dir
        self.skip_existing = skip_existing

    def run(self):
        saved = 0
        skipped = 0
        errors = []
        try:
            instruments = []
            for universe_key, universe in self.config_data.items():
                if universe_key == "meta":
                    continue
                for name, inst in universe["instruments"].items():
                    instruments.append((universe_key, name, inst))
            total = len(instruments)
            for i, (universe_key, name, inst) in enumerate(instruments, 1):
                filename = f"{safe_label(name)}_{inst['period']}_{inst['interval']}.json"
                path = os.path.join(self.target_dir, filename)
                
                if self.skip_existing and os.path.exists(path):
                    skipped += 1
                    self.progress.emit(f"POMINIĘTY (istnieje): {name}")
                    continue
                
                self.progress.emit(
                    f"Pobieranie ({i}/{total}): {name} "
                    f"[{inst.get('source')}] {inst.get('period')}/{inst.get('interval')}"
                )
                try:
                    df, start, end = download_instrument(
                        inst, inst["period"], inst["interval"]
                    )
                    path, meta = save_instrument(
                        name, inst, df, start, end, self.target_dir
                    )
                    saved += 1
                    self.progress.emit(
                        f"OK: {name} | {meta['status']} | rows={meta['rows']} | "
                        f"braki={meta['missing_rows']}"
                    )
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
                    self.progress.emit(f"BŁĄD: {name}: {exc}")
            generate_report(self.config_data, self.target_dir)
            msg = (
                f"Gotowe. Zapisano: {saved}/{total}. "
                f"Pominięto: {skipped}. "
                f"Błędy: {len(errors)}. Raport jakości wygenerowany."
            )
            if errors:
                msg += " Pierwsze: " + " | ".join(errors[:5])
            self.finished_signal.emit(msg, True)
        except Exception as exc:
            self.finished_signal.emit(f"Błąd ogólny: {exc}", False)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Market Data Downloader v4 — BTC / ETH / SOL / Macro")
        self.resize(1550, 950)
        self.config_data = load_config()
        self.workers = []
        self.init_ui()

    def init_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(QLabel(
            f"Konfiguracja: <b>{CONFIG_PATH}</b> | "
            f"Dane: <b>{TARGET_DIRECTORY}</b>"
        ))
        group = QGroupBox("Zakres danych")
        form = QFormLayout()
        self.combo_scope = QComboBox()
        self.combo_scope.addItem("Cały wymagany pakiet", "ALL")
        form.addRow("Zakres:", self.combo_scope)
        
        self.skip_existing_checkbox = QCheckBox("Pomiń już pobrane pliki")
        self.skip_existing_checkbox.setChecked(True)
        form.addRow(self.skip_existing_checkbox)
        
        self.info = QLabel(
            "1h: 90 dni | 1D: 1 rok (CoinGecko limit) | 2 lata (Binance/Yahoo). "
            "OI & Taker Ratio max 30 dni. "
            "Derivatives: Binance Futures. Spot: Binance/Yahoo/CoinGecko. "
            "Macro: Yahoo/FRED. Dominance/market cap: CoinGecko. "
            "Stablecoin supply: DeFiLlama."
        )
        form.addRow("Plan:", self.info)
        group.setLayout(form)
        layout.addWidget(group)
        buttons = QHBoxLayout()
        b1 = QPushButton("🚀 Pobierz cały pakiet")
        b1.clicked.connect(self.start_download)
        buttons.addWidget(b1)
        b2 = QPushButton("📊 Odśwież raport jakości")
        b2.clicked.connect(self.generate_report)
        buttons.addWidget(b2)
        layout.addLayout(buttons)
        splitter = QGridLayout()
        self.instrument_text = QTextEdit()
        self.instrument_text.setReadOnly(True)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        splitter.addWidget(self.instrument_text, 0, 0)
        splitter.addWidget(self.log_text, 0, 1)
        layout.addLayout(splitter)
        self.status = QLabel("Gotowy.")
        layout.addWidget(self.status)
        self.setCentralWidget(central)
        self.refresh_display()

    def refresh_display(self):
        lines = []
        for uk, u in self.config_data.items():
            if uk == "meta":
                continue
            lines.append(f"[{uk}] {u['description']}")
            for name, inst in u["instruments"].items():
                lines.append(
                    f"  • {name} | {inst['source']} | "
                    f"{inst.get('ticker', inst.get('field', inst.get('coin_id', '')))} | "
                    f"{inst['period']}/{inst['interval']}"
                )
        self.instrument_text.setPlainText("\n".join(lines))

    def log(self, msg):
        self.log_text.append(
            f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        )
        self.status.setText(msg)

    def start_download(self):
        skip_existing = self.skip_existing_checkbox.isChecked()
        self.log(f"Start pobierania całego pakietu... (Pomiń istniejące: {skip_existing})")
        worker = BulkDownloadWorker(self.config_data, TARGET_DIRECTORY, skip_existing)
        worker.progress.connect(self.log)
        worker.finished_signal.connect(self.finished)
        self.workers.append(worker)
        worker.start()

    def finished(self, msg, ok):
        self.log(msg)
        worker = self.sender()
        if worker in self.workers:
            self.workers.remove(worker)
        worker.deleteLater()

    def generate_report(self):
        if not os.path.exists(TARGET_DIRECTORY):
            self.log("Brak katalogu danych.")
            return
        generate_report(self.config_data, TARGET_DIRECTORY)
        self.log("Raport jakości v4 zapisany.")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet("QWidget { font-size: 12pt; }")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())