# A TUTAJ PRZYKŁAD DLA OHLC

with open("BTC_PERP_OHLCV_90d_1h_znormalizowany.json", encoding="utf-8") as f:
    doc = json.load(f)

for record in doc["records"][-5:]:
    ts = record["timestamp_utc"]
    values = record["values"]

    print(
        ts,
        values.get("open"),
        values.get("high"),
        values.get("low"),
        values.get("close"),
        values.get("volume"),
    )