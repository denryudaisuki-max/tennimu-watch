#!/usr/bin/env bash
#
# GitHub Actions の cron は大幅に間引かれる（実測: 71時間で59回＝想定の7%、
# 中央値59分・最大208分の間隔）。cron の発火回数に確認回数を依存させると、
# 「5分ごと」と設定しても実際には1時間に1回しか見られない。
#
# そこで1つのジョブを長時間走らせ、その中で自前のタイマーで確認する。
# cron はこのループを再開させるきっかけとしてのみ使う。
#
set -uo pipefail

INTERVAL="${INTERVAL_SECONDS:-300}"   # 確認間隔（秒）
RUN_FOR="${RUN_SECONDS:-19800}"       # ループを回す時間（秒）。既定 5時間30分

# Actions では python、macOS では python3。手元でも同じスクリプトを試せるようにする。
PYTHON="$(command -v python3 || command -v python)"
if [[ -z "$PYTHON" ]]; then
  echo "python が見つかりません" >&2
  exit 1
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

save_state() {
  if [[ -z "$(git status --porcelain state_hotel.json state_routeinn.json state_tennimu.json)" ]]; then
    return
  fi
  git add state_hotel.json state_routeinn.json state_tennimu.json
  git commit -q -m "状態更新: $(date -u '+%Y-%m-%d %H:%M UTC')"
  git pull -q --rebase origin main 2>/dev/null || true
  git push -q origin HEAD:main 2>/dev/null || echo "[warn] push に失敗。次の変化時にまとめて反映されます"
}

start=$SECONDS
n=0
while (( SECONDS - start < RUN_FOR )); do
  n=$((n + 1))
  echo "───── チェック #${n}　$(TZ=Asia/Tokyo date '+%Y-%m-%d %H:%M:%S JST') ─────"

  # 3回連続で失敗した場合はスクリプト側が LINE に警告を出す。
  # どれか1つが落ちても残りは動かす（|| で握って次へ進める）。
  "$PYTHON" check_hotel.py || echo "[warn] 東横イン監視が失敗しました"
  "$PYTHON" check_routeinn.py || echo "[warn] ルートイン監視が失敗しました"
  "$PYTHON" check_tennimu.py || echo "[warn] テニミュ監視が失敗しました"

  save_state

  remaining=$(( RUN_FOR - (SECONDS - start) ))
  (( remaining <= INTERVAL )) && break
  sleep "$INTERVAL"
done

echo "───── ループ終了：${n}回チェックしました。次の起動を待ちます ─────"
