# Development

このドキュメントは、Notion AI Meeting Notes Archiver の仕組みや開発・運用の詳細をまとめたものです。通常利用だけならREADMEで十分です。

## 仕組み

1. Notionデスクトップアプリのローカル保存先をスキャンします。

   ```text
   ~/Library/Application Support/Notion/Partitions/notion/File System/*/t
   ```

2. Notion AI Meeting Notes のraw音声データを検出します。

   ```text
   32-bit float little-endian
   mono
   16000 Hz
   WAVヘッダーなし
   ```

3. raw音声をWAVファイルとして復元します。

4. Notionのローカル `notion.db` のスナップショットを読み、`audio` ブロックや `transcription` ブロックから該当ページを推定します。

5. Notion File Upload APIを使って、該当ページへ音声ブロックとしてアップロードします。

6. Notionページ側にraw音声のSHA-256を含むアーカイブマーカーを残します。Notionへの追加後、ローカルmanifest更新前にプロセスが落ちても、次回実行時にページ側マーカーを見つけて重複アップロードを避けます。

7. ローカルSQLite manifestを保持し、同じ録音を再生成・再アップロードしないようにします。

## 設定

標準の設定ファイル:

```text
~/Library/Application Support/Notion AI Meeting Notes Archiver/config.json
```

ローカル開発時は `config.example.json` をコピーして `config.json` を作れます。

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

環境変数は開発用のフォールバックです。通常はKeychainを使ってください。

## LaunchAgent

標準のLaunchAgent:

```text
~/Library/LaunchAgents/com.local.notion-ai-meeting-notes-archiver.plist
```

NotionのローカルFile Systemは `000/t`、`001/t` のように複数作られることがあります。録音rawは `File System/*/t` をすべてスキャンします。

標準引数:

- `--min-size-mb 1`
- `--min-stable-seconds 600`
- `watch --upload --interval 300`

初回セットアップ時は `--ignore-before` にセットアップ時刻を設定します。これにより、過去の録音が初回起動で意図せず大量アップロードされることを防ぎます。再セットアップ時は既存の `--ignore-before` を引き継ぎます。

## 手動コマンド

最近の候補をスキャン:

```bash
python3 notion_ai_meeting_notes_archiver.py --since-days 7 scan
```

1回だけアーカイブしてアップロード:

```bash
python3 notion_ai_meeting_notes_archiver.py archive --upload
```

常駐監視:

```bash
python3 notion_ai_meeting_notes_archiver.py watch --upload --interval 300
```

アップロード済みmanifestレコードのローカルWAV/metadataを削除:

```bash
python3 notion_ai_meeting_notes_archiver.py cleanup-uploaded
```

Notion APIへの疎通確認を省略してdoctorを走らせる:

```bash
python3 notion_ai_meeting_notes_archiver.py doctor --no-api
```

## ログ

```bash
tail -n 100 "$HOME/Library/Logs/Notion AI Meeting Notes Archiver/notion-ai-meeting-notes-archiver.out.log"
tail -n 100 "$HOME/Library/Logs/Notion AI Meeting Notes Archiver/notion-ai-meeting-notes-archiver.err.log"
```

## 安全上の注意

- このツールは、該当ページを特定するためにNotionデスクトップアプリのローカルデータを読みます。
- `delete_after_upload` がtrueの場合、復元したローカルWAVはアップロード成功後に削除されます。
- manifest DBは重複処理を防ぐためにarchive directoryへ残ります。
- transcription blockによるページ推定は保守的です。近い時刻に複数候補がある場合は、推測でアップロードせずスキップします。ただし、transcriptionの終了時刻とrawファイルの最終更新時刻がほぼ一致する場合は強い候補として扱います。
- archive directoryごとに同時実行は1プロセスだけです。LaunchAgent処理中に手動実行すると、手動側はスキップされることがあります。
- PATをplistへ貼った、コミットした、ログに出した、ターミナルに表示した可能性がある場合は、NotionでそのPATをrevokeし、新しいPATを発行してください。

## テスト

```bash
/usr/bin/python3 -W error -m unittest discover -s tests
python3 -m unittest discover -s tests
```

`py_compile` がmacOSのPythonキャッシュ権限で失敗する場合:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/notion-ai-meeting-notes-pycache \
  /usr/bin/python3 -m py_compile notion_ai_meeting_notes_archiver.py tests/test_archiver.py
```
