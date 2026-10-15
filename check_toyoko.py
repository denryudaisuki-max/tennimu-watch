#!/usr/bin/env python3
"""
東横INN大阪ドーム前の空室を監視し、空きが出たら LINE に通知する。

HTML をパースするのではなく、予約サイトが内部で使っている tRPC の
JSON API（hotels.availabilities.prices）を直接叩く。認証不要。

■ 監視している泊まり方
1/23(土) から1泊（→1/24 日）と、2泊（→1/25 月）を別々に見る。
どちらかに空きが出たら、その条件ぶんだけ通知する。
2泊は 1/23 と 1/24 の両方が空いている必要があるため、1泊より条件が厳しい。

■ 実測メモ（2026-10-16）
・1/23(土) は売り切れ。「予約開始前」ではない。
  1/21 ¥7,800 / 1/22 ¥7,800 / 1/23 満室 / 1/24 ¥10,800 / 1/29 ¥8,800
  1/24 以降も予約できているので受付自体は開いている。土曜だけ埋まっている。
・lowestPrice は「1泊あたり」ではなく滞在合計（札幌で実測済み）。
  ここでは MAX_PRICE=None（金額不問）なので判定には使わず、表示だけに使う。

■ 設計上の注意
1つの条件で取得に失敗しても、そこで return しないこと。
過去にテニミュ側で「1件失敗すると後ろの通知処理に到達しない」不具合を出した。
失敗は記録して続行し、取れた分の通知は必ず出す。
"""

import json
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import JST, http_get, line_push, load_state, save_state

# ---- 監視条件 -------------------------------------------------------------
HOTEL_CODE = "00356"  # 検索URLの /search/detail/00356/ から
HOTEL_NAME = "東横INN大阪ドーム前"

SMOKING = "all"  # all / no_smoking / smoking（喫煙・禁煙を問わない）

# 滞在合計がこの金額未満のときだけ通知する（None なら金額を問わない）
MAX_PRICE = None

# 見たい泊まり方。どれかに空きが出たら、その条件ぶんを通知する。
STAYS = [
    {"checkin": "2027-01-23", "nights": 1},  # 1/23(土) → 1/24(日)
    {"checkin": "2027-01-23", "nights": 2},  # 1/23(土) → 1/25(月)
]

# 監視する人数・室数。増やせばその条件も一緒に見る。
SEARCHES = [
    {"people": 1, "rooms": 1},
]
# ---------------------------------------------------------------------------

API = "https://www.toyoko-inn.com/api/trpc/hotels.availabilities.prices"
DETAIL_URL = f"https://www.toyoko-inn.com/search/detail/{HOTEL_CODE}/"
STATE_PATH = Path(__file__).with_name("state_toyoko.json")
PARSE_FAIL_ALERT_AT = 3


def checkout_of(stay: dict) -> str:
    d = datetime.strptime(stay["checkin"], "%Y-%m-%d") + timedelta(days=stay["nights"])
    return d.strftime("%Y-%m-%d")


def label_of(stay: dict, search: dict) -> str:
    return (
        f"{stay['checkin']}→{checkout_of(stay)}"
        f"（{stay['nights']}泊）{search['people']}名{search['rooms']}室"
    )


def api_datetime(date_str: str, offset_days: int = 0) -> str:
    """JST の日付を、APIが要求する UTC ISO 形式に変換する。

    2027-01-23(JST 00:00) -> 2027-01-22T15:00:00.000Z
    """
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)
    d += timedelta(days=offset_days)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def booking_url(stay: dict, search: dict) -> str:
    q = urllib.parse.urlencode(
        {
            "people": search["people"],
            "room": search["rooms"],
            "smoking": SMOKING,
            "start": stay["checkin"],
            "end": checkout_of(stay),
        }
    )
    return f"{DETAIL_URL}?{q}"


def fetch_availability(stay: dict, search: dict) -> dict[str, dict]:
    payload = {
        "0": {
            "json": {
                "hotelCodes": [HOTEL_CODE],
                "checkinDate": api_datetime(stay["checkin"]),
                "checkoutDate": api_datetime(stay["checkin"], stay["nights"]),
                "numberOfPeople": search["people"],
                "numberOfRoom": search["rooms"],
                "smokingType": SMOKING,
            },
            "meta": {"values": {"checkinDate": ["Date"], "checkoutDate": ["Date"]}},
        }
    }
    url = f"{API}?batch=1&input=" + urllib.parse.quote(
        json.dumps(payload, separators=(",", ":"))
    )
    raw = http_get(url, accept="application/json")
    try:
        return json.loads(raw)[0]["result"]["data"]["json"]["prices"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return {}


def qualifies(info: dict) -> bool:
    """空室があるか（必要なら金額も見る）。メンテナンス中は満室扱い。"""
    if not info.get("existEnoughVacantRooms") or info.get("isUnderMaintenance"):
        return False
    if MAX_PRICE is None:
        return True
    price = info.get("lowestPrice") or 0
    return 0 < price < MAX_PRICE


def record_failure(state: dict, reason: str) -> int:
    fails = state.get("parse_failures", 0) + 1
    state["parse_failures"] = fails
    save_state(STATE_PATH, state)
    msg = (
        f"⚠️ 東横イン監視がデータを取得できませんでした（{fails}回連続）\n"
        f"{HOTEL_NAME}／{reason}\n"
        f"APIの仕様が変わった可能性があります。\n{DETAIL_URL}"
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

    budget = f"／滞在合計 ¥{MAX_PRICE:,} 未満" if MAX_PRICE else "／金額不問"
    print(f"[{now}] {HOTEL_NAME}{budget}")

    new_state: dict[str, bool] = {}
    found: list[dict] = []
    failed: list[str] = []

    for stay in STAYS:
        for search in SEARCHES:
            label = label_of(stay, search)
            try:
                prices = fetch_availability(stay, search)
            except RuntimeError as e:
                # ここで return しない。記録して次の条件へ進む。
                failed.append(f"{label}: {e}")
                print(f"    ⚠ {label}　取得に失敗")
                continue

            info = prices.get(HOTEL_CODE)
            if info is None:
                failed.append(f"{label}: 応答にホテルが入っていない")
                print(f"    ⚠ {label}　応答が空")
                continue

            ok = qualifies(info)
            new_state[label] = ok
            price = info.get("lowestPrice") or 0
            if info.get("isUnderMaintenance"):
                detail = "メンテナンス中"
            elif not info.get("existEnoughVacantRooms"):
                detail = "満室"
            else:
                detail = f"¥{price:,}（{stay['nights']}泊合計）"
            print(f"    {'★' if ok else ' '} {label}　{detail}")

            if ok and not (state.get("qualified") or {}).get(label):
                found.append({"label": label, "price": price, "url": booking_url(stay, search)})

    if found:
        lines = ["🏨 空室が出ました！", "", HOTEL_NAME]
        for f in found:
            lines += ["", f"・{f['label']}"]
            if f["price"]:
                lines.append(f"　¥{f['price']:,}（滞在合計）")
            lines.append(f"　{f['url']}")
        lines += ["", f"検知 {now}"]
        text = "\n".join(lines)
        print("--- 通知 ---\n" + text)
        line_push(text)
    elif any(new_state.values()):
        print("  → 空室はあるが前回から変化なし（通知しません）")
    elif new_state:
        print("  → 空室なし（通知しません）")
    else:
        print("  → 1件も判定できませんでした")

    # 取れた分の状態は、失敗した条件があっても必ず保存する。
    state["qualified"] = new_state

    if failed:
        return record_failure(state, " / ".join(failed))

    state["parse_failures"] = 0
    save_state(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
