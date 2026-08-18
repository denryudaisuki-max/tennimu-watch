#!/usr/bin/env python3
"""
イープラスの公演詳細ページを監視し、狙っている受付が復活したら LINE に通知する。

このページのステータスは「受付」単位で、席種（当日引換券 / 見切席当日引換券）の
区別はできない。席種別の表は atom.eplus.jp の申込ページにしかなく、そちらは
機械的に取得できない（403）。したがって「見切席だけが復活した」場合にも通知は
飛ぶ。通知を受けたらリンクを開いて席種を自分で確認すること。
"""

import html as H
import re
import sys
from datetime import datetime
from pathlib import Path

from common import JST, http_get, line_push, load_state, save_state

URL = "https://eplus.jp/sf/detail/0473460001"
EVENT = "ミュージカル『テニスの王子様』4th 全国大会 青学vs立海 前編"

# ---- 監視対象 -------------------------------------------------------------
# キーは詳細ページの <article class="block-ticket-article {キー} local-..."> から。
TARGETS = [
    {"key": "20260829-開演-1200-13", "label": "8/29(土) 12:00開演"},
    {"key": "20260829-開演-1730-13", "label": "8/29(土) 17:30開演"},
    {"key": "20260830-開演-1200-13", "label": "8/30(日) 12:00開演"},
]

# 受付名にこれらの語を含むものだけを見る（一般発売・先行抽選は無視）
WATCH_RECEIPTS = ("当日引換券", "キャンセル待ち")
# ---------------------------------------------------------------------------

# このステータスなら「買える」とみなす
AVAILABLE = {"受付中", "残りわずか", "空席あり"}

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


def parse_receipts(block: str) -> dict[str, dict]:
    """{受付名: {"status": ..., "url": ...}} を返す。"""
    out: dict[str, dict] = {}
    for sec in re.split(r'(?=<section class="block-ticket">)', block):
        m_title = re.search(r'<h4 class="block-ticket__title">(.*?)</h4>', sec, re.S)
        m_status = re.search(r'<p class="ticket-status">(.*?)</p>', sec, re.S)
        if not m_title or not m_status:
            continue
        title = strip_tags(m_title.group(1))
        if not title:
            continue
        m_url = re.search(r"window\.location\.href='([^']+)'", sec)
        out[title] = {
            "status": strip_tags(m_status.group(1)) or "(不明)",
            "url": m_url.group(1) if m_url else URL,
        }
    return out


def watched(receipts: dict[str, dict]) -> dict[str, dict]:
    return {
        name: info
        for name, info in receipts.items()
        if any(word in name for word in WATCH_RECEIPTS)
    }


def record_failure(state: dict, reason: str) -> int:
    fails = state.get("parse_failures", 0) + 1
    state["parse_failures"] = fails
    save_state(STATE_PATH, state)
    msg = (
        f"⚠️ チケット監視が公演を読み取れませんでした（{fails}回連続）\n"
        f"{reason}\n"
        f"ページ構造が変わったか、公演が一覧から消えた可能性があります。\n{URL}"
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
    prev_all = state.get("receipts") or {}
    # 公演ごとの入れ子でなければ（旧形式）無視して作り直す
    if not all(isinstance(v, dict) for v in prev_all.values()):
        prev_all = {}

    page = http_get(URL)

    print(f"[{now}] {EVENT}")
    new_all: dict[str, dict[str, str]] = {}
    revived: list[tuple[str, str, str, str]] = []  # (公演, 受付, ステータス, URL)

    for target in TARGETS:
        block = extract_article(page, target["key"])
        receipts = watched(parse_receipts(block)) if block else {}
        if not receipts:
            return record_failure(state, f"{target['label']}（{target['key']}）")

        prev = prev_all.get(target["key"]) or {}
        print(f"  ● {target['label']}")
        current: dict[str, str] = {}
        for name, info in receipts.items():
            status = info["status"]
            current[name] = status
            mark = "★" if status in AVAILABLE else " "
            print(f"    {mark} {name}: {status}")
            if status in AVAILABLE and prev.get(name) not in AVAILABLE:
                revived.append((target["label"], name, status, info["url"]))

        new_all[target["key"]] = current

    state["parse_failures"] = 0

    if revived:
        lines = ["🎾 チケットが動きました！", "", EVENT]
        last_label = None
        for label, name, status, url in revived:
            if label != last_label:
                lines += ["", f"【{label}】"]
                last_label = label
            lines.append(f"・{name}：{status}")
            lines.append(f"　{url}")
        lines += [
            "",
            "※席種（当日引換券／見切席当日引換券）は判別できません。",
            "　リンクを開いて確認してください。",
            "",
            f"検知 {now}",
        ]
        text = "\n".join(lines)
        print("--- 通知 ---\n" + text)
        line_push(text)
    else:
        print("  → 変化なし（通知しません）")

    state["receipts"] = new_all
    save_state(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
