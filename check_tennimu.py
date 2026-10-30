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

# ページに出ている公演・受付を全部見る。個別の指定は不要。
# 公演日を過ぎたものは自動で見送る。
#
# ただし下記は特に待っている公演なので、通知で目立たせ、先頭に並べる。
# さらに、まだ公演日前なのにページから消えた場合は異常として警告を出す。
HIGHLIGHT = [
    {"date": "2026/11/15", "time": "17:30"},
]

# この文字列が受付ステータスにあれば「買える」とみなす。
BUYABLE = ("受付中",)
# ---------------------------------------------------------------------------

STATE_PATH = Path(__file__).with_name("state_tennimu.json")
PARSE_FAIL_ALERT_AT = 3


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_performances(page: str) -> list[dict]:
    """公演ごとに 日付・開演時刻・会場・地域 と、受付ごとの状態を取り出す。

    1公演に受付が複数ある（一般発売／先行／当日引換券／キャンセル待ちなど）。
    以前は全受付の状態を1つのリストに混ぜていたため「一般発売が受付中」でも
    「当日引換券が復活した」と誤判定しうる作りだった。受付ごとに分けて持つ。
    """
    out = []
    # 公演1件ごとのブロックに切る
    for chunk in page.split('class="block-ticket-fixed__date"')[1:]:
        ymd = re.search(r"<span>(\d{4})/</span>\s*<span>(\d{1,2})/(\d{1,2})", chunk)
        tm = re.search(r'class="block-ticket-fixed__time">開演：(\d{1,2}:\d{2})', chunk)
        vn = re.search(r'class="block-ticket-fixed__venue">([^<]*)<', chunk)
        rg = re.search(r'class="block-ticket-fixed__region">([^<]*)<', chunk)
        if not ymd or not tm:
            continue
        y, mo, d = (int(x) for x in ymd.groups())

        uketsuke = []
        for sec in chunk.split('<section class="block-ticket"')[1:]:
            nm = re.search(r'class="block-ticket__title">(.*?)</h4>', sec, re.S)
            statuses = [
                strip_tags(m)
                for m in re.findall(
                    r'<span class="ticket-status__item[^"]*"\s*>([^<]*)</span>', sec
                )
            ]
            uketsuke.append(
                {
                    "name": strip_tags(nm.group(1)) if nm else "",
                    "statuses": [s for s in statuses if s],
                }
            )

        out.append(
            {
                "date": f"{y}/{mo}/{d}",
                "time": tm.group(1),
                "venue": strip_tags(vn.group(1)) if vn else "",
                "region": strip_tags(rg.group(1)) if rg else "",
                "uketsuke": uketsuke,
            }
        )

    # 同じ公演が複数ブロックに分かれて載る。受付の種類ごとに別ブロックになるため、
    # 一般発売だけのブロックと当日引換券だけのブロックが別々に現れる。
    # まとめないと、先に見つかったブロックの受付しか見えない。
    merged: dict[tuple[str, str], dict] = {}
    for p in out:
        key = (p["date"], p["time"])
        if key in merged:
            merged[key]["uketsuke"] += p["uketsuke"]
            # 会場・地域は空でない方を残す
            merged[key]["venue"] = merged[key]["venue"] or p["venue"]
            merged[key]["region"] = merged[key]["region"] or p["region"]
        else:
            merged[key] = p
    return list(merged.values())


def perf_date(perf: dict) -> date:
    y, mo, d = (int(x) for x in perf["date"].split("/"))
    return date(y, mo, d)


def target_date(target: dict) -> date:
    y, mo, d = (int(x) for x in target["date"].split("/"))
    return date(y, mo, d)


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
    today = datetime.now(JST).date()
    seen_highlight: set[tuple[str, str]] = set()
    shown = 0

    for perf in sorted(performances, key=lambda x: (perf_date(x), x["time"])):
        # 公演日を過ぎたものは、ページから消えるのが当たり前なので何もしない。
        if perf_date(perf) < today:
            continue

        key = (perf["date"], perf["time"])
        hot = key in {(h["date"], h["time"]) for h in HIGHLIGHT}
        if hot:
            seen_highlight.add(key)

        for u in perf["uketsuke"]:
            if not u["name"]:
                continue
            label = f"{perf['date']} {perf['time']} [{u['name']}]"
            buyable = any(any(b in st for b in BUYABLE) for st in u["statuses"])
            new_state[label] = buyable
            if buyable and not (state.get("buyable") or {}).get(label):
                found.append({"perf": perf, "uketsuke": u, "hot": hot})

        detail = " / ".join(
            f"{u['name']}: {'・'.join(u['statuses']) or '状態なし'}"
            for u in perf["uketsuke"] if u["name"]
        )
        buy_any = any(new_state.get(f"{perf['date']} {perf['time']} [{u['name']}]")
                      for u in perf["uketsuke"] if u["name"])
        mark = "★" if buy_any else ("◎" if hot else " ")
        print(f"    {mark} {perf['date']} {perf['time']}　{perf['venue']}　{detail or '受付なし'}")
        shown += 1

    print(f"  （公演日前の {shown} 公演を監視対象にしています）")

    # 特に待っている公演が、まだ公演日前なのにページから消えていたら異常。
    missing = [
        f"{h['date']} {h['time']}"
        for h in HIGHLIGHT
        if target_date(h) >= today and (h["date"], h["time"]) not in seen_highlight
    ]

    if found:
        # 特に待っている公演を先頭に出す。
        found.sort(key=lambda f: (not f["hot"], f["perf"]["date"], f["perf"]["time"]))
        hot_hits = [f for f in found if f["hot"]]

        if hot_hits:
            h = hot_hits[0]["perf"]
            lines = [
                "🔔🔔🔔 お待ちかねの公演です 🔔🔔🔔",
                "",
                f"★ {h['date']} {h['time']}　{h['venue']}{h['region']}",
                "　が復活しました！",
                "",
                "──────────────",
            ]
        else:
            lines = ["🎫 チケットが復活しました！", ""]

        lines.append(EVENT_NAME)
        for f in found:
            pf, u = f["perf"], f["uketsuke"]
            head = "★ " if f["hot"] else "・"
            lines += [
                "",
                f"{head}{pf['date']} {pf['time']}　{pf['venue']}{pf['region']}",
                f"　{u['name']}　{'・'.join(u['statuses'])}",
            ]
        lines += ["", EVENT_URL, "", f"検知 {now}"]
        text = "\n".join(lines)
        print("--- 通知 ---\n" + text)
        line_push(text)
    elif any(new_state.values()):
        print("  → 買える受付はあるが前回から変化なし（通知しません）")
    elif new_state:
        print("  → まだ復活していません（通知しません）")
    else:
        print("  → 見るべき公演がありません（全て公演日を過ぎています）")

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
