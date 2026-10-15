#!/usr/bin/env python3
"""
ローソンチケットの「当日引換券」受付を監視する……はずだったもの。

■■ 現在このスクリプトは動かない。本番ループには組み込んでいない。■■

2026-10-16 に GitHub Actions 上でヘッドレス Chrome から試したが、
net::ERR_HTTP2_PROTOCOL_ERROR で弾かれた（手元の curl と同じエラー）。
l-tike 側の bot 対策で、データセンターIPからは実ブラウザでも通らない。

これ以上通すにはブラウザ指紋の偽装か住宅用プロキシが要る。
それは転売対策の回避にあたるのでやらない。この件はここで打ち切り。
大阪の当日引換券は、公演ごとに見られるイープラス側（check_tennimu.py）で
代替している。そちらのほうが粒度も細かい。

残してある理由: ローチケ側の対策が変わったときに再検証できるようにするため。
「1回だけ実行（手動）」から check_lawson.py を選べば試せる。

--- 以下、調査でわかったこと ---

■ 遮断の実測（2026-10-16）
  手元 curl HTTP/2            → INTERNAL_ERROR で切断
  手元 curl HTTP/1.1          → タイムアウト
  手元 Python urllib          → タイムアウト
  手元の実ブラウザ（住宅IP）   → 読める
  Actions のヘッドレス Chrome → ERR_HTTP2_PROTOCOL_ERROR で弾かれる  ← ここで詰んだ

■ 取れる粒度（重要）
ローチケは「受付」単位でしか状態を持たない。ページ内の JSON はこうなっている:
  {"scheduleNo":"1","receptionName":"当日引換券",
   "entryStatus":"3","entryStatusName":"予定枚数終了",
   "receptionDateEnd":"20261025 1830"}
カレンダーに公演日（10/17〜10/25）は並ぶが、状態は大阪全公演まとめて1つ。
**公演ごと・土日のみの絞り込みはローチケ側の情報では不可能。**
公演ごとに見たい場合はイープラス側（check_tennimu.py）が担当する。

■ 判定
data-status-name 属性（無ければ JSON の entryStatusName）を読み、
NOTIFY_STATUS のいずれかになったら通知する。
"""

import re
import sys
from datetime import datetime
from pathlib import Path

from common import JST, line_push, load_state, save_state

# ---- 監視条件 -------------------------------------------------------------
EVENT_NAME = "ミュージカル『新テニスの王子様』The Final Stage（大阪）"
RECEPTION_NAME = "当日引換券"
L_CODE = "52849"
URL = (
    "https://l-tike.com/order/?gLcode=52849&gEntryMthd=02"
    "&gScheduleNo=1&gCarrierCd=08&gBaseVenueCd=52470"
)

# この状態になったら通知する。復活＝予定枚数終了から出ること。
NOTIFY_STATUS = ("発売中", "本日発売", "キャンセル待ち受付中")
# ---------------------------------------------------------------------------

STATE_PATH = Path(__file__).with_name("state_lawson.json")
PARSE_FAIL_ALERT_AT = 3
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def fetch_html() -> str:
    """ヘッドレス Chrome でページを開き、描画後の HTML を返す。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page(user_agent=UA, locale="ja-JP")
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            return page.content()
        finally:
            browser.close()


def parse_status(html: str) -> str | None:
    """受付の状態名を返す。読めなければ None。"""
    m = re.search(r'data-status-name="([^"]+)"', html)
    if m:
        return m.group(1).strip()
    # 属性が無い場合は埋め込み JSON を見る（エスケープされている）
    m = re.search(r'entryStatusName\\?"\s*:\s*\\?"([^"\\]+)', html)
    return m.group(1).strip() if m else None


def record_failure(state: dict, reason: str) -> int:
    fails = state.get("parse_failures", 0) + 1
    state["parse_failures"] = fails
    save_state(STATE_PATH, state)
    msg = (
        f"⚠️ ローチケ監視がデータを取得できませんでした（{fails}回連続）\n"
        f"{EVENT_NAME}／{reason}\n"
        f"遮断されたか、ページの作りが変わった可能性があります。\n{URL}"
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
    print(f"[{now}] {EVENT_NAME}　先着 {RECEPTION_NAME}（Lコード {L_CODE}）")

    try:
        html = fetch_html()
    except Exception as e:  # Playwright 未導入・起動失敗・遮断をまとめて拾う
        return record_failure(state, f"ページを開けませんでした: {type(e).__name__}: {e}")

    status = parse_status(html)
    if status is None:
        return record_failure(state, "受付の状態を読み取れませんでした")

    buyable = status in NOTIFY_STATUS
    print(f"    {'★' if buyable else ' '} 受付状態: {status}")

    prev = state.get("buyable")
    if buyable and not prev:
        lines = [
            "🎫 ローチケで当日引換券が動きました！",
            "",
            EVENT_NAME,
            f"先着 {RECEPTION_NAME}　→　{status}",
            "",
            "※ローチケは大阪全公演まとめて1つの状態です。",
            "　どの公演が取れるかは申込画面で確認してください。",
            "",
            URL,
            "",
            f"検知 {now}",
        ]
        text = "\n".join(lines)
        print("--- 通知 ---\n" + text)
        line_push(text)
    elif buyable:
        print("  → 買える状態だが前回から変化なし（通知しません）")
    else:
        print("  → まだ復活していません（通知しません）")

    state["buyable"] = buyable
    state["last_status"] = status
    state["parse_failures"] = 0
    save_state(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
