from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from logging import getLogger
from select import select
from typing import (
    Any,
    DefaultDict,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Set,
    TypedDict,
)

import dlt
from dlt.common.pendulum import pendulum
from dlt.common.schema.typing import TTableSchema, TTableSchemaColumns
from dlt.common.schema.utils import merge_column
from dlt.common.typing import TDataItem
from dlt.extract.items import DataItemWithMeta
from dlt.sources.credentials import ConnectionStringCredentials
from dlt.sources.sql_database import ReflectionLevel, TableBackend
from dlt.sources.sql_database import arrow_helpers as arrow
from psycopg2.extras import ReplicationCursor, ReplicationMessage

from .exceptions import NoMessageException

log = getLogger(__name__)


class ReplicationOptions(TypedDict, total=False):
    backend: Optional[TableBackend]
    backend_kwargs: Optional[Mapping[str, Any]]
    column_hints: Optional[TTableSchemaColumns]
    include_lsn: Optional[bool]  # Default is true
    include_deleted_ts: Optional[bool]  # Default is true
    include_commit_ts: Optional[bool]
    include_tx_id: Optional[bool]
    included_columns: Optional[Set[str]]
    reflection_level: Optional[ReflectionLevel]


class TableItems(NamedTuple):
    schema: TTableSchema
    items: List[TDataItem]


class MessageConsumer(ABC):
    def __init__(
        self,
        credentials: ConnectionStringCredentials,
        table_qnames: Set[str],
        repl_options: DefaultDict[str, ReplicationOptions],
        target_batch_size: int = 1000,
    ):
        self.credentials = credentials
        self.table_qnames = table_qnames
        self.target_batch_size = target_batch_size
        self.repl_options = repl_options

        # maps table names to list of data items
        self.data_items: Dict[str, List[TDataItem]] = defaultdict(list)
        # maps table names to table schema
        self.last_table_schema: Dict[str, TTableSchema] = {}
        # maps table names to new_typeinfo hashes
        self.last_table_hashes: Dict[str, int] = {}
        self.last_commit_lsn: int

    @abstractmethod
    def read_wal(
        self, slot_name: str, start_lsn: int, upto_lsn: int
    ) -> Iterator[TableItems]:
        ...

    def flush_batch(
        self, cur: ReplicationCursor, write_lsn: int
    ) -> Iterator[TableItems]:
        for table_name, data_items in self.data_items.items():
            log.debug("Flushing %s events for table '%s'", len(data_items), table_name)
            yield TableItems(self.last_table_schema[table_name], data_items)
        self.clear_state()
        cur.send_feedback(write_lsn=write_lsn, reply=True, force=True)

    def clear_state(self, *, with_schemas: bool = False) -> None:
        self.data_items.clear()
        if with_schemas:
            self.last_table_schema.clear()
            self.last_table_hashes.clear()


def read_message(
    cursor: ReplicationCursor,
    *,
    status_interval: float = 10,
    max_retries: int = 10,
) -> ReplicationMessage:
    for attempt in range(max_retries):
        msg = cursor.read_message()
        if msg is not None:
            return msg  # type: ignore[no-any-return]

        now_ts = pendulum.now().timestamp()
        last_feedback_ts = cursor.feedback_timestamp.timestamp()
        timeout = max(0, status_interval - (now_ts - last_feedback_ts))

        log_fn = log.warning if attempt > 2 else log.debug
        log_fn(
            "Waiting for input (max %.1fs), attempt %d/%d",
            timeout,
            attempt + 1,
            max_retries,
        )
        select([cursor], [], [], timeout)

    raise NoMessageException()


def add_replication_columns(
    columns: TTableSchemaColumns,
    *,
    include_lsn: bool = True,
    include_deleted_ts: bool = True,
    include_commit_ts: bool = False,
    include_tx_id: bool = False,
    **_: Any,
) -> TTableSchemaColumns:
    if include_lsn:
        columns["_pg_lsn"] = {
            "data_type": "bigint",
            "name": "_pg_lsn",
            "nullable": True,
        }
    if include_deleted_ts:
        columns["_pg_deleted_ts"] = {
            "data_type": "timestamp",
            "name": "_pg_deleted_ts",
            "nullable": True,
        }
    if include_commit_ts:
        columns["_pg_commit_ts"] = {
            "data_type": "timestamp",
            "name": "_pg_commit_ts",
            "nullable": True,
        }
    if include_tx_id:
        columns["_pg_tx_id"] = {
            "data_type": "bigint",
            "name": "_pg_tx_id",
            "nullable": True,
            "precision": 32,
        }
    return columns


@dataclass
class BackendHandler:
    """
    Consumes messages from ItemGenerator once a batch is ready for emitting.

    It is mainly responsible for emitting schema and dict data times or transforming
    into arrow tables.
    """

    table: str
    repl_options: ReplicationOptions

    def __call__(self, table_items: TableItems) -> Iterable[DataItemWithMeta]:
        if table_items.schema["name"] != self.table:
            return

        # Apply column hints if provided
        columns = table_items.schema["columns"]
        if column_hints := self.repl_options.get("column_hints"):
            for col_name, col_hint in column_hints.items():
                if col_name in columns:
                    columns[col_name] = merge_column(columns[col_name], col_hint)

        # Process based on backend
        data = table_items.items
        backend = self.repl_options.get("backend", "sqlalchemy")
        try:
            if backend == "sqlalchemy":
                yield from self.emit_schema_and_items(columns, data)
            elif backend == "pyarrow":
                yield from self.emit_arrow_table(columns, data)
            else:
                raise NotImplementedError(f"Unsupported backend: {backend}")
        except Exception:
            log.error(
                "A fatal error occurred while processing batch for '%s' (columns=%s, data=%s)",
                self.table,
                columns,
                data,
            )
            raise

    def emit_schema_and_items(
        self, columns: TTableSchemaColumns, items: List[TDataItem]
    ) -> Iterator[DataItemWithMeta]:
        yield dlt.mark.with_hints(
            [],
            dlt.mark.make_hints(table_name=self.table, columns=columns),
            create_table_variant=True,
        )
        yield dlt.mark.with_table_name(items, self.table)

    def emit_arrow_table(
        self, columns: TTableSchemaColumns, items: List[TDataItem]
    ) -> Iterator[DataItemWithMeta]:
        # Create rows for pyarrow using ordered column keys
        rows = [
            tuple(item.get(column, None) for column in list(columns.keys()))
            for item in items
        ]
        tz = self.repl_options.get("backend_kwargs", {}).get("tz", "UTC")
        yield dlt.mark.with_table_name(
            arrow.row_tuples_to_arrow(rows, columns=columns, tz=tz),
            self.table,
        )
