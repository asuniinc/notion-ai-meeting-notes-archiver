from __future__ import annotations

import datetime as dt
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import notion_ai_meeting_notes_archiver as arch


TZ = dt.datetime.now().astimezone().tzinfo


def local_dt(hour: int, minute: int, second: int = 0) -> dt.datetime:
    return dt.datetime(2026, 6, 3, hour, minute, second, tzinfo=TZ)


class FakeNotionClient(arch.NotionClient):
    def __init__(self) -> None:
        super().__init__("test-token")
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def request_json(
        self,
        method: str,
        path_or_url: str,
        body: dict[str, object] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, object]:
        self.calls.append((method, path_or_url, body))
        if method == "PATCH" and path_or_url.endswith("/children"):
            return {"results": [{"id": "test-block-id"}]}
        return {"id": "test-block-id"}


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

    def test_transcription_match_accepts_exact_end_over_nearby_candidate(self) -> None:
        candidate = arch.RawCandidate(
            raw_path=Path("/tmp/raw"),
            size=60 * arch.RAW_SAMPLE_RATE * arch.RAW_BYTES_PER_SAMPLE,
            mtime=local_dt(10, 26, 8),
            duration_seconds=1486.8,
            expected_filename="Audio Recording 2026-06-04 at 10.26.08 AM.wav",
        )
        contexts = [
            arch.TranscriptionContext(
                block_id="block-1",
                title=None,
                started_at=local_dt(10, 1, 13),
                ended_at=local_dt(10, 26, 8),
                page_id="page-1",
            ),
            arch.TranscriptionContext(
                block_id="block-2",
                title=None,
                started_at=local_dt(10, 0),
                ended_at=local_dt(10, 26, 11),
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

    def test_extract_notion_id_from_url(self) -> None:
        page_id = "374c120d8b74804c8df2ff78aefa2d49"
        url = f"https://www.notion.so/example-page-{page_id}?pvs=4"

        self.assertEqual(
            arch.extract_notion_id(url),
            "374c120d-8b74-804c-8df2-ff78aefa2d49",
        )

    def test_doctor_write_test_appends_and_archives_block(self) -> None:
        client = FakeNotionClient()

        ok, detail = arch.doctor_notion_write_test(
            client,
            "374c120d-8b74-804c-8df2-ff78aefa2d49",
        )

        self.assertTrue(ok, detail)
        self.assertEqual(client.calls[0][0], "GET")
        self.assertEqual(client.calls[1][0], "PATCH")
        self.assertTrue(client.calls[1][1].endswith("/children"))
        self.assertEqual(client.calls[2], ("DELETE", "/blocks/test-block-id", None))

    def test_scan_raw_candidates_skips_recently_modified_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notion_root = Path(tmp)
            raw_root = notion_root / "File System" / "000" / "t" / "06"
            raw_root.mkdir(parents=True)
            size = 12 * arch.RAW_SAMPLE_RATE * arch.RAW_BYTES_PER_SAMPLE
            recent = raw_root / "recent"
            stable = raw_root / "stable"
            recent.write_bytes(b"\0" * size)
            stable.write_bytes(b"\0" * size)
            now = time.time()
            os.utime(recent, (now, now))
            os.utime(stable, (now - 900, now - 900))

            candidates = arch.scan_raw_candidates(
                notion_root,
                min_size=1,
                since_days=1,
                min_stable_seconds=600,
            )

            self.assertEqual([candidate.raw_path for candidate in candidates], [stable])

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

    def test_manifest_get_does_not_require_python_310_zip_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = arch.Manifest(Path(tmp))
            metadata = {
                "raw_path": "/tmp/raw",
                "raw_size": 4,
                "raw_mtime": 1.0,
                "raw_sha256": "b" * 64,
                "wav_path": "/tmp/meeting.wav",
                "metadata_path": "/tmp/meeting.metadata.json",
            }

            manifest.upsert(metadata)
            found = manifest.get("b" * 64)

            self.assertIsNotNone(found)
            self.assertEqual(found["raw_sha256"], "b" * 64)

    def test_corrupt_notion_db_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "notion.db"
            db_path.write_bytes(b"not a sqlite database")

            self.assertEqual(arch.extract_db_audio_events(db_path), [])
            self.assertEqual(arch.extract_db_transcription_contexts(db_path), [])


if __name__ == "__main__":
    unittest.main()
