#!/usr/bin/env python3
"""チケット監視・ホテル監視で共通して使う処理。"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def http_get(url: str, accept: str = "text/html", tries: int = 3) -> str:
    """GET してテキストを返す。一時的な失敗はリトライする。"""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": accept,
                    "Accept-Language": "ja,en;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < tries - 1:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"取得に失敗: {last}")


def line_push(text: str) -> None:
    """LINE の broadcast API で自分（友だち登録済みの自分だけ）に送る。"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token:
        print("[warn] LINE_CHANNEL_ACCESS_TOKEN 未設定。本文を出力するだけにします:\n" + text)
        return
    body = json.dumps(
        {"messages": [{"type": "text", "text": text[:4900]}]}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/broadcast",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(f"[line] sent, http {r.status}")
                return
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            last = f"HTTP {e.code}: {detail}"
            if 400 <= e.code < 500:  # 4xx は再試行しても直らない
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
        if attempt < 2:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"LINE 送信に失敗: {last}")


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
