"""Free-form notes, captured from Discord during the day and folded into the
next scheduled proposal.

No chat with the agent -- just somewhere to drop a thought ("bench Vasquez,
he's got a knock") whenever it occurs to you. Every message in the configured
Discord channel is captured; the next `Orchestrator.propose()` run folds every
note sent since the last one into the brief, then marks them consumed so they
don't repeat into the following gameweek. Same dual backend as
`decisions/store.py`, for the same reason: a note dropped mid-week must survive
a container restart, or it silently never reaches the agent.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .config import Settings

logger = logging.getLogger(__name__)


class Note(BaseModel):
    id: str
    author: str
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    consumed: bool = False


class NoteStore(ABC):
    @abstractmethod
    def add(self, author: str, text: str) -> Note: ...

    @abstractmethod
    def pending(self) -> list[Note]: ...

    @abstractmethod
    def mark_consumed(self, ids: list[str]) -> None: ...


class FileNoteStore(NoteStore):
    """One JSON file, rewritten whole -- notes are few and low-volume, unlike proposals."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read(self) -> list[Note]:
        if not self.path.exists():
            return []
        try:
            return [Note.model_validate(row) for row in json.loads(self.path.read_text())]
        except Exception as exc:  # noqa: BLE001
            logger.error("Corrupt notes file %s: %s", self.path, exc)
            return []

    def _write(self, notes: list[Note]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([n.model_dump(mode="json") for n in notes], indent=2))
        tmp.replace(self.path)

    def add(self, author: str, text: str) -> Note:
        note = Note(id=uuid.uuid4().hex[:12], author=author, text=text)
        with self._lock:
            notes = self._read()
            notes.append(note)
            self._write(notes)
        return note

    def pending(self) -> list[Note]:
        with self._lock:
            return [n for n in self._read() if not n.consumed]

    def mark_consumed(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._lock:
            notes = self._read()
            wanted = set(ids)
            for n in notes:
                if n.id in wanted:
                    n.consumed = True
            self._write(notes)


class AzureTableNoteStore(NoteStore):
    """Same table as proposals, a different partition -- one fewer resource to provision."""

    PARTITION = "note"

    def __init__(self, connection_string: str, table_name: str) -> None:
        from azure.data.tables import TableServiceClient

        service = TableServiceClient.from_connection_string(connection_string)
        service.create_table_if_not_exists(table_name)
        self.table = service.get_table_client(table_name)

    def add(self, author: str, text: str) -> Note:
        note = Note(id=uuid.uuid4().hex[:12], author=author, text=text)
        self.table.upsert_entity(
            {
                "PartitionKey": self.PARTITION,
                "RowKey": note.id,
                "consumed": note.consumed,
                "payload": note.model_dump_json(),
            }
        )
        return note

    def pending(self) -> list[Note]:
        out: list[Note] = []
        query = f"PartitionKey eq '{self.PARTITION}' and consumed eq false"
        for entity in self.table.query_entities(query):
            try:
                out.append(Note.model_validate_json(entity["payload"]))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.error("Skipping unreadable note row %s: %s", entity.get("RowKey"), exc)
        return out

    def mark_consumed(self, ids: list[str]) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        for note_id in ids:
            try:
                entity = self.table.get_entity(self.PARTITION, note_id)
            except ResourceNotFoundError:
                continue
            note = Note.model_validate_json(entity["payload"])
            note.consumed = True
            entity["consumed"] = True
            entity["payload"] = note.model_dump_json()
            self.table.upsert_entity(entity)


def build_note_store(settings: Settings) -> NoteStore:
    if settings.state_backend == "azure_table":
        conn = settings.azure_table_connection_string.get_secret_value()
        if not conn:
            raise RuntimeError(
                "STATE_BACKEND=azure_table requires AZURE_TABLE_CONNECTION_STRING."
            )
        return AzureTableNoteStore(conn, settings.azure_table_name)
    return FileNoteStore(Path(settings.state_dir) / "notes.json")
