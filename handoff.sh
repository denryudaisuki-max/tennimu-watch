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
ホテルの空室を5分ごとに監視し、条件を満たしたら LINE に通知する仕組み。
GitHub Actions 上で動いているので、Mac は閉じていてよい。
いま監視しているのは2件（それぞれ独立。片方が落ちても他方は動く）:
  ① 東横イン札幌 4館      2026-10-17〜10-19（2泊）滞在合計 ¥19,000 未満のとき
  ② ホテルルートイン土岐  2026-10-30〜11-01（2泊）空室が出たら（金額条件なし）
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

line "■ 監視条件① 東横イン（check_hotel.py より）"
grep -E '^(AREA_ID|AREA_LABEL|CHECKIN|NIGHTS|SMOKING|MAX_PRICE) *=' check_hotel.py | sed 's/^/  /'
echo "  SEARCHES:"; sed -n '/^SEARCHES = \[/,/^\]/p' check_hotel.py | sed 's/^/    /'
echo "  HOTELS:"; sed -n '/^HOTELS = {/,/^}/p' check_hotel.py | sed 's/^/    /'

line "■ 監視条件② ルートイン（check_routeinn.py より）"
grep -E '^(HOTEL_NAME|HOTEL_CODE|HOTEL_ID|CHECKIN|CHECKOUT|MAX_PRICE) *=' check_routeinn.py | sed 's/^/  /'
echo "  SEARCHES:"; sed -n '/^SEARCHES = \[/,/^\]/p' check_routeinn.py | sed 's/^/    /'

line "■ 今の在庫（実際に API を叩いて確認）"
"$PY" check_hotel.py 2>&1 | sed 's/^/  /' || echo "  （実行に失敗）"
echo
"$PY" check_routeinn.py 2>&1 | sed 's/^/  /' || echo "  （実行に失敗）"
echo
echo "  ※ここで state_*.json が更新される。通知は飛ばない（条件を満たしたときのみ）"

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
  1. 【決着 2026-08-30】MAX_PRICE=19000 は「滞在合計」。ユーザー本人に確認済み。
     「1泊あたり」ではない。もう蒸し返さなくてよい。
     ・2泊合計が ¥19,000 未満のときだけ通知する、が正しい挙動。
     ・1泊あたり ¥9,500 相当。東横インの通常料金としては自然な水準で、
       10/17の札幌が高騰しているのが平常に戻るのを待つ、という設定。
     ・現状 北口は2泊 ¥28,400 なので「通知しない」で正しい。約33%の値下がりが必要。

  2. lowestPrice は「滞在合計」で確定（2026-08-30 に1/2/3泊を叩いて再実測）。
     札幌駅北口 10/17〜  1泊 ¥15,700 ／ 2泊 ¥28,400 ／ 3泊 ¥41,100
     2泊目以降は +¥12,700/泊。泊数に比例して増えるので合計で確定。推測ではない。

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

  6. ルートイン（tripla API）で詰まったら、まずこの4点を疑う。全部実測で判明した。
     ・App-Version: tripla-booking-widget/1.0 ヘッダが無いと
       「You don't have permission to access this」で弾かれる。最大の関門。
     ・日付は checkin ではなく checkin_date / checkout_date（YYYY-MM-DD）。
     ・土岐は kids_type=share_bed なので rooms[][children] が必須。
       無いと「Invalid adults or children」。
     ・在庫APIが使うのは UUID ではなく数値の hotel_id（土岐は 4603）。
       UUID は settings/booking_widget を叩くときに使う。
     key/secret は公開JSに直書きの公開クレデンシャル。個人の資格情報ではない。

  7. watch_loop.sh を手元でそのまま走らせると、状態が変われば
     本番リポジトリに commit & push する（git 操作が中に入っている）。
     試すなら使い捨てのコピーで。うっかり本番を汚さないこと。
EOS

line "■ よく使う操作"
cat <<EOS
  1回だけ確認          python3 check_hotel.py
                       python3 check_routeinn.py
  稼働状況             gh run list --repo $REPO --limit 5
  手動起動             gh workflow run "空室監視" --repo $REPO
  ループ停止           gh run cancel <run番号> --repo $REPO
  通知テスト（ルートイン）
                       gh workflow run "通知テスト（ルートイン）" --repo $REPO \\
                         -f checkin=YYYY-MM-DD -f nights=2
                       空きのある日を指定すると LINE が届く。本番の監視ループとは
                       concurrency グループが別なので、走らせたまま試せる。
                       本番ファイルは書き換えないので、戻す作業も要らない。
  通知テスト（東横イン）
                       check_hotel.py の MAX_PRICE を一時的に 30000 にして実行
                       （確認後は必ず戻すこと）
  ループを試す         引き継ぎ事項7を読むこと。本番に push するので
                       使い捨てのコピーを作ってから回す
EOS

line "■ 新しいセッションでの最初の一言（コピペ用）"
cat <<EOS
  ホテルの空室監視（東横イン札幌／ルートイン土岐）をやっています。
  $(pwd) にコードがあります。
  bash handoff.sh を実行して現状を把握してください。
EOS
echo
