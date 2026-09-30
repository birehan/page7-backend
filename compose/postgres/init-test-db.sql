-- Runs only on first Postgres volume init (empty data dir).
-- Existing volumes: `just ensure-test-db` creates the same database.
CREATE DATABASE pgblank_test OWNER pgblank;
