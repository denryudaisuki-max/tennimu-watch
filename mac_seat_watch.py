#!/usr/bin/env python3
"""
席種単位（当日引換券 / 見切席当日引換券）の在庫を監視して LINE に通知する。

■ なぜ Mac 上で動かすのか
公開ページ（eplus.jp/sf/detail）は「受付」単位のステータスしか持たない。
席種の内訳は申込ページ（atom.eplus.jp）の表にしかなく、そこは URL 直打ちだと
「画面遷移エラー」になる。eplus が飛込防止として /sf/dvcjudge で DVC_UNIQUE_ID
（有効1時間）を発行し、正規の導線を経ていないアクセスを弾いているため。

そこで、実ブラウザ(Chrome)でサイトの導線をそのままなぞる:
    詳細ページを開く → 当日引換券受付の「次へ」を押す → 席種の表を読む
トークンの偽造や bot 検知の回避は一切していない。人が指で行う操作と同じ手順を
タイマーが代行しているだけ。そのぶん Mac を起動したままにする必要がある。

■ 判定
表の「当日引換券」列が ○（空席あり）または △（残りわずか）になったら通知する。
見切席当日引換券は別列なので、見切席が売れ残っていても影響を受けない。
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from common import JST, line_push, load_state, save_state

DETAIL_URL = "https://eplus.jp/sf/detail/0473460001"
EVENT = "ミュージカル『テニスの王子様』4th 全国大会 青学vs立海 前編"

# ---- 監視条件 -------------------------------------------------------------
# 表の行ラベルに含まれる文字列で公演を指定する
TARGETS = {
    "2026/08/29": {"12：00": "8/29(土) 12:00開演", "17：30": "8/29(土) 17:30開演"},
    "2026/08/30": {"12：00": "8/30(日) 12:00開演"},
}
# この席種の列を見る（表の見出しと部分一致。見切席は別列なので巻き込まない）
WATCH_SEAT = "当日引換券"
EXCLUDE_SEAT = "見切席"

AVAILABLE_MARKS = {"○", "△"}  # ○空席あり / △残りわずか
# ---------------------------------------------------------------------------

STATE_PATH = Path(__file__).with_name("state_seat.json")
TOKEN_FILE = Path.home() / ".config" / "ticket-watch" / "line-token"
PARSE_FAIL_ALERT_AT = 3


def load_token() -> None:
    """環境変数が無ければローカルのトークンファイルを読む。"""
    if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"):
        return
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = tok


def fetch_seat_table(headless: bool = True) -> list[dict]:
    """サイトの導線をなぞって申込ページに入り、席種の表を返す。

    戻り値: [{"date": "2026/08/29(土)", "time": "12：00開演", "seats": {"当日引換券": "×", ...}}, ...]
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=headless)
        ctx = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()
        try:
            page.goto(DETAIL_URL, wait_until="domcontentloaded", timeout=45000)
            # ページ自身の JS（/sf/dvcjudge を含む）が動き終わるのを待つ
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PWTimeout:
                pass

            # 当日引換券受付の「次へ」を、狙っている公演のどれかから押す。
            # 遷移先の表には全公演ぶんが載るので、1回押せば足りる。
            btn = page.evaluate(
                """() => {
                  const arts = [...document.querySelectorAll('article.block-ticket-article')];
                  for (const a of arts) {
                    for (const sec of a.querySelectorAll('section.block-ticket')) {
                      const t = (sec.querySelector('.block-ticket__title')||{}).innerText || '';
                      if (!t.includes('当日引換')) continue;
                      const b = sec.querySelector('button[onclick]');
                      if (!b) continue;
                      b.id = 'seatwatch-target';
                      return true;
                    }
                  }
                  return false;
                }"""
            )
            if not btn:
                raise RuntimeError("詳細ページに当日引換券受付のボタンが見つかりません")

            page.click("#seatwatch-target", timeout=15000)
            page.wait_for_load_state("domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)

            html = page.content()
            if "画面遷移エラー" in html:
                raise RuntimeError("画面遷移エラー（飛込トークンが無効）")
            return parse_table(html)
        finally:
            browser.close()


def parse_table(html: str) -> list[dict]:
    """申込ページの席種テーブルを解析する。"""
    m = re.search(r"<table[^>]*>(?:(?!</table>).)*?event-seat.*?</table>", html, re.S)
    if not m:
        return []
    table = m.group(0)

    headers = [
        re.sub(r"<[^>]+>", "", h).strip()
        for h in re.findall(r'<span class="event-seat">(.*?)</span>', table, re.S)
    ]
    rows = []
    for tr in re.findall(r"<tr>(?:(?!</tr>).)*?</tr>", table, re.S):
        d = re.search(r"(\d{4}/\d{2}/\d{2})\(.\)", tr)
        if not d:
            continue
        t = re.search(r"<span>\s*([\d：]+)開演", tr)
        marks = re.findall(r'align="center">\s*([○×△－])', tr)
        if not marks:  # 休演など
            continue
        rows.append(
            {
                "date": d.group(1),
                "time": (t.group(1) + "開演") if t else "",
                "seats": dict(zip(headers, marks)),
            }
        )
    return rows


def watched_seat_value(seats: dict) -> str | None:
    """『当日引換券』（見切席でない方）の記号を取り出す。"""
    for name, mark in seats.items():
        if WATCH_SEAT in name and EXCLUDE_SEAT not in name:
            return mark
    return None


def main() -> int:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    load_token()
    state = load_state(STATE_PATH)
    prev: dict[str, str] = state.get("marks", {})

    headless = os.environ.get("SEATWATCH_HEADLESS", "1") != "0"
    try:
        rows = fetch_seat_table(headless=headless)
    except Exception as e:  # noqa: BLE001 - どんな失敗でもループは続けたい
        rows = []
        err = str(e)
    else:
        err = ""

    if not rows:
        fails = state.get("parse_failures", 0) + 1
        state["parse_failures"] = fails
        save_state(STATE_PATH, state)
        msg = (
            f"⚠️ 席種監視が表を読めませんでした（{fails}回連続）\n"
            f"{err or '表が見つかりません'}\n"
            f"Chromeやページ構造を確認してください。"
        )
        print(msg, file=sys.stderr)
        if fails == PARSE_FAIL_ALERT_AT:
            try:
                line_push(msg)
            except RuntimeError as e2:
                print(f"[warn] 障害通知も送れませんでした: {e2}", file=sys.stderr)
        return 1

    state["parse_failures"] = 0
    print(f"[{now}] 席種単位の在庫")

    current: dict[str, str] = {}
    revived: list[tuple[str, str]] = []
    for row in rows:
        times = TARGETS.get(row["date"])
        if not times:
            continue
        label = next((v for k, v in times.items() if k in row["time"]), None)
        if not label:
            continue
        mark = watched_seat_value(row["seats"])
        if mark is None:
            continue
        current[label] = mark
        others = "  ".join(f"{k}:{v}" for k, v in row["seats"].items())
        star = "★" if mark in AVAILABLE_MARKS else " "
        print(f"  {star} {label}    {others}")
        if mark in AVAILABLE_MARKS and prev.get(label) not in AVAILABLE_MARKS:
            revived.append((label, mark))

    if not current:
        print("  → 監視対象の公演が表にありません", file=sys.stderr)
        return 1

    if revived:
        lines = ["🎾 当日引換券が出ました！（見切席ではありません）", "", EVENT, ""]
        for label, mark in revived:
            word = "空席あり" if mark == "○" else "残りわずか"
            lines.append(f"・{label}　{mark}（{word}）")
        lines += ["", f"▼申込\n{DETAIL_URL}", "", f"検知 {now}"]
        text = "\n".join(lines)
        print("--- 通知 ---\n" + text)
        line_push(text)
    else:
        print("  → 変化なし（通知しません）")

    state["marks"] = current
    save_state(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
