#!/usr/bin/env python3
"""
ホテルルートイン土岐の空室を監視し、部屋が出たら LINE に通知する。

ルートインの予約は tripla という予約SaaS で動いている。東横インとは
別系統なので check_hotel.py とは API を共有しない。

■ API の構成（公開されている予約ウィジェットと同じ経路）
  1. POST idp.tripla.ai/api/client_sessions  {key, secret}
     → data.client_session（以降 Client-Session ヘッダに載せる）
  2. GET  api.tripla.ai/hotels/<HOTEL_ID>/rooms
     → {"plans": [...]}。plans が空なら満室。

  key/secret は誰でも受け取る公開JS（app.*.js）に直書きされている
  ウィジェット用の公開クレデンシャルで、個人の資格情報ではない。

■ ハマりどころ（実測で判明）
  ・App-Version: tripla-booking-widget/1.0 が無いと
    「You don't have permission to access this」で弾かれる。
  ・日付は checkin / checkout ではなく checkin_date / checkout_date。
    形式は YYYY-MM-DD。
  ・この施設は kids_type=share_bed のため、部屋ごとに children が必須。
    無いと「Invalid adults or children」。
  ・配列は rooms[][adults]=1 の形（サイト側の直列化と同じ）。
  ・total_price は「滞在合計」。room_rate に日ごとの内訳が入る。
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from common import JST, line_push, load_state, save_state

# ---- 監視条件 -------------------------------------------------------------
HOTEL_NAME = "ホテルルートイン土岐"
HOTEL_CODE = "5a9056b2-6041-4f3b-aafc-dda5b14fcd6a"  # 予約URLの code=
HOTEL_ID = 4603  # settings/booking_widget が返す hotel_id
CHECKIN = "2026-10-30"  # チェックイン日（JST）
CHECKOUT = "2026-11-01"  # チェックアウト日（2泊）

# 滞在合計がこの金額未満のときだけ通知する（None なら金額を問わない）
MAX_PRICE = None

# 監視する人数の組み合わせ。増やせばその条件も一緒に見る。
SEARCHES = [
    {"adults": 1, "children": 0},
]
# ---------------------------------------------------------------------------

IDP = "https://idp.tripla.ai/api/client_sessions"
API = "https://api.tripla.ai"
WIDGET_KEY = "c8c604a2d81f7b2fe901"
WIDGET_SECRET = "1882351c176e635f5c64"
BASE_HEADERS = {
    "Accept": "*/*",
    "App-Version": "tripla-booking-widget/1.0",  # 無いと権限エラーになる
    "Tripla-Locale": "ja",
}

STATE_PATH = Path(__file__).with_name("state_routeinn.json")
PARSE_FAIL_ALERT_AT = 3


def label_of(search: dict) -> str:
    if search["children"]:
        return f"大人{search['adults']}名・子供{search['children']}名"
    return f"大人{search['adults']}名"


def nights() -> int:
    a = datetime.strptime(CHECKIN, "%Y-%m-%d")
    b = datetime.strptime(CHECKOUT, "%Y-%m-%d")
    return (b - a).days


def _request(url: str, data: dict | None = None, headers: dict | None = None) -> str:
    h = dict(BASE_HEADERS)
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def client_session() -> str:
    raw = _request(IDP, {"key": WIDGET_KEY, "secret": WIDGET_SECRET})
    return json.loads(raw)["data"]["client_session"]


def booking_url(search: dict) -> str:
    q = urllib.parse.urlencode(
        {
            "code": HOTEL_CODE,
            "checkin": CHECKIN.replace("-", "/"),
            "checkout": CHECKOUT.replace("-", "/"),
            "type": "plan",
            "is_day_use": "false",
            "rooms": json.dumps([{"adults": search["adults"]}], separators=(",", ":")),
        }
    )
    return f"https://reserve.route-inn.co.jp/booking/recommender?{q}"


def fetch_plans(token: str, search: dict) -> list[dict]:
    q = urllib.parse.urlencode(
        [
            ("checkin_date", CHECKIN),
            ("checkout_date", CHECKOUT),
            ("rooms[][adults]", str(search["adults"])),
            ("rooms[][children]", str(search["children"])),
        ]
    )
    raw = _request(f"{API}/hotels/{HOTEL_ID}/rooms?{q}", headers={"Client-Session": token})
    return json.loads(raw).get("plans") or []


def available_rooms(plans: list[dict]) -> list[dict]:
    """予約できる（部屋タイプ×プラン）を、滞在合計の安い順に並べて返す。"""
    out = []
    for plan in plans:
        for room in plan.get("rooms") or []:
            if room.get("availability") != "available":
                continue
            price = room.get("total_price")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            if MAX_PRICE is not None and price >= MAX_PRICE:
                continue
            out.append(
                {
                    "plan": plan.get("name") or "",
                    "room": room.get("room_type_name") or "",
                    "price": int(price),
                    "inventory": room.get("inventory"),
                }
            )
    out.sort(key=lambda r: r["price"])
    # 同じ部屋タイプがプラン違いで何件も並ぶので、最安の1件に畳む
    cheapest: dict[str, dict] = {}
    for r in out:
        cheapest.setdefault(r["room"], r)
    return list(cheapest.values())


def record_failure(state: dict, reason: str) -> int:
    fails = state.get("parse_failures", 0) + 1
    state["parse_failures"] = fails
    save_state(STATE_PATH, state)
    msg = (
        f"⚠️ ルートイン監視がデータを取得できませんでした（{fails}回連続）\n"
        f"{HOTEL_NAME} {CHECKIN}〜{CHECKOUT}／{reason}\n"
        f"APIの仕様が変わった可能性があります。\n{booking_url(SEARCHES[0])}"
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
        f"[{now}] {HOTEL_NAME}　{CHECKIN} 〜 {CHECKOUT}（{nights()}泊{budget}）"
    )

    try:
        token = client_session()
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as e:
        return record_failure(state, f"セッション取得に失敗: {e}")

    new_state: dict[str, bool] = {}
    found: dict[str, list[dict]] = {}

    for search in SEARCHES:
        label = label_of(search)
        try:
            plans = fetch_plans(token, search)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            return record_failure(state, f"{label} の取得に失敗: {e}")

        rooms = available_rooms(plans)
        new_state[label] = bool(rooms)

        print(f"  ● {label}")
        if rooms:
            for r in rooms[:5]:
                print(f"    ★ {r['room']}／{r['plan']}　¥{r['price']:,}（{nights()}泊合計）")
            if len(rooms) > 5:
                print(f"      ほか {len(rooms) - 5} 件")
        else:
            print(f"    満室（プラン {len(plans)} 件）")

        prev = (state.get("available") or {}).get(label)
        if rooms and not prev:
            found[label] = rooms
        elif rooms:
            print("      （前回から変化なし。再通知しません）")

    state["parse_failures"] = 0

    if found:
        lines = [
            "🏨 空室が出ました！",
            "",
            f"{HOTEL_NAME}",
            f"{CHECKIN} 〜 {CHECKOUT}（{nights()}泊）",
        ]
        for search in SEARCHES:
            label = label_of(search)
            if label not in found:
                continue
            lines += ["", f"【{label}】"]
            for r in found[label][:5]:
                lines.append(f"・{r['room']}　¥{r['price']:,}（{nights()}泊合計）")
                lines.append(f"　{r['plan']}")
            lines.append(f"　{booking_url(search)}")
        lines += ["", f"検知 {now}"]
        text = "\n".join(lines)
        print("--- 通知 ---\n" + text)
        line_push(text)
    elif any(new_state.values()):
        print("  → 空室はあるが前回から変化なし（通知しません）")
    else:
        print("  → 空室なし（通知しません）")

    state["available"] = new_state
    save_state(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
