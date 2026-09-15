import faulthandler
import pytest

from typing import Iterator, Tuple

import dlt
from dlt.common.utils import uniq_id


def pytest_configure():
    faulthandler.enable()


@pytest.fixture()
def pg_version(src_config: Tuple[dlt.Pipeline, str]) -> int:
    """Server version of the Postgres under test, e.g. 90624 or 140012.

    This asks the same connection the test uses instead of reading PG_VERSION.
    PG_VERSION only picks the docker image; if the container up is not the one it
    names, the tests still branch on what they are actually talking to.
    """
    src_pl, _ = src_config
    with src_pl.sql_client() as c:
        return int(
            c.execute_sql(
                "SELECT setting FROM pg_settings WHERE name = 'server_version_num';"
            )[0][0]
        )


@pytest.fixture()
def src_config() -> Iterator[Tuple[dlt.Pipeline, str]]:
    # random slot to enable parallel runs
    slot = "test_slot_" + uniq_id(4)
    # setup
    src_pl = dlt.pipeline(
        pipeline_name="src_pl",
        destination=dlt.destinations.postgres(
            credentials=dlt.secrets.get("sources.pg_replication.credentials")
        ),
        dev_mode=True,
    )
    yield src_pl, slot
    # teardown
    with src_pl.sql_client() as c:
        # drop tables
        try:
            c.drop_dataset()
        except Exception as e:
            print(e)
        with c.with_staging_dataset():
            try:
                c.drop_dataset()
            except Exception as e:
                print(e)
        # drop replication slot
        try:
            c.execute_sql(f"SELECT pg_drop_replication_slot('{slot}');")
        except Exception as e:
            print(e)
