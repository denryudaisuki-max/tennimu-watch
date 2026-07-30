# テニミュ チケット復活監視

イープラスの [公演詳細ページ](https://eplus.jp/sf/detail/0473460001) を5分ごとにチェックし、
**2026/8/30(日) 17:30開演**（TACHIKAWA STAGE GARDEN）の受付が復活したら LINE に通知します。

GitHub Actions 上で動くので、PC は閉じていて構いません。

## 判定について

このページの受付ステータスは、席種（全席指定 / サイドシート / 見切席）の
**どれか1つでも空きがあれば「受付中」** になります。保存済みの申込画面と突き合わせて確認済みです。

| 公演 | 申込画面の席種 | 詳細ページの表示 |
|---|---|---|
| 8/30 12:00 | × × △ | 受付中 |
| **8/30 17:30** | **× × ×** | **予定枚数終了** |

「予定枚数終了」→「受付中」に変わった瞬間に通知します。
同じ状態が続く間は再通知しません（連投しない）。

## セットアップ

### 1. LINE のトークンを用意する

1. [LINE Official Account Manager](https://manager.line.biz/) で公式アカウントを作成
2. 設定 → Messaging API → **「Messaging APIを利用する」** を有効化
3. [LINE Developers コンソール](https://developers.line.biz/console/) → 該当チャネル →
   **「Messaging API設定」タブ** → 最下部の **チャネルアクセストークン（長期）** を発行
4. スマホの LINE で、そのアカウントを **友だち追加**（これをしないと届きません）

> 2024年9月4日以降、Developers コンソールから Messaging API チャネルを直接作ることはできません。
> 必ず Official Account Manager 側から有効化してください。

### 2. GitHub にリポジトリを作る

**パブリックリポジトリにしてください。** 5分間隔だと月およそ8,600分になり、
プライベートの無料枠（月2,000分）では1週間ほどで尽きます。パブリックなら Actions は無制限・無料です。
トークンはコードではなく Secrets に入れるので、公開しても漏れません。

```bash
cd ~/Desktop/tennimu-watch
git remote add origin https://github.com/<ユーザー名>/tennimu-watch.git
git branch -M main
git push -u origin main
```

### 3. トークンを Secrets に登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret**

- Name: `LINE_CHANNEL_ACCESS_TOKEN`
- Secret: 発行したトークン

### 4. 動作確認

**Actions タブ → 「チケット監視」→ Run workflow** で手動実行。
ログに現在のステータスが出れば動いています。

通知そのものを試したいときは、`check.py` の `TARGET_KEY` を一時的に
`20260830-開演-1200-13`（受付中の公演）に変えて実行すると LINE が飛びます。確認後は戻してください。

## 注意点

- **スケジュールは遅れます。** GitHub Actions の cron は最短5分ですが、混雑時は数分〜十数分ずれます。
  瞬間的に出て消える戻り席は取りこぼす可能性があります。確実性を求める仕組みではありません。
- **リポジトリが60日間無活動だとスケジュールが自動停止します。** 今回は約1ヶ月なので影響しませんが、
  長期運用するなら何かコミットしてください（状態変化時は自動でコミットされます）。
- 無料枠はコミュニケーションプランで**月200通**。通知は復活時のみなので十分収まります。
- 対象公演を読み取れない状態が3回続くと、警告を LINE に送り、ジョブも失敗させます
  （GitHub から障害メールが届きます）。

## ファイル

| ファイル | 役割 |
|---|---|
| `check.py` | 取得・判定・LINE通知 |
| `.github/workflows/watch.yml` | 5分ごとの実行 |
| `state.json` | 前回のステータス（連投防止用・自動更新） |

## 監視対象を変える

`check.py` の `TARGET_KEY` を書き換えます。キーは詳細ページの
`<article class="block-ticket-article {キー} local-...">` から取れます。

```
20260830-開演-1730-13
└ 日付   └開演 └時刻 └都道府県コード(13=東京)
```

## 終わったら

不要になったらリポジトリを削除するか、Actions タブでワークフローを Disable してください。
