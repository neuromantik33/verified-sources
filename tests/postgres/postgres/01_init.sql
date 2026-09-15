-- The entrypoint has already created the database and role named by postgres.env:
-- CREATE DATABASE dlt_data;
-- CREATE USER loader WITH PASSWORD 'loader';
-- ALTER DATABASE dlt_data OWNER TO loader;

-- Postgres only builds in gen_random_uuid() from 13 onwards, but pgcrypto has
-- shipped it since 9.4. Install it everywhere and the tests can call
-- public.gen_random_uuid() on any version without branching.
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

-- template1 too, so databases created later inherit it.
\connect template1
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

\connect postgres
CREATE DATABASE dlt_source;
ALTER DATABASE dlt_source OWNER TO loader;
