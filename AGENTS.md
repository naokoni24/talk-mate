# プロジェクト共通ルール

## talk-mate

作業前後に必ず以下を実行すること。

**作業前:**
- `/Users/nao/Documents/Obsidian Vault/talk-mate/talk-mate.md` を読み込む。初回で存在しない場合は、この依頼内容をもとに仕様ノートとして新規作成する。

**作業後:**
- 同ノートを実装内容に合わせて更新する。
- リポジトリへ同期: `cp "/Users/nao/Documents/Obsidian Vault/talk-mate/talk-mate.md" /Users/nao/Desktop/projects/talk-mate/talk-mate.md`

**git管理:**
- `/Users/nao/Desktop/projects/talk-mate` 直下で初回のみ `git init`。
- 作業後は必ずコミットする。リモート `origin` (`https://github.com/naokoni24/talk-mate`) が設定済みなら `main` へ push、未設定ならコミットのみ行い push はスキップして報告する。
