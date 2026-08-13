#!/usr/bin/env python3
"""
okresl_rynek.py

Market Profile Builder – wczytuje znormalizowane pliki JSON,
ocenia jakość danych, liczy score'y reżimu i wybiera profil bazowy
dla programu gielda_analiza.py.

Użycie:
    python okresl_rynek.py [--data-dir KATALOG] [--output PLIK] [--verbose]
"""

import json
import os
import re
import glob
import argparse
from datetime import datetime, timezone

import pandas as pd
import numpy as np


# ============================================================
# 1. DEFINICJE 12 PROFILI BAZOWYCH
# ============================================================

PROFILES = {
    "BASE_PROFILE_FALLBACK_NEUTRAL": {
        "display_name": "Fallback Neutral",
        "description": "Brak wystarczających danych lub sprzeczne sygnały.",
        "bias": "neutral",
    },
    "BASE_PROFILE_HIGH_VOL_STRESS": {
        "display_name": "High Volatility Stress",
        "description": "Ekstremalna zmienność, ryzyko nagłych ruchów i fałszywych wybić.",
        "bias": "defensive",
    },
    "BASE_PROFILE_MACRO_RISK_OFF_DEFENSIVE": {
        "display_name": "Macro Risk-Off Defensive",
        "description": "Makro defensywne, ostrożność nawet jeśli crypto nie jest w downtrendzie.",
        "bias": "defensive",
    },
    "BASE_PROFILE_BEAR_TREND_DEFENSIVE": {
        "display_name": "Bear Trend Defensive",
        "description": "Crypto w trendzie spadkowym.",
        "bias": "short",
    },
    "BASE_PROFILE_BEAR_BOUNCE_CAUTIOUS": {
        "display_name": "Bear Bounce Cautious",
        "description": "Trend spadkowy z krótkoterminowym odbiciem.",
        "bias": "short",
    },
    "BASE_PROFILE_RANGE_COMPRESSION_WAIT": {
        "display_name": "Range Compression Wait",
        "description": "Konsolidacja z niską zmiennością, rynek się ściska.",
        "bias": "neutral",
    },
    "BASE_PROFILE_RANGE_CHOP_SELECTIVE": {
        "display_name": "Range Chop Selective",
        "description": "Chaotyczna konsolidacja, dużo fałszywych ruchów.",
        "bias": "neutral",
    },
    "BASE_PROFILE_BULL_TREND_MOMENTUM": {
        "display_name": "Bull Trend Momentum",
        "description": "Szeroki trend wzrostowy.",
        "bias": "long",
    },
    "BASE_PROFILE_BULL_PULLBACK_CONSTRUCTIVE": {
        "display_name": "Bull Pullback Constructive",
        "description": "Trend wzrostowy z aktualną korektą.",
        "bias": "long",
    },
    "BASE_PROFILE_ALTSEASON_ROTATION": {
        "display_name": "Altseason Rotation",
        "description": "Kapitał rotuje w alty, ETH/SOL mocniejsze względem BTC.",
        "bias": "long",
    },
    "BASE_PROFILE_BTC_LEADERSHIP_ALTS_WEAK": {
        "display_name": "BTC Leadership Alts Weak",
        "description": "BTC mocny, alty słabe.",
        "bias": "neutral",
    },
    "BASE_PROFILE_SOL_RECOVERY_SELECTIVE": {
        "display_name": "SOL Recovery Selective",
        "description": "SOL zaczyna odbijać, ale nie jest jeszcze potwierdzony jako lider.",
        "bias": "long",
    },
}


# ============================================================
# 2. MAPOWANIE NAZW PLIKÓW NA ROLE
# ============================================================

ROLE_PATTERNS = [
    ("BTC_USD_90d_1h",              "btc_usd_1h"),
    ("BTC_USD_1D_1y_1d",            "btc_usd_1d"),
    ("ETH_USD_90d_1h",              "eth_usd_1h"),
    ("ETH_USD_1D_1y_1d",            "eth_usd_1d"),
    ("SOL_USD_90d_1h",              "sol_usd_1h"),
    ("SOL_USD_1D_1y_1d",            "sol_usd_1d"),
    ("SOL_BTC_90d_1h",              "sol_btc_1h"),
    ("SOL_BTC_1D_1y_1d",            "sol_btc_1d"),
    ("ETH_BTC_90d_1h",              "eth_btc_1h"),
    ("ETH_BTC_1D_1y_1d",            "eth_btc_1d"),
    ("BTC_PERP_OHLCV_90d_1h",       "btc_perp_1h"),
    ("ETH_PERP_OHLCV_90d_1h",       "eth_perp_1h"),
    ("SOL_PERP_OHLCV_90d_1h",       "sol_perp_1h"),
    ("BTC_FUNDING_90d_1h",          "btc_funding_1h"),
    ("ETH_FUNDING_90d_1h",          "eth_funding_1h"),
    ("SOL_FUNDING_90d_1h",          "sol_funding_1h"),
    ("BTC_OI_90d_1h",               "btc_oi_1h"),
    ("ETH_OI_90d_1h",               "eth_oi_1h"),
    ("SOL_OI_90d_1h",               "sol_oi_1h"),
    ("BTC_TAKER_RATIO_90d_1h",      "btc_taker_1h"),
    ("ETH_TAKER_RATIO_90d_1h",      "eth_taker_1h"),
    ("SOL_TAKER_RATIO_90d_1h",      "sol_taker_1h"),
    ("BTC_DOMINANCE_1y_1d",         "btc_dominance_1d"),
    ("ETH_DOMINANCE_1y_1d",         "eth_dominance_1d"),
    ("SOL_DOMINANCE_1y_1d",         "sol_dominance_1d"),
    ("TOTAL_MARKET_CAP_1y_1d",      "total_mcap_1d"),
    ("TOTAL_EX_BTC_1y_1d",          "total_ex_btc_1d"),
    ("USDT_SUPPLY_2y_1d",           "usdt_supply_1d"),
    ("USDC_SUPPLY_2y_1d",           "usdc_supply_1d"),
    ("DXY_2y_1d",                   "dxy_1d"),
    ("GOLD_2y_1d",                  "gold_1d"),
    ("NASDAQ100_2y_1d",             "nasdaq100_1d"),
    ("SP500_2y_1d",                 "sp500_1d"),
    ("USDJPY_2y_1d",                "usdjpy_1d"),
    ("VIX_2y_1d",                   "vix_1d"),
    ("US10Y",                       "us10y_1d"),
]


def classify_file(filename: str):
    """Dopasuj nazwę pliku do roli."""
    base = os.path.basename(filename).replace("_znormalizowany.json", "")
    base_upper = base.upper()

    # Najpierw dokładne dopasowanie
    for pattern, role in ROLE_PATTERNS:
        if base_upper == pattern.upper():
            return role

    # Fallback: dopasowanie częściowe
    for pattern, role in ROLE_PATTERNS:
        if pattern.upper() in base_upper:
            return role

    return None


# ============================================================
# 3. WCZYTYWANIE PLIKÓW
# ============================================================

def to_float(value):
    """Bezpieczna konwersja do float."""
    if value is None:
        return np.nan
    try:
        return float(value)
    except Exception:
        return np.nan


def load_normalized_file(filepath: str):
    """Wczytaj znormalizowany plik JSON, zwróć metadane i dane."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        print(f"  [WARN] Nie można wczytać {filepath}: {e}")
        return None

    meta = {
        "source_file":   doc.get("source_file", ""),
        "instrument":    doc.get("instrument", ""),
        "dataset":       doc.get("dataset", ""),
        "interval":      doc.get("interval", ""),
        "status":        doc.get("status", "UNKNOWN"),
        "quality_flag":  doc.get("quality_flag", ""),
        "quality_score": doc.get("quality_score", 0),
        "missing_ratio": doc.get("missing_ratio"),
        "value_metric":  doc.get("value_metric", ""),
        "records_count": len(doc.get("records", [])),
    }

    records = doc.get("records", [])
    if not records:
        return {"meta": meta, "series": None, "df": None}

    timestamps = []
    values = []
    ohlcv = {"open": [], "high": [], "low": [], "close": [], "volume": []}
    has_ohlcv = False

    for r in records:
        ts_raw = r.get("timestamp_utc")
        if ts_raw is None:
            continue

        try:
            dt = pd.to_datetime(ts_raw, utc=True)
        except Exception:
            continue

        timestamps.append(dt)

        if "values" in r and isinstance(r["values"], dict):
            has_ohlcv = True
            for k in ohlcv:
                ohlcv[k].append(to_float(r["values"].get(k)))
        else:
            values.append(to_float(r.get("value")))

    if not timestamps:
        return {"meta": meta, "series": None, "df": None}

    idx = pd.DatetimeIndex(timestamps)

    if has_ohlcv:
        df = pd.DataFrame(ohlcv, index=idx).sort_index()
        return {"meta": meta, "series": None, "df": df}

    series = pd.Series(values, index=idx).sort_index().dropna()
    return {"meta": meta, "series": series, "df": None}


def load_all_data(data_dir: str):
    """Wczytaj wszystkie znormalizowane pliki i przypisz role."""
    pattern = os.path.join(data_dir, "*_znormalizowany.json")
    files = sorted(glob.glob(pattern))

    data = {}
    quality = []

    for fp in files:
        role = classify_file(fp)
        if role is None:
            print(f"  [WARN] Nie rozpoznano roli: {os.path.basename(fp)}")
            continue

        result = load_normalized_file(fp)
        if result is None:
            continue

        # Jeśli przypadkiem są duplikaty roli, zostaw tę z większą liczbą rekordów
        if role in data:
            old_count = data[role]["meta"].get("records_count", 0)
            new_count = result["meta"].get("records_count", 0)
            if new_count <= old_count:
                continue

        data[role] = result
        quality.append({
            "file":          os.path.basename(fp),
            "role":          role,
            "status":        result["meta"]["status"],
            "quality_score": result["meta"]["quality_score"],
            "missing_ratio": result["meta"]["missing_ratio"],
            "records_count": result["meta"]["records_count"],
        })

    return data, quality


# ============================================================
# 4. HELPERY
# ============================================================

def get_series(data: dict, role: str):
    """Pobierz serię cen/wartości dla danej roli."""
    entry = data.get(role)
    if entry is None:
        return None

    if entry.get("series") is not None and len(entry["series"]) > 0:
        return entry["series"]

    df = entry.get("df")
    if df is not None and "close" in df.columns:
        s = df["close"].dropna()
        if len(s) > 0:
            return s

    return None


def get_ohlcv(data: dict, role: str):
    """Pobierz DataFrame OHLCV dla danej roli."""
    entry = data.get(role)
    if entry is None:
        return None
    return entry.get("df")


def first_ohlcv(data: dict, roles: list):
    """
    Bezpieczny fallback dla OHLCV.
    Nie używamy `or` na DataFrame, bo pandas zgłasza ValueError.
    """
    for role in roles:
        df = get_ohlcv(data, role)
        if df is not None:
            return df
    return None


def safe_last(s):
    if s is None or len(s) == 0:
        return None
    try:
        return float(s.iloc[-1])
    except Exception:
        return None


def safe_return(s, periods: int):
    if s is None or len(s) <= periods:
        return None
    try:
        return float(s.iloc[-1] / s.iloc[-periods - 1] - 1.0)
    except Exception:
        return None


def safe_sma(s, window: int):
    if s is None or len(s) < window:
        return None
    try:
        return float(s.rolling(window).mean().iloc[-1])
    except Exception:
        return None


def clip(v, lo=-1.0, hi=1.0):
    if v is None:
        return 0.0
    try:
        v = float(v)
    except Exception:
        return 0.0
    return max(lo, min(hi, v))


def weighted_average(pairs):
    """
    pairs: lista (value, weight)
    Zwraca średnią ważoną tylko z niepustych wartości.
    """
    cleaned = []
    for value, weight in pairs:
        if value is None:
            continue
        try:
            value = float(value)
            weight = float(weight)
        except Exception:
            continue
        if weight <= 0:
            continue
        cleaned.append((value, weight))

    if not cleaned:
        return None

    total_weight = sum(w for _, w in cleaned)
    if total_weight <= 0:
        return None

    return sum(v * w for v, w in cleaned) / total_weight


def compute_atr(df, window=14):
    """ATR + percentyl."""
    if df is None or len(df) < window + 1:
        return None, None

    required = ["high", "low", "close"]
    if any(col not in df.columns for col in required):
        return None, None

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.rolling(window).mean()
    atr_clean = atr.dropna()

    if len(atr_clean) == 0:
        return None, None

    last_atr = float(atr_clean.iloc[-1])
    pct = float((atr_clean < last_atr).mean() * 100.0) if len(atr_clean) > 1 else 50.0

    return last_atr, pct


def compute_zscore(s):
    if s is None or len(s) < 5:
        return None
    try:
        m = float(s.mean())
        std = float(s.std())
        if std < 1e-18:
            return 0.0
        return float((s.iloc[-1] - m) / std)
    except Exception:
        return None


# ============================================================
# 5. LICZENIE TRENDU
# ============================================================

def compute_trend_score(daily, hourly=None):
    """Trend score od -1 do +1."""
    if daily is None or len(daily) < 50:
        return None

    last = float(daily.iloc[-1])
    sma20 = safe_sma(daily, 20)
    sma50 = safe_sma(daily, 50)
    ret7 = safe_return(daily, 7)
    ret30 = safe_return(daily, 30)
    ret24 = safe_return(hourly, 24) if hourly is not None else None

    pairs = []

    if sma20 is not None and sma20 > 0:
        pairs.append((clip((last - sma20) / sma20 * 20.0), 0.25))

    if sma50 is not None and sma50 > 0:
        pairs.append((clip((last - sma50) / sma50 * 20.0), 0.20))

    if ret7 is not None:
        pairs.append((clip(ret7 * 10.0), 0.20))

    if ret30 is not None:
        pairs.append((clip(ret30 * 5.0), 0.20))

    if ret24 is not None:
        pairs.append((clip(ret24 * 20.0), 0.15))

    return weighted_average(pairs)


def compute_simple_trend(s):
    """Prosty trend dla danych makro."""
    if s is None or len(s) < 50:
        return None

    last = float(s.iloc[-1])
    sma20 = safe_sma(s, 20)
    sma50 = safe_sma(s, 50)
    ret7 = safe_return(s, 7)

    pairs = []

    if sma20 is not None and sma50 is not None and sma50 > 0:
        pairs.append((clip((sma20 - sma50) / sma50 * 50.0), 0.40))

    if sma20 is not None and sma20 > 0:
        pairs.append((clip((last - sma20) / sma20 * 20.0), 0.30))

    if ret7 is not None:
        pairs.append((clip(ret7 * 15.0), 0.30))

    return weighted_average(pairs)


# ============================================================
# 6. SCORING REŻIMU
# ============================================================

def score_all(data: dict):
    scores = {}
    tags = []

    # --- Serie bazowe ---
    btc_d = get_series(data, "btc_usd_1d")
    btc_h = get_series(data, "btc_usd_1h")
    eth_d = get_series(data, "eth_usd_1d")
    eth_h = get_series(data, "eth_usd_1h")
    sol_d = get_series(data, "sol_usd_1d")
    sol_h = get_series(data, "sol_usd_1h")

    # --- Trendy crypto ---
    scores["btc_trend_score"] = compute_trend_score(btc_d, btc_h)
    scores["eth_trend_score"] = compute_trend_score(eth_d, eth_h)
    scores["sol_trend_score"] = compute_trend_score(sol_d, sol_h)

    trend_values = [
        v for v in (
            scores["btc_trend_score"],
            scores["eth_trend_score"],
            scores["sol_trend_score"],
        ) if v is not None
    ]
    scores["crypto_trend_score"] = float(sum(trend_values) / len(trend_values)) if trend_values else None

    # --- Relative strength ---
    sol_btc_d = get_series(data, "sol_btc_1d")
    sol_btc_h = get_series(data, "sol_btc_1h")
    eth_btc_d = get_series(data, "eth_btc_1d")
    eth_btc_h = get_series(data, "eth_btc_1h")

    scores["sol_relative_strength_score"] = compute_trend_score(sol_btc_d, sol_btc_h)
    scores["eth_relative_strength_score"] = compute_trend_score(eth_btc_d, eth_btc_h)

    # --- Zmienność ---
    vol_df = first_ohlcv(data, ["sol_perp_1h", "btc_perp_1h"])
    if vol_df is not None:
        _, atr_pct = compute_atr(vol_df)
        if atr_pct is not None:
            scores["volatility_stress_score"] = clip(atr_pct / 100.0, 0.0, 1.0)
        else:
            scores["volatility_stress_score"] = None
    else:
        scores["volatility_stress_score"] = None

    # --- Makro ---
    macro_pairs = []

    dxy = get_series(data, "dxy_1d")
    dxy_trend = compute_simple_trend(dxy)
    if dxy_trend is not None:
        macro_pairs.append((-dxy_trend, 0.25))

    vix = get_series(data, "vix_1d")
    vix_last = safe_last(vix)
    if vix_last is not None:
        if vix_last >= 30:
            vix_stress = 1.0
        elif vix_last >= 20:
            vix_stress = 0.6
        elif vix_last >= 15:
            vix_stress = 0.3
        else:
            vix_stress = 0.0
        macro_pairs.append((-vix_stress, 0.25))

    sp500 = get_series(data, "sp500_1d")
    spx_trend = compute_simple_trend(sp500)
    if spx_trend is not None:
        macro_pairs.append((spx_trend, 0.25))

    nasdaq = get_series(data, "nasdaq100_1d")
    ndx_trend = compute_simple_trend(nasdaq)
    if ndx_trend is not None:
        macro_pairs.append((ndx_trend, 0.25))

    us10y = get_series(data, "us10y_1d")
    us10y_trend = compute_simple_trend(us10y)
    if us10y_trend is not None:
        macro_pairs.append((-us10y_trend, 0.15))

    scores["macro_risk_score"] = clip(weighted_average(macro_pairs))

    # --- Płynność ---
    liquidity_pairs = []

    usdt = get_series(data, "usdt_supply_1d")
    usdt_7d = safe_return(usdt, 7)
    if usdt_7d is not None:
        liquidity_pairs.append((clip(usdt_7d * 50.0), 0.25))

    usdc = get_series(data, "usdc_supply_1d")
    usdc_7d = safe_return(usdc, 7)
    if usdc_7d is not None:
        liquidity_pairs.append((clip(usdc_7d * 50.0), 0.25))

    total_mcap = get_series(data, "total_mcap_1d")
    mcap_7d = safe_return(total_mcap, 7)
    if mcap_7d is not None:
        liquidity_pairs.append((clip(mcap_7d * 20.0), 0.50))

    scores["liquidity_score"] = clip(weighted_average(liquidity_pairs))

    # --- Derywatywy ---
    derivatives_pairs = []

    sol_funding = get_series(data, "sol_funding_1h")
    funding_z = compute_zscore(sol_funding)
    if funding_z is not None:
        derivatives_pairs.append((clip(-funding_z * 0.30), 0.60))

    sol_oi = get_series(data, "sol_oi_1h")
    if sol_oi is not None and len(sol_oi) > 10:
        oi_change = sol_oi.diff().dropna()
        oi_z = compute_zscore(oi_change)
        if oi_z is not None:
            derivatives_pairs.append((clip(oi_z * 0.20), 0.20))

    sol_taker = get_series(data, "sol_taker_ratio_1h")
    if sol_taker is not None and len(sol_taker) > 10:
        last_taker = safe_last(sol_taker)
        if last_taker is not None:
            derivatives_pairs.append((clip((last_taker - 1.0) * 2.0), 0.20))
        else:
            taker_z = compute_zscore(sol_taker)
            if taker_z is not None:
                derivatives_pairs.append((clip(taker_z * 0.30), 0.20))

    scores["derivatives_background_score"] = clip(weighted_average(derivatives_pairs))

    # --- Rotacja altów ---
    btc_dom = get_series(data, "btc_dominance_1d")
    total_ex_btc = get_series(data, "total_ex_btc_1d")

    btc_dom_7d = safe_return(btc_dom, 7)
    total_ex_7d = safe_return(total_ex_btc, 7)
    eth_btc_trend = scores.get("eth_relative_strength_score")
    sol_btc_trend = scores.get("sol_relative_strength_score")

    rotation_pairs = []

    if btc_dom_7d is not None:
        rotation_pairs.append((clip(-btc_dom_7d * 50.0), 0.30))

    if total_ex_7d is not None:
        rotation_pairs.append((clip(total_ex_7d * 20.0), 0.30))

    if eth_btc_trend is not None:
        rotation_pairs.append((eth_btc_trend, 0.20))

    if sol_btc_trend is not None:
        rotation_pairs.append((sol_btc_trend, 0.20))

    scores["alt_rotation_score"] = clip(weighted_average(rotation_pairs))

    # --- BTC leadership ---
    leadership_pairs = []

    if btc_dom_7d is not None:
        leadership_pairs.append((clip(btc_dom_7d * 50.0), 0.40))

    if eth_btc_trend is not None:
        leadership_pairs.append((-eth_btc_trend, 0.30))

    if sol_btc_trend is not None:
        leadership_pairs.append((-sol_btc_trend, 0.30))

    scores["btc_leadership_score"] = clip(weighted_average(leadership_pairs))

    # --- SOL recovery ---
    sol_btc_7d = safe_return(sol_btc_d, 7)
    sol_btc_30d = safe_return(sol_btc_d, 30)
    sol_usd_7d = safe_return(sol_d, 7)

    recovery_pairs = []

    if sol_btc_7d is not None:
        recovery_pairs.append((clip(sol_btc_7d * 30.0), 0.40))

    if sol_btc_30d is not None:
        recovery_pairs.append((clip(-sol_btc_30d * 10.0), 0.30))

    if sol_usd_7d is not None:
        recovery_pairs.append((clip(sol_usd_7d * 15.0), 0.30))

    scores["sol_recovery_score"] = clip(weighted_average(recovery_pairs))

    # --- Tagi ---
    if scores.get("sol_relative_strength_score") is not None and scores["sol_relative_strength_score"] < -0.20:
        tags.append("sol_relative_weak")

    if sol_btc_7d is not None and sol_btc_7d > 0.005:
        tags.append("sol_recovering")

    if scores.get("eth_relative_strength_score") is not None and scores["eth_relative_strength_score"] > 0.20:
        tags.append("eth_leadership")

    if btc_dom_7d is not None and btc_dom_7d > 0.003:
        tags.append("btc_dominance_up")

    if btc_dom_7d is not None and btc_dom_7d < -0.003:
        tags.append("btc_dominance_down")

    if scores.get("liquidity_score") is not None and scores["liquidity_score"] > 0.20:
        tags.append("liquidity_expanding")

    if scores.get("liquidity_score") is not None and scores["liquidity_score"] < -0.20:
        tags.append("liquidity_contracting")

    if scores.get("macro_risk_score") is not None and scores["macro_risk_score"] > 0.30:
        tags.append("macro_risk_on")

    if scores.get("macro_risk_score") is not None and scores["macro_risk_score"] < -0.30:
        tags.append("macro_risk_off")

    if scores.get("volatility_stress_score") is not None and scores["volatility_stress_score"] > 0.60:
        tags.append("volatility_high")

    if scores.get("volatility_stress_score") is not None and scores["volatility_stress_score"] < 0.20:
        tags.append("volatility_low")

    return scores, tags


# ============================================================
# 7. WYBÓR PROFILU
# ============================================================

def adjust_confidence(confidence: float, quality: list) -> float:
    poor_count = sum(1 for q in quality if q["status"] == "POOR")
    missing_count = sum(1 for q in quality if q["status"] in ("MISSING_FILE", "MISSING"))

    confidence -= 0.03 * poor_count
    confidence -= 0.04 * missing_count

    return max(0.05, min(1.0, float(confidence)))


def select_profile(scores: dict, quality: list):
    reasons = []

    roles_present = {q["role"] for q in quality}
    critical_roles = {
        "btc_usd_1d",
        "btc_usd_1h",
        "sol_usd_1d",
        "sol_usd_1h",
    }
    missing_critical = len(critical_roles - roles_present)

    # P1: jakość danych
    if len(quality) == 0 or missing_critical >= 2:
        reasons.append("Brak wystarczających danych krytycznych – fallback.")
        return "BASE_PROFILE_FALLBACK_NEUTRAL", 0.10, reasons

    # P2: ekstremalna zmienność
    vol = scores.get("volatility_stress_score")
    if vol is not None and vol > 0.80:
        reasons.append(f"Ekstremalna zmienność (stress={vol:.2f}).")
        return "BASE_PROFILE_HIGH_VOL_STRESS", 0.80, reasons

    # P3: makro risk-off
    macro = scores.get("macro_risk_score")
    if macro is not None and macro < -0.45:
        reasons.append(f"Makro risk-off (score={macro:.2f}).")
        return "BASE_PROFILE_MACRO_RISK_OFF_DEFENSIVE", 0.70, reasons

    # Trendy
    crypto_trend = scores.get("crypto_trend_score")
    if crypto_trend is None:
        crypto_trend = scores.get("btc_trend_score")

    sol_trend = scores.get("sol_trend_score")
    if sol_trend is None:
        sol_trend = crypto_trend

    # P4: bear trend
    if crypto_trend is not None and crypto_trend < -0.30:
        if sol_trend is not None and sol_trend > -0.10:
            reasons.append(
                f"Trend spadkowy z krótkoterminowym odbiciem "
                f"(crypto={crypto_trend:.2f}, SOL={sol_trend:.2f})."
            )
            return "BASE_PROFILE_BEAR_BOUNCE_CAUTIOUS", 0.60, reasons

        reasons.append(f"Trend spadkowy (crypto={crypto_trend:.2f}).")
        return "BASE_PROFILE_BEAR_TREND_DEFENSIVE", 0.70, reasons

    # P5: range
    if crypto_trend is not None and abs(crypto_trend) < 0.15:
        if vol is not None and vol < 0.25:
            reasons.append(
                f"Konsolidacja z niską zmiennością "
                f"(trend={crypto_trend:.2f}, vol={vol:.2f})."
            )
            return "BASE_PROFILE_RANGE_COMPRESSION_WAIT", 0.60, reasons

        reasons.append(f"Chaotyczna konsolidacja (trend={crypto_trend:.2f}).")
        return "BASE_PROFILE_RANGE_CHOP_SELECTIVE", 0.60, reasons

    # P6: rotacja / specyficzne stany bycze
    alt_rotation = scores.get("alt_rotation_score")
    btc_leadership = scores.get("btc_leadership_score")
    sol_recovery = scores.get("sol_recovery_score")

    if alt_rotation is not None and alt_rotation > 0.35:
        reasons.append(f"Altseason rotation (score={alt_rotation:.2f}).")
        return "BASE_PROFILE_ALTSEASON_ROTATION", 0.70, reasons

    if btc_leadership is not None and btc_leadership > 0.35:
        reasons.append(f"BTC leadership, alty słabe (score={btc_leadership:.2f}).")
        return "BASE_PROFILE_BTC_LEADERSHIP_ALTS_WEAK", 0.60, reasons

    if (
        sol_recovery is not None
        and sol_recovery > 0.25
        and crypto_trend is not None
        and crypto_trend > 0.05
    ):
        reasons.append(f"SOL recovery (score={sol_recovery:.2f}).")
        return "BASE_PROFILE_SOL_RECOVERY_SELECTIVE", 0.60, reasons

    # P7: bull trend
    if crypto_trend is not None and crypto_trend > 0.30:
        if sol_trend is not None and sol_trend < crypto_trend - 0.15:
            reasons.append(
                f"Trend wzrostowy z korektą "
                f"(crypto={crypto_trend:.2f}, SOL={sol_trend:.2f})."
            )
            return "BASE_PROFILE_BULL_PULLBACK_CONSTRUCTIVE", 0.70, reasons

        reasons.append(f"Szeroki trend wzrostowy (crypto={crypto_trend:.2f}).")
        return "BASE_PROFILE_BULL_TREND_MOMENTUM", 0.70, reasons

    # P8: fallback
    reasons.append("Brak wyraźnego reżimu – fallback neutral.")
    return "BASE_PROFILE_FALLBACK_NEUTRAL", 0.30, reasons


# ============================================================
# 8. BUDOWANIE WYJŚCIA
# ============================================================

def build_output(scores: dict, profile_id: str, confidence: float, quality: list, tags: list, reasons: list):
    now = datetime.now(timezone.utc).isoformat()

    good_count = sum(1 for q in quality if q["status"] == "GOOD")
    poor_count = sum(1 for q in quality if q["status"] == "POOR")
    missing_count = sum(1 for q in quality if q["status"] in ("MISSING_FILE", "MISSING"))

    poor_datasets = [q["role"] for q in quality if q["status"] == "POOR"]
    missing_datasets = [q["role"] for q in quality if q["status"] in ("MISSING_FILE", "MISSING")]

    for ds in poor_datasets:
        if "oi" in ds and "oi_data_poor" not in tags:
            tags.append("oi_data_poor")
        if "taker" in ds and "taker_data_poor" not in tags:
            tags.append("taker_data_poor")

    # Usuń duplikaty tagów, zachowując kolejność
    tags = list(dict.fromkeys(tags))

    pinfo = PROFILES.get(profile_id, {})

    return {
        "generated_at": now,
        "recommended_profile": profile_id,
        "profile_display_name": pinfo.get("display_name", profile_id),
        "profile_description": pinfo.get("description", ""),
        "profile_bias": pinfo.get("bias", "neutral"),
        "profile_confidence": round(float(confidence), 3),
        "regime_scores": {
            k: round(float(v), 4) if v is not None else None
            for k, v in scores.items()
        },
        "data_quality": {
            "total_datasets": len(quality),
            "good_count": good_count,
            "poor_count": poor_count,
            "missing_count": missing_count,
            "poor_datasets": poor_datasets,
            "missing_datasets": missing_datasets,
        },
        "tags": tags,
        "reasons": reasons,
        "suggested_intervals": {
            "analysis_interval": "1h",
            "trigger_interval": "15m",
            "context_interval": "1d",
        },
    }


# ============================================================
# 9. MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Market Profile Builder")
    parser.add_argument("--data-dir", default="./pobrane_dane_v4", help="Katalog z plikami *_znormalizowany.json")
    parser.add_argument("--output", default="market_profile_recommendation.json", help="Plik wyjściowy")
    parser.add_argument("--verbose", action="store_true", help="Szczegółowe logi")
    args = parser.parse_args()

    print("=" * 60)
    print("MARKET PROFILE BUILDER")
    print("=" * 60)

    # 1. Wczytanie danych
    print(f"\n[1/5] Wczytywanie danych z: {args.data_dir}")
    data, quality = load_all_data(args.data_dir)
    print(f"  Wczytano {len(data)} plików.")

    if args.verbose:
        for q in quality:
            print(
                f"    {q['role']:30s}  "
                f"status={q['status']:12s}  "
                f"records={q['records_count']}"
            )

    # 2. Scoring
    print("\n[2/5] Liczenie score'ów reżimu...")
    scores, tags = score_all(data)

    if args.verbose:
        for k, v in scores.items():
            if v is None:
                print(f"    {k:35s} = None")
            else:
                print(f"    {k:35s} = {v:.4f}")

    # 3. Wybór profilu
    print("\n[3/5] Wybór profilu bazowego...")
    profile_id, confidence, reasons = select_profile(scores, quality)
    confidence = adjust_confidence(confidence, quality)

    print(f"  Profil: {profile_id}")
    print(f"  Confidence: {confidence:.2f}")

    # 4. Budowanie wyjścia
    print("\n[4/5] Budowanie wyjścia...")
    output = build_output(scores, profile_id, confidence, quality, tags, reasons)

    # 5. Zapis
    print(f"\n[5/5] Zapis do: {args.output}")
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # Podsumowanie
    print("\n" + "=" * 60)
    print("PODSUMOWANIE")
    print("=" * 60)
    print(f"  Profil:      {output['profile_display_name']}")
    print(f"  Bias:        {output['profile_bias']}")
    print(f"  Confidence:  {output['profile_confidence']}")
    print(f"  Tagi:        {', '.join(output['tags']) if output['tags'] else 'brak'}")
    print("  Powody:")
    for r in output["reasons"]:
        print(f"    - {r}")

    print("\n  Score'y:")
    for k, v in output["regime_scores"].items():
        print(f"    {k:35s} = {v}")

    dq = output["data_quality"]
    print(
        f"\n  Jakość danych: "
        f"{dq['good_count']} GOOD, "
        f"{dq['poor_count']} POOR, "
        f"{dq['missing_count']} MISSING"
    )
    print("=" * 60)


if __name__ == "__main__":
    main()