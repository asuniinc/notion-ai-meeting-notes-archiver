# Notion AI Meeting Notes Archiver

Notion desktop app can keep AI Meeting Notes recordings in its local Electron
storage as raw audio data. This tool detects those local recordings, restores
them as WAV files, matches them to the Notion page that created them, uploads the
audio back to that page, and removes the local WAV after a successful upload.

## Internal Sharing Status

This tool is intended for trusted internal use on each user's own Mac.

- Each user should create their own Notion Personal Access Token (PAT).
- The PAT is stored in macOS Keychain, not in the LaunchAgent plist.
- A PAT acts with the permissions of the user who created it, so private AI
  Meeting Notes pages do not need to be manually shared with a bot connection.
- Do not commit `config.json`, generated LaunchAgent plists, logs, local WAVs, or
  manifest databases.

Notion's PAT docs: https://developers.notion.com/guides/get-started/personal-access-tokens

## What It Does

1. Scans Notion's local app storage:

   ```text
   ~/Library/Application Support/Notion/Partitions/notion/File System/000/t
   ```

2. Detects raw AI Meeting Notes audio:

   ```text
   32-bit float little-endian
   mono
   16000 Hz
   no WAV header
   ```

3. Restores each recording as a `.wav` file.

4. Reads Notion's local `notion.db` snapshot to match old and recent recordings
   to `audio` or `transcription` blocks.

5. Uses the Notion File Upload API to attach the WAV as an audio block.

6. Writes a page-side archive marker containing the raw audio SHA-256. If the app
   crashes after appending to Notion but before updating the local manifest, the
   next run detects the marker and avoids a duplicate upload.

7. Keeps a local SQLite manifest so completed recordings are not regenerated.

## Install

From this directory:

```bash
./scripts/install.sh
```

The installer will:

- copy the app to `~/Library/Application Support/Notion AI Meeting Notes Archiver`
- prompt for the user's Notion PAT and store it in Keychain service
  `notion-ai-meeting-notes-archiver`
- create `~/Library/LaunchAgents/com.local.notion-ai-meeting-notes-archiver.plist`
- start a 60-second watch loop
- run `doctor`

The installer sets `--ignore-before` to the install time so old recordings are
not uploaded accidentally on first launch.

For non-interactive deployment, set `NOTION_PAT` before running the installer or
leave it unset to keep an existing Keychain token.

The installer uses `/usr/bin/python3` by default when available. Set `PYTHON` to
override the runtime path.

## Check Setup

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json doctor
```

Use `--no-api` to skip the Notion API request:

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json doctor --no-api
```

`doctor` prints only status and service names. It never prints the token.

## Manual Commands

Scan recent candidates:

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json --since-days 7 scan
```

Archive and upload once:

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json archive --upload
```

Watch continuously:

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json watch --upload --interval 60
```

Delete local WAV and metadata files for already uploaded manifest records:

```bash
python3 notion_ai_meeting_notes_archiver.py --config config.json cleanup-uploaded
```

## Configuration

Create `config.json` from `config.example.json` for local development:

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

Token lookup order:

1. macOS Keychain generic password named by `notion_token_keychain_service`
2. fallback environment variable named by `notion_token_env`

The environment variable fallback is for development only. Internal installs
should use Keychain.

## Safety Notes

- The tool reads local Notion desktop data, including page/block metadata needed
  to find the matching page.
- The local WAV exists only until upload when `delete_after_upload` is true.
- The manifest database remains in the archive directory for deduplication.
- Matching through transcription blocks is conservative. If two candidate pages
  are too close in time, the recording is skipped instead of guessed.
- One archiver process can run at a time per archive directory. Manual runs will
  skip if the LaunchAgent is already processing.
- If a PAT is ever pasted into a plist, committed, logged, or printed in a
  terminal, revoke it in Notion and create a new one.

## Uninstall

```bash
./scripts/uninstall.sh
```

Remove the installed app directory and Keychain token too:

```bash
./scripts/uninstall.sh --purge
```

The uninstall script does not remove the user's archive unless `--purge` is used.

## Development Checks

```bash
python3 -m py_compile notion_ai_meeting_notes_archiver.py tests/test_archiver.py
python3 -m unittest discover -s tests
plutil -lint launchd/com.local.notion-ai-meeting-notes-archiver.plist.template
```

## Current Limitations

- The recorder detection depends on Notion desktop's internal storage format and
  may need updates if Notion changes it.
- File uploads that complete but fail before the audio block is appended may
  leave an unattached Notion file upload. A later run uploads again, but it will
  not create a duplicate page block unless the page-side marker is absent.
- `--force` intentionally bypasses local and remote duplicate checks.
