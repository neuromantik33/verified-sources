from functools import lru_cache
import json
from typing import Optional, Any, Dict

from dlt.common import Decimal
from dlt.common.data_types.typing import TDataType
from dlt.common.data_types.type_helpers import coerce_value
from dlt.common.schema.typing import (
    TColumnSchema,
    TColumnType,
    TTableSchemaColumns,
    TTableSchema,
)
from dlt.destinations import postgres
from dlt.destinations.impl.postgres.postgres import PostgresTypeMapper

from .decoders import ColumnType
from .pg_logicaldec_pb2 import RowMessage  # type: ignore[attr-defined]

_DUMMY_VALS: Dict[TDataType, Any] = {
    "bigint": 0,
    "binary": b" ",
    "bool": True,
    "complex": [0],
    "date": "2000-01-01",
    "decimal": Decimal(0),
    "double": 0.0,
    "text": "",
    "time": "00:00:00",
    "timestamp": "2000-01-01T00:00:00",
    "wei": 0,
}
"""Dummy values used to replace NULLs in NOT NULL columns in key-only delete records."""

_PG_TYPES: Dict[int, str] = {
    16: "boolean",
    17: "bytea",
    20: "bigint",
    21: "smallint",
    23: "integer",
    701: "double precision",
    1043: "character varying",
    1082: "date",
    1083: "time without time zone",
    1184: "timestamp with time zone",
    1700: "numeric",
    3802: "jsonb",
}
"""Maps postgres type OID to type string. Only includes types present in PostgresTypeMapper."""

_DATUM_PRECISIONS: Dict[str, int] = {
    "datum_int32": 32,
    "datum_int64": 64,
    "datum_float": 32,
    "datum_double": 64,
}
"""TODO: Add comment here"""


def _get_precision(type_id: int, atttypmod: int) -> Optional[int]:
    """Get precision from postgres type attributes."""
    # https://stackoverflow.com/a/3351120
    if type_id == 21:  # smallint
        return 16
    elif type_id == 23:  # integer
        return 32
    elif type_id == 20:  # bigint
        return 64
    if atttypmod != -1:
        if type_id == 1700:  # numeric
            return ((atttypmod - 4) >> 16) & 65535
        elif type_id in (
            1083,
            1184,
        ):  # time without time zone, timestamp with time zone
            return atttypmod
        elif type_id == 1043:  # character varying
            return atttypmod - 4
    return None


def _get_scale(type_id: int, atttypmod: int) -> Optional[int]:
    """Get scale from postgres type attributes."""
    # https://stackoverflow.com/a/3351120
    if atttypmod != -1:
        if type_id in (21, 23, 20):  # smallint, integer, bigint
            return 0
        if type_id == 1700:  # numeric
            return (atttypmod - 4) & 65535
    return None


@lru_cache(maxsize=None)
def _type_mapper() -> PostgresTypeMapper:
    return PostgresTypeMapper(postgres().capabilities())


def _to_dlt_column_type(type_id: int, atttypmod: int) -> TColumnType:
    """Converts postgres type OID to dlt column type.

    Type OIDs not in _PG_TYPES mapping default to "text" type.
    """
    pg_type = _PG_TYPES.get(type_id)
    precision = _get_precision(type_id, atttypmod)
    scale = _get_scale(type_id, atttypmod)
    return _type_mapper().from_db_type(pg_type, precision, scale)


def _to_dlt_column_schema(col: ColumnType) -> TColumnSchema:
    """Converts pypgoutput ColumnType to dlt column schema."""
    dlt_column_type = _to_dlt_column_type(col.type_id, col.atttypmod)
    partial_column_schema = {
        "name": col.name,
        "primary_key": bool(col.part_of_pkey),
    }
    return {**dlt_column_type, **partial_column_schema}  # type: ignore[typeddict-item]


def _to_dlt_val(val: str, data_type: TDataType, byte1: str, for_delete: bool) -> Any:
    """Converts pgoutput's text-formatted value into dlt-compatible data value."""
    if byte1 == "n":
        if for_delete:
            # replace None with dummy value to prevent NOT NULL violations in staging table
            return _DUMMY_VALS[data_type]
        return None
    elif byte1 == "t":
        if data_type == "binary":
            # https://www.postgresql.org/docs/current/datatype-binary.html#DATATYPE-BINARY-BYTEA-HEX-FORMAT
            return bytes.fromhex(val.replace("\\x", ""))
        elif data_type == "complex":
            return json.loads(val)
        return coerce_value(data_type, "text", val)
    else:
        raise ValueError(
            f"Byte1 in replication message must be 'n' or 't', not '{byte1}'."
        )


def _extract_table_schema(row_msg: RowMessage) -> TTableSchema:
    schema_name, table_name = row_msg.table.split(".")
    # Remove leading and trailing quotes
    table_name = table_name[1:-1]
    import re

    regex = r"^(?P<table_name>[a-zA-Z_][a-zA-Z0-9_]{0,62})_snapshot_(?P<snapshot_name>[a-zA-Z0-9_-]+)$"
    match = re.match(regex, table_name)
    if match:
        table_name = match.group("table_name")
        snapshot_name = match.group("snapshot_name")
        print(f"Table name: {table_name}, Snapshot name: {snapshot_name}")

    columns: TTableSchemaColumns = {}
    for c, c_info in zip(row_msg.new_tuple, row_msg.new_typeinfo):
        assert _PG_TYPES[c.column_type] == c_info.modifier
        col_type: TColumnType = _type_mapper().from_db_type(c_info.modifier)
        col_schema: TColumnSchema = {
            "name": c.column_name,
            "nullable": c_info.value_optional,
            **col_type,
        }

        precision = _DATUM_PRECISIONS.get(c.WhichOneof("datum"))
        if precision is not None:
            col_schema["precision"] = precision

        columns[c.column_name] = col_schema

    return {"name": table_name, "columns": columns}
