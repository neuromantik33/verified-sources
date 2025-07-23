import hashlib
from contextlib import closing
from logging import getLogger
from typing import Any, DefaultDict, Iterator, Optional, Sequence, Set, Tuple

from dlt.common.libs.sql_alchemy import MetaData, Table
from dlt.common.schema.typing import TColumnSchema, TTableSchema, TTableSchemaColumns
from dlt.common.typing import TDataItem
from dlt.sources.credentials import ConnectionStringCredentials
from dlt.sources.sql_database import engine_from_credentials
from dlt.sources.sql_database.schema_types import ColumnAny, sqla_col_to_column_schema
from psycopg2.extras import ReplicationMessage

from .pg_logicaldec_pb2 import DatumMessage, Op, RowMessage, TypeInfo
from .schema_types import to_dlt_column_schema, to_dlt_val
from ..helpers import (
    MessageConsumer,
    ReplicationOptions,
    TableItems,
    add_replication_columns,
    compare_schemas,
    epoch_micros_to_datetime,
    get_rep_conn,
    read_message,
)

log = getLogger(__name__)


class DecoderbufsConsumer(MessageConsumer):
    def __init__(
        self,
        credentials: ConnectionStringCredentials,
        table_qnames: Set[str],
        repl_options: DefaultDict[str, ReplicationOptions],
        target_batch_size: int = 1000,
    ):
        super().__init__(credentials, table_qnames, repl_options, target_batch_size)

    def read_wal(
        self, slot_name: str, start_lsn: int, upto_lsn: int
    ) -> Iterator[TableItems]:
        consumed_all = False
        last_commit_lsn: int
        conn = get_rep_conn(self.credentials)
        with closing(conn), conn.cursor() as cur:
            log.debug("Starting replication for slot '%s'...", slot_name)
            cur.start_replication(slot_name, start_lsn=start_lsn)
            while True:
                repl_msg = read_message(cur)
                try:
                    msg, lsn = decode_replication_message(repl_msg, upto_lsn)
                    if msg.op == Op.BEGIN:
                        pass
                    elif msg.op == Op.COMMIT:
                        last_commit_lsn = lsn
                        if lsn >= upto_lsn:
                            consumed_all = True
                        # combine items for all tables
                        n_items = sum(
                            [len(items) for items in self.data_items.values()]
                        )
                        if consumed_all or n_items >= self.target_batch_size:
                            yield from self.flush_batch(cur, last_commit_lsn)
                            if consumed_all:
                                cur.send_feedback(
                                    flush_lsn=last_commit_lsn,
                                    reply=True,
                                    force=True,
                                )
                                break
                    elif msg.table in self.table_qnames:
                        assert msg.op in {Op.INSERT, Op.UPDATE, Op.DELETE}
                        table_schema = self.get_table_schema(msg)
                        if table_schema is None:
                            yield from self.flush_batch(cur, last_commit_lsn)
                            self.clear_state(with_schemas=True)
                            table_schema = self.get_table_schema(msg)
                        assert table_schema is not None
                        table_name = msg.table.split(".")[1]
                        data_item = gen_data_item(
                            msg,
                            table_schema["columns"],
                            lsn,
                            **self.repl_options[table_name],
                        )
                        self.data_items[msg.table].append(data_item)
                except Exception:
                    log.error(
                        "A fatal error occurred while processing a message: %s", msg
                    )
                    raise
                else:
                    cur.send_feedback(write_lsn=lsn)
        assert (
            consumed_all
        ), f"upto_lsn = {upto_lsn}, last_commit_lsn = {last_commit_lsn}"
        assert conn.closed, "Connection was was not closed!"

    def get_table_schema(self, msg: RowMessage) -> Optional[TTableSchema]:
        """
        Given a row message, calculates or fetches a table schema.
        """
        table_qname = msg.table
        table_name = table_qname.split(".")[1]
        schema_cache = self.last_table_schema
        schema_hashes = self.last_table_hashes

        cached_schema = schema_cache.get(table_qname)

        # 1. Fast path: DELETE uses cached schema (or fetch from SQLA if missing)
        if msg.op == Op.DELETE:
            if cached_schema is None:
                cached_schema = self._fetch_table_schema_with_sqla(table_qname)
                schema_cache[table_qname] = cached_schema
            return cached_schema

        # 2. Fast path: type hash matches cached
        current_hash = hash_typeinfo(msg.new_typeinfo)
        if current_hash == schema_hashes.get(table_qname):
            return schema_cache[table_qname]

        # 3. Infer new schema from message
        inferred_schema = infer_table_schema(msg, self.repl_options[table_name])

        if cached_schema is None:
            # No previous schema, so cache and return the new one
            schema_cache[table_qname] = inferred_schema
            schema_hashes[table_qname] = current_hash
            return inferred_schema

        # 4. Compare and retain merged schema if compatible
        try:
            merged_schema = compare_schemas(cached_schema, inferred_schema)
            schema_cache[table_qname] = merged_schema
            schema_hashes[table_qname] = current_hash
            return merged_schema
        except AssertionError as e:
            log.warning(str(e))
            return None

    def _fetch_table_schema_with_sqla(self, table_qname: str) -> TTableSchema:
        """Last resort function used to fetch the table schema from the database"""
        engine = engine_from_credentials(self.credentials)
        schema, table_name = table_qname.split(".")
        options = self.repl_options[table_name]
        try:
            metadata = MetaData(schema=schema)
            table = Table(table_name, metadata, autoload_with=engine)
            included_columns = options.get("included_columns")

            def get_column_entry(c: ColumnAny) -> Optional[Tuple[str, TColumnSchema]]:
                col = sqla_col_to_column_schema(
                    c, options.get("reflection_level", "full")
                )
                if col is None:
                    return None
                if included_columns and c.name not in included_columns:
                    return None
                return col["name"], col

            columns = dict(
                entry
                for c in table.columns
                if (entry := get_column_entry(c)) is not None
            )

            return TTableSchema(
                name=table_name,
                columns=add_replication_columns(columns, **options),
            )
        finally:
            engine.dispose()


def decode_replication_message(
    msg: ReplicationMessage, upto_lsn: int
) -> Tuple[RowMessage, int]:
    row_msg = RowMessage()
    row_msg.ParseFromString(msg.payload)
    assert row_msg.op != Op.UNKNOWN, f"Unsupported operation : {row_msg}"
    lsn = msg.data_start
    log.debug(
        "op: %s, current lsn: %s, max lsn: %s", Op.Name(row_msg.op), lsn, upto_lsn
    )
    return row_msg, lsn


def gen_data_item(
    msg: RowMessage,
    column_schema: TTableSchemaColumns,
    lsn: int,
    *,
    include_lsn: bool = True,
    include_deleted_ts: bool = True,
    include_commit_ts: bool = False,
    include_tx_id: bool = False,
    included_columns: Optional[Set[str]] = None,
    **_: Any,
) -> TDataItem:
    """Generates data item from a row message and corresponding metadata."""
    data_item: TDataItem = {}
    if include_lsn:
        data_item["_pg_lsn"] = lsn
    if include_commit_ts:
        data_item["_pg_commit_ts"] = epoch_micros_to_datetime(msg.commit_time)
    if include_tx_id:
        data_item["_pg_tx_id"] = msg.transaction_id

    # Select the relevant row tuple based on operation type
    is_delete = msg.op == Op.DELETE
    row = msg.old_tuple if is_delete else msg.new_tuple
    if is_delete and include_deleted_ts:
        data_item["_pg_deleted_ts"] = epoch_micros_to_datetime(msg.commit_time)

    for data in row:
        col_name = _actual_column_name(data)
        if not included_columns or col_name in included_columns:
            data_item[col_name] = to_dlt_val(
                data, column_schema[col_name], for_delete=is_delete
            )

    return data_item


def _actual_column_name(column: DatumMessage) -> str:
    """
    Certain column names are quoted since they are reserved keywords,
    however let the destination decide on how to normalize them
    """
    col_name = column.column_name
    if col_name.startswith('"') and col_name.endswith('"'):
        col_name = col_name[1:-1]
    return col_name


def infer_table_schema(msg: RowMessage, options: ReplicationOptions) -> TTableSchema:
    """Infers the table schema from the replication message and optional hints."""
    # Choose the correct source based on operation type
    assert msg.op != Op.DELETE
    included_columns = options.get("included_columns")
    columns = {
        col_name: to_dlt_column_schema(
            col_name, datum=col, type_info=msg.new_typeinfo[i]
        )
        for i, col in enumerate(msg.new_tuple)
        if (col_name := _actual_column_name(col))
        and (not included_columns or col_name in included_columns)
    }

    return TTableSchema(
        name=msg.table.split(".")[1],
        columns=add_replication_columns(columns, **options),
    )


def hash_typeinfo(new_typeinfo: Sequence[TypeInfo]) -> int:
    """Generate a hash for the entire new_typeinfo list by hashing each TypeInfo message."""
    typeinfo_tuple = tuple(
        (info.modifier, info.value_optional) for info in new_typeinfo
    )
    hash_obj = hashlib.blake2b(repr(typeinfo_tuple).encode(), digest_size=8)
    return int(hash_obj.hexdigest(), 16)
