"""Proposal persistence.

Container Apps replicas are disposable, so a proposal that lives only in memory
is a proposal you lose to a routine restart -- and then nothing commits at the
deadline. Two backends:

* ``file``        -- JSON on disk. Fine locally, or with a mounted Azure Files
                     volume. Default.
* ``azure_table`` -- Azure Table Storage. What you want for a real deployment.
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from ..config import Settings
from .schema import Proposal, ProposalStatus

logger = logging.getLogger(__name__)


class ProposalStore(ABC):
    @abstractmethod
    def save(self, proposal: Proposal) -> None: ...

    @abstractmethod
    def get(self, proposal_id: str) -> Proposal | None: ...

    @abstractmethod
    def list_all(self) -> list[Proposal]: ...

    def latest(self, *, gameweek: int | None = None) -> Proposal | None:
        items = self.list_all()
        if gameweek is not None:
            items = [p for p in items if p.gameweek == gameweek]
        if not items:
            return None
        return max(items, key=lambda p: (p.created_at, p.revision))

    def pending(self) -> list[Proposal]:
        return [p for p in self.list_all() if p.status == ProposalStatus.PENDING]

    def supersede_open_proposals(self, gameweek: int, except_id: str | None = None) -> int:
        """Mark older open proposals for this gameweek as superseded."""
        count = 0
        for proposal in self.list_all():
            if proposal.gameweek != gameweek or proposal.id == except_id:
                continue
            if proposal.status in (ProposalStatus.PENDING, ProposalStatus.AMENDED):
                proposal.touch(ProposalStatus.SUPERSEDED)
                self.save(proposal)
                count += 1
        return count


class FileProposalStore(ProposalStore):
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, proposal_id: str) -> Path:
        return self.directory / f"{proposal_id}.json"

    def save(self, proposal: Proposal) -> None:
        with self._lock:
            tmp = self._path(proposal.id).with_suffix(".tmp")
            tmp.write_text(proposal.model_dump_json(indent=2))
            tmp.replace(self._path(proposal.id))

    def get(self, proposal_id: str) -> Proposal | None:
        path = self._path(proposal_id)
        if not path.exists():
            return None
        try:
            return Proposal.model_validate_json(path.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.error("Corrupt proposal file %s: %s", path, exc)
            return None

    def list_all(self) -> list[Proposal]:
        out: list[Proposal] = []
        for path in self.directory.glob("*.json"):
            try:
                out.append(Proposal.model_validate_json(path.read_text()))
            except Exception as exc:  # noqa: BLE001
                logger.error("Skipping unreadable proposal %s: %s", path, exc)
        return out


class AzureTableProposalStore(ProposalStore):
    """One row per proposal; the model is stored as a JSON blob in ``payload``."""

    PARTITION = "proposal"

    def __init__(self, connection_string: str, table_name: str) -> None:
        from azure.data.tables import TableServiceClient

        service = TableServiceClient.from_connection_string(connection_string)
        service.create_table_if_not_exists(table_name)
        self.table = service.get_table_client(table_name)

    def save(self, proposal: Proposal) -> None:
        self.table.upsert_entity(
            {
                "PartitionKey": self.PARTITION,
                "RowKey": proposal.id,
                "status": proposal.status.value,
                "gameweek": proposal.gameweek,
                "entry_id": proposal.entry_id,
                "payload": proposal.model_dump_json(),
            }
        )

    def get(self, proposal_id: str) -> Proposal | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            entity = self.table.get_entity(self.PARTITION, proposal_id)
        except ResourceNotFoundError:
            return None
        return Proposal.model_validate_json(entity["payload"])

    def list_all(self) -> list[Proposal]:
        out: list[Proposal] = []
        for entity in self.table.query_entities(f"PartitionKey eq '{self.PARTITION}'"):
            try:
                out.append(Proposal.model_validate_json(entity["payload"]))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.error("Skipping unreadable row %s: %s", entity.get("RowKey"), exc)
        return out


def build_store(settings: Settings) -> ProposalStore:
    if settings.state_backend == "azure_table":
        conn = settings.azure_table_connection_string.get_secret_value()
        if not conn:
            raise RuntimeError(
                "STATE_BACKEND=azure_table requires AZURE_TABLE_CONNECTION_STRING."
            )
        return AzureTableProposalStore(conn, settings.azure_table_name)
    return FileProposalStore(Path(settings.state_dir) / "proposals")
