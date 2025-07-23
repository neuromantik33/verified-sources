from abc import ABC, abstractmethod
from collections import defaultdict
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import partial
from logging import getLogger
from select import select
from typing import (
    Any,
    Callable,
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
import psycopg2
from dlt.common.libs.sql_alchemy import Engine, MetaData, Table, sa
from dlt.common.pendulum import pendulum
from dlt.common.schema.typing import TColumnSchema, TTableSchema, TTableSchemaColumns
from dlt.common.schema.utils import merge_column
from dlt.common.typing import TDataItem
from dlt.extract import DltSource
from dlt.extract.items import DataItemWithMeta
from dlt.sources.credentials import ConnectionStringCredentials
from dlt.sources.sql_database import (
    ReflectionLevel,
    TableBackend,
    TQueryAdapter,
    TTypeAdapter,
    arrow_helpers as arrow,
    engine_from_credentials,
)
from psycopg2.extensions import connection as ConnectionExt, cursor, quote_ident
from psycopg2.extras import (
    LogicalReplicationConnection,
    ReplicationCursor,
    ReplicationMessage,
)

from .exceptions import NoMessageException

log = getLogger(__name__)


class SqlTableOptions(TypedDict, total=False):
    backend: TableBackend
    backend_kwargs: Optional[Dict[str, Any]]
    chunk_size: int
    defer_table_reflect: Optional[bool]
    detect_precision_hints: Optional[bool]
    included_columns: Optional[List[str]]
    metadata: Optional[MetaData]
    query_adapter_callback: Optional[TQueryAdapter]
    reflection_level: Optional[ReflectionLevel]
    table_adapter_callback: Optional[Callable[[Table], None]]
    type_adapter_callback: Optional[TTypeAdapter]


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


def configure_engine(
    credentials: ConnectionStringCredentials,
    rep_conn: LogicalReplicationConnection,
    snapshot_name: Optional[str],
) -> Engine:
    """
    Configures the SQLAlchemy engine.
    Also attaches the replication connection in order to prevent it being garbage collected and closed.

    Args:
        snapshot_name (str, optional): This is used during the initial first table snapshot allowing
            all transactions to run with the same consistent snapshot.
    """
    engine: Engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True, max_row_buffer=2 * 50000)
    setattr(engine, "rep_conn", rep_conn)  # noqa

    @sa.event.listens_for(engine, "begin")
    def on_begin(conn: sa.Connection) -> None:
        cur = conn.connection.cursor()
        if snapshot_name is None:
            # Using the same isolation level that pg_backup uses
            cur.execute(
                "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ ONLY, DEFERRABLE;"
            )
        else:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;")
            cur.execute("SET TRANSACTION SNAPSHOT %s;", (snapshot_name,))

    @sa.event.listens_for(engine, "engine_disposed")
    def on_engine_disposed(e: Engine) -> None:
        delattr(e, "rep_conn")

    return engine


def cleanup_snapshot_resources(snapshots: DltSource) -> None:
    """FIXME Awful hack to release the underlying SQL engine when snapshotting tables"""
    resources = snapshots.resources
    if resources:
        engine: Engine = next(iter(resources.values()))._explicit_args["credentials"]
        engine.dispose()


def get_pg_version(cur: cursor) -> int:
    """Returns Postgres server version as int."""
    return cur.connection.server_version


def create_replication_slot(  # type: ignore[return]
    slot_name: str, cur: ReplicationCursor, output_plugin: str
) -> Optional[Dict[str, str]]:
    """Creates a replication slot if it doesn't exist yet."""

    # FIXME Why?
    def _create_slot() -> None:
        command = f"CREATE_REPLICATION_SLOT {quote_ident(slot_name, cur)} LOGICAL {quote_ident(output_plugin, cur)}"
        log.info("Executing '%s'", command)
        cur.execute(command)

    try:
        # cur.create_replication_slot(name, output_plugin=output_plugin)
        _create_slot()
        log.debug(
            "Successfully created replication slot '%s' (%s)", slot_name, output_plugin
        )
        result = cur.fetchone()
        return {
            "slot_name": result[0],
            "consistent_point": result[1],
            "snapshot_name": result[2],
            "output_plugin": result[3],
        }
    except psycopg2.errors.DuplicateObject:  # the replication slot already exists
        log.info(
            "Replication slot '%s' cannot be created because it already exists",
            slot_name,
        )


def drop_replication_slot(slot_name: str, cur: ReplicationCursor) -> None:
    """Drops a replication slot if it exists."""
    try:
        cur.drop_replication_slot(slot_name)
        log.info("Successfully dropped replication slot '%s'", slot_name)
    except psycopg2.errors.UndefinedObject:  # the replication slot does not exist
        log.info(
            "Replication slot '%s' cannot be dropped because it does not exist",
            slot_name,
        )


def get_max_lsn(
    credentials: ConnectionStringCredentials, slot_name: str
) -> Optional[int]:
    """
    Returns maximum Log Sequence Number (LSN).

    Returns None if the replication slot is empty.
    Does not consume the slot, i.e. messages are not flushed.
    """
    with get_cursor(credentials) as cur:
        pg_version = get_pg_version(cur)
        lsn_field = "lsn" if pg_version >= 100000 else "location"
        # subtract '0/0' to convert pg_lsn type to int (https://stackoverflow.com/a/73738472)
        cur.execute(
            f"""
            SELECT {lsn_field} - '0/0' AS max_lsn
            FROM pg_logical_slot_peek_binary_changes(%s, NULL, NULL)
            ORDER BY {lsn_field} DESC
            LIMIT 1;
            """,
            (slot_name,),
        )
        row = cur.fetchone()
        return row[0] if row else None  # type: ignore[no-any-return]


def lsn_int_to_hex(lsn: int) -> str:
    """Convert integer LSN to postgres hexadecimal representation."""
    # https://stackoverflow.com/questions/66797767/lsn-external-representation.
    return f"{lsn >> 32 & 4294967295:X}/{lsn & 4294967295:08X}"


def advance_slot(
    upto_lsn: int,
    slot_name: str,
    credentials: ConnectionStringCredentials,
) -> None:
    """
    Advances position in the replication slot.

    Flushes all messages upto (and including) the message with LSN = `upto_lsn`.
    This function is used as alternative to psycopg2's `send_feedback` method, because
    the behavior of that method seems odd when used outside of `consume_stream`.
    """
    assert upto_lsn > 0
    with get_cursor(credentials) as cur:
        # There is unfortunately no way in pg9.6 to manually advance the replication slot
        if get_pg_version(cur) > 100000:
            cur.execute(
                "select * from pg_replication_slot_advance(%s, %s);",
                (slot_name, lsn_int_to_hex(upto_lsn)),
            )


@contextmanager
def get_cursor(credentials: ConnectionStringCredentials) -> Iterator[cursor]:
    """Returns a psycopg2 cursor to interact with postgres."""
    with closing(_get_conn(credentials)) as conn:
        with conn.cursor() as cur:
            yield cur


def get_rep_conn(
    credentials: ConnectionStringCredentials,
) -> LogicalReplicationConnection:
    """
    Returns a psycopg2 LogicalReplicationConnection to interact with postgres replication functionality.

    Raises error if the user does not have the REPLICATION attribute assigned.
    """
    return _get_conn(credentials, LogicalReplicationConnection)  # type: ignore[return-value]


def _get_conn(
    credentials: ConnectionStringCredentials,
    connection_factory: Optional[Any] = None,
) -> ConnectionExt:
    """Returns a psycopg2 connection to interact with postgres."""
    return psycopg2.connect(  # type: ignore[no-any-return]
        database=credentials.database,
        user=credentials.username,
        password=credentials.password,
        host=credentials.host,
        port=credentials.port,
        connection_factory=connection_factory,
        **({} if credentials.query is None else credentials.query),
    )


# Helper classes
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

        # maps table qnames to list of data items
        self.data_items: Dict[str, List[TDataItem]] = defaultdict(list)
        # maps table qname to table schema
        self.last_table_schema: Dict[str, TTableSchema] = {}
        # maps table qnames to new_typeinfo hashes
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
        for table, data_items in self.data_items.items():
            log.debug("Flushing %s events for table '%s'", len(data_items), table)
            yield TableItems(self.last_table_schema[table], data_items)
        self.clear_state()
        cur.send_feedback(write_lsn=write_lsn, reply=True, force=True)

    def clear_state(self, *, with_schemas: bool = False) -> None:
        self.data_items.clear()
        if with_schemas:
            self.last_table_schema.clear()
            self.last_table_hashes.clear()


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


def epoch_micros_to_datetime(microseconds_since_1970: int) -> pendulum.DateTime:
    return pendulum.from_timestamp(microseconds_since_1970 / 1_000_000)


def microseconds_to_time(microseconds: int) -> pendulum.Time:
    return pendulum.Time().add(microseconds=microseconds)


def epoch_days_to_date(epoch_days: int) -> pendulum.Date:
    return pendulum.Date(1970, 1, 1).add(days=epoch_days)


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


ALLOWED_COL_SCHEMA_FIELDS: Set[str] = {
    "name",
    "data_type",
    "nullable",
    "precision",
    "scale",
    "timezone",
}


def compare_schemas(last: TTableSchema, new: TTableSchema) -> TTableSchema:
    """
    Compares the last schema with the new one and chooses the more
    precise one if they are relatively equal or else raises a
    AssertionError due to an incompatible schema change
    """
    assert last["name"] == new["name"], "Table names do not match"

    table_schema = TTableSchema(name=last["name"], columns={})
    last_cols, new_cols = last["columns"], new["columns"]
    assert len(last_cols) == len(
        new_cols
    ), f"Columns mismatch last:{last_cols} new:{new_cols}"

    for name, s1 in last_cols.items():
        s2 = new_cols.get(name)
        assert (
            s2 and s1["data_type"] == s2["data_type"]
        ), f"Incompatible schema for column '{name}'"

        # Ensure new has no fields outside allowed fields
        extra_fields = set(s2.keys()) - ALLOWED_COL_SCHEMA_FIELDS
        assert not extra_fields, f"Unexpected fields {extra_fields} in column '{name}'"

        # Select the more precise schema by comparing nullable, precision, and scale
        col_schema = TColumnSchema(name=name, data_type=s1["data_type"])
        if "nullable" in s1 or "nullable" in s2:
            # Get nullable values (could be True, False, or None)
            s1_null = s1.get("nullable")
            s2_null = s2.get("nullable")
            if s1_null is not None and s2_null is not None:
                col_schema["nullable"] = s1_null or s2_null  # Default is True
            else:
                col_schema["nullable"] = s1_null if s1_null is not None else s2_null
        if "precision" in s1 or "precision" in s2:
            col_schema["precision"] = s1.get("precision", s2.get("precision"))
        if "scale" in s1 or "scale" in s2:
            col_schema["scale"] = s1.get("scale", s2.get("scale"))
        if "timezone" in s1 or "timezone" in s2:
            col_schema["timezone"] = s1.get("timezone", s2.get("timezone"))

        # Update with the more detailed schema per column
        table_schema["columns"][name] = col_schema

    return table_schema


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
