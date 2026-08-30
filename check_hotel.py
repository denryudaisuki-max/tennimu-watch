#!/usr/bin/env python3
"""
東横インの空室を監視し、条件を満たす部屋が出たら LINE に通知する。

HTML をパースするのではなく、予約サイトが内部で使っている tRPC の
JSON API（hotels.availabilities.prices）を直接叩く。認証不要で、
ホテルごとに「空室が足りているか」と「最安値」が返る。

■ 金額について（重要）
API が返す lowestPrice は「1泊あたり」ではなく **滞在の合計金額**。
実測: 札幌駅北口 10/17 から 1泊 ¥15,700 / 2泊 ¥28,400。
サイトの検索結果カードに出る「¥24,623〜」も同じく滞在合計なので、
MAX_PRICE は画面に見えている数字と同じ土俵で比べられる。

人数・室数によって空室状況は変わるため、SEARCHES に並べた条件を個別に判定する。
"""

import json
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import JST, http_get, line_push, load_state, save_state

# ---- 監視条件 -------------------------------------------------------------
AREA_ID = 429
AREA_LABEL = "札幌"
CHECKIN = "2026-10-17"  # チェックイン日（JST）
NIGHTS = 2  # 10/17 IN → 10/19 OUT
SMOKING = "all"  # all / no_smoking / smoking

# 滞在合計がこの金額未満のときだけ通知する（None なら金額を問わない）
MAX_PRICE = 19000

# 監視する人数・室数の組み合わせ。増やせばその条件も一緒に見る。
SEARCHES = [
    {"people": 1, "rooms": 1},
]

# 監視するホテル（コード: 名前）。検索結果に出ていた札幌の4館。
HOTELS = {
    "00066": "東横INN札幌駅北口",
    "00018": "東横INN札幌駅西口北大前",
    "00059": "東横INN札幌駅南口",
    "00100": "東横INN札幌すすきの交差点",
}
# ---------------------------------------------------------------------------

API = "https://www.toyoko-inn.com/api/trpc/hotels.availabilities.prices"
STATE_PATH = Path(__file__).with_name("state_hotel.json")
PARSE_FAIL_ALERT_AT = 3


def label_of(search: dict) -> str:
    return f"{search['people']}名{search['rooms']}室"


def api_datetime(date_str: str, offset_days: int = 0) -> str:
    """JST の日付を、APIが要求する UTC ISO 形式に変換する。

    2026-10-17(JST 00:00) -> 2026-10-16T15:00:00.000Z
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


def qualifies(info: dict) -> bool:
    """空室があり、かつ滞在合計が予算未満か。"""
    if not info.get("existEnoughVacantRooms") or info.get("isUnderMaintenance"):
        return False
    price = info.get("lowestPrice") or 0
    if MAX_PRICE is None:
        return True
    return 0 < price < MAX_PRICE


def previous_for(state: dict, label: str) -> dict[str, bool]:
    """保存済みの状態から、その条件の前回値を取り出す。

    監視対象（都市・日程・条件）を変えたときは、古い形式や別条件の値は
    無視して作り直す。
    """
    entry = (state.get("qualified") or {}).get(label)
    return entry if isinstance(entry, dict) else {}


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

    budget = f"／滞在合計 ¥{MAX_PRICE:,} 未満" if MAX_PRICE else ""
    print(
        f"[{now}] 東横イン {AREA_LABEL}　{CHECKIN} 〜 {checkout_date()}"
        f"（{NIGHTS}泊{budget}）"
    )

    new_state: dict[str, dict[str, bool]] = {}
    found: dict[str, list[str]] = {}  # 条件ごとの「今回あらたに条件を満たしたホテル」
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
            ok = qualifies(info)
            current[code] = ok
            price = info.get("lowestPrice") or 0
            if not info.get("existEnoughVacantRooms"):
                detail = "満室"
            elif MAX_PRICE and price >= MAX_PRICE:
                detail = f"¥{price:,}（予算オーバー）"
            else:
                detail = f"¥{price:,}"
            print(f"    {'★' if ok else ' '} {name}: {detail}")

        new_state[label] = current
        hit = [c for c, v in current.items() if v and not prev.get(c, False)]
        if hit:
            found[label] = hit

    state["parse_failures"] = 0

    if found:
        lines = [
            "🏨 条件に合う部屋が出ました！",
            "",
            f"{AREA_LABEL}　{CHECKIN} 〜 {checkout_date()}（{NIGHTS}泊）",
        ]
        for search in SEARCHES:
            label = label_of(search)
            if label not in found:
                continue
            lines += ["", f"【{label}】"]
            for code in found[label]:
                price = (all_prices[label].get(code) or {}).get("lowestPrice") or 0
                lines.append(f"・{HOTELS[code]}　¥{price:,}（{NIGHTS}泊合計）")
                lines.append(f"　{search_url(search, code)}")
        lines += ["", f"検知 {now}"]
        text = "\n".join(lines)
        print("--- 通知 ---\n" + text)
        line_push(text)
    else:
        print("  → 条件を満たす部屋なし（通知しません）")

    state["qualified"] = new_state
    save_state(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
