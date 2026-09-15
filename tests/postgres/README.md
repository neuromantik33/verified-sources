# Test PostgreSQL instance

This docker compose file will spin up the test postgreSQL instance that can be used to run tests or do ad hoc loading. The `postgres.env` will name the default database `dlt_data`, the sys admin user `loader` and set the password to `loader`. You may also use the `01_init.sql` to create database with more granular permissions.

Below is a relevant `toml` fragment that you can put into `secrets.toml`.
```toml
destination.postgres.credentials="postgres://loader:loader@localhost:5432/dlt_data"
```

## Choosing a Postgres version

The replication sources handle both pre-10 and post-10 Postgres. The two differ in
what the slot column is called (`location` vs `lsn`) and whether you can advance
the slot by hand. `PG_VERSION` picks which one the stack runs:

```
PG_VERSION=9.6 docker compose up -d --wait   # legacy, pre-10 (default)
PG_VERSION=14  docker compose up -d --wait   # modern, post-10
```

Any [`debezium/postgres`](https://hub.docker.com/r/debezium/postgres) tag works,
9.6 through 17. Those images ship `decoderbufs` prebuilt with `wal_level=logical`
already set, so there is no image to build.

Each version gets its own compose project (`dlt-pg-<version>`), which keeps its
data directory away from the other majors. They all publish on the same host port,
so run one at a time or set `PG_PORT` to move one.

From the repo root, `make pg-up`, `make pg-down` and `make test-pg-replication`
wrap the same thing and take `PG_VERSION` too:

```
make test-pg-replication                 # 9.6
make test-pg-replication PG_VERSION=14
```

The tests ask the server its version rather than reading `PG_VERSION`, so a stale
container cannot fool them into testing the wrong code path.

To start the instance, from the `tests/postgres` folder
```
docker-compose up --build -d
```

remove `-d` to see the log

To complete wipe out the instance, run
```
docker-compose down -v
```
