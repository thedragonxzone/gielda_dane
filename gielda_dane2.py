import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
import yfinance as yf
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QPushButton, QTextEdit,
    QVBoxLayout, QWidget
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_DIRECTORY = os.path.join(BASE_DIR, "pobrane_dane_v4")
CONFIG_PATH = os.path.join(BASE_DIR, "sources_config2.json")
TIMEOUT = 20
USER_AGENT = "MarketDataDownloader/4.0"
UTC = timezone.utc


# ----------------------------
# Konfiguracja / pomocnicze
# ----------------------------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def safe_label(name):
    return (
        str(name).replace("/", "_").replace("!", "")
        .replace(" ", "_").replace("%", "pct")
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
        "90d": 90,
        "1y": 365,
        "2y": 730,
    }[period]


def requested_window(period, interval):
    end = utc_now()
    start = end - timedelta(days=period_to_days(period))
    return start, end


def expected_timestamps(start, end, interval):
    freq = {"1h": "1h", "1d": "1D"}[interval]
    idx = pd.date_range(
        start=start.floor("h" if interval == "1h" else "D"),
        end=end.floor("h" if interval == "1h" else "D"),
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

    # Nie wymagamy pełnego pokrycia dla rynków tradycyjnych,
    # bo weekendy/święta są prawidłowymi brakami.
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
    status = "GOOD" if ratio <= 0.02 else ("WARN" if ratio <= 0.10 else "POOR")

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


def request_json(url, params=None, headers=None, retries=3):
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
                time.sleep(1.5 * (attempt + 1))
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
    r = requests.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    from io import StringIO
    raw = pd.read_csv(StringIO(r.text))
    if "DATE" not in raw.columns or series not in raw.columns:
        raise ValueError(f"Nieoczekiwany format FRED dla {series}")
    raw["DATE"] = pd.to_datetime(raw["DATE"], errors="coerce", utc=True)
    raw[series] = pd.to_numeric(raw[series], errors="coerce")
    raw = raw.dropna(subset=["DATE", series]).set_index("DATE")
    return pd.DataFrame({"Close": raw[series]})


# ----------------------------
# Binance public spot/futures
# ----------------------------

BINANCE_SPOT = "https://api.binance.com/api/v3/klines"
BINANCE_FUTURES = "https://fapi.binance.com/fapi/v1/klines"


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
        last_open = int(data[-1][0])
        next_start = last_open + 1
        if next_start <= start_ms:
            break
        start_ms = next_start
        if len(data) < limit:
            break
        time.sleep(0.15)

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


# ----------------------------
# CoinGlass
# ----------------------------

CG_BASE = "https://open-api-v4.coinglass.com"


def coinglass_request(endpoint, params, api_key):
    if not api_key:
        raise RuntimeError("Brak COINGLASS_API_KEY w zmiennych środowiskowych")
    headers = {
        "CG-API-KEY": api_key,
        "accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    data = request_json(CG_BASE + endpoint, params=params, headers=headers)
    if isinstance(data, dict) and str(data.get("code")) not in ("0", "200", "None"):
        raise RuntimeError(f"CoinGlass API: {data.get('msg', data)}")
    return data.get("data", data) if isinstance(data, dict) else data


def coinglass_history(inst, start, end):
    endpoint = inst["endpoint"]
    base_params = dict(inst.get("params") or {})
    interval = base_params.pop("interval", inst.get("interval", "1h"))
    limit = min(int(base_params.pop("limit", 1000)), 1000)

    # CoinGlass ogranicza pojedynczą odpowiedź do limitu rekordów.
    # 90 dni x 1h = 2160 rekordów, więc trzeba pobrać kilka stron.
    interval_ms = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000,
        "15m": 900_000, "30m": 1_800_000,
        "1h": 3_600_000, "4h": 14_400_000,
        "6h": 21_600_000, "8h": 28_800_000,
        "12h": 43_200_000, "1d": 86_400_000, "1w": 604_800_000,
    }.get(interval, 3_600_000)

    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    all_rows = []

    for _ in range(20):
        params = dict(base_params)
        params.update({
            "interval": interval,
            "limit": limit,
            "start_time": cursor,
            "end_time": end_ms,
        })
        data = coinglass_request(
            endpoint,
            params,
            os.getenv(inst.get("auth_env", "COINGLASS_API_KEY")),
        )
        if not isinstance(data, list) or not data:
            break

        all_rows.extend(data)
        time_col = next(
            (c for c in ["time", "timestamp", "t"] if c in data[0]), None
        )
        if not time_col:
            break

        times = []
        for row in data:
            try:
                times.append(int(row[time_col]))
            except (TypeError, ValueError):
                pass
        if not times:
            break

        last_time = max(times)
        if len(data) < limit or last_time >= end_ms - interval_ms:
            break

        next_cursor = last_time + interval_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.2)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    time_col = next((c for c in ["time", "timestamp", "t"] if c in df.columns), None)
    if time_col:
        df = df.drop_duplicates(subset=[time_col], keep="last")
        df = df.sort_values(time_col)
    return df


def coinglass_to_df(inst, start, end):
    df = coinglass_history(inst, start, end)
    if df.empty:
        return None

    time_col = next((c for c in ["time", "timestamp", "t"] if c in df.columns), None)
    if time_col:
        unit = "ms"
        vals = pd.to_numeric(df[time_col], errors="coerce")
        if vals.dropna().median() < 10_000_000_000:
            unit = "s"
        df.index = pd.to_datetime(vals, unit=unit, utc=True)
        df = df.drop(columns=[time_col], errors="ignore")

    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="ignore")

    return normalize_index(df)


# ----------------------------
# CoinGecko: global cap / dominance
# ----------------------------

CG_PUBLIC = "https://api.coingecko.com/api/v3"
CG_PRO = "https://pro-api.coingecko.com/api/v3"


def coingecko_get(path, params=None):
    api_key = os.getenv("COINGECKO_API_KEY")
    base = CG_PRO if api_key else CG_PUBLIC
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["x-cg-pro-api-key"] = api_key
    return request_json(base + path, params=params, headers=headers)


def coingecko_market_chart_range(coin_id, start, end):
    params = {
        "vs_currency": "usd",
        "from": int(start.timestamp()),
        "to": int(end.timestamp()),
    }
    data = coingecko_get(f"/coins/{coin_id}/market_chart/range", params)
    rows = data.get("market_caps", [])
    if not rows:
        raise ValueError(f"CoinGecko brak market_caps dla {coin_id}")
    df = pd.DataFrame(rows, columns=["timestamp_ms", "market_cap"])
    df["Datetime"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df = df.set_index("Datetime")[["market_cap"]]
    return df.resample("1D").last().dropna()


def coingecko_global_chart(start, end):
    data = coingecko_get(
        "/global/market_cap_chart",
        {
            "vs_currency": "usd",
            "days": max(2, (end - start).days),
        },
    )
    rows = data.get("market_cap_chart", {}).get("market_cap", [])
    if not rows:
        raise ValueError("CoinGecko brak global market cap")
    df = pd.DataFrame(rows, columns=["timestamp_ms", "total_market_cap"])
    df["Datetime"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    return df.set_index("Datetime")[["total_market_cap"]].resample("1D").last().dropna()


def build_global_derived(start, end):
    total = coingecko_global_chart(start, end)
    btc = coingecko_market_chart_range("bitcoin", start, end).rename(columns={"market_cap": "btc_market_cap"})
    eth = coingecko_market_chart_range("ethereum", start, end).rename(columns={"market_cap": "eth_market_cap"})
    sol = coingecko_market_chart_range("solana", start, end).rename(columns={"market_cap": "sol_market_cap"})

    df = total.join([btc, eth, sol], how="outer").sort_index().ffill()
    df["total_ex_btc"] = df["total_market_cap"] - df["btc_market_cap"]
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
    # Najczęściej pierwszy jest właściwym głównym aktywem.
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
    # DeFiLlama bywa niespójna co do jednostki timestampu.
    vals = pd.to_numeric(df["timestamp"], errors="coerce")
    unit = "s" if vals.dropna().median() < 10_000_000_000 else "ms"
    df["Datetime"] = pd.to_datetime(vals, unit=unit, utc=True)
    df["supply_usd"] = pd.to_numeric(df["supply_usd"], errors="coerce")
    df = df.dropna().set_index("Datetime").sort_index()
    return df[["supply_usd"]].resample("1D").last().loc[start:end]


# ----------------------------
# Specjalne źródła / instrumenty
# ----------------------------

def derive_cross_from_spot(base_ticker, quote_ticker, period, interval):
    base = yahoo_download(base_ticker, period, interval)
    quote = yahoo_download(quote_ticker, period, interval)
    if base is None or quote is None or base.empty or quote.empty:
        raise ValueError("Brak danych do wyliczenia cross")
    base = normalize_index(base)
    quote = normalize_index(quote)
    out = pd.DataFrame(index=base.index.union(quote.index).sort_values())
    for col in ["Open", "High", "Low", "Close"]:
        if col in base.columns and col in quote.columns:
            out[f"{col}"] = base[col].reindex(out.index) / quote[col].reindex(out.index)
    out["Volume"] = base["Volume"].reindex(out.index) if "Volume" in base.columns else pd.NA
    return out.dropna(subset=["Close"])


def taker_delta_from_coinglass(inst, start, end):
    df = coinglass_to_df(inst, start, end)
    if df is None or df.empty:
        return df
    buy = next((c for c in df.columns if "buy_volume" in c or c == "taker_buy_volume_usd"), None)
    sell = next((c for c in df.columns if "sell_volume" in c or c == "taker_sell_volume_usd"), None)
    if not buy or not sell:
        raise ValueError("CoinGlass taker endpoint nie zwrócił buy/sell volume")
    df["taker_delta_usd"] = df[buy] - df[sell]
    return df


def download_instrument(inst, period, interval):
    start, end = requested_window(period, interval)
    source = inst["source"]
    kind = inst.get("kind")

    if source == "YFINANCE":
        df = yahoo_download(inst["ticker"], period, interval)
        return df, start, end

    if source == "FRED_CSV":
        return fred_csv(inst["ticker"], start, end), start, end

    if source == "BINANCE_SPOT_KLINES":
        return binance_klines(BINANCE_SPOT, inst["ticker"], interval, start, end), start, end

    if source == "BINANCE_FUTURES_KLINES":
        return binance_klines(BINANCE_FUTURES, inst["ticker"], interval, start, end), start, end

    if source == "COINGLASS_HISTORY":
        if kind == "taker_delta":
            return taker_delta_from_coinglass(inst, start, end), start, end
        return coinglass_to_df(inst, start, end), start, end

    if source == "COINGECKO_DERIVED":
        df = build_global_derived(start, end)
        col = inst["field"]
        return df[[col]].rename(columns={col: "Close"}), start, end

    if source == "DEFILLAMA_STABLECOIN":
        return llama_stablecoin_history(inst["ticker"], start, end), start, end

    if source == "YFINANCE_DERIVED_CROSS":
        return derive_cross_from_spot(
            inst["base_ticker"], inst["quote_ticker"], period, interval
        ), start, end

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
        "endpoint": inst.get("endpoint"),
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

    def __init__(self, config_data, target_dir):
        super().__init__()
        self.config_data = config_data
        self.target_dir = target_dir

    def run(self):
        saved = 0
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

        self.info = QLabel(
            "1h: 90 dni | 1D: 2 lata. "
            "Derivatives: CoinGlass. Spot: Binance/Yahoo. "
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
                    f"{inst.get('ticker', inst.get('field', ''))} | "
                    f"{inst['period']}/{inst['interval']}"
                )
        self.instrument_text.setPlainText("\n".join(lines))

    def log(self, msg):
        self.log_text.append(
            f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        )
        self.status.setText(msg)

    def start_download(self):
        self.log("Start pobierania całego pakietu...")
        worker = BulkDownloadWorker(self.config_data, TARGET_DIRECTORY)
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