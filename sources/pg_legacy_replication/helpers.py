import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from functools import partial
from typing import (
    Optional,
    Dict,
    Set,
    Iterator,
    Union,
    List,
    Sequence,
    Any,
    Iterable,
    Callable,
    NamedTuple,
    TypedDict,
)

import dlt
import psycopg2
from dlt.common import logger
from dlt.common.pendulum import pendulum
from dlt.common.schema.typing import TColumnSchema, TTableSchema, TTableSchemaColumns
from dlt.common.schema.utils import merge_column
from dlt.common.typing import TDataItem
from dlt.extract import DltSource, DltResource
from dlt.extract.items import DataItemWithMeta
from dlt.sources.credentials import ConnectionStringCredentials
from psycopg2.extensions import cursor, connection as ConnectionExt
from psycopg2.extras import (
    LogicalReplicationConnection,
    ReplicationCursor,
    ReplicationMessage,
    StopReplication,
)

# Favoring 1.x over 0.5.x imports
try:
    from dlt.common.libs.sql_alchemy import Engine, Table, MetaData  # type: ignore[attr-defined]
except ImportError:
    from sqlalchemy import Engine, Table, MetaData
from sqlalchemy import Connection as ConnectionSqla, event

try:
    from dlt.sources.sql_database import (  # type: ignore[import-not-found]
        sql_table,
        engine_from_credentials,
        TQueryAdapter,
        TTypeAdapter,
        ReflectionLevel,
        TableBackend,
        arrow_helpers as arrow,
    )
except ImportError:
    from ..sql_database import (  # type: ignore[import-untyped]
        sql_table,
        engine_from_credentials,
        TQueryAdapter,
        TTypeAdapter,
        ReflectionLevel,
        TableBackend,
        arrow_helpers as arrow,
    )

from .pg_logicaldec_pb2 import Op, RowMessage, TypeInfo
from .schema_types import _epoch_micros_to_datetime, _to_dlt_column_schema, _to_dlt_val


class SqlTableOptions(TypedDict, total=False):
    # Used by both sql_table and replication resources
    included_columns: Optional[Sequence[str]]
    # Used only by sql_table resource
    metadata: Optional[MetaData]
    chunk_size: Optional[int]
    backend: Optional[TableBackend]
    detect_precision_hints: Optional[bool]
    reflection_level: Optional[ReflectionLevel]
    defer_table_reflect: Optional[bool]
    table_adapter_callback: Optional[Callable[[Table], None]]
    backend_kwargs: Optional[Dict[str, Any]]
    type_adapter_callback: Optional[TTypeAdapter]
    query_adapter_callback: Optional[TQueryAdapter]


@dlt.sources.config.with_config(sections=("sources", "pg_legacy_replication"))
@dlt.source
def init_replication(
    slot_name: str,
    schema: str,
    table_names: Optional[Union[str, Sequence[str]]] = None,
    credentials: ConnectionStringCredentials = dlt.secrets.value,
    take_snapshots: bool = False,
    table_options: Optional[Dict[str, SqlTableOptions]] = None,
    reset: bool = False,
) -> Iterable[DltResource]:
    """Initializes replication for one, several, or all tables within a schema.

    Can be called repeatedly with the same `slot_name`:
    - creates a replication slot and publication with provided names if they do not exist yet
    - skips creation of slot and publication if they already exist (unless`reset` is set to `False`)
    - supports addition of new tables by extending `table_names`
    - removing tables is not supported, i.e. exluding a table from `table_names`
      will not remove it from the publication
    - switching from a table selection to an entire schema is possible by omitting
      the `table_names` argument
    - changing `publish` has no effect (altering the published DML operations is not supported)
    - table snapshots can only be persisted on the first call (because the snapshot
      is exported when the slot is created)

    Args:
        slot_name (str): Name of the replication slot to create if it does not exist yet.
        schema (str): Name of the schema to replicate tables from.
        table_names (Optional[Union[str, Sequence[str]]]):  Name(s) of the table(s)
          to include in the publication. If not provided, all tables in the schema
          are included.
        credentials (ConnectionStringCredentials): Postgres database credentials.
        take_snapshots (bool): Whether the table states in the snapshot exported
          during replication slot creation are persisted to tables. If true, a
          snapshot table is created in Postgres for all included tables, and corresponding
          resources (`DltResource` objects) for these tables are created and returned.
          The resources can be used to perform an initial load of all data present
          in the tables at the moment the replication slot got created.
        included_columns (Optional[Dict[str, Sequence[str]]]): Maps table name(s) to
          sequence of names of columns to include in the snapshot table(s).
          Any column not in the sequence is excluded. If not provided, all columns
          are included. For example:
          ```
          included_columns={
              "table_x": ["col_a", "col_c"],
              "table_y": ["col_x", "col_y", "col_z"],
          }
          ```
          Argument is only used if `take_snapshots` is `True`.
          ```
          Argument is only used if `take_snapshots` is `True`.
        reset (bool): If set to True, the existing slot and publication are dropped
          and recreated. Has no effect if a slot and publication with the provided
          names do not yet exist.

    Returns:
        - None if `take_snapshots` is `False`
        - a `DltResource` object or a list of `DltResource` objects for the snapshot
          table(s) if `take_snapshots` is `True` and the replication slot did not yet exist
    """
    rep_conn = _get_rep_conn(credentials)
    rep_cur = rep_conn.cursor()
    if reset:
        drop_replication_slot(slot_name, rep_cur)
    slot = create_replication_slot(slot_name, rep_cur)

    # Close connection if no snapshots are needed
    if not take_snapshots:
        rep_conn.close()
        return

    assert table_names is not None

    engine = _configure_engine(credentials, rep_conn)

    @event.listens_for(engine, "begin")
    def on_begin(conn: ConnectionSqla) -> None:
        cur = conn.connection.cursor()
        if slot is None:
            # Using the same isolation level that pg_backup uses
            cur.execute(
                "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ ONLY, DEFERRABLE"
            )
        else:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute(f"SET TRANSACTION SNAPSHOT '{slot['snapshot_name']}'")

    table_names = [table_names] if isinstance(table_names, str) else table_names or []

    for table in table_names:
        table_args = (
            table_options[table] if table_options and table in table_options else {}
        )
        yield sql_table(credentials=engine, table=table, schema=schema, **table_args)


def _configure_engine(
    credentials: ConnectionStringCredentials, rep_conn: LogicalReplicationConnection
) -> Engine:
    """
    Configures the SQLAlchemy engine.
    Also attaches the replication connection in order to prevent it being garbage collected and closed.
    """
    engine: Engine = engine_from_credentials(credentials, may_dispose_after_use=False)
    engine.execution_options(stream_results=True, max_row_buffer=2 * 50000)
    setattr(engine, "rep_conn", rep_conn)  # noqa

    @event.listens_for(engine, "engine_disposed")
    def on_engine_disposed(engine: Engine) -> None:
        delattr(engine, "rep_conn")

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
    name: str, cur: ReplicationCursor, output_plugin: str = "decoderbufs"
) -> Optional[Dict[str, str]]:
    """Creates a replication slot if it doesn't exist yet."""
    try:
        cur.create_replication_slot(name, output_plugin=output_plugin)
        logger.info("Successfully created replication slot '%s'", name)
        result = cur.fetchone()
        return {
            "slot_name": result[0],
            "consistent_point": result[1],
            "snapshot_name": result[2],
            "output_plugin": result[3],
        }
    except psycopg2.errors.DuplicateObject:  # the replication slot already exists
        logger.info(
            "Replication slot '%s' cannot be created because it already exists", name
        )


def drop_replication_slot(name: str, cur: ReplicationCursor) -> None:
    """Drops a replication slot if it exists."""
    try:
        cur.drop_replication_slot(name)
        logger.info("Successfully dropped replication slot '%s'", name)
    except psycopg2.errors.UndefinedObject:  # the replication slot does not exist
        logger.info(
            "Replication slot '%s' cannot be dropped because it does not exist", name
        )


def get_max_lsn(
    slot_name: str,
    credentials: ConnectionStringCredentials,
) -> Optional[int]:
    """Returns maximum Log Sequence Number (LSN) in replication slot.

    Returns None if the replication slot is empty.
    Does not consume the slot, i.e. messages are not flushed.
    Raises error if the replication slot or publication does not exist.
    """
    cur = _get_conn(credentials).cursor()
    lsn_field = "location" if get_pg_version(cur) < 100000 else "lsn"
    cur.execute(
        f"SELECT MAX({lsn_field} - '0/0') AS max_lsn "  # subtract '0/0' to convert pg_lsn type to int (https://stackoverflow.com/a/73738472)
        f"FROM pg_logical_slot_peek_binary_changes('{slot_name}', NULL, NULL);"
    )
    lsn: int = cur.fetchone()[0]
    cur.connection.close()
    return lsn


def lsn_int_to_hex(lsn: int) -> str:
    """Convert integer LSN to postgres' hexadecimal representation."""
    # https://stackoverflow.com/questions/66797767/lsn-external-representation.
    return f"{lsn >> 32 & 4294967295:X}/{lsn & 4294967295:08X}"


def advance_slot(
    upto_lsn: int,
    slot_name: str,
    credentials: ConnectionStringCredentials,
) -> None:
    """Advances position in the replication slot.

    Flushes all messages upto (and including) the message with LSN = `upto_lsn`.
    This function is used as alternative to psycopg2's `send_feedback` method, because
    the behavior of that method seems odd when used outside of `consume_stream`.
    """
    if upto_lsn != 0:
        cur = _get_conn(credentials).cursor()
        if get_pg_version(cur) > 100000:
            cur.execute(
                f"SELECT * FROM pg_replication_slot_advance('{slot_name}', '{lsn_int_to_hex(upto_lsn)}');"
            )
        cur.connection.close()


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


def _get_rep_conn(
    credentials: ConnectionStringCredentials,
) -> LogicalReplicationConnection:
    """Returns a psycopg2 LogicalReplicationConnection to interact with postgres replication functionality.

    Raises error if the user does not have the REPLICATION attribute assigned.
    """
    return _get_conn(credentials, LogicalReplicationConnection)  # type: ignore[return-value]


class MessageConsumer:
    """Consumes messages from a ReplicationCursor sequentially.

    Generates data item for each `insert`, `update`, and `delete` message.
    Processes in batches to limit memory usage.
    Maintains message data needed by subsequent messages in internal state.
    """

    def __init__(
        self,
        upto_lsn: int,
        table_qnames: Set[str],
        target_batch_size: int = 1000,
        table_options: Optional[Dict[str, SqlTableOptions]] = None,
    ) -> None:
        self.upto_lsn = upto_lsn
        self.table_qnames = table_qnames
        self.target_batch_size = target_batch_size
        self.included_columns = {
            table: set(options["included_columns"])
            for table, options in (table_options or {}).items()
            if options.get("included_columns")
        }

        self.consumed_all: bool = False
        # maps table names to list of data items
        self.data_items: Dict[str, List[TDataItem]] = defaultdict(list)
        # maps table name to table schema
        self.last_table_schema: Dict[str, TTableSchema] = {}
        # maps table names to new_typeinfo hashes
        self.last_table_hashes: Dict[str, int] = {}
        self.last_commit_ts: pendulum.DateTime
        self.last_commit_lsn: int

    def __call__(self, msg: ReplicationMessage) -> None:
        """Processes message received from stream."""
        self.process_msg(msg)

    def process_msg(self, msg: ReplicationMessage) -> None:
        """Processes encoded replication message.

        Identifies message type and decodes accordingly.
        Message treatment is different for various message types.
        Breaks out of stream with StopReplication exception when
        - `upto_lsn` is reached
        - `target_batch_size` is reached
        - a table's schema has changed
        """
        row_msg = RowMessage()
        try:
            row_msg.ParseFromString(msg.payload)
            if row_msg.op == Op.UNKNOWN:
                raise AssertionError(f"Unsupported operation : {row_msg}")

            if row_msg.op == Op.BEGIN:
                self.last_commit_ts = _epoch_micros_to_datetime(row_msg.commit_time)
            elif row_msg.op == Op.COMMIT:
                self.process_commit(msg.data_start)
            else:  # INSERT, UPDATE or DELETE
                self.process_change(row_msg, msg.data_start)
        except StopReplication:
            raise
        except Exception:
            logger.error(
                "A fatal error occurred while processing a message: %s", row_msg
            )
            raise

    def process_commit(self, lsn: int) -> None:
        """Updates object state when Commit message is observed.

        Raises StopReplication when `upto_lsn` or `target_batch_size` is reached.
        """
        self.last_commit_lsn = lsn
        if lsn >= self.upto_lsn:
            self.consumed_all = True
        n_items = sum(
            [len(items) for items in self.data_items.values()]
        )  # combine items for all tables
        if self.consumed_all or n_items >= self.target_batch_size:
            raise StopReplication

    def process_change(self, msg: RowMessage, lsn: int) -> None:
        """Processes replication message of type Insert, Update or Delete"""
        if msg.table not in self.table_qnames:
            return
        table_name = msg.table.split(".")[1]
        table_schema = self.get_table_schema(msg, table_name)
        data_item = gen_data_item(
            msg, table_schema["columns"], self.included_columns.get(table_name)
        )
        data_item["lsn"] = lsn
        self.data_items[table_name].append(data_item)

    def get_table_schema(self, msg: RowMessage, table_name: str) -> TTableSchema:
        last_schema = self.last_table_schema.get(table_name)
        included_columns = self.included_columns.get(table_name)

        # Used cached schema if the operation is a delete since the inferred one will always be less precise
        if msg.op == Op.DELETE and last_schema:
            return last_schema

        # Return cached schema if hash matches
        current_hash = hash_typeinfo(msg.new_typeinfo)
        if current_hash == self.last_table_hashes.get(table_name):
            return self.last_table_schema[table_name]

        new_schema = infer_table_schema(msg, included_columns)
        if last_schema is None:
            # Cache the inferred schema and hash if it is not already cached
            self.last_table_schema[table_name] = new_schema
            self.last_table_hashes[table_name] = current_hash
        else:
            try:
                retained_schema = compare_schemas(last_schema, new_schema)
                self.last_table_schema[table_name] = retained_schema
            except AssertionError as e:
                logger.warning(str(e))
                raise StopReplication

        return new_schema


def hash_typeinfo(new_typeinfo: Sequence[TypeInfo]) -> int:
    """Generate a hash for the entire new_typeinfo list by hashing each TypeInfo message."""
    typeinfo_tuple = tuple(
        (info.modifier, info.value_optional) for info in new_typeinfo
    )
    hash_obj = hashlib.blake2b(repr(typeinfo_tuple).encode(), digest_size=8)
    return int(hash_obj.hexdigest(), 16)


class TableItems(NamedTuple):
    table: str
    schema: TTableSchema
    items: List[TDataItem]


@dataclass
class ItemGenerator:
    credentials: ConnectionStringCredentials
    slot_name: str
    table_qnames: Set[str]
    upto_lsn: int
    start_lsn: int = 0
    target_batch_size: int = 1000
    table_options: Optional[Dict[str, SqlTableOptions]] = None
    last_commit_lsn: Optional[int] = field(default=None, init=False)
    generated_all: bool = False

    def __iter__(self) -> Iterator[TableItems]:
        """Yields replication messages from MessageConsumer.

        Starts replication of messages published by the publication from the replication slot.
        Maintains LSN of last consumed Commit message in object state.
        Does not advance the slot.
        """
        cur = _get_rep_conn(self.credentials).cursor()
        ack_lsn = partial(cur.send_feedback, reply=True, force=True)
        cur.start_replication(
            slot_name=self.slot_name, start_lsn=self.start_lsn, decode=False
        )
        consumer = MessageConsumer(
            upto_lsn=self.upto_lsn,
            table_qnames=self.table_qnames,
            target_batch_size=self.target_batch_size,
            table_options=self.table_options,
        )
        try:
            cur.consume_stream(consumer)
        except StopReplication:  # completed batch or reached `upto_lsn`
            pass
        finally:
            last_commit_lsn = consumer.last_commit_lsn
            ack_lsn(write_lsn=last_commit_lsn)
            for table, data_items in consumer.data_items.items():
                yield TableItems(
                    table, consumer.last_table_schema.get(table), data_items
                )
            # Update state after flush
            self.last_commit_lsn = last_commit_lsn
            self.generated_all = consumer.consumed_all
            ack_lsn(flush_lsn=last_commit_lsn)
            cur.connection.close()


def create_table_dispatch(
    table: str,
    column_hints: Optional[TTableSchemaColumns] = None,
    table_options: Optional[SqlTableOptions] = None,
) -> Callable[[TableItems], Iterable[DataItemWithMeta]]:
    """Creates a dispatch handler that processes data items based on a specified table and optional column hints."""

    def handle(table_items: TableItems) -> Iterable[DataItemWithMeta]:
        if table_items.table != table:
            return
        columns = table_items.schema["columns"]
        if column_hints:
            for col_name, col_hint in column_hints.items():
                columns[col_name] = merge_column(columns.get(col_name, {}), col_hint)
        backend = (
            table_options.get("backend", "sqlalchemy")
            if table_options
            else "sqlalchemy"
        )
        if backend == "sqlalchemy":
            yield dlt.mark.with_hints(
                [],
                dlt.mark.make_hints(table_name=table, columns=columns),
                create_table_variant=True,
            )
            yield dlt.mark.with_table_name(table_items.items, table)
        elif backend == "pyarrow":
            backend_kwargs = table_options.get("backend_kwargs", {})
            ordered_keys = list(columns.keys())
            rows = [
                tuple(data_item.get(column, None) for column in ordered_keys)
                for data_item in table_items.items
            ]
            arrow_table = arrow.row_tuples_to_arrow(
                rows, columns, tz=backend_kwargs.get("tz", "UTC")
            )
            yield dlt.mark.with_table_name(arrow_table, table)
        else:
            raise NotImplementedError(f"Unsupported backend: {backend}")

    return handle


def infer_table_schema(
    msg: RowMessage, included_columns: Optional[Set[str]] = None
) -> TTableSchema:
    """Infers the table schema from the replication message and optional hints"""
    # Choose the correct source based on operation type
    is_change = msg.op != Op.DELETE
    tuples = msg.new_tuple if is_change else msg.old_tuple

    # Filter and map columns, conditionally using `new_typeinfo` when available
    columns: TTableSchemaColumns = {
        col.column_name: _to_dlt_column_schema(
            col, msg.new_typeinfo[i] if is_change else None
        )
        for i, col in enumerate(tuples)
        if not included_columns or col.column_name in included_columns
    }

    # Add replication columns
    columns["lsn"] = {"data_type": "bigint", "name": "lsn", "nullable": True}
    columns["deleted_ts"] = {
        "data_type": "timestamp",
        "name": "deleted_ts",
        "nullable": True,
    }

    return {
        "name": (msg.table.split(".")[1]),
        "columns": columns,
    }


def gen_data_item(
    msg: RowMessage,
    column_schema: TTableSchemaColumns,
    included_columns: Optional[Set[str]] = None,
) -> TDataItem:
    """Generates data item from a row message and corresponding metadata."""
    data_item: TDataItem = {}
    if msg.op != Op.DELETE:
        row = msg.new_tuple
    else:
        row = msg.old_tuple
        data_item["deleted_ts"] = _epoch_micros_to_datetime(msg.commit_time)

    for data in row:
        col_name = data.column_name
        if included_columns and col_name not in included_columns:
            continue
        data_item[col_name] = _to_dlt_val(
            data,
            column_schema[col_name]["data_type"],
            for_delete=msg.op == Op.DELETE,
        )

    return data_item


ALLOWED_COL_SCHEMA_FIELDS: Set[str] = {
    "name",
    "data_type",
    "nullable",
    "precision",
    "scale",
}


def compare_schemas(last: TTableSchema, new: TTableSchema) -> TTableSchema:
    """Compares the last schema with the new one and chooses the more
    precise one if they are relatively equal or else raises a
    AssertionError due to an incompatible schema change"""
    table_name = last["name"]
    assert table_name == new["name"], "Table names do not match"

    table_schema: TTableSchema = {"name": table_name, "columns": {}}

    for name, s1 in last["columns"].items():
        s2 = new["columns"].get(name)
        assert (
            s2 is not None and s1["data_type"] == s2["data_type"]
        ), f"Incompatible schema for column '{name}'"

        # Ensure new has no fields outside of allowed fields
        extra_fields = set(s2.keys()) - ALLOWED_COL_SCHEMA_FIELDS
        assert not extra_fields, f"Unexpected fields {extra_fields} in column '{name}'"

        # Select the more precise schema by comparing nullable, precision, and scale
        col_schema: TColumnSchema = {
            "name": name,
            "data_type": s1["data_type"],
        }
        if "nullable" in s1 or "nullable" in s2:
            col_schema["nullable"] = s1.get("nullable", s2.get("nullable"))
        if "precision" in s1 or "precision" in s2:
            col_schema["precision"] = s1.get("precision", s2.get("precision"))
        if "scale" in s1 or "scale" in s2:
            col_schema["scale"] = s1.get("scale", s2.get("scale"))

        # Update with the more detailed schema per column
        table_schema["columns"][name] = col_schema

    return table_schema
