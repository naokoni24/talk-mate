# talk-mate

OpenAI Realtime API (`gpt-realtime-mini`) をブラウザから WebRTC で直接利用する、日本語の音声相談アプリです。音声はこのサーバーを通らず、サーバーは短命キー発行・利用額管理・履歴保存だけを担います。

## 起動

Python 3 の標準ライブラリだけで動きます。

```bash
export OPENAI_API_KEY='sk-...'
python3 talk_mate.py
```

`http://localhost:8788` を開きます。未設定でも画面は起動し、通話開始時に理由を表示します。

## 環境変数

| 変数 | 必須 | 説明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | はい | Realtime API 用 |
| `GEMINI_API_KEY` | 任意 | 終話後に Gemini Flash Lite で要約 |
| `PORT` | 任意 | 待受ポート（既定: 8788） |
| `BASIC_USER` / `BASIC_PASS` | 任意 | 両方でログインを有効化 |
| `COOKIE_SECRET` | 任意 | 署名 Cookie 用。未指定時は開発用の一時値 |
| `MAX_SESSION_SECONDS` | 任意 | 1 通話の上限秒数（既定: 600） |
| `DAILY_BUDGET_USD` | 任意 | 1 日の実測利用額上限（既定: 1.0） |
| `USER_TRANSCRIPT` | 任意 | `1` でユーザー音声の文字起こしを有効化（既定: 無効） |

## Render へのデプロイ

1. GitHub にこのリポジトリを push し、Render で **Web Service** を作成します。
2. Runtime は Python 3、Build Command は空欄、Start Command は `python3 talk_mate.py` を設定します。
3. 環境変数として最低 `OPENAI_API_KEY` を登録します。公開運用では `BASIC_USER`、`BASIC_PASS`、ランダムな `COOKIE_SECRET` も登録してください。
4. Render が渡す `PORT` をそのまま利用します。SQLite は Render のエフェメラルディスク上では再デプロイ時に消えるため、履歴を保持したい場合は Persistent Disk を `/opt/render/project/src` に接続します。

## コストの目安

モデルは固定で `gpt-realtime-mini`（音声入力 $10 / 1M token、出力 $20 / 1M token）。概算として、ユーザー音声 1 分を約600 token、AI 音声 1 分を約1,200 token とすると、両者が各1分ずつ話す会話は約 **$0.03** です。実際の利用額は Realtime の `response.done.usage` を積算して記録します。応答は20〜30秒を目安に短く指示し、10分の強制終了・90秒無音終了・日次上限を設けています。
