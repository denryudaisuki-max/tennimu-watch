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
ホテルの空室とチケットの復活を5分ごとに監視し、条件を満たしたら LINE に通知する仕組み。
GitHub Actions 上で動いているので、Mac は閉じていてよい。
いま監視しているのは2件（それぞれ独立。1つ落ちても残りは動く）:
  ① ホテルルートイン土岐  2026-10-30〜11-01（2泊）空室が出たら（金額条件なし）
  ② テニミュ The Final Stage  「予定枚数終了→受付中」に変わったら
       ・2026-11-15(日)17:30 東京（受付の種類を問わない）
       ・大阪 SkyシアターMBS の土日7公演（当日引換券のみ）
         10/17(土)17:30 10/18(日)12:30/17:30
         10/24(土)12:30/17:30 10/25(日)12:30/17:30
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

line "■ 監視条件① ルートイン（check_routeinn.py より）"
grep -E '^(HOTEL_NAME|HOTEL_CODE|HOTEL_ID|CHECKIN|CHECKOUT|MAX_PRICE) *=' check_routeinn.py | sed 's/^/  /'
echo "  SEARCHES:"; sed -n '/^SEARCHES = \[/,/^\]/p' check_routeinn.py | sed 's/^/    /'

line "■ 監視条件② テニミュ（check_tennimu.py より）"
grep -E '^(EVENT_NAME|EVENT_URL) *=' check_tennimu.py | sed 's/^/  /'
echo "  TARGETS:"; sed -n '/^TARGETS = \[/,/^\]/p' check_tennimu.py | sed 's/^/    /'

line "■ 今の状況（実際に取りにいって確認）"
"$PY" check_routeinn.py 2>&1 | sed 's/^/  /' || echo "  （実行に失敗）"
echo
"$PY" check_tennimu.py 2>&1 | sed 's/^/  /' || echo "  （実行に失敗）"
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
  1. 【廃止 2026-10-15】東横イン札幌の監視はユーザーの指示で削除した。
     10/17〜10/19 の宿で「2泊合計 ¥19,000 未満」を待っていたが、
     直前まで最安 ¥22,400 までしか下がらず条件を満たさないまま終了。
     復活させるなら git 履歴から check_hotel.py と state_hotel.json を戻す。
     そのとき必要になる知識だけ残す:
       ・tRPC の JSON API hotels.availabilities.prices を叩いていた（認証不要）
       ・lowestPrice は「1泊あたり」ではなく滞在合計。実測で確定済み。
         札幌駅北口 10/17〜 1泊 ¥15,700 / 2泊 ¥28,400 / 3泊 ¥41,100
       ・MAX_PRICE も滞在合計で比べる（ユーザー本人に確認済み）

  3. cron は使い物にならないので使っていない。
     実測で */5 に対して実行率7%、間隔の中央値59分、最大208分の空白。
     代わりに1ジョブを5時間30分走らせ、その中の自前タイマーで5分ごとに確認。
     cron は「ループが切れたときの再開トリガー」に格下げしてある。

  4. 設定変更後は、走っているループを止めないと反映されない。
     ループ開始時のコミットをチェックアウトしたまま最大5.5時間走り続けるため。
       gh run cancel <走っているrun> --repo denryudaisuki-max/tennimu-watch
       gh workflow run "空室監視" --repo denryudaisuki-max/tennimu-watch

  5. イープラスは「どこまで取れるか」で話が分かれる。ここを混同しないこと。
     以前「イープラスは自動取得できない」と書いてあったが、それは後者の話。

     ・公開の公演詳細ページ /sf/detail/xxxxxxxxx → 普通に GET できる。
       公演ごとの「受付中／予定枚数終了／受付終了」まで読める。
       2026-08-31 に GitHub Actions の IP からも取得できることを確認済み。
       いま check_tennimu.py がこれで 11/15(日)17:30 の復活を見ている。
     ・申込ページ（席種別の在庫が見える唯一の場所）→ 今も取れない。
       Akamai と飛込防止（/sf/dvcjudge の DVC_UNIQUE_ID）に阻まれる。
     ・ファンクラブ枠（TSC サポーターズクラブ当日引換券）→ 監視できない。
       w1.onlineticket.jp/sf/tkt18/detail/0473460021?P6=364 は
       会員番号6桁＋パスワードのログイン画面しか返さない（実測6KB）。
       この受付は公開ページに1件も載らない（2026-09-26 確認）。
         公開ページの受付: 先着一般発売 / 抽選イープラス先行 の2種類だけ
         TSCページの受付:  先着テニミュ サポーターズクラブ当日引換券 だけ
       在庫は一般発売と完全に別枠。TSCが×でも一般発売は受付中のことがある。
       ユーザーのパスワードを預かってログインさせることはしない。

     つまり「その公演が買える状態になったか」は分かるが、
     「どの席種が何枚残っているか」「ファンクラブ枠の在庫」は分からない。
     前者で足りる用途なら作れる。後者が要るなら手動で見るしかない。

     なお ticket-status__item--accepting というクラス名は「予定枚数終了」にも
     付いている。クラスで判定してはいけない。必ずテキストで見ること。

  6. ルートイン（tripla API）で詰まったら、まずこの4点を疑う。全部実測で判明した。
     ・App-Version: tripla-booking-widget/1.0 ヘッダが無いと
       「You don't have permission to access this」で弾かれる。最大の関門。
     ・日付は checkin ではなく checkin_date / checkout_date（YYYY-MM-DD）。
     ・土岐は kids_type=share_bed なので rooms[][children] が必須。
       無いと「Invalid adults or children」。
     ・在庫APIが使うのは UUID ではなく数値の hotel_id（土岐は 4603）。
       UUID は settings/booking_widget を叩くときに使う。
     key/secret は公開JSに直書きの公開クレデンシャル。個人の資格情報ではない。

  7. テニミュ監視は2週間おきに1〜2時間ほど取得に失敗する。故障ではない。
     実測: 09/02 と 09/16 の 05:15〜07:05 JST に19〜22回連続で失敗し、
     どちらも自動復帰した。イープラス側の定期メンテと見られる。
     3回目でLINE警告が飛ぶので、月に1〜2通この警告が届く。
     サイト自体が止まっている時間帯なので取りこぼしの実害はない。

  8. 【2026-10-15 修正済】終わった公演を TARGETS に残すと監視が丸ごと止まる
     という不具合があった。同じ轍を踏まないこと。
     10/2 公演の終了後、ページから消えた対象を「異常」と判定し、
     その時点で return していたため、後ろにある通知処理に到達しなくなった。
     結果、11日間・3300回連続で失敗し、11/15 が復活しても通知されない状態だった。
     警告LINEは3回目の1通だけなので、気づくのが遅れた。
     現在は (a) 公演日を過ぎた対象は自動で見送る
            (b) 1件見つからなくても他の対象の通知は続行する
     に直してある。対象を増やすときは、この2点を壊さないこと。

  9. 【2026-10-15 ハマった】同じ公演がページ上で複数ブロックに分かれて載る。
     受付の種類ごとに別ブロックになるため、こうなっている:
       2026/10/17 17:30 ブロックA → 先着一般発売 / 抽選イープラス先行
       2026/10/17 17:30 ブロックB → キャンセル待ち券 / 先着当日引換券受付
     日付ブロックの数は公演数ではない。まとめないと、先に見つかった
     ブロックの受付しか見えず、当日引換券を永久に取りこぼす。
     parse_performances の最後で同じ日時をマージしてある。

 10. 「当日引換券」は2種類あって別物。名前が似ているので混同しないこと。
       ・先着当日引換券受付               → 公開ページに載る。監視できる
       ・先着テニミュ サポーターズクラブ当日引換券 → TSC会員ページのみ。監視できない
     いま大阪の土日で見ているのは前者。

 11. 受付ごとに判定している。TARGETS に uketsuke を書くと、名前にその文字を
     含む受付だけを見る（未指定なら全受付）。
     指定せず全受付を混ぜると「一般発売が受付中」になっただけで
     「当日引換券が復活した」と誤通知する。

 12. watch_loop.sh を手元でそのまま走らせると、状態が変われば
     本番リポジトリに commit & push する（git 操作が中に入っている）。
     試すなら使い捨てのコピーで。うっかり本番を汚さないこと。
EOS

line "■ よく使う操作"
cat <<EOS
  1回だけ確認          python3 check_routeinn.py
                       python3 check_tennimu.py
  Actions上で1回試す    gh workflow run "1回だけ実行（手動）" --repo $REPO \\
                         -f script=check_tennimu.py -f notify=false
                       疎通確認用。「手元では取れるが GitHub からは弾かれる」を
                       見つけられる。ループは止まらない。
  稼働状況             gh run list --repo $REPO --limit 5
  手動起動             gh workflow run "空室監視" --repo $REPO
  ループ停止           gh run cancel <run番号> --repo $REPO
  通知テスト（ルートイン）
                       gh workflow run "通知テスト（ルートイン）" --repo $REPO \\
                         -f checkin=YYYY-MM-DD -f nights=2
                       空きのある日を指定すると LINE が届く。本番の監視ループとは
                       concurrency グループが別なので、走らせたまま試せる。
                       本番ファイルは書き換えないので、戻す作業も要らない。
  ループを試す         引き継ぎ事項12を読むこと。本番に push するので
                       使い捨てのコピーを作ってから回す
EOS

line "■ 新しいセッションでの最初の一言（コピペ用）"
cat <<EOS
  空室とチケットの監視（ルートイン土岐／テニミュ）をやっています。
  $(pwd) にコードがあります。
  bash handoff.sh を実行して現状を把握してください。
EOS
echo
