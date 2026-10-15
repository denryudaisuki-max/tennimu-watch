#!/usr/bin/env python3
"""
ミュージカル『新テニスの王子様』の指定公演が復活したら LINE に通知する。

■ 何を見ているか
イープラスの「公演詳細」ページ（/sf/detail/xxxxxxxxx）を取得し、
公演ごとの受付ステータスを読む。「予定枚数終了 → 受付中」に
変わった瞬間が「復活」。

  https://eplus.jp/sf/detail/0473460021

■ 過去の結論との違い（重要）
以前「イープラスは自動取得できない」と結論づけたが、あれは
**席種別の在庫**（申込ページ /sf/dvcjudge 側）の話。あちらは Akamai と
飛込防止で今も取れない。
このスクリプトが見ている**公開の公演詳細ページは普通に GET できる**。
公演単位の「受付中／予定枚数終了」までなら分かる。
つまり「どの席種が何枚あるか」は分からないが、
「その公演が買える状態になったか」は分かる。今回はそれで足りる。

■ HTML の構造（実測）
公演1件ごとに block-ticket-fixed__date で始まるブロックがあり、
その中に開演時刻・会場と、受付ごとの ticket-status__item が入る。
1公演に受付が複数ある（先行と一般発売など）ため、状態は配列で持つ。

  <div class="block-ticket-fixed__date"><span>2026/</span><span>11/15(日)</span></div>
  <span class="block-ticket-fixed__time">開演：17:30～</span>
  <span class="ticket-status__item ...">予定枚数終了</span>

注意: クラス名 ticket-status__item--accepting は「予定枚数終了」にも
付いている。クラスでは判定できないので、必ずテキストで見ること。
"""

import html
import re
import sys
from datetime import date, datetime
from pathlib import Path

from common import JST, http_get, line_push, load_state, save_state

# ---- 監視条件 -------------------------------------------------------------
EVENT_NAME = "ミュージカル『新テニスの王子様』The Final Stage"
EVENT_URL = "https://eplus.jp/sf/detail/0473460021"

# 復活を待っている公演。増やせばまとめて見る。
TARGETS = [
    {"date": "2026/11/15", "time": "17:30"},
    # 2026/10/2 18:00 は公演終了により削除（2026-10-15）。
    # 公演日を過ぎた対象は自動で見送るので、消し忘れても失敗にはならない。
]

# この文字列が受付ステータスにあれば「買える」とみなす。
BUYABLE = ("受付中",)
# ---------------------------------------------------------------------------

STATE_PATH = Path(__file__).with_name("state_tennimu.json")
PARSE_FAIL_ALERT_AT = 3


def label_of(target: dict) -> str:
    return f"{target['date']} {target['time']}"


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_performances(page: str) -> list[dict]:
    """公演ごとに 日付・開演時刻・会場・受付ステータス群 を取り出す。"""
    out = []
    # 公演1件ごとのブロックに切る
    for chunk in page.split('class="block-ticket-fixed__date"')[1:]:
        ymd = re.search(r"<span>(\d{4})/</span>\s*<span>(\d{1,2})/(\d{1,2})", chunk)
        tm = re.search(r'class="block-ticket-fixed__time">開演：(\d{1,2}:\d{2})', chunk)
        vn = re.search(r'class="block-ticket-fixed__venue">([^<]*)<', chunk)
        if not ymd or not tm:
            continue
        y, mo, d = (int(x) for x in ymd.groups())
        statuses = [
            strip_tags(m)
            for m in re.findall(
                r'<span class="ticket-status__item[^"]*"\s*>([^<]*)</span>', chunk
            )
        ]
        out.append(
            {
                "date": f"{y}/{mo}/{d}",
                "time": tm.group(1),
                "venue": vn.group(1).strip() if vn else "",
                "statuses": [s for s in statuses if s],
            }
        )
    return out


def target_date(target: dict) -> date:
    y, mo, d = (int(x) for x in target["date"].split("/"))
    return date(y, mo, d)


def find_target(performances: list[dict], target: dict) -> dict | None:
    want = target_date(target)
    for p in performances:
        y, mo, d = (int(x) for x in p["date"].split("/"))
        if date(y, mo, d) == want and p["time"] == target["time"]:
            return p
    return None


def record_failure(state: dict, reason: str) -> int:
    fails = state.get("parse_failures", 0) + 1
    state["parse_failures"] = fails
    save_state(STATE_PATH, state)
    msg = (
        f"⚠️ テニミュ監視がデータを取得できませんでした（{fails}回連続）\n"
        f"{reason}\n"
        f"ページの作りが変わったか、遮断された可能性があります。\n{EVENT_URL}"
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

    print(f"[{now}] {EVENT_NAME}")

    try:
        page = http_get(EVENT_URL, accept="text/html")
    except RuntimeError as e:
        return record_failure(state, f"ページ取得に失敗: {e}")

    performances = parse_performances(page)
    if not performances:
        # 1件も読めない＝作りが変わったか遮断された。黙って見逃さないよう失敗扱いにする。
        return record_failure(state, "公演を1件も読み取れませんでした")

    print(f"  ページ全体で {len(performances)} 公演を確認")

    new_state: dict[str, bool] = {}
    found: list[dict] = []
    missing: list[str] = []
    today = datetime.now(JST).date()

    for target in TARGETS:
        label = label_of(target)

        # 公演日を過ぎた対象は、ページから消えるのが当たり前なので失敗にしない。
        # ここを失敗扱いにしていたため、10/2 公演の終了後に監視が11日間止まった。
        if target_date(target) < today:
            print(f"      {label} は公演日を過ぎたので見ません")
            continue

        p = find_target(performances, target)
        if p is None:
            # まだ未来の公演が一覧から消えているのは異常。ただしここで抜けると
            # 他の公演の通知まで止まるので、記録だけして最後にまとめて報告する。
            missing.append(label)
            print(f"    ⚠ {label} が一覧に見つかりません")
            continue

        buyable = any(any(b in s for b in BUYABLE) for s in p["statuses"])
        new_state[label] = buyable
        mark = "★" if buyable else " "
        print(f"    {mark} {label}　{p['venue']}　{' / '.join(p['statuses']) or '(状態なし)'}")

        if buyable and not (state.get("buyable") or {}).get(label):
            found.append({"label": label, **p})

    if found:
        lines = ["🎫 チケットが復活しました！", "", EVENT_NAME]
        for f in found:
            lines += [
                "",
                f"{f['label']}　{f['venue']}",
                f"状態: {' / '.join(f['statuses'])}",
            ]
        lines += ["", EVENT_URL, "", f"検知 {now}"]
        text = "\n".join(lines)
        print("--- 通知 ---\n" + text)
        line_push(text)
    elif any(new_state.values()):
        print("  → 買える状態だが前回から変化なし（通知しません）")
    elif new_state:
        print("  → まだ復活していません（通知しません）")
    else:
        print("  → 見るべき対象がありません（公演日を過ぎた対象のみ）")

    # 取れた分の状態は、欠けた対象があっても必ず保存する。
    # 保存しないと、次回に同じ復活をもう一度通知してしまう。
    state["buyable"] = new_state

    if missing:
        return record_failure(state, "対象公演が見つかりません: " + ", ".join(missing))

    state["parse_failures"] = 0
    save_state(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
