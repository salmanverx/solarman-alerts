"""
Status check for the Solarman OpenAPI - prints only the fields that matter
for the 4 alerts (grid status, grid voltage, battery SOC), not the full
raw API responses.

Usage:
    python solarman_discovery.py
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = "https://globalapi.solarmanpv.com"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solarman_config.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    config["email"] = os.environ.get("SOLARMAN_EMAIL", config.get("email", ""))
    config["password"] = os.environ.get("SOLARMAN_PASSWORD", config.get("password", ""))
    config["appId"] = os.environ.get("SOLARMAN_APP_ID", config.get("appId", ""))
    config["appSecret"] = os.environ.get("SOLARMAN_APP_SECRET", config.get("appSecret", ""))

    missing = [k for k in ("appId", "appSecret", "email", "password") if not config.get(k)]
    if missing:
        print(f"Missing required config values: {', '.join(missing)}")
        print(f"Fill them into {CONFIG_PATH} (or set SOLARMAN_EMAIL / SOLARMAN_PASSWORD env vars) and re-run.")
        sys.exit(1)

    return config


def post(path, body, token=None, query=""):
    url = f"{BASE_URL}{path}"
    if query:
        url += f"?{query}"

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"bearer {token}"

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} calling {path}: {e.read().decode('utf-8', errors='replace')}")
        sys.exit(1)


# The only device dataList keys we actually care about for the 3 alerts.
# (Full dataList has ~78 entries for this inverter model - everything else is noise.)
WANTED_KEYS = {
    "GRID_RELAY_ST1": "grid_status",
    "G_V_LN": "grid_voltage_v",
}


def main():
    config = load_config()

    password_hash = hashlib.sha256(config["password"].encode("utf-8")).hexdigest().lower()

    token_resp = post(
        "/account/v1.0/token",
        {"appSecret": config["appSecret"], "email": config["email"], "password": password_hash},
        query=f"appId={config['appId']}&language=en",
    )

    token = token_resp.get("access_token")
    if not token:
        print(f"Login failed: {token_resp.get('code')} - {token_resp.get('msg')}")
        print("Note: Solarman can reject passwords with certain special characters.")
        sys.exit(1)

    stations_resp = post("/station/v1.0/list", {"page": 1, "size": 20}, token=token, query="language=en")
    station_list = stations_resp.get("stationList") or []
    if not station_list:
        print("No stations found on this account.")
        return
    station = station_list[0]
    station_id = station["id"]

    realtime_resp = post("/station/v1.0/realTime", {"stationId": station_id}, token=token, query="language=en")
    battery_soc = realtime_resp.get("batterySoc")

    devices_resp = post(
        "/station/v1.0/device",
        {"page": 1, "size": 50, "stationId": station_id},
        token=token,
        query="language=en",
    )
    device_list = devices_resp.get("deviceListItems") or []
    inverter = next((d for d in device_list if d.get("deviceType") == "INVERTER"), None)

    result = {
        "station_name": station.get("name"),
        "station_id": station_id,
        "battery_soc_pct": battery_soc,
    }

    if inverter:
        device_data_resp = post(
            "/device/v1.0/currentData",
            {"deviceSn": inverter["deviceSn"]},
            token=token,
            query="language=en",
        )
        by_key = {item["key"]: item["value"] for item in device_data_resp.get("dataList", [])}
        for api_key, label in WANTED_KEYS.items():
            result[label] = by_key.get(api_key)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
