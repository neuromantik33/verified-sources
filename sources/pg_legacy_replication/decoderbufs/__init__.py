import hashlib
from contextlib import closing
from logging import getLogger
from typing import Any, DefaultDict, Iterator, Optional, Sequence, Set, Tuple

from dlt.common.schema.typing import TTableSchema, TTableSchemaColumns
from dlt.common.typing import TDataItem
from dlt.sources.credentials import ConnectionStringCredentials
from psycopg2.extras import ReplicationMessage

from ..consumer import (
    MessageConsumer,
    ReplicationOptions,
    TableItems,
    add_replication_columns,
    read_message,
)
from ..helpers import (
    compare_schemas,
    epoch_micros_to_datetime,
    get_rep_conn,
    reflect_schema_cols,
)
from .pg_logicaldec_pb2 import DatumMessage, Op, RowMessage, TypeInfo
from .schema_types import to_dlt_column_schema, to_dlt_val

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
                        self.data_items[table_name].append(data_item)
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
        schema_name, table_name = msg.table.split(".")
        options = self.repl_options[table_name]

        def build_schema_from_cols(cols: TTableSchemaColumns) -> TTableSchema:
            return TTableSchema(
                name=table_name,
                columns=add_replication_columns(cols, **options),
            )

        def reflect_and_cache_schema() -> TTableSchema:
            cols = reflect_schema_cols(
                self.credentials, schema_name, table_name, **options
            )
            schema = build_schema_from_cols(cols)
            self.last_table_schema[table_name] = schema
            return schema

        cached_schema = self.last_table_schema.get(table_name)

        # 1. DELETEs use cached schema or reflect from DB
        if msg.op == Op.DELETE:
            return cached_schema or reflect_and_cache_schema()

        # 2. If type hasn't changed, use cached schema
        current_hash = hash_typeinfo(msg.new_typeinfo)
        if current_hash == self.last_table_hashes.get(table_name):
            return cached_schema

        # 3. Infer schema from message
        cols = infer_schema_cols(msg, **options)
        current_schema = build_schema_from_cols(cols)
        try:
            if cached_schema is not None:
                current_schema = compare_schemas(cached_schema, current_schema)

            self.last_table_schema[table_name] = current_schema
            self.last_table_hashes[table_name] = current_hash

            return current_schema

        except AssertionError as e:
            log.warning(str(e))
            return None


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


def infer_schema_cols(
    msg: RowMessage,
    included_columns: Optional[Set[str]] = None,
    **_: Any,
) -> TTableSchemaColumns:
    """
    Infers the table schema columns from the replication message and optional hints.
    """
    assert msg.op != Op.DELETE
    return {
        col_name: to_dlt_column_schema(
            col_name, datum=col, type_info=msg.new_typeinfo[i]
        )
        for i, col in enumerate(msg.new_tuple)
        if (col_name := _actual_column_name(col))
        and (not included_columns or col_name in included_columns)
    }


def hash_typeinfo(new_typeinfo: Sequence[TypeInfo]) -> int:
    """Generate a hash for the entire new_typeinfo list by hashing each TypeInfo message."""
    typeinfo_tuple = tuple(
        (info.modifier, info.value_optional) for info in new_typeinfo
    )
    hash_obj = hashlib.blake2b(repr(typeinfo_tuple).encode(), digest_size=8)
    return int(hash_obj.hexdigest(), 16)
