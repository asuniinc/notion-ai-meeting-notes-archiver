# Notion AI Meeting Notes Archiver

Notion AI Meeting Notes のローカル録音データを復元し、該当するNotionページへ音声ファイルとしてアップロードするための社内向けツールです。

## 対応環境

このツールは **macOS専用** です。Windowsでは動きません。

理由:

- NotionデスクトップアプリのmacOSローカル保存先を前提にしています。
- 認証トークンの保存にmacOS Keychainを使います。
- 常駐実行にmacOSのLaunchAgent/launchdを使います。
- インストール/アンインストールスクリプトはzshとmacOS標準コマンドを前提にしています。

## 社内利用時の前提

- 各ユーザーが自分のNotion Personal Access Token (PAT) を作成します。
- PATはLaunchAgent plistではなく、macOS Keychainに保存します。
- PATは作成したユーザー本人の権限で動くため、Notion AI Meeting Notes がプライベートページに作られても、都度bot connectionへ共有する必要がありません。
- `config.json`、生成されたLaunchAgent plist、ログ、ローカルWAV、manifest DBはコミットしないでください。

Notion PAT docs: https://developers.notion.com/guides/get-started/personal-access-tokens

## 何をするツールか

1. Notionデスクトップアプリのローカル保存先をスキャンします。

   ```text
   ~/Library/Application Support/Notion/Partitions/notion/File System/000/t
   ```

2. Notion AI Meeting Notes のraw音声データを検出します。

   ```text
   32-bit float little-endian
   mono
   16000 Hz
   WAVヘッダーなし
   ```

3. raw音声を `.wav` ファイルとして復元します。

4. Notionのローカル `notion.db` のスナップショットを読み、`audio` ブロックや `transcription` ブロックから該当ページを推定します。

5. Notion File Upload APIを使って、該当ページへ音声ブロックとしてアップロードします。

6. Notionページ側にraw音声のSHA-256を含むアーカイブマーカーを残します。Notionへの追加後、ローカルmanifest更新前にプロセスが落ちても、次回実行時にページ側マーカーを見つけて重複アップロードを避けます。

7. ローカルSQLite manifestを保持し、同じ録音を再生成・再アップロードしないようにします。

## インストール

このリポジトリのディレクトリで実行します。

```bash
./scripts/install.sh
```

インストーラが行うこと:

- アプリを `~/Library/Application Support/Notion AI Meeting Notes Archiver` にコピーします。
- Notion PATの入力を求め、Keychain service `notion-ai-meeting-notes-archiver` に保存します。
- `~/Library/LaunchAgents/com.local.notion-ai-meeting-notes-archiver.plist` を作成します。
- 60秒間隔の常駐監視を開始します。
- `doctor` を実行してセットアップ状態を確認します。

初回インストール時は `--ignore-before` にインストール時刻を設定します。これにより、過去の録音が初回起動で意図せず大量アップロードされることを防ぎます。再インストール時は既存の `--ignore-before` を引き継ぎます。明示的に変えたい場合は `IGNORE_BEFORE` を指定してください。

LaunchAgentは `--min-size-mb 1` で動きます。短いテスト録音も検出するためです。

非対話実行では、`NOTION_PAT` を設定してからインストーラを実行できます。未設定の場合は既存のKeychain tokenをそのまま使います。

```bash
NOTION_PAT="ntn_..." ./scripts/install.sh
```

Pythonは `/usr/bin/python3` を優先して使います。別のPythonを使いたい場合は `PYTHON` で指定してください。

```bash
PYTHON=/path/to/python3 ./scripts/install.sh
```

## セットアップ確認

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json doctor
```

Notion APIへの疎通確認を省略する場合:

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json doctor --no-api
```

`doctor` は状態とKeychain service名だけを表示します。トークン値は表示しません。

## 手動コマンド

最近の候補をスキャン:

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json --since-days 7 scan
```

1回だけアーカイブしてアップロード:

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json archive --upload
```

常駐監視:

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json watch --upload --interval 60
```

アップロード済みmanifestレコードのローカルWAV/metadataを削除:

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json cleanup-uploaded
```

## 設定

ローカル開発時は `config.example.json` をコピーして `config.json` を作ります。

```json
{
  "notion_root": "~/Library/Application Support/Notion/Partitions/notion",
  "notion_db": "~/Library/Application Support/Notion/notion.db",
  "archive_dir": "~/Library/Application Support/Notion AI Meeting Notes Archiver/Archive",
  "notion_token_env": "NOTION_API_KEY",
  "notion_token_keychain_service": "notion-ai-meeting-notes-archiver",
  "notion_version": "2026-03-11",
  "delete_after_upload": true,
  "fallback_page_id": null
}
```

トークンの探索順:

1. `notion_token_keychain_service` で指定されたmacOS Keychain generic password
2. `notion_token_env` で指定された環境変数

環境変数は開発用のフォールバックです。社内利用ではKeychainを使ってください。

## 安全上の注意

- このツールは、該当ページを特定するためにNotionデスクトップアプリのローカルデータを読みます。
- `delete_after_upload` がtrueの場合、復元したローカルWAVはアップロード成功後に削除されます。
- manifest DBは重複処理を防ぐためにarchive directoryへ残ります。
- transcription blockによるページ推定は保守的です。近い時刻に複数候補がある場合は、推測でアップロードせずスキップします。
- archive directoryごとに同時実行は1プロセスだけです。LaunchAgent処理中に手動実行すると、手動側はスキップされることがあります。
- PATをplistへ貼った、コミットした、ログに出した、ターミナルに表示した可能性がある場合は、NotionでそのPATをrevokeし、新しいPATを発行してください。

## アンインストール

```bash
./scripts/uninstall.sh
```

インストール済みアプリディレクトリとKeychain tokenも削除する場合:

```bash
./scripts/uninstall.sh --purge
```

`--purge` を付けない場合、archiveは削除しません。

## 開発時の確認

```bash
python3 -m py_compile notion_ai_meeting_notes_archiver.py tests/test_archiver.py
python3 -m unittest discover -s tests
plutil -lint launchd/com.local.notion-ai-meeting-notes-archiver.plist.template
```

## 現在の制限

- macOS専用です。Windows/Linuxには対応していません。
- Notionデスクトップアプリの内部保存形式に依存しています。Notion側の実装が変わると更新が必要になる可能性があります。
- File Upload自体は完了したが、その後の音声ブロック追加前に失敗した場合、Notion側に未紐付けのfile uploadが残る可能性があります。次回実行で再アップロードされますが、ページ側マーカーがある限り重複ブロックは作りません。
- `--force` はローカル/リモートの重複チェックを意図的に無視します。
