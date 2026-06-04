# Notion AI Meeting Notes Archiver

Notion AI Meeting Notes の録音を、該当するNotionページへ音声ファイルとして自動アップロードするmacOS専用ツールです。

## できること

- Notionデスクトップアプリに一時保存された録音を見つけます。
- 録音をWAVファイルとして復元します。
- 対応するNotionページを自動で推定します。
- Notionページへ音声ブロックとしてアップロードします。
- 同じ録音を繰り返しアップロードしないようにします。

## 対応環境

このツールは **macOS専用** です。Windowsでは動きません。

使っているmacOS機能:

- Notionデスクトップアプリのローカル保存先
- macOS Keychain
- LaunchAgent

## はじめに

### 1. NotionのPATを用意する

NotionのPersonal Access Token (PAT) を作成します。

Notion PAT docs: https://developers.notion.com/guides/get-started/personal-access-tokens

作成したPATはあとでセットアップ中に貼り付けます。PATはmacOS Keychainに保存され、設定ファイルやLaunchAgentには書き込まれません。

### 2. セットアップする

一番簡単な方法は、`setup.command` をダブルクリックすることです。

ターミナルから実行する場合:

```bash
python3 notion_ai_meeting_notes_archiver.py setup
```

セットアップで行うこと:

- macOS上で必要なファイルを配置します。
- Notion PATをKeychainへ保存します。
- 5分おきに動く常駐監視を設定します。
- Notion APIとローカル環境を確認します。
- 任意で、指定したNotionページへテスト書き込みします。

テスト書き込みも同時に行う場合:

```bash
python3 notion_ai_meeting_notes_archiver.py setup --test-page-url "https://www.notion.so/..."
```

## 状態確認

動いているか確認するには:

```bash
python3 notion_ai_meeting_notes_archiver.py status
```

見るポイント:

- `常駐: 動作中`
- `監視間隔: 300秒`
- `録音安定待ち: 600秒`
- `最新エラー: なし`

詳しく確認するには:

```bash
python3 notion_ai_meeting_notes_archiver.py doctor
```

指定ページへ実際に書き込みできるか確認するには:

```bash
python3 notion_ai_meeting_notes_archiver.py doctor --test-page-url "https://www.notion.so/..."
```

## 使い方

セットアップ後は、基本的に何もしなくて大丈夫です。Notion AI Meeting Notesで録音すると、録音終了後しばらくしてから該当ページへ音声ファイルが追加されます。

現在の標準設定:

- 5分おきに確認します。
- 録音中のファイルを誤ってアップロードしないよう、最終更新から600秒待ちます。
- アップロード成功後、復元したローカルWAVは削除します。

## 更新

リポジトリを更新してから、もう一度セットアップします。

```bash
git pull
python3 notion_ai_meeting_notes_archiver.py setup
```

既存のKeychain tokenと初回起動時刻は引き継がれます。

## 困ったとき

### 動いているか分からない

```bash
python3 notion_ai_meeting_notes_archiver.py status
```

`最新エラー` に何か出ている場合は、その内容を共有してください。

### Notion tokenが未設定と表示される

もう一度セットアップしてください。

```bash
python3 notion_ai_meeting_notes_archiver.py setup
```

### 録音したのにアップロードされない

- 録音終了直後は600秒待ちます。
- NotionデスクトップアプリのローカルDB反映に数分かかることがあります。
- `status` で `最新エラー` を確認してください。
- `doctor --test-page-url` で、Notionページへ書き込めるか確認してください。

### 常駐を再起動したい

```bash
python3 notion_ai_meeting_notes_archiver.py setup
```

## アンインストール

常駐を止めるだけなら:

```bash
./scripts/uninstall.sh
```

アプリ本体、archive、manifest、Keychain tokenも削除するなら:

```bash
./scripts/uninstall.sh --purge
```

`--purge` を付けない場合、archive、manifest、Keychain tokenは削除しません。

## 詳細情報

開発者向けの仕組み、設定ファイル、手動コマンド、テスト方法は [DEVELOPMENT.md](DEVELOPMENT.md) を見てください。
