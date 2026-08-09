import json
import csv
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# Konfiguracja
# ============================================================

GROUPS_FILENAME = "grupy_do_normalizacji.json"
QUALITY_REPORT_FILENAME = "raport_jakosci_v4.json"
SPECIAL_CSV_FILENAME = "US10Y.csv"

NORMALIZED_SUFFIX = "_znormalizowany.json"
PROCESSED_LIST_FILENAME = "znormalizowano.txt"

# Jeśli chcesz mieć pliki w tym samym katalogu, zostaw "".
# Jeśli chcesz w podkatalogu, wpisz np. "znormalizowane".
OUTPUT_SUBDIR = ""

MACRO_INSTRUMENTS = {
    "DXY",
    "GOLD",
    "NASDAQ100",
    "SP500",
    "USDJPY",
    "VIX",
    "US10Y",
}

MISSING_TOKENS = {
    "",
    ".",
    "na",
    "n/a",
    "nan",
    "none",
    "null",
}


# ============================================================
# Czyszczenie JSON-a z ewentualnych białych znaków
# ============================================================

def normalize_strings(obj: Any) -> Any:
    """
    Usuwa białe znaki z kluczy i wartości tekstowych.
    """
    if isinstance(obj, dict):
        return {str(k).strip(): normalize_strings(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [normalize_strings(v) for v in obj]

    if isinstance(obj, str):
        return obj.strip()

    return obj


# ============================================================
# Timestamp UTC
# ============================================================

def normalize_timestamp(value: Any) -> Optional[str]:
    """
    Normalizuje timestamp/date do UTC ISO 8601.

    Jeśli nie rozpozna daty/timestampu, zwraca None.
    Oryginalny timestamp i tak jest zapisywany osobno jako timestamp_raw.
    """
    if value is None or value == "":
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        ts = float(value)

        # epoch seconds / milliseconds / microseconds
        if ts > 1_000_000_000_000_000:
            ts = ts / 1_000_000.0
        elif ts > 1_000_000_000_000:
            ts = ts / 1_000.0

        try:
            return (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except Exception:
            return None

    if isinstance(value, str):
        s = value.strip()

        if not s:
            return None

        # Najpierw ISO datetime
        iso = s

        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"

        try:
            dt = datetime.fromisoformat(iso)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)

            return dt.isoformat().replace("+00:00", "Z")

        except ValueError:
            pass

        # Proste daty dzienne
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(s, fmt)
                dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat().replace("+00:00", "Z")
            except ValueError:
                pass

        # Jeśli string jest liczbą, spróbuj jako epoch timestamp
        try:
            x = float(s.replace(",", ""))
            return normalize_timestamp(x)
        except Exception:
            return None

    return None


def parse_date_to_utc(value: Any) -> Optional[str]:
    """
    Alias do normalize_timestamp, używany głównie dla CSV.
    """
    return normalize_timestamp(value)


def is_date_like(value: Any) -> bool:
    return parse_date_to_utc(value) is not None


# ============================================================
# Jakość danych
# ============================================================

def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None

    s = str(value).strip()

    if s.lower() in MISSING_TOKENS:
        return None

    s = s.replace(",", "")

    try:
        x = float(s)

        # NaN -> None
        if x != x:
            return None

        return x

    except Exception:
        return None


def derive_quality_flag(missing_ratio: Any) -> str:
    r = safe_float(missing_ratio)

    if r is None:
        return "UNKNOWN"

    if r == 0.0:
        return "GOOD"

    if r <= 0.01:
        return "GOOD"

    if r <= 0.05:
        return "WARN"

    if r <= 0.30:
        return "POOR"

    return "BAD"


def compute_quality_score(quality_flag: str, missing_ratio: Any) -> float:
    qf = str(quality_flag or "").upper().strip()

    if qf in {"MISSING_FILE", "ERROR"}:
        return 0.0

    r = safe_float(missing_ratio) or 0.0
    score = max(0.0, 1.0 - r)

    if qf == "POOR":
        score = min(score, 0.5)
    elif qf == "WARN":
        score = min(score, 0.8)
    elif qf == "BAD":
        score = min(score, 0.3)

    return round(max(0.0, min(1.0, score)), 6)


# ============================================================
# Klasyfikacja instrumentu
# ============================================================

def parse_instrument(raw: str) -> Tuple[str, Optional[str], str]:
    i = str(raw).strip().upper()

    if i.endswith("_PERP_OHLCV"):
        return i[:-11], None, "PERP"

    if i.endswith("_FUNDING"):
        return i[:-8], None, "PERP"

    if i.endswith("_OI"):
        return i[:-3], None, "PERP"

    if i.endswith("_TAKER_RATIO"):
        return i[:-12], None, "PERP"

    if i.endswith("_DOMINANCE"):
        return i[:-10], None, "GLOBAL"

    if i == "TOTAL_MARKET_CAP":
        return "TOTAL", None, "GLOBAL"

    if i == "TOTAL_EX_BTC":
        return "TOTAL", "BTC", "GLOBAL"

    if i.endswith("_SUPPLY"):
        return i.split("_")[0], None, "STABLECOIN"

    if i.endswith("_USD_1D"):
        return i[:-7], "USD", "SPOT"

    if i.endswith("_USD"):
        return i[:-4], "USD", "SPOT"

    if i.endswith("_BTC_1D"):
        return i[:-7], "BTC", "RATIO"

    if i.endswith("_BTC"):
        return i[:-4], "BTC", "RATIO"

    if i in MACRO_INSTRUMENTS:
        return i, None, "MACRO"

    return i, None, "UNKNOWN"


def infer_interval(meta: Dict[str, Any], filename: str) -> str:
    interval = meta.get("interval")

    if interval:
        return str(interval).strip()

    name = filename.upper()

    if "_1H" in name:
        return "1h"

    if "_1D" in name:
        return "1d"

    return ""


def classify_dataset(instrument: str, interval: str) -> str:
    i = str(instrument).strip().upper()

    if i in MACRO_INSTRUMENTS:
        return "macro_daily"

    if i.endswith("_PERP_OHLCV"):
        return "derivatives_ohlcv_hourly"

    if i.endswith("_FUNDING"):
        return "derivatives_hourly"

    if i.endswith("_OI"):
        return "derivatives_hourly"

    if i.endswith("_TAKER_RATIO"):
        return "derivatives_hourly"

    if i.endswith("_DOMINANCE"):
        return "dominance_daily"

    if i in {"TOTAL_MARKET_CAP", "TOTAL_EX_BTC"}:
        return "crypto_market_structure_daily"

    if i.endswith("_SUPPLY"):
        return "stablecoin_daily"

    if interval == "1h":
        return "crypto_hourly"

    if interval == "1d":
        return "crypto_daily"

    return "other"


def semantic_metric_for_instrument(instrument: str) -> str:
    i = str(instrument).strip().upper()

    if i.endswith("_FUNDING"):
        return "funding_rate"

    if i.endswith("_OI"):
        return "open_interest"

    if i.endswith("_TAKER_RATIO"):
        return "taker_long_short_ratio"

    if i.endswith("_DOMINANCE"):
        return "dominance_share"

    if i == "TOTAL_MARKET_CAP":
        return "total_market_cap"

    if i == "TOTAL_EX_BTC":
        return "total_market_cap_ex_btc"

    if i.endswith("_SUPPLY"):
        return "stablecoin_supply"

    if i in MACRO_INSTRUMENTS:
        return "close"

    if i.endswith("_BTC_1D") or i.endswith("_BTC"):
        return "ratio_close"

    if i.endswith("_USD_1D") or i.endswith("_USD"):
        return "close"

    return "value"


def normalize_column_name(name: Any) -> str:
    s = str(name).strip().lower()

    if not s:
        return "value"

    out = ""

    for ch in s:
        if ch.isalnum() or ch == "_":
            out += ch
        else:
            out += "_"

    while "__" in out:
        out = out.replace("__", "_")

    out = out.strip("_")

    return out or "value"


# ============================================================
# Wybór kolumn i wartości głównej
# ============================================================

def build_column_metrics(
    instrument: str,
    columns: List[str],
    dataset: str,
) -> Tuple[List[str], str]:
    """
    Zwraca:
      - listę znormalizowanych nazw kolumn/metryk,
      - nazwę metryki, która ma być użyta jako value/close.
    """
    sem = semantic_metric_for_instrument(instrument)

    if not columns:
        return [sem], sem

    if len(columns) == 1:
        col_norm = normalize_column_name(columns[0])

        if sem in {
            "funding_rate",
            "open_interest",
            "taker_long_short_ratio",
            "dominance_share",
            "total_market_cap",
            "total_market_cap_ex_btc",
            "stablecoin_supply",
        }:
            return [sem], sem

        if sem == "ratio_close":
            return ["ratio_close"], "ratio_close"

        if sem == "close":
            if col_norm == "adj_close":
                return ["adj_close"], "adj_close"
            return ["close"], "close"

        return [col_norm], col_norm

    metrics = [normalize_column_name(c) for c in columns]

    if dataset == "macro_daily":
        priority = ["adj_close", "close"]
    else:
        priority = ["close", "adj_close"]

    for candidate in priority:
        if candidate in metrics:
            return metrics, candidate

    return metrics, metrics[0]


# ============================================================
# Parsowanie tabeli index/columns/data
# ============================================================

def is_column_oriented(index: List[Any], columns: List[Any], data: List[Any]) -> bool:
    if not data or not columns or not index:
        return False

    if len(data) != len(columns):
        return False

    if not all(isinstance(col, list) for col in data):
        return False

    if len(data) == len(index):
        first = data[0]

        if isinstance(first, list) and len(first) == len(index) and len(columns) != len(index):
            return True

        return False

    return all(len(col) == len(index) for col in data)


def iter_table_rows(
    index: List[Any],
    columns: List[Any],
    data: List[Any],
):
    """
    Iterator po wierszach:
      timestamp_raw, row_values
    """
    if not index:
        for row in data:
            if not isinstance(row, list):
                row = [row]
            yield None, row
        return

    column_oriented = is_column_oriented(index, columns, data)

    for i, ts in enumerate(index):
        if column_oriented:
            row = []

            for col in data:
                if isinstance(col, list) and i < len(col):
                    row.append(col[i])
                else:
                    row.append(None)

        else:
            if i < len(data):
                row = data[i]

                if not isinstance(row, list):
                    row = [row]
            else:
                row = []

        yield ts, row


# ============================================================
# Ścieżka wyjściowa
# ============================================================

def get_output_path(file_path: Path) -> Path:
    if OUTPUT_SUBDIR:
        out_dir = file_path.parent / OUTPUT_SUBDIR
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{file_path.stem}{NORMALIZED_SUFFIX}"

    return file_path.with_name(f"{file_path.stem}{NORMALIZED_SUFFIX}")


# ============================================================
# Normalizacja jednego pliku JSON timeseries
# ============================================================

def normalize_timeseries_file(file_path: Path, group_id: str) -> Optional[str]:
    with open(file_path, "r", encoding="utf-8-sig") as f:
        doc = normalize_strings(json.load(f))

    if not isinstance(doc, dict):
        return None

    if "index" not in doc or "data" not in doc:
        return None

    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}

    instrument = str(meta.get("instrument") or file_path.stem).strip()
    base_asset, quote_asset, market = parse_instrument(instrument)

    interval = infer_interval(meta, file_path.name)
    dataset = classify_dataset(instrument, interval)

    source = str(meta.get("source") or "").strip()
    source_url = str(meta.get("source_url") or "").strip()

    status = str(meta.get("status") or "").strip()
    missing_ratio = meta.get("missing_ratio")

    quality_flag = status or derive_quality_flag(missing_ratio)
    quality_score = compute_quality_score(quality_flag, missing_ratio)

    columns_raw = doc.get("columns") or []
    index_raw = doc.get("index") or []
    data_raw = doc.get("data") or []

    if not isinstance(columns_raw, list):
        columns_raw = [columns_raw]

    if not isinstance(index_raw, list):
        index_raw = [index_raw]

    if not isinstance(data_raw, list):
        data_raw = [data_raw]

    columns = [str(c).strip() for c in columns_raw]

    metrics, value_metric = build_column_metrics(
        instrument,
        columns,
        dataset,
    )

    records: List[Dict[str, Any]] = []

    for ts_raw, row in iter_table_rows(index_raw, columns, data_raw):
        ts_utc = normalize_timestamp(ts_raw)

        values: Dict[str, Any] = {}

        values_count = max(len(metrics), len(row), 1 if metrics else 0)

        if values_count == 0:
            continue

        for j in range(values_count):
            if j < len(metrics):
                key = metrics[j]
            else:
                key = f"value_{j}"

            value = row[j] if j < len(row) else None
            values[key] = value

        value = values.get(value_metric)

        # Jeśli z jakiegoś powodu nie ma wybranej metryki,
        # weź pierwszą dostępną wartość, żeby nie zgubić rekordu.
        if value is None and value_metric not in values and values:
            value = next(iter(values.values()))

        records.append({
            "timestamp_utc": ts_utc,
            "timestamp_raw": ts_raw,
            "value": value,
            "values": values,
        })

    normalized = {
        "schema_version": "1.0",
        "source_file": file_path.name,
        "group_id": group_id,
        "dataset": dataset,
        "instrument": instrument,
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "market": market,
        "interval": interval,
        "source": source,
        "source_url": source_url,
        "status": status,
        "quality_flag": quality_flag,
        "quality_score": quality_score,
        "missing_ratio": missing_ratio,
        "value_metric": value_metric,
        "columns_original": columns,
        "columns_normalized": metrics,
        "records": records,
    }

    out_path = get_output_path(file_path)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2, ensure_ascii=False)

    return out_path.name


# ============================================================
# Normalizacja specjalnego pliku US10Y.csv
# ============================================================

def normalize_us10y_csv(file_path: Path) -> Optional[str]:
    """
    Normalizuje US10Y.csv do US10Y_znormalizowany.json.

    Oczekiwany format:
      observation_date,DGS10
      1962-01-02,4.06
      1962-01-03,4.03
      1962-01-16,

    Brak wartości po przecinku jest traktowany jako null,
    ale rekord nie jest usuwany.
    """
    with open(file_path, "r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    if not rows:
        return None

    start = 0

    first_row = [str(x).strip() for x in rows[0]]

    # Jeśli pierwsza komórka nie wygląda na datę, uznajemy pierwszą linię za nagłówek.
    if first_row and not is_date_like(first_row[0]):
        header = first_row
        start = 1
    else:
        header = ["observation_date", "DGS10"]

    value_column_original = header[1] if len(header) > 1 else "DGS10"

    instrument = "US10Y"
    base_asset, quote_asset, market = parse_instrument(instrument)

    dataset = "macro_daily"
    interval = "1d"

    source = "FRED_DGS10"
    source_url = "https://fred.stlouisfed.org/series/DGS10"

    value_metric = "us10y_yield"

    records: List[Dict[str, Any]] = []

    total_rows = 0
    missing_values = 0
    invalid_rows = 0

    for row in rows[start:]:
        if not row:
            continue

        row_clean = [str(x).strip() for x in row]

        if not any(row_clean):
            continue

        date_raw = row_clean[0] if len(row_clean) > 0 else ""

        if not date_raw:
            invalid_rows += 1
            continue

        ts_utc = parse_date_to_utc(date_raw)

        if ts_utc is None:
            invalid_rows += 1
            continue

        raw_value = row_clean[1] if len(row_clean) > 1 else ""
        value = safe_float(raw_value)

        total_rows += 1

        if value is None:
            missing_values += 1

        records.append({
            "timestamp_utc": ts_utc,
            "timestamp_raw": date_raw,
            "value": value,
            "values": {
                value_metric: value,
            },
        })

    if total_rows == 0:
        return None

    missing_ratio = round(missing_values / total_rows, 6) if total_rows else None

    quality_flag = derive_quality_flag(missing_ratio)

    # Jeśli były wiersze z datą, której nie udało się sparsować,
    # obniżamy flagę jakości do WARN.
    if invalid_rows > 0 and quality_flag in {"GOOD", "UNKNOWN"}:
        quality_flag = "WARN"

    quality_score = compute_quality_score(quality_flag, missing_ratio)

    normalized = {
        "schema_version": "1.0",
        "source_file": file_path.name,
        "group_id": "special_csv",
        "dataset": dataset,
        "instrument": instrument,
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "market": market,
        "interval": interval,
        "source": source,
        "source_url": source_url,
        "status": quality_flag,
        "quality_flag": quality_flag,
        "quality_score": quality_score,
        "missing_ratio": missing_ratio,
        "value_metric": value_metric,
        "columns_original": [value_column_original],
        "columns_normalized": [value_metric],
        "records": records,
    }

    out_path = get_output_path(file_path)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2, ensure_ascii=False)

    return out_path.name


# ============================================================
# Główna funkcja: normalizacja po grupach + US10Y.csv
# ============================================================

def normalize_by_groups(directory_path: str) -> None:
    base_dir = Path(directory_path)

    if not base_dir.is_dir():
        print(f"❌ Katalog nie istnieje: {base_dir}")
        return

    groups_path = base_dir / GROUPS_FILENAME

    groups: List[Dict[str, Any]] = []
    skipped: List[str] = []
    processed_entries: List[Tuple[str, str]] = []

    if groups_path.is_file():
        with open(groups_path, "r", encoding="utf-8-sig") as f:
            catalog = normalize_strings(json.load(f))

        groups = catalog.get("groups") or []

        if not isinstance(groups, list):
            groups = []

    else:
        skipped.append(f"{GROUPS_FILENAME}: plik nie istnieje — przetwarzam tylko pliki specjalne")

    # --------------------------------------------------------
    # 1. Normalizacja plików JSON z grup
    # --------------------------------------------------------

    for group in groups:
        if not isinstance(group, dict):
            continue

        group_id = str(group.get("group_id") or "").strip()

        paths = {
            str(p).strip()
            for p in group.get("paths", []) or []
        }

        is_timeseries_group = {
            "columns[]",
            "index[]",
            "data[][]",
        }.issubset(paths)

        files = group.get("files") or []

        if not isinstance(files, list):
            continue

        for filename in files:
            filename = str(filename).strip()

            if not filename:
                continue

            if filename.endswith(NORMALIZED_SUFFIX):
                continue

            if filename == QUALITY_REPORT_FILENAME:
                skipped.append(f"{filename}: raport jakości — pomijam")
                continue

            if not is_timeseries_group:
                skipped.append(f"{filename}: grupa nie wygląda na timeseries")
                continue

            file_path = base_dir / filename

            if not file_path.is_file():
                skipped.append(f"{filename}: plik nie istnieje")
                continue

            try:
                normalized_name = normalize_timeseries_file(file_path, group_id)

                if normalized_name:
                    processed_entries.append((filename, normalized_name))
                    print(f"✅ {filename} -> {normalized_name}")
                else:
                    skipped.append(f"{filename}: nie rozpoznano jako timeseries")

            except Exception as e:
                skipped.append(f"{filename}: {e}")

    # --------------------------------------------------------
    # 2. Normalizacja specjalnego pliku US10Y.csv
    # --------------------------------------------------------

    special_csv_path = base_dir / SPECIAL_CSV_FILENAME

    if special_csv_path.is_file():
        try:
            normalized_name = normalize_us10y_csv(special_csv_path)

            if normalized_name:
                processed_entries.append((SPECIAL_CSV_FILENAME, normalized_name))
                print(f"✅ {SPECIAL_CSV_FILENAME} -> {normalized_name}")
            else:
                skipped.append(f"{SPECIAL_CSV_FILENAME}: brak poprawnych danych do normalizacji")

        except Exception as e:
            skipped.append(f"{SPECIAL_CSV_FILENAME}: {e}")

    else:
        skipped.append(f"{SPECIAL_CSV_FILENAME}: plik nie istnieje")

    # --------------------------------------------------------
    # 3. Zapis listy przetworzonych plików
    # --------------------------------------------------------

    if OUTPUT_SUBDIR:
        output_base = base_dir / OUTPUT_SUBDIR
        output_base.mkdir(parents=True, exist_ok=True)
    else:
        output_base = base_dir

    processed_list_path = output_base / PROCESSED_LIST_FILENAME

    with open(processed_list_path, "w", encoding="utf-8") as f:
        for source_name, normalized_name in processed_entries:
            f.write(f"{source_name} -> {normalized_name}\n")

    # --------------------------------------------------------
    # Podsumowanie
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print(f"🚀 Gotowe. Przetworzone pliki: {len(processed_entries)}")
    print("=" * 70)
    print(f"📝 Lista przetworzonych plików: {processed_list_path}")

    if skipped:
        print("\n⚠️ Pominięte / błędy:")

        for s in skipped:
            print(f"  - {s}")


# ============================================================
# Uruchomienie
# ============================================================

if __name__ == "__main__":
    import sys

    DEFAULT_DIRECTORY = "/home/radek_debian/projects/gielda_dane/pobrane_dane_v4"

    directory = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIRECTORY

    normalize_by_groups(directory)