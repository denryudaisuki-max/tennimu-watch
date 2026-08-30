#!/usr/bin/env bash
#
# 新しいセッションへの引き継ぎ用。これ1本を実行すれば現状がすべて分かる。
#   bash handoff.sh
#
set -uo pipefail
cd "$(dirname "$0")"
REPO="denryudaisuki-max/tennimu-watch"
PY="$(command -v python3 || command -v python)"

line() { printf '\n\033[1m%s\033[0m\n' "$1"; printf '%.0s─' {1..70}; echo; }

line "■ これは何か"
cat <<'EOS'
東横イン札幌の空室を5分ごとに監視し、条件を満たしたら LINE に通知する仕組み。
GitHub Actions 上で動いているので、Mac は閉じていてよい。
詳細はすべて README.md に書いてある。まずそれを読むこと。
EOS

line "■ リポジトリ"
echo "  https://github.com/$REPO"
echo "  ローカル: $(pwd)"
echo "  HEAD: $(git rev-parse --short HEAD 2>/dev/null)  $(git log -1 --format=%s 2>/dev/null)"
if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  echo "  ⚠ 未コミットの変更あり:"; git status --short | sed 's/^/     /'
else
  echo "  作業ツリーはクリーン"
fi

line "■ 監視条件（check_hotel.py より）"
grep -E '^(AREA_ID|AREA_LABEL|CHECKIN|NIGHTS|SMOKING|MAX_PRICE) *=' check_hotel.py | sed 's/^/  /'
echo "  SEARCHES:"; sed -n '/^SEARCHES = \[/,/^\]/p' check_hotel.py | sed 's/^/    /'
echo "  HOTELS:"; sed -n '/^HOTELS = {/,/^}/p' check_hotel.py | sed 's/^/    /'

line "■ 今の在庫（実際に API を叩いて確認）"
"$PY" check_hotel.py 2>&1 | sed 's/^/  /' || echo "  （実行に失敗）"
echo
echo "  ※ここで state_hotel.json が更新される。通知は飛ばない（条件を満たしたときのみ）"

line "■ GitHub Actions の稼働状況"
if command -v gh >/dev/null 2>&1; then
  gh run list --repo "$REPO" --limit 5 \
    --json databaseId,name,status,event,createdAt \
    --jq '.[] | "  \(.createdAt[5:16]|sub("T";" ")) UTC  \(.name)  \(.event)  \(.status)  run=\(.databaseId)"' \
    2>/dev/null || echo "  （gh の認証が切れている可能性）"
else
  echo "  gh コマンドが無い。brew install gh で入れる"
fi

line "■ 引き継ぎ事項（コードからは読み取れないこと）"
cat <<'EOS'
  1. 【未確認】MAX_PRICE=19000 は「2泊の合計」という前提で組んである。
     もし「1泊あたり」の意味なら判定が真逆になる。
     ・現状: 札幌駅北口は2泊 ¥28,400（=1泊 ¥14,200）
     ・合計解釈 → 予算オーバーで通知しない（今の挙動）
     ・1泊解釈 → 既に条件を満たすので即通知すべき
     ユーザーに確認すること。

  2. lowestPrice は「滞在合計」。1泊あたりではない（実測済み）。
     札幌駅北口 10/17〜  1泊 ¥15,700 / 2泊 ¥28,400

  3. cron は使い物にならないので使っていない。
     実測で */5 に対して実行率7%、間隔の中央値59分、最大208分の空白。
     代わりに1ジョブを5時間30分走らせ、その中の自前タイマーで5分ごとに確認。
     cron は「ループが切れたときの再開トリガー」に格下げしてある。

  4. 設定変更後は、走っているループを止めないと反映されない。
     ループ開始時のコミットをチェックアウトしたまま最大5.5時間走り続けるため。
       gh run cancel <走っているrun> --repo denryudaisuki-max/tennimu-watch
       gh workflow run "空室監視" --repo denryudaisuki-max/tennimu-watch

  5. 過去にテニミュのチケット監視もやっていたが、公演終了により削除済み。
     イープラスの申込ページ（席種別の在庫が見える唯一の場所）は
     Akamai と飛込防止（/sf/dvcjudge の DVC_UNIQUE_ID）で自動取得できない。
     公開ページは「受付」単位までしか分からない。この件は決着済み。
EOS

line "■ よく使う操作"
cat <<EOS
  1回だけ確認          python3 check_hotel.py
  ループを手元で試す    INTERVAL_SECONDS=3 RUN_SECONDS=8 bash watch_loop.sh
  稼働状況             gh run list --repo $REPO --limit 5
  手動起動             gh workflow run "空室監視" --repo $REPO
  ループ停止           gh run cancel <run番号> --repo $REPO
  通知テスト           check_hotel.py の MAX_PRICE を一時的に 30000 にして実行
                       （確認後は必ず戻す）
EOS

line "■ 新しいセッションでの最初の一言（コピペ用）"
cat <<EOS
  東横イン札幌の空室監視をやっています。$(pwd) にコードがあります。
  bash handoff.sh を実行して現状を把握してください。
EOS
echo
