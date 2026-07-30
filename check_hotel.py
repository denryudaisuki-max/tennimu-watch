#!/usr/bin/env python3
"""
東横インの空室を監視し、満室から空室ありに変わったら LINE に通知する。

HTML をパースするのではなく、予約サイトが内部で使っている tRPC の
JSON API（hotels.availabilities.prices）を直接叩く。認証不要で、
ホテルごとに「空室が足りているか」と「最安値」が返る。
"""

import json
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import JST, http_get, line_push, load_state, save_state

# ---- 監視条件 -------------------------------------------------------------
AREA_ID = 439
AREA_LABEL = "仙台"
CHECKIN = "2026-10-03"  # 泊まる日（JST）
NIGHTS = 1
PEOPLE = 1
ROOMS = 1
SMOKING = "all"  # all / no_smoking / smoking

# 監視するホテル（コード: 名前）。検索結果に出ていた仙台の4館。
HOTELS = {
    "00011": "東横INN仙台東口1号館",
    "00024": "東横INN仙台東口2号館",
    "00036": "東横INN仙台西口広瀬通",
    "00058": "東横INN仙台駅西口中央",
}
# ---------------------------------------------------------------------------

API = "https://www.toyoko-inn.com/api/trpc/hotels.availabilities.prices"
STATE_PATH = Path(__file__).with_name("state_hotel.json")
PARSE_FAIL_ALERT_AT = 3


def api_datetime(date_str: str, offset_days: int = 0) -> str:
    """JST の日付を、APIが要求する UTC ISO 形式に変換する。

    2026-10-03(JST 00:00) -> 2026-10-02T15:00:00.000Z
    """
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)
    d += timedelta(days=offset_days)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def checkout_date() -> str:
    d = datetime.strptime(CHECKIN, "%Y-%m-%d") + timedelta(days=NIGHTS)
    return d.strftime("%Y-%m-%d")


def search_url(hotel_code: str | None = None) -> str:
    q = urllib.parse.urlencode(
        {
            "people": PEOPLE,
            "room": ROOMS,
            "smoking": SMOKING,
            "start": CHECKIN,
            "end": checkout_date(),
        }
    )
    if hotel_code:
        return f"https://www.toyoko-inn.com/search/detail/{hotel_code}/?{q}"
    return f"https://www.toyoko-inn.com/search/result/?area={AREA_ID}&{q}"


def fetch_availability() -> dict[str, dict]:
    payload = {
        "0": {
            "json": {
                "hotelCodes": list(HOTELS),
                "checkinDate": api_datetime(CHECKIN),
                "checkoutDate": api_datetime(CHECKIN, NIGHTS),
                "numberOfPeople": PEOPLE,
                "numberOfRoom": ROOMS,
                "smokingType": SMOKING,
            },
            "meta": {"values": {"checkinDate": ["Date"], "checkoutDate": ["Date"]}},
        }
    }
    url = (
        f"{API}?batch=1&input="
        + urllib.parse.quote(json.dumps(payload, separators=(",", ":")))
    )
    raw = http_get(url, accept="application/json")
    try:
        return json.loads(raw)[0]["result"]["data"]["json"]["prices"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return {}


def main() -> int:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    state = load_state(STATE_PATH)
    prev: dict[str, bool] = state.get("vacant", {})

    prices = fetch_availability()

    # 監視対象のホテルが1つも返ってこなければ異常とみなす
    if not any(code in prices for code in HOTELS):
        fails = state.get("parse_failures", 0) + 1
        state["parse_failures"] = fails
        save_state(STATE_PATH, state)
        msg = (
            f"⚠️ 東横イン監視がデータを取得できませんでした（{fails}回連続）\n"
            f"{AREA_LABEL} {CHECKIN} 泊\n"
            f"APIの仕様が変わった可能性があります。\n{search_url()}"
        )
        print(msg, file=sys.stderr)
        if fails == PARSE_FAIL_ALERT_AT:
            try:
                line_push(msg)
            except RuntimeError as e:
                print(f"[warn] 障害通知も送れませんでした: {e}", file=sys.stderr)
        return 1

    state["parse_failures"] = 0

    current: dict[str, bool] = {}
    print(f"[{now}] 東横イン {AREA_LABEL}　{CHECKIN} 〜 {checkout_date()}"
          f"（{PEOPLE}名 {ROOMS}室）")
    for code, name in HOTELS.items():
        info = prices.get(code) or {}
        vacant = bool(info.get("existEnoughVacantRooms")) and not info.get(
            "isUnderMaintenance"
        )
        current[code] = vacant
        price = info.get("lowestPrice") or 0
        mark = "★" if vacant else " "
        detail = f"¥{price:,}" if vacant and price else "満室"
        print(f"  {mark} {name}: {detail}")

    # 「空室発見」= 前回 満室だったホテルが 今回 空室ありになった
    found = [c for c, v in current.items() if v and not prev.get(c, False)]

    if found:
        lines = [
            "🏨 東横インに空室が出ました！",
            "",
            f"{AREA_LABEL}　{CHECKIN} 〜 {checkout_date()}（{PEOPLE}名 {ROOMS}室）",
            "",
        ]
        for code in found:
            price = (prices.get(code) or {}).get("lowestPrice") or 0
            yen = f"　¥{price:,}〜" if price else ""
            lines.append(f"・{HOTELS[code]}{yen}")
            lines.append(f"　{search_url(code)}")
        lines += ["", f"▼一覧\n{search_url()}", "", f"検知 {now}"]
        text = "\n".join(lines)
        print("--- 通知 ---\n" + text)
        line_push(text)
    else:
        print("  → 変化なし（通知しません）")

    state["vacant"] = current
    save_state(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
