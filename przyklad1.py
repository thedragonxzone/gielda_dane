import json

with open("BTC_FUNDING_90d_1h_znormalizowany.json", encoding="utf-8") as f:
    doc = json.load(f)

instrument = doc["instrument"]
dataset = doc["dataset"]
interval = doc["interval"]
value_metric = doc["value_metric"]

print("instrument:", instrument)
print("dataset:", dataset)
print("interval:", interval)
print("value_metric:", value_metric)

for record in doc["records"][-5:]:
    ts = record["timestamp_utc"]
    value = record["value"]

    print(ts, value)