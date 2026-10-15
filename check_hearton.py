#!/usr/bin/env python3
"""
ハートンホテル西梅田の空室を監視し、予約できるようになったら LINE に通知する。

■ 何を見ているか
Trip.com（OTA）の検索結果ページ。ページに埋め込まれている JSON から、
このホテル（hotelId=1280983）の statusInfo.status を読む。

  status 1   → 買える（buttonContent は「空室を確認」）
  status 100 → 買えない（「ご指定の日程とその前後は予約できません」）

実測（2026-10-16）: 10/24→25（本命）=100 ／ 10/21→22 =1 ／ 11/18→19 =1

■ 重要な限界
これは Trip.com の在庫であって、ホテル公式の在庫ではない。
OTA は割当枠が別なので、Trip.com が満室でも公式サイトには空きがあることがある。
公式予約は d-reserve.jp（hotelCode=0000003206）だが、Vue の SPA で
空室 API を特定できなかったため、そちらは監視できていない。

■ URL について
パラメータを削ると結果データがページに埋まらなくなる（翻訳文字列だけになる）。
ユーザーがブラウザで検索したときの URL をそのまま使い、日付だけ差し替えること。

■ 注意
1回あたり 600KB 超を取得する。5分間隔だと1日およそ190MB になるため、
将来 Trip.com 側に遮断される可能性がある。
遮断されたら「ホテルが見つからない」として3回連続で LINE に警告が出る。
"""

import re
import sys
from datetime import datetime
from pathlib import Path

from common import JST, http_get, line_push, load_state, save_state

# ---- 監視条件 -------------------------------------------------------------
HOTEL_NAME = "ハートンホテル西梅田"
HOTEL_ID = "1280983"  # Trip.com の hotelId

# 見たい泊まり方。
STAYS = [
    {"checkin": "2026-10-24", "checkout": "2026-10-25"},  # 10/24(土) → 10/25(日)
]

# 「買える」とみなす status。1 = 空室を確認できる状態。
BUYABLE_STATUS = (1,)
# ---------------------------------------------------------------------------

# ユーザーが検索したときの URL。日付以外は触らないこと（削ると結果が埋まらない）。
BASE_URL = "https://jp.trip.com/hotels/list?city=219&cityName=%E5%A4%A7%E9%98%AA&provinceId=11088&countryId=78&districtId=0&checkin=2026-10-24&checkout=2026-10-25&barCurr=JPY&lat=0&lon=0&searchType=&searchWord=%E3%83%8F%E3%83%BC%E3%83%88%E3%83%B3%E3%83%9B%E3%83%86%E3%83%AB%E8%A5%BF%E6%A2%85%E7%94%B0&searchValue=31%7C1280983_31_1280983_1&searchCoordinate=&crn=1&adult=1&children=0&ages=&meals=&beds=&googlePlaceId=&searchBoxArg=t&ctm_ref=ix_sb_dl&travelPurpose=&domestic=true&rate=&discount=&payType=&bookPolicy=&bedType=&bookable=&breakfast=&edmHotelData=&optionType=&optionId="
OFFICIAL_URL = (
    "https://d-reserve.jp/guest-reserve-front/GCER001F00100/GCER001A01"
    "?hotelCode=0000003206"
)
STATE_PATH = Path(__file__).with_name("state_hearton.json")
PARSE_FAIL_ALERT_AT = 3


def label_of(stay: dict) -> str:
    return f"{stay['checkin']}→{stay['checkout']}"


def url_for(stay: dict) -> str:
    u = re.sub(r"checkin=[0-9-]*", "checkin=" + stay["checkin"], BASE_URL)
    return re.sub(r"checkout=[0-9-]*", "checkout=" + stay["checkout"], u)


def fetch_status(stay: dict) -> tuple[int, str] | None:
    """このホテルの (status, 表示文言) を返す。見つからなければ None。"""
    page = http_get(url_for(stay), accept="text/html")
    # JSON が HTML 内にエスケープされて埋まっているので緩く拾う
    m = re.search(r"hotelId.{0,8}:.{0,8}" + re.escape(HOTEL_ID) + r"(.{0,600})", page, re.S)
    if not m:
        return None
    seg = m.group(1)
    st = re.search(r"status.{0,6}:\s*(\d+)", seg)
    if not st:
        return None
    # ページ内では \"buttonContent\":\"...\" の形でエスケープされている
    btn = re.search(r'buttonContent\\?"?\s*:\s*\\?"([^"\\]{0,80})', seg)
    return int(st.group(1)), (btn.group(1).strip() if btn else "")


def record_failure(state: dict, reason: str) -> int:
    fails = state.get("parse_failures", 0) + 1
    state["parse_failures"] = fails
    save_state(STATE_PATH, state)
    msg = (
        f"⚠️ ハートンホテル監視がデータを取得できませんでした（{fails}回連続）\n"
        f"{HOTEL_NAME}／{reason}\n"
        "Trip.com の作りが変わったか、遮断された可能性があります。"
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
    print(f"[{now}] {HOTEL_NAME}（Trip.com の在庫）")

    new_state: dict[str, bool] = {}
    found: list[dict] = []
    failed: list[str] = []

    for stay in STAYS:
        label = label_of(stay)
        try:
            got = fetch_status(stay)
        except RuntimeError as e:
            # ここで return しないこと。記録して次へ進む。
            failed.append(f"{label}: {e}")
            print(f"    ⚠ {label}　取得に失敗")
            continue

        if got is None:
            failed.append(f"{label}: ページにホテルが見つからない")
            print(f"    ⚠ {label}　ページにホテルが見つかりません")
            continue

        status, button = got
        ok = status in BUYABLE_STATUS
        new_state[label] = ok
        print(f"    {'★' if ok else ' '} {label}　status={status}　{button}")

        if ok and not (state.get("available") or {}).get(label):
            found.append({"label": label, "button": button, "url": url_for(stay)})

    if found:
        lines = ["🏨 予約できるようになりました！", "", f"{HOTEL_NAME}（Trip.com）"]
        for f in found:
            lines += ["", f"・{f['label']}　{f['button']}", f"　{f['url']}"]
        lines += ["", "※公式サイトにも空きがあるかもしれません:", OFFICIAL_URL,
                  "", f"検知 {now}"]
        text = "\n".join(lines)
        print("--- 通知 ---\n" + text)
        line_push(text)
    elif any(new_state.values()):
        print("  → 予約できるが前回から変化なし（通知しません）")
    elif new_state:
        print("  → まだ予約できません（通知しません）")
    else:
        print("  → 1件も判定できませんでした")

    state["available"] = new_state

    if failed:
        return record_failure(state, " / ".join(failed))

    state["parse_failures"] = 0
    save_state(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
