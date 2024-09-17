from contextlib import closing, contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple, TypedDict

import psycopg2
from dlt.common import logger
from dlt.common.libs.sql_alchemy import Engine, MetaData, Table, sa
from dlt.common.pendulum import pendulum
from dlt.common.schema.typing import TColumnSchema, TTableSchema, TTableSchemaColumns
from dlt.extract import DltSource
from dlt.sources.credentials import ConnectionStringCredentials
from dlt.sources.sql_database import (
    TableBackend,
    TQueryAdapter,
    TTypeAdapter,
    engine_from_credentials,
)
from dlt.sources.sql_database.schema_types import (
    ColumnAny,
    ReflectionLevel,
    sqla_col_to_column_schema,
)
from psycopg2.extensions import connection as ConnectionExt
from psycopg2.extensions import cursor
from psycopg2.extras import LogicalReplicationConnection, ReplicationCursor


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
            cur.execute(f"SET TRANSACTION SNAPSHOT '{snapshot_name}';")

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


def get_replication_slot(
    slot_name: str, credentials: ConnectionStringCredentials, output_plugin: str
) -> Optional[Dict[str, str]]:
    """
    Returns the replication slot for the given name, or None if doesn't exist.
    """
    with get_cursor(credentials) as cur:
        cur.execute(
            "SELECT slot_name, restart_lsn, plugin FROM pg_replication_slots WHERE slot_name = %s;",
            (slot_name,),
        )
        result = cur.fetchone()
        if not result:
            return None
        slot, restart_lsn, plugin = result

    assert (
        plugin == output_plugin
    ), f"Replication slot '{slot}' uses plugin '{plugin}', expected '{output_plugin}'"

    return {
        "slot_name": slot,
        "consistent_point": restart_lsn,
        "output_plugin": plugin,
    }


def create_replication_slot(
    slot_name: str, cur: ReplicationCursor, output_plugin: str
) -> Dict[str, str]:
    """Creates a replication slot if it doesn't exist yet."""
    cur.create_replication_slot(slot_name, output_plugin=output_plugin)
    logger.debug(
        "Successfully created replication slot '%s' (%s)", slot_name, output_plugin
    )
    result = cur.fetchone()
    return {
        "slot_name": result[0],
        "consistent_point": result[1],
        "snapshot_name": result[2],
        "output_plugin": result[3],
    }


def drop_replication_slot(slot_name: str, cur: ReplicationCursor) -> None:
    """Drops a replication slot if it exists."""
    try:
        cur.drop_replication_slot(slot_name)
        logger.info("Successfully dropped replication slot '%s'", slot_name)
    except psycopg2.errors.UndefinedObject:  # the replication slot does not exist
        logger.info(
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


def epoch_micros_to_datetime(microseconds_since_1970: int) -> pendulum.DateTime:
    return pendulum.from_timestamp(microseconds_since_1970 / 1_000_000)


def microseconds_to_time(microseconds: int) -> pendulum.Time:
    return pendulum.Time().add(microseconds=microseconds)


def epoch_days_to_date(epoch_days: int) -> pendulum.Date:
    return pendulum.Date(1970, 1, 1).add(days=epoch_days)


# Schema helpers
def reflect_schema_cols(
    credentials: ConnectionStringCredentials,
    schema: str,
    table_name: str,
    included_columns: Optional[Set[str]] = None,
    reflection_level: ReflectionLevel = "full",
    **_: Any,
) -> TTableSchemaColumns:
    """
    Last resort function used to fetch the table schema columns directly from the database.
    """
    engine = engine_from_credentials(credentials)
    try:
        metadata = MetaData(schema=schema)
        table = Table(table_name, metadata, autoload_with=engine)

        def get_column_entry(c: ColumnAny) -> Optional[Tuple[str, TColumnSchema]]:
            col = sqla_col_to_column_schema(c, reflection_level)
            if col is None:
                return None
            if included_columns and c.name not in included_columns:
                return None
            return col["name"], col

        return dict(
            entry for c in table.columns if (entry := get_column_entry(c)) is not None
        )
    finally:
        engine.dispose()


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
