"""Configurable LangGraph checkpoint backends.

SQLite remains the zero-configuration development backend.  The SQLAlchemy
backend stores the same LangGraph checkpoint contract in MySQL so every API
instance can resume the same thread.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
import sqlite3
from typing import Any, cast

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    ChannelVersions,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain_core.runnables import RunnableConfig
from sqlalchemy import (
    Column,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    delete,
    select,
)
from sqlalchemy.dialects.mysql import LONGBLOB, insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from config import settings


_BLOB = LargeBinary().with_variant(LONGBLOB(), "mysql")


class SQLAlchemyCheckpointSaver(BaseCheckpointSaver[str]):
    """Synchronous LangGraph saver backed by a shared SQL database.

    The current production target is MySQL.  SQLite is also supported by this
    class for deterministic unit tests, while the built-in ``SqliteSaver`` is
    retained for local application development.
    """

    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine
        metadata = MetaData()
        self.checkpoints = Table(
            "langgraph_checkpoints",
            metadata,
            Column("thread_id", String(128), primary_key=True),
            Column("checkpoint_ns", String(128), primary_key=True, default=""),
            Column("checkpoint_id", String(64), primary_key=True),
            Column("parent_checkpoint_id", String(64), nullable=True),
            Column("type", String(64), nullable=False),
            Column("checkpoint", _BLOB, nullable=False),
            Column("metadata", _BLOB, nullable=True),
        )
        self.writes = Table(
            "langgraph_checkpoint_writes",
            metadata,
            Column("thread_id", String(128), primary_key=True),
            Column("checkpoint_ns", String(128), primary_key=True, default=""),
            Column("checkpoint_id", String(64), primary_key=True),
            Column("task_id", String(128), primary_key=True),
            Column("idx", Integer, primary_key=True),
            Column("task_path", String(512), nullable=False, default=""),
            Column("channel", String(255), nullable=False),
            Column("type", String(64), nullable=False),
            Column("value", _BLOB, nullable=False),
        )
        self._metadata = metadata
        self._is_setup = False

    @classmethod
    def from_url(cls, url: str) -> "SQLAlchemyCheckpointSaver":
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        return cls(engine)

    def setup(self) -> None:
        if not self._is_setup:
            self._metadata.create_all(self.engine, checkfirst=True)
            self._is_setup = True

    @staticmethod
    def _blob(value: Any) -> bytes:
        return bytes(value) if isinstance(value, memoryview) else value

    @staticmethod
    def _config(
        thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def _pending_writes(
        self, connection: Any, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> list[tuple[str, str, Any]]:
        rows = connection.execute(
            select(
                self.writes.c.task_id,
                self.writes.c.channel,
                self.writes.c.type,
                self.writes.c.value,
            )
            .where(
                and_(
                    self.writes.c.thread_id == thread_id,
                    self.writes.c.checkpoint_ns == checkpoint_ns,
                    self.writes.c.checkpoint_id == checkpoint_id,
                )
            )
            .order_by(self.writes.c.task_id, self.writes.c.idx)
        ).mappings()
        return [
            (
                row["task_id"],
                row["channel"],
                self.serde.loads_typed((row["type"], self._blob(row["value"]))),
            )
            for row in rows
        ]

    def _to_tuple(self, connection: Any, row: Any) -> CheckpointTuple:
        config = self._config(
            row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"]
        )
        parent_config = (
            self._config(
                row["thread_id"],
                row["checkpoint_ns"],
                row["parent_checkpoint_id"],
            )
            if row["parent_checkpoint_id"]
            else None
        )
        metadata = (
            json.loads(self._blob(row["metadata"])) if row["metadata"] else {}
        )
        return CheckpointTuple(
            config,
            self.serde.loads_typed(
                (row["type"], self._blob(row["checkpoint"]))
            ),
            cast(CheckpointMetadata, metadata),
            parent_config,
            self._pending_writes(
                connection,
                row["thread_id"],
                row["checkpoint_ns"],
                row["checkpoint_id"],
            ),
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        self.setup()
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        statement = select(self.checkpoints).where(
            and_(
                self.checkpoints.c.thread_id == thread_id,
                self.checkpoints.c.checkpoint_ns == checkpoint_ns,
            )
        )
        if checkpoint_id := get_checkpoint_id(config):
            statement = statement.where(
                self.checkpoints.c.checkpoint_id == checkpoint_id
            )
        else:
            statement = statement.order_by(
                self.checkpoints.c.checkpoint_id.desc()
            ).limit(1)
        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().first()
            return self._to_tuple(connection, row) if row else None

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        self.setup()
        statement = select(self.checkpoints)
        if config:
            configurable = config["configurable"]
            clauses = [
                self.checkpoints.c.thread_id == str(configurable["thread_id"])
            ]
            if "checkpoint_ns" in configurable:
                clauses.append(
                    self.checkpoints.c.checkpoint_ns
                    == str(configurable.get("checkpoint_ns", ""))
                )
            statement = statement.where(and_(*clauses))
        if before and (before_id := get_checkpoint_id(before)):
            statement = statement.where(
                self.checkpoints.c.checkpoint_id < before_id
            )
        statement = statement.order_by(self.checkpoints.c.checkpoint_id.desc())

        emitted = 0
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings()
            for row in rows:
                item = self._to_tuple(connection, row)
                if filter and any(
                    item.metadata.get(key) != value for key, value in filter.items()
                ):
                    continue
                yield item
                emitted += 1
                if limit is not None and emitted >= limit:
                    break

    def _upsert(self, connection: Any, table: Table, values: dict[str, Any]) -> None:
        dialect = self.engine.dialect.name
        primary_keys = [column.name for column in table.primary_key.columns]
        updates = {key: value for key, value in values.items() if key not in primary_keys}
        if dialect == "mysql":
            statement = mysql_insert(table).values(**values)
            connection.execute(statement.on_duplicate_key_update(**updates))
        elif dialect == "sqlite":
            statement = sqlite_insert(table).values(**values)
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=primary_keys, set_=updates
                )
            )
        else:
            key_clause = and_(*[
                table.c[key] == values[key] for key in primary_keys
            ])
            connection.execute(delete(table).where(key_clause))
            connection.execute(table.insert().values(**values))

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del new_versions
        self.setup()
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        type_, serialized_checkpoint = self.serde.dumps_typed(checkpoint)
        serialized_metadata = json.dumps(
            get_checkpoint_metadata(config, metadata),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        values = {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint["id"],
            "parent_checkpoint_id": configurable.get("checkpoint_id"),
            "type": type_,
            "checkpoint": serialized_checkpoint,
            "metadata": serialized_metadata,
        }
        with self.engine.begin() as connection:
            self._upsert(connection, self.checkpoints, values)
        return self._config(thread_id, checkpoint_ns, checkpoint["id"])

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.setup()
        configurable = config["configurable"]
        rows = []
        for index, (channel, value) in enumerate(writes):
            type_, serialized = self.serde.dumps_typed(value)
            rows.append({
                "thread_id": str(configurable["thread_id"]),
                "checkpoint_ns": str(configurable.get("checkpoint_ns", "")),
                "checkpoint_id": str(configurable["checkpoint_id"]),
                "task_id": task_id,
                "idx": WRITES_IDX_MAP.get(channel, index),
                "task_path": task_path,
                "channel": channel,
                "type": type_,
                "value": serialized,
            })
        with self.engine.begin() as connection:
            for row in rows:
                # Special LangGraph writes replace a previous attempt. Ordinary
                # writes are immutable and duplicates are ignored.
                if row["channel"] in WRITES_IDX_MAP:
                    self._upsert(connection, self.writes, row)
                    continue
                dialect = self.engine.dialect.name
                if dialect == "mysql":
                    connection.execute(
                        mysql_insert(self.writes).values(**row).prefix_with("IGNORE")
                    )
                elif dialect == "sqlite":
                    connection.execute(
                        sqlite_insert(self.writes)
                        .values(**row)
                        .on_conflict_do_nothing(
                            index_elements=[
                                column.name for column in self.writes.primary_key.columns
                            ]
                        )
                    )
                else:
                    connection.execute(self.writes.insert().values(**row))

    def delete_thread(self, thread_id: str) -> None:
        self.setup()
        with self.engine.begin() as connection:
            connection.execute(
                delete(self.writes).where(self.writes.c.thread_id == str(thread_id))
            )
            connection.execute(
                delete(self.checkpoints).where(
                    self.checkpoints.c.thread_id == str(thread_id)
                )
            )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        items = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for item in items:
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(
            self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)


def create_checkpointer() -> tuple[BaseCheckpointSaver[Any] | None, str]:
    """Build the configured saver and return it with a diagnostic backend name."""
    if not settings.langgraph_checkpoint_enabled:
        return None, "disabled"

    backend = settings.langgraph_checkpoint_backend.strip().lower()
    if backend == "sqlite":
        checkpoint_path = Path(settings.langgraph_checkpoint_path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
        return SqliteSaver(connection), "sqlite"
    if backend == "mysql":
        saver = SQLAlchemyCheckpointSaver.from_url(
            settings.langgraph_checkpoint_database_url or settings.mysql_url
        )
        # Production configuration fails fast instead of silently degrading to
        # a local file and splitting one interview across incompatible states.
        saver.setup()
        return saver, "mysql"
    raise ValueError(
        "LANGGRAPH_CHECKPOINT_BACKEND must be one of: sqlite, mysql"
    )
