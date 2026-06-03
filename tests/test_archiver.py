import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

import notion_ai_meeting_notes_archiver as arch


TZ = dt.datetime.now().astimezone().tzinfo


def local_dt(hour: int, minute: int, second: int = 0) -> dt.datetime:
    return dt.datetime(2026, 6, 3, hour, minute, second, tzinfo=TZ)


class ArchiverTests(unittest.TestCase):
    def test_transcription_match_skips_ambiguous_candidates(self) -> None:
        candidate = arch.RawCandidate(
            raw_path=Path("/tmp/raw"),
            size=60 * arch.RAW_SAMPLE_RATE * arch.RAW_BYTES_PER_SAMPLE,
            mtime=local_dt(12, 10),
            duration_seconds=600,
            expected_filename="Audio Recording 2026-06-03 at 12.10.00 PM.wav",
        )
        contexts = [
            arch.TranscriptionContext(
                block_id="block-1",
                title=None,
                started_at=local_dt(12, 0),
                ended_at=None,
                page_id="page-1",
            ),
            arch.TranscriptionContext(
                block_id="block-2",
                title=None,
                started_at=local_dt(12, 0, 5),
                ended_at=None,
                page_id="page-2",
            ),
        ]

        arch.match_transcription_contexts([candidate], contexts, Path("notion.db"))

        self.assertIsNone(candidate.event)

    def test_transcription_match_accepts_clear_best_candidate(self) -> None:
        candidate = arch.RawCandidate(
            raw_path=Path("/tmp/raw"),
            size=60 * arch.RAW_SAMPLE_RATE * arch.RAW_BYTES_PER_SAMPLE,
            mtime=local_dt(12, 10),
            duration_seconds=600,
            expected_filename="Audio Recording 2026-06-03 at 12.10.00 PM.wav",
        )
        contexts = [
            arch.TranscriptionContext(
                block_id="block-1",
                title=None,
                started_at=local_dt(12, 0),
                ended_at=None,
                page_id="page-1",
            ),
            arch.TranscriptionContext(
                block_id="block-2",
                title=None,
                started_at=local_dt(12, 0, 30),
                ended_at=None,
                page_id="page-2",
            ),
        ]

        arch.match_transcription_contexts([candidate], contexts, Path("notion.db"))

        self.assertIsNotNone(candidate.event)
        self.assertEqual(candidate.page_id, "page-1")

    def test_find_archived_audio_block_by_marker(self) -> None:
        client = arch.NotionClient("test-token")
        raw_sha = "a" * 64
        marker = arch.archive_marker(raw_sha)

        def fake_children(_: str) -> list[dict[str, object]]:
            return [
                {
                    "id": "paragraph-id",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"plain_text": f"Archived audio\n{marker}"}]
                    },
                },
                {"id": "audio-id", "type": "audio", "audio": {}},
            ]

        client.list_block_children = fake_children  # type: ignore[method-assign]

        found = client.find_archived_audio_block("page-id", raw_sha, "recording.wav")

        self.assertEqual(found, {"marker_block_id": "paragraph-id", "uploaded_block_id": "audio-id"})

    def test_delete_local_archive_files_counts_only_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "meeting.wav"
            metadata = root / "meeting.metadata.json"
            wav.write_bytes(b"data")

            deleted = arch.delete_local_archive_files(wav, metadata, root)

            self.assertEqual(deleted, 1)
            self.assertFalse(wav.exists())

    def test_local_notion_db_opens_path_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "Notion DB With Spaces.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE block (
                  id TEXT,
                  properties TEXT,
                  created_time INTEGER,
                  last_edited_time INTEGER,
                  alive INTEGER,
                  version INTEGER,
                  meta_last_access_timestamp INTEGER,
                  type TEXT,
                  parent_table TEXT,
                  parent_id TEXT
                )
                """
            )
            conn.commit()
            conn.close()

            db = arch.LocalNotionDb(db_path)
            try:
                self.assertEqual(db.audio_recording_events(), [])
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
