-- create the test database
-- CREATE DATABASE dlt_data;
-- CREATE USER loader WITH PASSWORD 'loader';
-- ALTER DATABASE dlt_data OWNER TO loader;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE DATABASE dlt_source;
ALTER DATABASE dlt_source OWNER TO loader;