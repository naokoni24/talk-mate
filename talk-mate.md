# talk-mate 仕様・実装ノート

最終更新: 2026-07-12

## 概要

`gpt-realtime-mini` を使う、日本語のブラウザ音声相談Webアプリ。Python 3標準ライブラリだけで構成し、`python3 talk_mate.py` で起動する。音声ストリームはブラウザと OpenAI Realtime API の WebRTC 接続で直結し、Python サーバーは中継しない。

リポジトリ: https://github.com/naokoni24/talk-mate.git （`main`）

## 実装済み

- 単一ファイル `talk_mate.py`（`ThreadingHTTPServer`、既定ポート 8788）
- HTML/CSS/JavaScript をPythonから返すスマホ優先の一画面UI
- 4ペルソナ（占い・雑談／健康の一般相談／愚痴聞き／恋愛相談）をコード内dictで定義
  - 短い日本語応答、危機時のいのちの電話案内を全ペルソナのサーバー注入 instructions に含める
  - 健康相談は診断・薬の指示を禁止し、緊急症状には119・受診を案内
- `POST /api/realtime/secret` がOpenAIの `client_secrets` を呼び、ペルソナ設定済み短命キーだけをブラウザに返す
- ブラウザが短命キーで `/v1/realtime/calls` にSDPを送るWebRTC接続
- opening を接続後に音声で開始し、`response.output_audio_transcript.delta` からAI発話字幕を表示
- SQLite (`talk_mate.db`) に日時、ペルソナ、通話時間、入出力トークン、実測概算費用、字幕、要約を保存
- `response.done.usage` の利用量を集計し、終話時に `POST /api/sessions` で保存
- 日次実測コストと `DAILY_BUDGET_USD` を照合し、上限時は新規短命キー発行を拒否
- `MAX_SESSION_SECONDS`（既定600秒）の自動終了、残り60秒警告、90秒無音の自動終了
- `USER_TRANSCRIPT=1` の場合だけユーザー音声転写をRealtimeセッションに有効化（既定OFF）
- Geminiキーを設定した場合のみ `gemini-2.5-flash-lite` による終話後要約
- 任意の `BASIC_USER` / `BASIC_PASS` ログイン。HMAC SHA-256署名Cookie、有効7日
- 履歴画面、APIキー未設定時の安全な開始ブロック、READMEのRenderデプロイ手順

## API

- `GET /`, `GET/POST /login`, `GET /logout`
- `GET /api/status`, `GET /api/personas`, `GET /api/sessions`
- `POST /api/realtime/secret`, `POST /api/sessions`, `POST /api/summarize`

## 料金目安とコスト対策

`gpt-realtime-mini` の音声入力は $10/1M token、出力は $20/1M token。ユーザー音声1分を600 token、AI音声1分を1,200 tokenとする目安では、双方が各1分話す会話は約$0.03。応答を20〜30秒程度に制限し、上限時間、無音終了、日次上限、入力転写の既定無効化でコストを抑える。保存費用はRealtimeイベントの実際のusageから計算する。

## 運用上の注意

- `OPENAI_API_KEY` はサーバー環境変数にのみ置く。ブラウザには短命キーだけを返す。
- RenderのエフェメラルディスクではSQLite履歴はデプロイで失われる。残す場合はPersistent Diskを接続する。
- 本番公開ではBasic認証とランダムな `COOKIE_SECRET` を設定する。
