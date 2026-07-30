#!/usr/bin/env python3
"""
東横インの空室を監視し、満室から空室ありに変わったら LINE に通知する。

HTML をパースするのではなく、予約サイトが内部で使っている tRPC の
JSON API（hotels.availabilities.prices）を直接叩く。認証不要で、
ホテルごとに「空室が足りているか」と「最安値」が返る。

人数・室数によって空室状況は変わる（1名では空いていても2名では満室、
ということがある）ため、SEARCHES に並べた条件それぞれを個別に判定する。
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
SMOKING = "all"  # all / no_smoking / smoking

# 監視する人数・室数の組み合わせ。増やせばその条件も一緒に見る。
SEARCHES = [
    {"people": 1, "rooms": 1},
    {"people": 2, "rooms": 1},
]

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


def label_of(search: dict) -> str:
    return f"{search['people']}名{search['rooms']}室"


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


def search_url(search: dict, hotel_code: str | None = None) -> str:
    q = urllib.parse.urlencode(
        {
            "people": search["people"],
            "room": search["rooms"],
            "smoking": SMOKING,
            "start": CHECKIN,
            "end": checkout_date(),
        }
    )
    if hotel_code:
        return f"https://www.toyoko-inn.com/search/detail/{hotel_code}/?{q}"
    return f"https://www.toyoko-inn.com/search/result/?area={AREA_ID}&{q}"


def fetch_availability(search: dict) -> dict[str, dict]:
    payload = {
        "0": {
            "json": {
                "hotelCodes": list(HOTELS),
                "checkinDate": api_datetime(CHECKIN),
                "checkoutDate": api_datetime(CHECKIN, NIGHTS),
                "numberOfPeople": search["people"],
                "numberOfRoom": search["rooms"],
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


def previous_for(state: dict, label: str) -> dict[str, bool]:
    """保存済みの状態から、その条件の前回値を取り出す。

    条件が1つだった頃の古い形式 {"00011": false, ...} も読めるようにしておく。
    """
    vacant = state.get("vacant") or {}
    entry = vacant.get(label)
    if isinstance(entry, dict):
        return entry
    if vacant and all(isinstance(v, bool) for v in vacant.values()):
        return vacant  # 旧形式（1名1室のみを見ていた頃）
    return {}


def record_failure(state: dict, reason: str) -> int:
    fails = state.get("parse_failures", 0) + 1
    state["parse_failures"] = fails
    save_state(STATE_PATH, state)
    msg = (
        f"⚠️ 東横イン監視がデータを取得できませんでした（{fails}回連続）\n"
        f"{AREA_LABEL} {CHECKIN} 泊／{reason}\n"
        f"APIの仕様が変わった可能性があります。\n{search_url(SEARCHES[0])}"
    )
    print(msg, file=sys.stderr)
    if fails == PARSE_FAIL_ALERT_AT:
        try:
            line_push(msg)
        except RuntimeError as e:
            print(f"[warn] 障害通知も送れませんでした: {e}", file=sys.stderr)
    return 1


def main() -> int:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    state = load_state(STATE_PATH)

    print(f"[{now}] 東横イン {AREA_LABEL}　{CHECKIN} 〜 {checkout_date()}")

    new_vacant: dict[str, dict[str, bool]] = {}
    found: dict[str, list[str]] = {}  # 条件ごとの「今回あらたに空いたホテル」
    all_prices: dict[str, dict[str, dict]] = {}

    for search in SEARCHES:
        label = label_of(search)
        prices = fetch_availability(search)
        if not any(code in prices for code in HOTELS):
            return record_failure(state, f"{label} の応答が空")
        all_prices[label] = prices

        prev = previous_for(state, label)
        current: dict[str, bool] = {}
        print(f"  ● {label}")
        for code, name in HOTELS.items():
            info = prices.get(code) or {}
            vacant = bool(info.get("existEnoughVacantRooms")) and not info.get(
                "isUnderMaintenance"
            )
            current[code] = vacant
            price = info.get("lowestPrice") or 0
            mark = "★" if vacant else " "
            detail = f"¥{price:,}" if vacant and price else "満室"
            print(f"    {mark} {name}: {detail}")

        new_vacant[label] = current
        hit = [c for c, v in current.items() if v and not prev.get(c, False)]
        if hit:
            found[label] = hit

    state["parse_failures"] = 0

    if found:
        lines = [
            "🏨 東横インに空室が出ました！",
            "",
            f"{AREA_LABEL}　{CHECKIN} 〜 {checkout_date()}",
        ]
        for search in SEARCHES:
            label = label_of(search)
            if label not in found:
                continue
            lines += ["", f"【{label}】"]
            for code in found[label]:
                price = (all_prices[label].get(code) or {}).get("lowestPrice") or 0
                yen = f"　¥{price:,}〜" if price else ""
                lines.append(f"・{HOTELS[code]}{yen}")
                lines.append(f"　{search_url(search, code)}")
        lines += ["", f"検知 {now}"]
        text = "\n".join(lines)
        print("--- 通知 ---\n" + text)
        line_push(text)
    else:
        print("  → 変化なし（通知しません）")

    state["vacant"] = new_vacant
    save_state(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
