#!/usr/bin/env python3
"""
イープラスの公演詳細ページを監視し、対象公演の受付が復活したら LINE に通知する。

判定は「受付ステータス」単位で行う。このページのステータスは席種のいずれかに
空きがあれば「受付中」になるため、全席指定 / サイドシート / 見切席 のどれか
1つでも復活すれば検知できる。
"""

import html as H
import re
import sys
from datetime import datetime
from pathlib import Path

from common import JST, http_get, line_push, load_state, save_state

URL = "https://eplus.jp/sf/detail/0473460001"

# 監視対象。ページ内 <article class="block-ticket-article {KEY} local-.. ..."> のキー。
# 20260830-開演-1730-13 = 2026/8/30(日) 17:30開演 (東京都)
TARGET_KEY = "20260830-開演-1730-13"
TARGET_LABEL = "2026/8/30(日) 17:30開演　TACHIKAWA STAGE GARDEN"

# このステータスなら「買える」とみなす
AVAILABLE = {"受付中", "残りわずか", "空席あり"}

# 連続でこの回数パースに失敗したら LINE でも知らせる
PARSE_FAIL_ALERT_AT = 3

STATE_PATH = Path(__file__).with_name("state.json")


def strip_tags(s: str) -> str:
    return H.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def extract_article(page: str, key: str) -> str | None:
    """対象公演の <article> ブロックを結合して返す。

    1公演につき <article> は2つある（終了した受付のまとまりと、現在の受付の
    まとまり）。先頭の1つだけを見ると当日引換券やキャンセル待ち券を取りこぼす
    ので、同じキーのブロックはすべて連結する。
    """
    marks = [
        m.start()
        for m in re.finditer(r'<article class="block-ticket-article \d{8}-', page)
    ]
    marks.append(len(page))
    blocks = [
        page[marks[i] : marks[i + 1]]
        for i in range(len(marks) - 1)
        if f"block-ticket-article {key} " in page[marks[i] : marks[i + 1]]
    ]
    return "".join(blocks) if blocks else None


def parse_receipts(block: str) -> dict[str, str]:
    """{受付名: ステータス} を返す。"""
    out: dict[str, str] = {}
    pairs = re.findall(
        r'<h4 class="block-ticket__title">(.*?)</h4>.*?<p class="ticket-status">(.*?)</p>',
        block,
        re.S,
    )
    for title_html, status_html in pairs:
        title = strip_tags(title_html)
        status = strip_tags(status_html)
        if title:
            out[title] = status or "(不明)"
    return out


def purchase_url(block: str) -> str:
    m = re.search(r"window\.location\.href='([^']+)'", block)
    return m.group(1) if m else URL


def main() -> int:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    state = load_state(STATE_PATH)
    prev: dict[str, str] = state.get("receipts", {})

    page = http_get(URL)
    block = extract_article(page, TARGET_KEY)
    current = parse_receipts(block) if block else {}

    if not current:
        # ページ構造が変わった / 公演が消えた。無言で見逃さないよう失敗させる。
        fails = state.get("parse_failures", 0) + 1
        state["parse_failures"] = fails
        save_state(STATE_PATH, state)
        msg = (
            f"⚠️ チケット監視が対象公演を読み取れませんでした（{fails}回連続）\n"
            f"{TARGET_LABEL}\n"
            f"ページ構造が変わったか、公演が一覧から消えた可能性があります。\n{URL}"
        )
        print(msg, file=sys.stderr)
        if fails == PARSE_FAIL_ALERT_AT:
            try:
                line_push(msg)
            except RuntimeError as e:
                print(f"[warn] 障害通知も送れませんでした: {e}", file=sys.stderr)
        return 1

    # last_ok のような毎回変わる値は state に入れない
    # （state.json を毎回コミットすることになり、5分ごとに履歴が汚れるため）
    state["parse_failures"] = 0

    print(f"[{now}] {TARGET_LABEL}")
    for name, status in current.items():
        mark = "★" if status in AVAILABLE else " "
        print(f"  {mark} {name}: {status}")

    # 「復活」= 前回 買えなかった受付が 今回 買える状態になった
    revived = [
        name
        for name, status in current.items()
        if status in AVAILABLE and prev.get(name) not in AVAILABLE
    ]

    if revived:
        link = purchase_url(block)
        lines = [
            "🎾 チケット復活！",
            "",
            "ミュージカル『テニスの王子様』4th 全国大会 青学vs立海 前編",
            TARGET_LABEL,
            "",
        ]
        for name in revived:
            lines.append(f"・{name}：{current[name]}")
        lines += ["", f"▼購入\n{link}", "", f"検知 {now}"]
        text = "\n".join(lines)
        print("--- 通知 ---\n" + text)
        line_push(text)
    else:
        print("  → 変化なし（通知しません）")

    state["receipts"] = current
    save_state(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
