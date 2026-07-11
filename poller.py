"""
Solarman alert poller.

Fetches grid status, grid voltage, and battery SOC, compares them against
the previous run's state (state.json), and prints an ALERT line whenever
one of the 4 conditions crosses a threshold:

  - grid power gone / restored
  - grid voltage drops below LOW_VOLTAGE_THRESHOLD / recovers
  - battery SOC drops to <=50% (discharging)
  - battery SOC reaches 100% (full)

Notification delivery is a stub for now (notify() just prints) -
swap that function out once a channel (Telegram/etc.) is chosen.

Credentials come from environment variables so the exact same script
runs unchanged locally (loaded from solarman_config.json, which is
gitignored) and in GitHub Actions (loaded from repo Secrets).
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = "https://globalapi.solarmanpv.com"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
CONFIG_PATH = os.path.join(HERE, "solarman_config.json")

LOW_VOLTAGE_TRIGGER = 200.0
LOW_VOLTAGE_RECOVER = 205.0  # small hysteresis so it doesn't flap right at the line
BAD_STREAK_TO_ALERT = 2  # require 2 consecutive bad polls before firing (avoid single-sample noise)

WANTED_KEYS = {
    "GRID_RELAY_ST1": "grid_status",
    "G_V_LN": "grid_voltage_v",
}


def load_credentials():
    creds = {
        "email": os.environ.get("SOLARMAN_EMAIL"),
        "password": os.environ.get("SOLARMAN_PASSWORD"),
        "appId": os.environ.get("SOLARMAN_APP_ID"),
        "appSecret": os.environ.get("SOLARMAN_APP_SECRET"),
    }

    if not all(creds.values()) and os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            file_config = json.load(f)
        creds["email"] = creds["email"] or file_config.get("email")
        creds["password"] = creds["password"] or file_config.get("password")
        creds["appId"] = creds["appId"] or file_config.get("appId")
        creds["appSecret"] = creds["appSecret"] or file_config.get("appSecret")

    missing = [k for k, v in creds.items() if not v]
    if missing:
        print(f"Missing credentials: {', '.join(missing)}")
        print("Set them as env vars (SOLARMAN_EMAIL, SOLARMAN_PASSWORD, SOLARMAN_APP_ID, SOLARMAN_APP_SECRET)")
        print(f"or fill in {CONFIG_PATH} for local runs.")
        sys.exit(1)

    return creds


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


def fetch_readings(creds):
    password_hash = hashlib.sha256(creds["password"].encode("utf-8")).hexdigest().lower()

    token_resp = post(
        "/account/v1.0/token",
        {"appSecret": creds["appSecret"], "email": creds["email"], "password": password_hash},
        query=f"appId={creds['appId']}&language=en",
    )
    token = token_resp.get("access_token")
    if not token:
        print(f"Login failed: {token_resp.get('code')} - {token_resp.get('msg')}")
        sys.exit(1)

    stations_resp = post("/station/v1.0/list", {"page": 1, "size": 20}, token=token, query="language=en")
    station_list = stations_resp.get("stationList") or []
    if not station_list:
        print("No stations found on this account.")
        sys.exit(1)
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
    if not inverter:
        print("No inverter device found on this station.")
        sys.exit(1)

    device_data_resp = post(
        "/device/v1.0/currentData",
        {"deviceSn": inverter["deviceSn"]},
        token=token,
        query="language=en",
    )
    by_key = {item["key"]: item["value"] for item in device_data_resp.get("dataList", [])}

    return {
        "battery_soc_pct": battery_soc,
        "grid_status": by_key.get("GRID_RELAY_ST1"),
        "grid_voltage_v": float(by_key["G_V_LN"]) if by_key.get("G_V_LN") is not None else None,
    }


def notify(message):
    # Stub - swap for a real channel (Telegram/etc.) once that's decided.
    print(f"ALERT: {message}")


def load_state():
    if not os.path.exists(STATE_PATH):
        return None
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def soc_zone(soc):
    if soc is None:
        return "unknown"
    if soc <= 50:
        return "low"
    if soc >= 100:
        return "full"
    return "normal"


def main():
    creds = load_credentials()
    readings = fetch_readings(creds)
    print(json.dumps(readings, indent=2))

    old_state = load_state()

    grid_bad_now = readings["grid_status"] != "Pull-in"
    old_grid_streak = old_state.get("grid_bad_streak", 0) if old_state else 0
    grid_bad_streak = old_grid_streak + 1 if grid_bad_now else 0
    grid_ok_new = grid_bad_streak < BAD_STREAK_TO_ALERT

    voltage = readings["grid_voltage_v"]
    old_voltage_ok = old_state.get("voltage_ok", True) if old_state else True
    voltage_bad_now = voltage is not None and voltage < LOW_VOLTAGE_TRIGGER
    old_voltage_streak = old_state.get("voltage_bad_streak", 0) if old_state else 0
    voltage_bad_streak = old_voltage_streak + 1 if voltage_bad_now else 0
    if old_voltage_ok:
        voltage_ok_new = voltage_bad_streak < BAD_STREAK_TO_ALERT
    else:
        voltage_ok_new = voltage is not None and voltage >= LOW_VOLTAGE_RECOVER

    new_zone = soc_zone(readings["battery_soc_pct"])
    old_zone = old_state.get("soc_zone", "unknown") if old_state else "unknown"

    new_state = {
        "grid_ok": grid_ok_new,
        "grid_bad_streak": grid_bad_streak,
        "voltage_ok": voltage_ok_new,
        "voltage_bad_streak": voltage_bad_streak,
        "soc_zone": new_zone,
        "battery_soc_pct": readings["battery_soc_pct"],
    }

    if old_state is None:
        print("No previous state found - establishing baseline, no alerts fired this run.")
        save_state(new_state)
        return

    if old_state.get("grid_ok", True) and not grid_ok_new:
        notify(f"Grid power is GONE (relay status: {readings['grid_status']})")
    elif not old_state.get("grid_ok", True) and grid_ok_new:
        notify("Grid power RESTORED")

    if old_voltage_ok and not voltage_ok_new:
        notify(f"Low grid voltage: {voltage} V (below {LOW_VOLTAGE_TRIGGER} V)")
    elif not old_voltage_ok and voltage_ok_new:
        notify(f"Grid voltage back to normal: {voltage} V")

    if old_zone != "low" and new_zone == "low":
        notify(f"Battery SOC dropped to {readings['battery_soc_pct']}% (<=50%)")
    if old_zone != "full" and new_zone == "full":
        notify("Battery fully charged (100%)")

    save_state(new_state)


if __name__ == "__main__":
    main()
