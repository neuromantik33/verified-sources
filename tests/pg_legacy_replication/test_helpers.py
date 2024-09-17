import pytest
from dlt.common.schema.typing import TTableSchema
from dlt.common.typing import TDataItem
from google.protobuf.json_format import ParseDict as parse_dict

from sources.pg_legacy_replication.decoderbufs import gen_data_item, infer_schema_cols
from sources.pg_legacy_replication.decoderbufs.pg_logicaldec_pb2 import Op, RowMessage
from sources.pg_legacy_replication.helpers import compare_schemas

from .cases import (
    DATA_ITEMS,
    ROW_MESSAGES,
    SIMILAR_SCHEMAS,
    TABLE_SCHEMAS,
    SchemaChoice,
)


@pytest.mark.parametrize("data, expected_schema", zip(ROW_MESSAGES, TABLE_SCHEMAS))
def test_infer_table_schema_cols(
    data,
    expected_schema: TTableSchema,
):
    row_msg = RowMessage()
    parse_dict(data, row_msg)
    if row_msg.op == Op.DELETE:
        with pytest.raises(AssertionError):
            infer_schema_cols(row_msg)
    else:
        assert infer_schema_cols(row_msg) == expected_schema["columns"]


@pytest.mark.parametrize(
    "data, data_item, schema", zip(ROW_MESSAGES, DATA_ITEMS, TABLE_SCHEMAS)
)
def test_gen_data_item(data, data_item: TDataItem, schema: TTableSchema):
    row_msg = RowMessage()
    parse_dict(data, row_msg)
    assert (
        gen_data_item(
            row_msg,
            schema["columns"],
            lsn=1,
            include_commit_ts=True,
            include_tx_id=True,
        )
        == data_item
    )


@pytest.mark.parametrize("s1, s2, choice", SIMILAR_SCHEMAS)
def test_compare_schemas(s1: TTableSchema, s2: TTableSchema, choice: SchemaChoice):
    if choice == SchemaChoice.error:
        with pytest.raises(AssertionError):
            compare_schemas(s1, s2)
        with pytest.raises(AssertionError):
            compare_schemas(s2, s1)
    else:
        expected_schema = (s1, s2)[choice]
        assert compare_schemas(s1, s2) == expected_schema
        assert compare_schemas(s2, s1) == expected_schema
