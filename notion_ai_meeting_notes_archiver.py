#!/usr/bin/env python3
"""
Archive Notion AI Meeting Notes recordings from Notion's local Electron store.

The local recording payloads observed in the Notion desktop app are raw PCM:
32-bit float, little endian, mono, 16 kHz. This tool wraps those bytes in a WAV
header, stores them in a page-aware local archive, and can upload the resulting
WAV back to Notion using the official File Upload API.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable


DEFAULT_NOTION_ROOT = (
    "~/Library/Application Support/Notion/Partitions/notion"
)
DEFAULT_NOTION_DB = "~/Library/Application Support/Notion/notion.db"
DEFAULT_ARCHIVE_DIR = (
    "~/Library/Application Support/Notion AI Meeting Notes Archiver/Archive"
)
DEFAULT_NOTION_VERSION = "2026-03-11"
DEFAULT_KEYCHAIN_SERVICE = "notion-ai-meeting-notes-archiver"
RAW_SAMPLE_RATE = 16_000
RAW_CHANNELS = 1
RAW_BYTES_PER_SAMPLE = 4
RAW_FORMAT_TAG_IEEE_FLOAT = 3
WAV_HEADER_BYTES = 44
SINGLE_UPLOAD_LIMIT = 20 * 1024 * 1024
MULTIPART_CHUNK_BYTES = 10 * 1024 * 1024
RAW_MIN_DURATION_SECONDS = 10
TRANSCRIPTION_MATCH_WINDOW_SECONDS = 45
TRANSCRIPTION_AMBIGUITY_MARGIN_SECONDS = 15
ARCHIVE_MARKER_PREFIX = "Notion AI Meeting Notes Archive ID:"

AUDIO_NAME_RE = re.compile(
    rb"Audio Recording (?P<date>\d{4}-\d{2}-\d{2}) at "
    rb"(?P<hour>\d{1,2})\.(?P<minute>\d{2})\.(?P<second>\d{2}) "
    rb"(?P<ampm>AM|PM)\.wav"
)
UUID_RE = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
TEXT_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}"
)


def expand(path: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat()


def local_datetime_from_ts(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, tz=dt.datetime.now().astimezone().tzinfo)


def local_datetime_from_notion_ms(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    try:
        timestamp = float(value) / 1000
    except (TypeError, ValueError):
        return None
    return local_datetime_from_ts(timestamp).replace(microsecond=0)


def parse_local_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed


def parse_audio_recording_name(name: str) -> dt.datetime | None:
    match = re.search(
        r"Audio Recording (?P<date>\d{4}-\d{2}-\d{2}) at "
        r"(?P<hour>\d{1,2})\.(?P<minute>\d{2})\.(?P<second>\d{2}) "
        r"(?P<ampm>AM|PM)\.wav",
        name,
    )
    if not match:
        return None
    hour = int(match.group("hour"))
    if match.group("ampm") == "PM" and hour != 12:
        hour += 12
    if match.group("ampm") == "AM" and hour == 12:
        hour = 0
    date = dt.date.fromisoformat(match.group("date"))
    return dt.datetime(
        date.year,
        date.month,
        date.day,
        hour,
        int(match.group("minute")),
        int(match.group("second")),
        tzinfo=dt.datetime.now().astimezone().tzinfo,
    )


def audio_recording_name_from_dt(value: dt.datetime) -> str:
    local_value = value.astimezone()
    hour = local_value.hour
    ampm = "AM" if hour < 12 else "PM"
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12
    return (
        f"Audio Recording {local_value:%Y-%m-%d} at "
        f"{display_hour}.{local_value:%M}.{local_value:%S} {ampm}.wav"
    )


def safe_segment(value: str, fallback: str = "untitled") -> str:
    cleaned = re.sub(r"[/:\\\0]", " - ", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(". ")
    return cleaned[:120] or fallback


def normalize_uuid(value: str) -> str | None:
    compact = value.replace("-", "").lower()
    if len(compact) != 32:
        return None
    try:
        return str(uuid.UUID(compact))
    except ValueError:
        return None


def extract_notion_id(value: str | None) -> str | None:
    if not value:
        return None
    normalized = normalize_uuid(value)
    if normalized:
        return normalized
    matches = TEXT_UUID_RE.findall(value)
    if not matches:
        return None
    return normalize_uuid(matches[-1])


def sha256_file(path: Path, chunk_size: int = 2 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def raw_duration_seconds(size: int) -> float:
    return size / (RAW_SAMPLE_RATE * RAW_CHANNELS * RAW_BYTES_PER_SAMPLE)


def wav_header(data_size: int) -> bytes:
    byte_rate = RAW_SAMPLE_RATE * RAW_CHANNELS * RAW_BYTES_PER_SAMPLE
    block_align = RAW_CHANNELS * RAW_BYTES_PER_SAMPLE
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        RAW_FORMAT_TAG_IEEE_FLOAT,
        RAW_CHANNELS,
        RAW_SAMPLE_RATE,
        byte_rate,
        block_align,
        RAW_BYTES_PER_SAMPLE * 8,
        b"data",
        data_size,
    )


def content_type_for_filename(filename: str) -> str:
    if filename.lower().endswith(".wav"):
        return "audio/wav"
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def parse_json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def archive_marker(raw_sha256: str) -> str:
    return f"{ARCHIVE_MARKER_PREFIX} {raw_sha256}"


def notion_rich_text_plain(value: Any) -> str:
    """Extract plain text from Notion's local nested rich-text arrays."""
    pieces: list[str] = []
    if not isinstance(value, list):
        return ""
    for segment in value:
        if not isinstance(segment, list) or not segment:
            continue
        head = segment[0]
        if isinstance(head, str) and head != "‣":
            pieces.append(head)
            continue
        if head == "‣" and len(segment) > 1 and isinstance(segment[1], list):
            for mention in segment[1]:
                if not isinstance(mention, list) or not mention:
                    continue
                if mention[0] == "d" and len(mention) > 1 and isinstance(mention[1], dict):
                    date = mention[1]
                    start_date = date.get("start_date")
                    start_time = date.get("start_time")
                    if start_date and start_time:
                        pieces.append(f"{start_date} {start_time}")
                    elif start_date:
                        pieces.append(str(start_date))
    return "".join(pieces).strip()


def block_title_from_properties(properties: str | None) -> str | None:
    parsed = parse_json_object(properties)
    title = notion_rich_text_plain(parsed.get("title"))
    if title:
        return title
    for value in parsed.values():
        text = notion_rich_text_plain(value)
        if text:
            return text
    return None


def convert_raw_to_wav(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    data_size = src.stat().st_size
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    with src.open("rb") as fin, tmp.open("wb") as fout:
        fout.write(wav_header(data_size))
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    tmp.replace(dst)


def path_is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def prune_empty_archive_dirs(start: Path, archive_dir: Path) -> None:
    current = start
    root = archive_dir.resolve()
    while current.resolve() != root and path_is_inside(current, archive_dir):
        try:
            entries = list(current.iterdir())
        except OSError:
            return
        removable = [entry for entry in entries if entry.name == ".DS_Store"]
        remaining = [entry for entry in entries if entry.name != ".DS_Store"]
        if remaining:
            return
        for entry in removable:
            try:
                entry.unlink()
            except OSError:
                return
        parent = current.parent
        try:
            current.rmdir()
        except OSError:
            return
        current = parent


def delete_local_archive_files(
    wav_path: Path,
    metadata_path: Path | None,
    archive_dir: Path,
) -> int:
    deleted = 0
    for path in [wav_path, metadata_path]:
        if not path:
            continue
        if not path_is_inside(path, archive_dir):
            continue
        if path.name == ".notion_meeting_notes_manifest.sqlite3":
            continue
        try:
            if path.exists():
                path.unlink()
                deleted += 1
        except OSError:
            continue
    prune_empty_archive_dirs(wav_path.parent, archive_dir)
    return deleted


@contextlib.contextmanager
def archive_process_lock(archive_dir: Path, enabled: bool = True) -> Iterable[None]:
    if not enabled:
        yield
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    lock_path = archive_dir / ".notion_meeting_notes_archiver.lock"
    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another archiver process is already running: {lock_path}"
            ) from exc
        lock_file.write(f"pid={os.getpid()} acquired_at={now_iso()}\n")
        lock_file.flush()
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def is_plausible_raw_pcm(path: Path, min_size: int) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size < min_size or stat.st_size % RAW_BYTES_PER_SAMPLE != 0:
        return False
    duration = raw_duration_seconds(stat.st_size)
    if duration < RAW_MIN_DURATION_SECONDS or duration > 8 * 60 * 60:
        return False
    return True


@dataclasses.dataclass
class RecordingEvent:
    filename: str
    recorded_at: dt.datetime
    source_path: str
    byte_offset: int
    ids: list[str]
    page_id: str | None = None
    page_title: str | None = None
    page_path: str | None = None
    page_url: str | None = None
    source_block_id: str | None = None


@dataclasses.dataclass
class RawCandidate:
    raw_path: Path
    size: int
    mtime: dt.datetime
    duration_seconds: float
    expected_filename: str
    event: RecordingEvent | None = None
    page_id: str | None = None
    page_title: str | None = None
    page_path: str | None = None
    page_url: str | None = None
    source_block_id: str | None = None

    def key_time(self) -> str:
        return self.mtime.replace(microsecond=0).isoformat()


@dataclasses.dataclass
class TranscriptionContext:
    block_id: str
    title: str | None
    started_at: dt.datetime
    ended_at: dt.datetime | None
    page_id: str | None = None
    page_title: str | None = None
    page_path: str | None = None
    page_url: str | None = None


class Manifest:
    def __init__(self, archive_dir: Path) -> None:
        archive_dir.mkdir(parents=True, exist_ok=True)
        self.path = archive_dir / ".notion_meeting_notes_manifest.sqlite3"
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recordings (
              raw_path TEXT,
              raw_size INTEGER,
              raw_mtime REAL,
              raw_sha256 TEXT PRIMARY KEY,
              wav_path TEXT,
              metadata_path TEXT,
              recorded_at TEXT,
              duration_seconds REAL,
              expected_filename TEXT,
              page_id TEXT,
              page_title TEXT,
              page_path TEXT,
              page_url TEXT,
              file_upload_id TEXT,
              uploaded_block_id TEXT,
              uploaded_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def get(self, raw_sha256: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM recordings WHERE raw_sha256 = ?", (raw_sha256,)
        ).fetchone()
        if not row:
            return None
        columns = [info[1] for info in self.conn.execute("PRAGMA table_info(recordings)")]
        return dict(zip(columns, row))

    def uploaded_records(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM recordings
            WHERE uploaded_at IS NOT NULL
            ORDER BY recorded_at
            """
        ).fetchall()
        columns = [info[1] for info in self.conn.execute("PRAGMA table_info(recordings)")]
        return [dict(zip(columns, row)) for row in rows]

    def upsert(self, metadata: dict[str, Any]) -> None:
        now = now_iso()
        metadata = dict(metadata)
        metadata.setdefault("created_at", now)
        metadata["updated_at"] = now
        columns = [
            "raw_path",
            "raw_size",
            "raw_mtime",
            "raw_sha256",
            "wav_path",
            "metadata_path",
            "recorded_at",
            "duration_seconds",
            "expected_filename",
            "page_id",
            "page_title",
            "page_path",
            "page_url",
            "file_upload_id",
            "uploaded_block_id",
            "uploaded_at",
            "created_at",
            "updated_at",
        ]
        values = [metadata.get(column) for column in columns]
        placeholders = ", ".join(["?"] * len(columns))
        update = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"raw_sha256", "created_at"}
        )
        self.conn.execute(
            f"""
            INSERT INTO recordings ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(raw_sha256) DO UPDATE SET {update}
            """,
            values,
        )
        self.conn.commit()


class NotionApiError(RuntimeError):
    pass


class NotionClient:
    def __init__(
        self,
        token: str,
        version: str = DEFAULT_NOTION_VERSION,
        base_url: str = "https://api.notion.com/v1",
    ) -> None:
        self.token = token
        self.version = version
        self.base_url = base_url.rstrip("/")

    def _headers(self, content_type: str | None = "application/json") -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.version,
            "User-Agent": "notion-ai-meeting-notes-archiver/0.1",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def request_json(
        self,
        method: str,
        path_or_url: str,
        body: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        url = (
            path_or_url
            if path_or_url.startswith("https://")
            else f"{self.base_url}{path_or_url}"
        )
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                if response.status not in expected:
                    raise NotionApiError(
                        f"{method} {url} returned {response.status}: {payload[:500]!r}"
                    )
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise NotionApiError(f"{method} {url} returned {exc.code}: {payload}") from exc
        except urllib.error.URLError as exc:
            raise NotionApiError(f"{method} {url} failed: {exc.reason}") from exc
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def multipart_upload_part(
        self,
        upload_id: str,
        file_path: Path,
        *,
        filename: str,
        content_type: str,
        part_number: int | None = None,
        offset: int = 0,
        length: int | None = None,
    ) -> dict[str, Any]:
        boundary = f"----notion-meeting-{uuid.uuid4().hex}"
        body_chunks: list[bytes] = []
        if part_number is not None:
            body_chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    b'Content-Disposition: form-data; name="part_number"\r\n\r\n',
                    str(part_number).encode(),
                    b"\r\n",
                ]
            )
        body_chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="file"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
            ]
        )
        with file_path.open("rb") as fh:
            fh.seek(offset)
            file_bytes = fh.read(length) if length is not None else fh.read()
        body_chunks.append(file_bytes)
        body_chunks.extend([b"\r\n", f"--{boundary}--\r\n".encode()])
        data = b"".join(body_chunks)
        request = urllib.request.Request(
            f"{self.base_url}/file_uploads/{upload_id}/send",
            data=data,
            method="POST",
            headers={
                **self._headers(content_type=None),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(data)),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise NotionApiError(
                f"POST /file_uploads/{upload_id}/send returned {exc.code}: {payload}"
            ) from exc
        except urllib.error.URLError as exc:
            raise NotionApiError(
                f"POST /file_uploads/{upload_id}/send failed: {exc.reason}"
            ) from exc
        return json.loads(payload.decode("utf-8"))

    def get_block(self, block_id: str) -> dict[str, Any] | None:
        try:
            return self.request_json("GET", f"/blocks/{block_id}")
        except NotionApiError:
            return None

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        try:
            return self.request_json("GET", f"/pages/{page_id}")
        except NotionApiError:
            return None

    def can_append_children(self, page_or_block_id: str) -> bool:
        try:
            self.request_json("GET", f"/blocks/{page_or_block_id}")
            return True
        except NotionApiError:
            return self.get_page(page_or_block_id) is not None

    def list_block_children(self, block_id: str) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            query = {"page_size": "100"}
            if cursor:
                query["start_cursor"] = cursor
            response = self.request_json(
                "GET",
                f"/blocks/{block_id}/children?{urllib.parse.urlencode(query)}",
            )
            children.extend(response.get("results", []))
            if not response.get("has_more"):
                return children
            cursor = response.get("next_cursor")
            if not cursor:
                return children

    def find_archived_audio_block(
        self,
        page_or_block_id: str,
        raw_sha256: str,
        title: str,
    ) -> dict[str, str | None] | None:
        marker = archive_marker(raw_sha256)
        legacy_title = f"Archived audio: {title}"
        children = self.list_block_children(page_or_block_id)
        for index, block in enumerate(children):
            text = block_plain_text(block)
            if marker not in text and text.strip() != legacy_title:
                continue
            audio_block_id = None
            for next_block in children[index + 1 : index + 3]:
                if next_block.get("type") == "audio":
                    audio_block_id = next_block.get("id")
                    break
            return {
                "marker_block_id": block.get("id"),
                "uploaded_block_id": audio_block_id,
            }
        return None

    def append_paragraph_block(self, page_or_block_id: str, text: str) -> dict[str, Any]:
        return self.request_json(
            "PATCH",
            f"/blocks/{page_or_block_id}/children",
            {
                "children": [
                    {
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {
                                    "type": "text",
                                    "text": {"content": text},
                                }
                            ]
                        },
                    }
                ]
            },
        )

    def archive_block(self, block_id: str) -> dict[str, Any]:
        return self.request_json("DELETE", f"/blocks/{block_id}")

    def create_file_upload(self, path: Path, filename: str) -> dict[str, Any]:
        size = path.stat().st_size
        content_type = content_type_for_filename(filename)
        if size <= SINGLE_UPLOAD_LIMIT:
            body = {
                "mode": "single_part",
                "filename": filename,
                "content_type": content_type,
            }
        else:
            body = {
                "mode": "multi_part",
                "filename": filename,
                "content_type": content_type,
                "number_of_parts": math.ceil(size / MULTIPART_CHUNK_BYTES),
            }
        return self.request_json("POST", "/file_uploads", body)

    def upload_file(self, path: Path, filename: str) -> dict[str, Any]:
        created = self.create_file_upload(path, filename)
        upload_id = created["id"]
        size = path.stat().st_size
        content_type = content_type_for_filename(filename)
        if size <= SINGLE_UPLOAD_LIMIT:
            sent = self.multipart_upload_part(
                upload_id,
                path,
                filename=filename,
                content_type=content_type,
            )
            return sent
        parts = math.ceil(size / MULTIPART_CHUNK_BYTES)
        for part_number in range(1, parts + 1):
            offset = (part_number - 1) * MULTIPART_CHUNK_BYTES
            length = min(MULTIPART_CHUNK_BYTES, size - offset)
            self.multipart_upload_part(
                upload_id,
                path,
                filename=filename,
                content_type=content_type,
                part_number=part_number,
                offset=offset,
                length=length,
            )
        return self.request_json("POST", f"/file_uploads/{upload_id}/complete", {})

    def append_audio_block(
        self,
        page_or_block_id: str,
        file_upload_id: str,
        title: str,
        raw_sha256: str,
    ) -> dict[str, Any]:
        children = [
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {
                                "content": (
                                    f"Archived audio: {title}\n"
                                    f"{archive_marker(raw_sha256)}"
                                )
                            },
                        }
                    ]
                },
            },
            {
                "type": "audio",
                "audio": {
                    "type": "file_upload",
                    "file_upload": {"id": file_upload_id},
                },
            },
        ]
        return self.request_json(
            "PATCH",
            f"/blocks/{page_or_block_id}/children",
            {"children": children},
        )


def block_plain_text(block: dict[str, Any]) -> str:
    block_type = block.get("type")
    if not block_type:
        return ""
    body = block.get(block_type, {})
    rich_text = body.get("rich_text", [])
    if not isinstance(rich_text, list):
        return ""
    return "".join(part.get("plain_text", "") for part in rich_text)


def page_title(page: dict[str, Any]) -> str | None:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(part.get("plain_text", "") for part in prop.get("title", []))
    return None


def resolve_page_for_ids(
    client: NotionClient,
    ids: Iterable[str],
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    seen: set[str] = set()
    for raw_id in ids:
        block_or_page_id = normalize_uuid(raw_id)
        if not block_or_page_id or block_or_page_id in seen:
            continue
        seen.add(block_or_page_id)
        page = client.get_page(block_or_page_id)
        if page:
            title, path = resolve_page_path(client, block_or_page_id)
            return block_or_page_id, title, path, page.get("url"), None
        block = client.get_block(block_or_page_id)
        if not block:
            continue
        page_id = page_id_from_block_parent(client, block)
        if page_id:
            title, path = resolve_page_path(client, page_id)
            page = client.get_page(page_id) or {}
            return page_id, title, path, page.get("url"), block_or_page_id
    return None, None, None, None, None


def page_id_from_block_parent(
    client: NotionClient,
    block: dict[str, Any],
    max_depth: int = 20,
) -> str | None:
    current = block
    for _ in range(max_depth):
        parent = current.get("parent", {})
        parent_type = parent.get("type")
        if parent_type == "page_id":
            return normalize_uuid(parent.get("page_id", ""))
        if parent_type != "block_id":
            return None
        parent_id = normalize_uuid(parent.get("block_id", ""))
        if not parent_id:
            return None
        current = client.get_block(parent_id) or {}
        if not current:
            return None
    return None


def resolve_page_path(client: NotionClient, page_id: str, max_depth: int = 20) -> tuple[str | None, str | None]:
    titles: list[str] = []
    current_id = page_id
    for _ in range(max_depth):
        page = client.get_page(current_id)
        if not page:
            break
        title = page_title(page) or current_id
        titles.append(title)
        parent = page.get("parent", {})
        if parent.get("type") != "page_id":
            break
        parent_id = normalize_uuid(parent.get("page_id", ""))
        if not parent_id:
            break
        current_id = parent_id
    if not titles:
        return None, None
    titles.reverse()
    return titles[-1], " / ".join(titles)


class LocalNotionDb:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._tmpdir = tempfile.TemporaryDirectory(prefix="notion-ai-notion-db-")
        self._snapshot_path = Path(self._tmpdir.name) / db_path.name
        try:
            self._copy_sqlite_snapshot(db_path, self._snapshot_path)
        except (OSError, sqlite3.Error):
            self._tmpdir.cleanup()
            raise
        uri = f"{self._snapshot_path.as_uri()}?mode=ro&immutable=1"
        try:
            self.conn = sqlite3.connect(uri, uri=True, timeout=5)
            self.conn.execute("PRAGMA busy_timeout = 5000")
        except sqlite3.Error:
            self._tmpdir.cleanup()
            raise
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            self._tmpdir.cleanup()

    @staticmethod
    def _copy_sqlite_snapshot(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        source_uri = f"{src.as_uri()}?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=5)
        target = sqlite3.connect(dst)
        try:
            source.execute("PRAGMA busy_timeout = 5000")
            source.backup(target)
        finally:
            target.close()
            source.close()

    def get_block(self, block_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT *
            FROM block
            WHERE id = ?
            ORDER BY alive DESC, version DESC, meta_last_access_timestamp DESC
            LIMIT 1
            """,
            (block_id,),
        ).fetchone()

    def resolve_block_context(self, block_id: str) -> dict[str, str | None]:
        chain: list[sqlite3.Row] = []
        seen: set[str] = set()
        current = self.get_block(block_id)
        while current and current["id"] not in seen and len(chain) < 30:
            seen.add(current["id"])
            chain.append(current)
            if current["parent_table"] != "block":
                break
            current = self.get_block(current["parent_id"])

        page_rows = [row for row in chain if row["type"] == "page"]
        if not page_rows:
            return {
                "page_id": None,
                "page_title": None,
                "page_path": None,
                "page_url": None,
                "source_block_id": block_id,
            }

        closest_page = page_rows[0]
        page_title_value = block_title_from_properties(closest_page["properties"])
        page_titles = [
            block_title_from_properties(row["properties"]) or row["id"]
            for row in reversed(page_rows)
        ]
        return {
            "page_id": closest_page["id"],
            "page_title": page_title_value or closest_page["id"],
            "page_path": " / ".join(page_titles),
            "page_url": f"https://www.notion.so/{closest_page['id'].replace('-', '')}",
            "source_block_id": block_id,
        }

    def audio_recording_events(self) -> list[RecordingEvent]:
        rows = self.conn.execute(
            """
            SELECT id, properties, created_time, last_edited_time
            FROM (
              SELECT *,
                     ROW_NUMBER() OVER (
                       PARTITION BY id
                       ORDER BY alive DESC, version DESC, meta_last_access_timestamp DESC
                     ) AS rn
              FROM block
              WHERE type = 'audio'
                AND properties LIKE '%Audio Recording%'
                AND alive = 1
            )
            WHERE rn = 1
            ORDER BY created_time
            """
        ).fetchall()
        events: list[RecordingEvent] = []
        for row in rows:
            properties = parse_json_object(row["properties"])
            filename = notion_rich_text_plain(properties.get("title"))
            if not filename:
                continue
            recorded_at = parse_audio_recording_name(filename)
            if not recorded_at:
                continue
            context = self.resolve_block_context(row["id"])
            ids = [row["id"]]
            if context.get("page_id"):
                ids.append(str(context["page_id"]))
            events.append(
                RecordingEvent(
                    filename=filename,
                    recorded_at=recorded_at,
                    source_path=f"{self.db_path}:block:{row['id']}",
                    byte_offset=-1,
                    ids=ids,
                    page_id=context.get("page_id"),
                    page_title=context.get("page_title"),
                    page_path=context.get("page_path"),
                    page_url=context.get("page_url"),
                    source_block_id=context.get("source_block_id"),
                )
            )
        return events

    def transcription_contexts(self) -> list[TranscriptionContext]:
        rows = self.conn.execute(
            """
            SELECT id, properties, created_time, last_edited_time
            FROM (
              SELECT *,
                     ROW_NUMBER() OVER (
                       PARTITION BY id
                       ORDER BY alive DESC, version DESC, meta_last_access_timestamp DESC
                     ) AS rn
              FROM block
              WHERE type = 'transcription'
                AND alive = 1
                AND created_time IS NOT NULL
            )
            WHERE rn = 1
            ORDER BY created_time
            """
        ).fetchall()
        contexts: list[TranscriptionContext] = []
        for row in rows:
            started_at = local_datetime_from_notion_ms(row["created_time"])
            if not started_at:
                continue
            context = self.resolve_block_context(row["id"])
            title = block_title_from_properties(row["properties"])
            contexts.append(
                TranscriptionContext(
                    block_id=row["id"],
                    title=title,
                    started_at=started_at,
                    ended_at=local_datetime_from_notion_ms(row["last_edited_time"]),
                    page_id=context.get("page_id"),
                    page_title=context.get("page_title"),
                    page_path=context.get("page_path"),
                    page_url=context.get("page_url"),
                )
            )
        return contexts


def extract_db_audio_events(notion_db: Path) -> list[RecordingEvent]:
    if not notion_db.exists():
        return []
    try:
        db = LocalNotionDb(notion_db)
    except (OSError, sqlite3.Error):
        return []
    try:
        return db.audio_recording_events()
    except sqlite3.Error:
        return []
    finally:
        db.close()


def extract_db_transcription_contexts(notion_db: Path) -> list[TranscriptionContext]:
    if not notion_db.exists():
        return []
    try:
        db = LocalNotionDb(notion_db)
    except (OSError, sqlite3.Error):
        return []
    try:
        return db.transcription_contexts()
    except sqlite3.Error:
        return []
    finally:
        db.close()


def iter_files(root: Path) -> Iterable[Path]:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        yield Path(entry.path)
        except (OSError, PermissionError):
            continue


def extract_events(notion_root: Path, since_days: int) -> list[RecordingEvent]:
    roots = [
        notion_root / "IndexedDB",
        notion_root / "Service Worker" / "CacheStorage",
        notion_root / "File System" / "000" / "t" / "Paths",
    ]
    cutoff = time.time() - since_days * 24 * 60 * 60
    events_by_key: dict[tuple[str, int], RecordingEvent] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in iter_files(root):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff or stat.st_size > 64 * 1024 * 1024:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            for match in AUDIO_NAME_RE.finditer(data):
                filename = match.group(0).decode("utf-8", errors="replace")
                recorded_at = parse_audio_recording_name(filename)
                if not recorded_at:
                    continue
                window = data[max(0, match.start() - 4096) : match.end() + 4096]
                ids = []
                for found in UUID_RE.findall(window):
                    normalized = normalize_uuid(found.decode("ascii", errors="ignore"))
                    if normalized and normalized not in ids:
                        ids.append(normalized)
                key = (filename, int(recorded_at.timestamp()))
                events_by_key[key] = RecordingEvent(
                    filename=filename,
                    recorded_at=recorded_at,
                    source_path=str(path),
                    byte_offset=match.start(),
                    ids=ids,
                )
    return sorted(events_by_key.values(), key=lambda item: item.recorded_at)


def scan_raw_candidates(
    notion_root: Path,
    *,
    min_size: int,
    since_days: int,
    ignore_before: dt.datetime | None = None,
) -> list[RawCandidate]:
    raw_root = notion_root / "File System" / "000" / "t"
    cutoff = time.time() - since_days * 24 * 60 * 60
    if ignore_before:
        cutoff = max(cutoff, ignore_before.timestamp())
    candidates: list[RawCandidate] = []
    if not raw_root.exists():
        return candidates
    for path in iter_files(raw_root):
        try:
            stat = path.stat()
        except OSError:
            continue
        relative_parts = path.relative_to(raw_root).parts
        if path.name.startswith(".") or "Paths" in relative_parts:
            continue
        if stat.st_mtime < cutoff:
            continue
        if not is_plausible_raw_pcm(path, min_size=min_size):
            continue
        mtime = local_datetime_from_ts(stat.st_mtime).replace(microsecond=0)
        candidates.append(
            RawCandidate(
                raw_path=path,
                size=stat.st_size,
                mtime=mtime,
                duration_seconds=raw_duration_seconds(stat.st_size),
                expected_filename=audio_recording_name_from_dt(mtime),
            )
        )
    return sorted(candidates, key=lambda item: item.mtime)


def match_events(candidates: list[RawCandidate], events: list[RecordingEvent]) -> None:
    for candidate in candidates:
        exact = [event for event in events if event.filename == candidate.expected_filename]
        closest: RecordingEvent | None
        closest_delta: float
        if exact:
            closest = next((event for event in exact if event.page_id), exact[0])
            closest_delta = 0
        else:
            closest = None
            closest_delta = 999_999
            for event in events:
                delta = abs((event.recorded_at - candidate.mtime).total_seconds())
                if delta < closest_delta:
                    closest = event
                    closest_delta = delta
        if closest and closest_delta <= 2:
            candidate.event = closest
            candidate.page_id = candidate.page_id or closest.page_id
            candidate.page_title = candidate.page_title or closest.page_title
            candidate.page_path = candidate.page_path or closest.page_path
            candidate.page_url = candidate.page_url or closest.page_url
            candidate.source_block_id = candidate.source_block_id or closest.source_block_id


def match_transcription_contexts(
    candidates: list[RawCandidate],
    contexts: list[TranscriptionContext],
    notion_db: Path,
) -> None:
    for candidate in candidates:
        if candidate.event:
            continue
        candidate_started_at = candidate.mtime - dt.timedelta(
            seconds=candidate.duration_seconds
        )
        matches: list[tuple[float, str, TranscriptionContext]] = []
        for context in contexts:
            if not context.page_id:
                continue
            start_delta = abs(
                (context.started_at - candidate_started_at).total_seconds()
            )
            end_delta = 999_999.0
            if context.ended_at:
                end_delta = abs((context.ended_at - candidate.mtime).total_seconds())
            if (
                start_delta > TRANSCRIPTION_MATCH_WINDOW_SECONDS
                and end_delta > TRANSCRIPTION_MATCH_WINDOW_SECONDS
            ):
                continue
            if start_delta <= end_delta:
                score = start_delta
                reason = f"start_delta={start_delta:.1f}s"
            else:
                score = end_delta
                reason = f"end_delta={end_delta:.1f}s"
            matches.append((score, reason, context))
        if not matches:
            continue
        matches.sort(key=lambda item: item[0])
        best = matches[0]
        if (
            len(matches) > 1
            and matches[1][0] - best[0] < TRANSCRIPTION_AMBIGUITY_MARGIN_SECONDS
        ):
            continue
        _, reason, context = best
        ids = [context.block_id]
        if context.page_id:
            ids.append(context.page_id)
        candidate.event = RecordingEvent(
            filename=candidate.expected_filename,
            recorded_at=candidate.mtime,
            source_path=f"{notion_db}:transcription:{context.block_id}:{reason}",
            byte_offset=-1,
            ids=ids,
            page_id=context.page_id,
            page_title=context.page_title,
            page_path=context.page_path,
            page_url=context.page_url,
            source_block_id=context.block_id,
        )
        candidate.page_id = candidate.page_id or context.page_id
        candidate.page_title = candidate.page_title or context.page_title
        candidate.page_path = candidate.page_path or context.page_path
        candidate.page_url = candidate.page_url or context.page_url
        candidate.source_block_id = candidate.source_block_id or context.block_id


def build_archive_paths(
    archive_dir: Path,
    candidate: RawCandidate,
) -> tuple[Path, Path]:
    page_path = candidate.page_path or "_unmatched"
    segments = [safe_segment(segment) for segment in page_path.split("/") if segment.strip()]
    folder = archive_dir.joinpath(*segments)
    title = safe_segment(candidate.page_title or "Notion AI Meeting Notes")
    timestamp = candidate.mtime.strftime("%Y-%m-%d %H-%M-%S")
    filename = f"{timestamp} - {title}.wav"
    wav_path = folder / filename
    return wav_path, wav_path.with_suffix(".metadata.json")


def candidate_metadata(
    candidate: RawCandidate,
    raw_sha256: str,
    wav_path: Path,
    metadata_path: Path,
) -> dict[str, Any]:
    return {
        "raw_path": str(candidate.raw_path),
        "raw_size": candidate.size,
        "raw_mtime": candidate.raw_path.stat().st_mtime,
        "raw_sha256": raw_sha256,
        "wav_path": str(wav_path),
        "metadata_path": str(metadata_path),
        "recorded_at": candidate.mtime.isoformat(),
        "duration_seconds": candidate.duration_seconds,
        "expected_filename": candidate.expected_filename,
        "page_id": candidate.page_id,
        "page_title": candidate.page_title,
        "page_path": candidate.page_path,
        "page_url": candidate.page_url,
        "source_block_id": candidate.source_block_id,
        "event": dataclasses.asdict(candidate.event) if candidate.event else None,
    }


def load_config(path: Path | None) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if path and path.exists():
        config = json.loads(path.read_text())
    return config


def notion_client_from_config(config: dict[str, Any]) -> NotionClient | None:
    env_name = config.get("notion_token_env", "NOTION_API_KEY")
    token = read_keychain_token(
        config.get("notion_token_keychain_service", DEFAULT_KEYCHAIN_SERVICE)
    )
    if not token:
        token = os.environ.get(env_name)
    if not token:
        return None
    return NotionClient(token, version=config.get("notion_version", DEFAULT_NOTION_VERSION))


def read_keychain_token(service: str | None) -> str | None:
    if not service:
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def resolve_candidates(
    candidates: list[RawCandidate],
    client: NotionClient | None,
    fallback_page_id: str | None = None,
) -> None:
    if not client:
        if fallback_page_id:
            for candidate in candidates:
                candidate.page_id = candidate.page_id or fallback_page_id
                candidate.page_path = candidate.page_path or f"notion-page-{fallback_page_id}"
        return
    for candidate in candidates:
        if candidate.page_id:
            title, path = resolve_page_path(client, candidate.page_id)
            page = client.get_page(candidate.page_id) or {}
            candidate.page_title = title or candidate.page_title
            candidate.page_path = path or candidate.page_path
            candidate.page_url = page.get("url") or candidate.page_url
            continue
        ids = candidate.event.ids if candidate.event else []
        page_id, title, path, url, source_block_id = resolve_page_for_ids(client, ids)
        if not page_id and fallback_page_id:
            page_id = normalize_uuid(fallback_page_id)
            if page_id:
                title, path = resolve_page_path(client, page_id)
                page = client.get_page(page_id) or {}
                url = page.get("url")
        candidate.page_id = page_id
        candidate.page_title = title
        candidate.page_path = path
        candidate.page_url = url
        candidate.source_block_id = source_block_id


def scan_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    notion_root = expand(args.notion_root or config.get("notion_root", DEFAULT_NOTION_ROOT))
    notion_db = expand(args.notion_db or config.get("notion_db", DEFAULT_NOTION_DB))
    events = extract_db_audio_events(notion_db) + extract_events(notion_root, args.since_days)
    candidates = scan_raw_candidates(
        notion_root,
        min_size=args.min_size_mb * 1024 * 1024,
        since_days=args.since_days,
        ignore_before=parse_local_datetime(args.ignore_before),
    )
    match_events(candidates, events)
    match_transcription_contexts(
        candidates,
        extract_db_transcription_contexts(notion_db),
        notion_db,
    )
    client = notion_client_from_config(config) if args.resolve else None
    resolve_candidates(candidates, client, args.fallback_page_id or config.get("fallback_page_id"))
    if args.json:
        print(
            json.dumps(
                {
                    "notion_root": str(notion_root),
                    "notion_db": str(notion_db),
                    "events": [dataclasses.asdict(event) for event in events],
                    "candidates": [
                        {
                            **dataclasses.asdict(candidate),
                            "raw_path": str(candidate.raw_path),
                        }
                        for candidate in candidates
                    ],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0
    print(f"Notion root: {notion_root}")
    print(f"Notion DB: {notion_db}")
    print(f"Found {len(events)} metadata event(s), {len(candidates)} raw candidate(s).")
    for candidate in candidates:
        mark = "matched" if candidate.event else "unmatched"
        page = candidate.page_path or "-"
        print(
            f"{candidate.mtime:%Y-%m-%d %H:%M:%S}  "
            f"{candidate.size:>12} bytes  {mark:9}  "
            f"{candidate.duration_seconds / 60:7.1f} min  {page}"
        )
        print(f"  raw: {candidate.raw_path}")
        print(f"  name: {candidate.expected_filename}")
        if candidate.event:
            print(f"  event: {candidate.event.source_path}")
            if candidate.event.ids:
                print(f"  ids: {', '.join(candidate.event.ids[:8])}")
    return 0


def archive_once(args: argparse.Namespace, config: dict[str, Any]) -> int:
    notion_root = expand(args.notion_root or config.get("notion_root", DEFAULT_NOTION_ROOT))
    notion_db = expand(args.notion_db or config.get("notion_db", DEFAULT_NOTION_DB))
    archive_dir = expand(args.archive_dir or config.get("archive_dir", DEFAULT_ARCHIVE_DIR))
    if not getattr(args, "_archive_lock_held", False):
        try:
            with archive_process_lock(archive_dir, enabled=not args.dry_run):
                setattr(args, "_archive_lock_held", True)
                try:
                    return archive_once(args, config)
                finally:
                    delattr(args, "_archive_lock_held")
        except RuntimeError as exc:
            print(f"archive skipped: {exc}")
            return 1
    candidates = scan_raw_candidates(
        notion_root,
        min_size=args.min_size_mb * 1024 * 1024,
        since_days=args.since_days,
        ignore_before=parse_local_datetime(args.ignore_before),
    )
    manifest = None if args.dry_run else Manifest(archive_dir)
    delete_after_upload = bool(config.get("delete_after_upload", False))
    uploaded = 0
    archived = 0
    skipped = 0
    deleted = 0
    candidate_state: dict[str, tuple[str, dict[str, Any] | None]] = {}
    pending_candidates: list[RawCandidate] = []
    for candidate in candidates:
        raw_sha256 = "" if args.dry_run else sha256_file(candidate.raw_path)
        existing = manifest.get(raw_sha256) if manifest else None
        candidate_state[str(candidate.raw_path)] = (raw_sha256, existing)
        if existing and existing.get("uploaded_at") and args.upload and not args.force:
            existing_wav = Path(existing["wav_path"])
            existing_metadata = (
                Path(existing["metadata_path"]) if existing.get("metadata_path") else None
            )
            if delete_after_upload:
                deleted += delete_local_archive_files(
                    existing_wav,
                    existing_metadata,
                    archive_dir,
                )
            skipped += 1
            print(f"skip uploaded existing: {existing['wav_path']}")
            continue
        pending_candidates.append(candidate)
    candidates = pending_candidates
    if candidates:
        events = extract_db_audio_events(notion_db) + extract_events(notion_root, args.since_days)
        match_events(candidates, events)
        match_transcription_contexts(
            candidates,
            extract_db_transcription_contexts(notion_db),
            notion_db,
        )
        if getattr(args, "only_transcription_matches", False):
            candidates = [
                candidate
                for candidate in candidates
                if candidate.event and ":transcription:" in candidate.event.source_path
            ]
        if not args.include_unmatched:
            candidates = [candidate for candidate in candidates if candidate.event]
    client = notion_client_from_config(config) if args.upload and not args.dry_run else None
    fallback_page_id = args.fallback_page_id or config.get("fallback_page_id")
    resolve_candidates(candidates, client, fallback_page_id)
    for candidate in candidates:
        raw_sha256, existing = candidate_state.get(str(candidate.raw_path), ("", None))
        wav_path, metadata_path = build_archive_paths(archive_dir, candidate)
        if existing and Path(existing["wav_path"]).exists() and not args.force:
            wav_path = Path(existing["wav_path"])
            if existing.get("metadata_path"):
                metadata_path = Path(existing["metadata_path"])
            skipped += 1
            print(f"skip existing: {existing['wav_path']}")
        else:
            print(f"archive: {candidate.raw_path} -> {wav_path}")
            if not args.dry_run:
                convert_raw_to_wav(candidate.raw_path, wav_path)
                metadata = candidate_metadata(candidate, raw_sha256, wav_path, metadata_path)
                metadata_path.parent.mkdir(parents=True, exist_ok=True)
                metadata_path.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2, default=str)
                )
                manifest.upsert(metadata)
            archived += 1
        if args.upload:
            page_id = candidate.page_id or fallback_page_id
            if args.dry_run:
                if page_id:
                    print(f"dry-run upload: {wav_path} -> page {page_id}")
                else:
                    print(f"dry-run upload skipped: page could not be resolved for {candidate.raw_path}")
                continue
            if not client:
                print("upload skipped: Notion token is not set in Keychain or environment")
                continue
            if not page_id:
                print(f"upload skipped: page could not be resolved for {candidate.raw_path}")
                continue
            if existing and existing.get("uploaded_at") and not args.force:
                print(f"upload skipped existing: {existing['wav_path']}")
                continue
            if not client.can_append_children(page_id):
                print(
                    "upload skipped: page is not shared with integration "
                    f"or cannot be found: {page_id}"
                )
                continue
            if wav_path.exists():
                target_wav = wav_path
            elif existing and existing.get("wav_path"):
                target_wav = Path(existing["wav_path"])
            else:
                print(f"upload skipped: WAV file is missing for {candidate.raw_path}")
                continue
            if not args.force:
                try:
                    remote = client.find_archived_audio_block(
                        page_id,
                        raw_sha256,
                        target_wav.name,
                    )
                except NotionApiError as exc:
                    print(
                        "upload skipped: could not check existing archive marker "
                        f"for {page_id}: {str(exc).splitlines()[0]}"
                    )
                    continue
                if remote:
                    metadata = candidate_metadata(candidate, raw_sha256, target_wav, metadata_path)
                    metadata["uploaded_block_id"] = remote.get("uploaded_block_id")
                    metadata["uploaded_at"] = now_iso()
                    metadata_path.parent.mkdir(parents=True, exist_ok=True)
                    metadata_path.write_text(
                        json.dumps(metadata, ensure_ascii=False, indent=2, default=str)
                    )
                    manifest.upsert(metadata)
                    if delete_after_upload:
                        deleted += delete_local_archive_files(
                            target_wav,
                            metadata_path,
                            archive_dir,
                        )
                    skipped += 1
                    print(f"skip existing remote: {target_wav.name} -> {page_id}")
                    continue
            try:
                response = client.upload_file(target_wav, target_wav.name)
                upload_id = response["id"]
                attach = client.append_audio_block(
                    page_id,
                    upload_id,
                    target_wav.name,
                    raw_sha256,
                )
            except NotionApiError as exc:
                print(
                    "upload failed: "
                    f"{target_wav.name} -> {page_id}: {str(exc).splitlines()[0]}"
                )
                continue
            uploaded_block_id = None
            if attach.get("results"):
                uploaded_block_id = attach["results"][-1].get("id")
            metadata = candidate_metadata(candidate, raw_sha256, target_wav, metadata_path)
            metadata["file_upload_id"] = upload_id
            metadata["uploaded_block_id"] = uploaded_block_id
            metadata["uploaded_at"] = now_iso()
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, default=str)
            )
            manifest.upsert(metadata)
            if delete_after_upload:
                deleted += delete_local_archive_files(target_wav, metadata_path, archive_dir)
            uploaded += 1
            print(f"uploaded: {target_wav.name} -> {page_id}")
    print(
        "done: "
        f"archived={archived}, skipped={skipped}, uploaded={uploaded}, "
        f"deleted={deleted}"
    )
    return 0


def cleanup_uploaded_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    archive_dir = expand(args.archive_dir or config.get("archive_dir", DEFAULT_ARCHIVE_DIR))
    try:
        with archive_process_lock(archive_dir, enabled=not args.dry_run):
            return cleanup_uploaded_locked(args, archive_dir)
    except RuntimeError as exc:
        print(f"cleanup skipped: {exc}")
        return 1


def cleanup_uploaded_locked(args: argparse.Namespace, archive_dir: Path) -> int:
    manifest = Manifest(archive_dir)
    deleted = 0
    matched = 0
    for record in manifest.uploaded_records():
        wav_path = Path(record["wav_path"])
        metadata_path = (
            Path(record["metadata_path"]) if record.get("metadata_path") else None
        )
        if not wav_path.exists() and not (metadata_path and metadata_path.exists()):
            continue
        matched += 1
        if args.dry_run:
            print(f"dry-run delete: {wav_path}")
            if metadata_path:
                print(f"dry-run delete: {metadata_path}")
            continue
        count = delete_local_archive_files(wav_path, metadata_path, archive_dir)
        deleted += count
        print(f"deleted local archive files: {record['recorded_at']} {record['page_title']}")
    print(f"done: matched={matched}, deleted={deleted}")
    return 0


def doctor_status(state: str, label: str, detail: str) -> None:
    print(f"{state}: {label}: {detail}")


def doctor_notion_write_test(client: NotionClient, page_id_or_url: str) -> tuple[bool, str]:
    page_id = extract_notion_id(page_id_or_url)
    if not page_id:
        return False, f"invalid Notion page ID or URL: {page_id_or_url}"
    if not client.can_append_children(page_id):
        return False, f"page cannot be found or appended to: {page_id}"
    marker = f"Notion AI Meeting Notes Archiver doctor write test: {now_iso()}"
    response = client.append_paragraph_block(page_id, marker)
    results = response.get("results") or []
    block_id = results[-1].get("id") if results else None
    if not block_id:
        return False, f"write succeeded but response did not include a block ID: {page_id}"
    try:
        client.archive_block(block_id)
    except NotionApiError as exc:
        return True, (
            f"write succeeded on {page_id}, but cleanup failed for block "
            f"{block_id}: {str(exc).splitlines()[0]}"
        )
    return True, f"appended and archived a test paragraph on {page_id}"


def doctor_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    notion_root = expand(args.notion_root or config.get("notion_root", DEFAULT_NOTION_ROOT))
    notion_db = expand(args.notion_db or config.get("notion_db", DEFAULT_NOTION_DB))
    archive_dir = expand(args.archive_dir or config.get("archive_dir", DEFAULT_ARCHIVE_DIR))
    raw_root = notion_root / "File System" / "000" / "t"
    failures = 0

    if notion_root.exists():
        doctor_status("OK", "Notion root", str(notion_root))
    else:
        doctor_status("FAIL", "Notion root", f"missing: {notion_root}")
        failures += 1

    if raw_root.exists():
        doctor_status("OK", "Local recording store", str(raw_root))
    else:
        doctor_status("FAIL", "Local recording store", f"missing: {raw_root}")
        failures += 1

    if notion_db.exists():
        try:
            db = LocalNotionDb(notion_db)
            db.close()
            doctor_status("OK", "Notion DB", f"readable snapshot: {notion_db}")
        except (OSError, sqlite3.Error) as exc:
            doctor_status("FAIL", "Notion DB", f"cannot read snapshot: {exc}")
            failures += 1
    else:
        doctor_status("FAIL", "Notion DB", f"missing: {notion_db}")
        failures += 1

    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        probe = archive_dir / ".doctor-write-test"
        probe.write_text(now_iso())
        probe.unlink(missing_ok=True)
        doctor_status("OK", "Archive directory", str(archive_dir))
    except OSError as exc:
        doctor_status("FAIL", "Archive directory", f"not writable: {exc}")
        failures += 1

    service = config.get("notion_token_keychain_service", DEFAULT_KEYCHAIN_SERVICE)
    env_name = config.get("notion_token_env", "NOTION_API_KEY")
    keychain_token = read_keychain_token(service)
    env_token = os.environ.get(env_name)
    if keychain_token:
        doctor_status("OK", "Notion token", f"found in Keychain service '{service}'")
    elif env_token:
        doctor_status("WARN", "Notion token", f"using fallback environment variable '{env_name}'")
    else:
        doctor_status("FAIL", "Notion token", f"missing Keychain service '{service}'")
        failures += 1

    token = keychain_token or env_token
    client: NotionClient | None = None
    if token and not args.no_api:
        try:
            client = NotionClient(
                token,
                version=config.get("notion_version", DEFAULT_NOTION_VERSION),
            )
            me = client.request_json("GET", "/users/me")
            doctor_status(
                "OK",
                "Notion API",
                f"authenticated as {me.get('name') or me.get('id') or 'unknown'}",
            )
        except NotionApiError as exc:
            doctor_status("FAIL", "Notion API", str(exc).splitlines()[0])
            failures += 1
    elif token:
        doctor_status("WARN", "Notion API", "skipped by --no-api")

    if args.test_page_id:
        if not token:
            doctor_status("FAIL", "Notion write test", "Notion token is missing")
            failures += 1
        elif args.no_api:
            doctor_status("FAIL", "Notion write test", "requires API; remove --no-api")
            failures += 1
        elif client:
            try:
                ok, detail = doctor_notion_write_test(client, args.test_page_id)
            except NotionApiError as exc:
                ok = False
                detail = str(exc).splitlines()[0]
            doctor_status("OK" if ok else "FAIL", "Notion write test", detail)
            if not ok:
                failures += 1

    launch_agent = (
        Path.home()
        / "Library"
        / "LaunchAgents"
        / "com.local.notion-ai-meeting-notes-archiver.plist"
    )
    if launch_agent.exists():
        doctor_status("OK", "LaunchAgent", str(launch_agent))
    else:
        doctor_status("WARN", "LaunchAgent", f"not installed: {launch_agent}")

    return 1 if failures else 0


def watch_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    print(f"watching every {args.interval}s; press Ctrl-C to stop")
    while True:
        try:
            archive_once(args, config)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("stopped")
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive and upload Notion AI Meeting Notes local recordings."
    )
    parser.add_argument("--config", type=Path, help="JSON config path")
    parser.add_argument("--notion-root", help="Notion partition root")
    parser.add_argument("--notion-db", help="Notion SQLite database path")
    parser.add_argument("--archive-dir", help="Archive directory")
    parser.add_argument("--since-days", type=int, default=14)
    parser.add_argument("--min-size-mb", type=int, default=5)
    parser.add_argument("--ignore-before", help="Ignore raw recordings older than this ISO datetime")
    parser.add_argument("--fallback-page-id", help="Page ID to use when auto-resolution fails")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="List raw recording candidates")
    scan.add_argument("--json", action="store_true", help="Print JSON")
    scan.add_argument("--resolve", action="store_true", help="Resolve pages using Notion API")

    archive = sub.add_parser("archive", help="Convert and optionally upload recordings")
    archive.add_argument("--include-unmatched", action="store_true")
    archive.add_argument(
        "--only-transcription-matches",
        action="store_true",
        help="Only process candidates matched through local transcription blocks",
    )
    archive.add_argument("--force", action="store_true")
    archive.add_argument("--dry-run", action="store_true")
    archive.add_argument("--upload", action="store_true")

    cleanup = sub.add_parser(
        "cleanup-uploaded",
        help="Delete local WAV/metadata files for manifest records already uploaded",
    )
    cleanup.add_argument("--dry-run", action="store_true")

    doctor = sub.add_parser("doctor", help="Check local setup and Notion API access")
    doctor.add_argument("--no-api", action="store_true", help="Skip Notion API request")
    doctor.add_argument(
        "--test-page-id",
        help=(
            "Append and immediately archive a small paragraph to verify Notion "
            "write access for a page ID or URL"
        ),
    )

    watch = sub.add_parser("watch", help="Poll for new recordings")
    watch.add_argument("--include-unmatched", action="store_true")
    watch.add_argument("--force", action="store_true")
    watch.add_argument("--dry-run", action="store_true")
    watch.add_argument("--upload", action="store_true")
    watch.add_argument("--interval", type=int, default=60)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.command == "scan":
        return scan_command(args, config)
    if args.command == "archive":
        return archive_once(args, config)
    if args.command == "cleanup-uploaded":
        return cleanup_uploaded_command(args, config)
    if args.command == "doctor":
        return doctor_command(args, config)
    if args.command == "watch":
        return watch_command(args, config)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
