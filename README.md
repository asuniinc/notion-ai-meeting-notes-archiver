# Notion AI Meeting Notes Archiver

Notion AI Meeting Notes のローカル録音データを復元し、該当するNotionページへ音声ファイルとしてアップロードするためのツールです。

## 対応環境

このツールは **macOS専用** です。Windowsでは動きません。

理由:

- NotionデスクトップアプリのmacOSローカル保存先を前提にしています。
- 認証トークンの保存にmacOS Keychainを使います。
- 常駐実行にmacOSのLaunchAgent/launchdを使います。
- インストール/アンインストールスクリプトはzshとmacOS標準コマンドを前提にしています。

## 利用時の前提

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

LaunchAgentは `--min-size-mb 1` で動きます。短いテスト録音も検出するためです。録音中のrawファイルを途中でアップロードしないよう、最終更新から600秒以上たって安定したファイルだけを処理します。明示的に変えたい場合は `MIN_STABLE_SECONDS` を指定してください。

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

指定したNotionページへ実際に書き込みできるか確認する場合:

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json doctor --test-page-id <NotionページIDまたはURL>
```

このチェックは小さなテスト用paragraphを追加し、すぐarchiveします。ページへのappend権限まで確認したいときに使います。

## 更新方法

このリポジトリを更新して、インストール先とLaunchAgentへ反映します。

```bash
cd /path/to/notion-ai-meeting-notes-archiver
git pull
./scripts/install.sh
```

`install.sh` は既存のKeychain tokenと `--ignore-before` を引き継ぎます。更新後は次を確認してください。

```bash
python3 notion_ai_meeting_notes_archiver.py --config "$HOME/Library/Application Support/Notion AI Meeting Notes Archiver/config.json" doctor
```

必要に応じて、テスト用ページへの書き込み確認も行います。

```bash
python3 notion_ai_meeting_notes_archiver.py \
  --config "$HOME/Library/Application Support/Notion AI Meeting Notes Archiver/config.json" \
  doctor \
  --test-page-id <NotionページIDまたはURL>
```

## 再起動後確認

Mac再起動後は、LaunchAgentが自動起動しているか確認します。

```bash
launchctl print "gui/$(id -u)/com.local.notion-ai-meeting-notes-archiver"
```

見るポイント:

- `state = running`
- `program = /usr/bin/python3`
- `--min-size-mb 1` が引数に含まれている
- `--min-stable-seconds 600` が引数に含まれている
- stderrログが空、または新しいTracebackがない

ログ確認:

```bash
tail -n 100 "$HOME/Library/Logs/Notion AI Meeting Notes Archiver/notion-ai-meeting-notes-archiver.out.log"
tail -n 100 "$HOME/Library/Logs/Notion AI Meeting Notes Archiver/notion-ai-meeting-notes-archiver.err.log"
```

再起動後に短いテスト録音を作り、該当Notionページに音声が追加されることも確認してください。

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

環境変数は開発用のフォールバックです。通常はKeychainを使ってください。

## 安全上の注意

- このツールは、該当ページを特定するためにNotionデスクトップアプリのローカルデータを読みます。
- `delete_after_upload` がtrueの場合、復元したローカルWAVはアップロード成功後に削除されます。
- manifest DBは重複処理を防ぐためにarchive directoryへ残ります。
- transcription blockによるページ推定は保守的です。近い時刻に複数候補がある場合は、推測でアップロードせずスキップします。
- archive directoryごとに同時実行は1プロセスだけです。LaunchAgent処理中に手動実行すると、手動側はスキップされることがあります。
- PATをplistへ貼った、コミットした、ログに出した、ターミナルに表示した可能性がある場合は、NotionでそのPATをrevokeし、新しいPATを発行してください。

## トラブルシュート

### `doctor` でNotion tokenが見つからない

KeychainにPATが保存されていません。もう一度インストーラを実行してPATを入力してください。

```bash
./scripts/install.sh
```

非対話実行の場合:

```bash
NOTION_PAT="ntn_..." ./scripts/install.sh
```

### `doctor --test-page-id` が失敗する

指定したページID/URLが正しいか、PATを作ったユーザーがそのページに書き込めるかを確認してください。PATが無効な場合はNotionで再発行し、Keychainへ保存し直してください。

### LaunchAgentが動いていない

plistが存在するか確認します。

```bash
ls "$HOME/Library/LaunchAgents/com.local.notion-ai-meeting-notes-archiver.plist"
```

再インストールします。

```bash
./scripts/install.sh
```

### ログにTracebackが出る

stderrログを確認します。

```bash
tail -n 200 "$HOME/Library/Logs/Notion AI Meeting Notes Archiver/notion-ai-meeting-notes-archiver.err.log"
```

更新後のバグ修正で解決している可能性があるため、まず `git pull && ./scripts/install.sh` を試してください。

### 録音したのにアップロードされない

- 録音が10秒未満だと対象外です。
- LaunchAgentの引数に `--min-size-mb 1` が入っているか確認してください。
- 録音終了直後のファイルは未完成扱いになり、デフォルトで最終更新から600秒待ってから処理されます。手動検証で早めたい場合は `--min-stable-seconds` を小さくしてください。
- NotionのローカルDB反映に時間がかかることがあります。数分待ってログを確認してください。
- `doctor --test-page-id` でページへの書き込み権限を確認してください。

### PC負荷が高い

通常時はCPUほぼ0%、メモリは数十MB程度です。CPUが継続的に高い場合はstderrログに例外が出ていないか確認し、最新版へ更新してください。

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
