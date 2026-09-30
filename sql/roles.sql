-- Phase 15 least-privilege roles for testing-db (architecture/05 §11).
-- Apply once per environment via Cloud SQL Auth Proxy as the postgres
-- superuser, with SET lock_timeout = '5s' (architecture/03).
--
-- Usage:
--   SET lock_timeout = '5s';
--   \c testing-db
--   \i roles.sql

SET lock_timeout = '5s';

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pgblank_migrator') THEN
    CREATE ROLE pgblank_migrator LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pgblank_runtime') THEN
    CREATE ROLE pgblank_runtime LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pgblank_retention') THEN
    CREATE ROLE pgblank_retention LOGIN;
  END IF;
END
$$;

-- Passwords are set out-of-band (Secret Manager / gcloud sql users set-password).
-- Example (do not commit passwords):
--   ALTER ROLE pgblank_migrator PASSWORD '...';
--   ALTER ROLE pgblank_runtime  PASSWORD '...';
--   ALTER ROLE pgblank_retention PASSWORD '...';

GRANT CONNECT ON DATABASE "testing-db" TO pgblank_migrator, pgblank_runtime, pgblank_retention;
GRANT USAGE ON SCHEMA public TO pgblank_migrator, pgblank_runtime, pgblank_retention;

-- Migrator: schema DDL only (Alembic). Owns tables it creates when run as owner.
GRANT ALL ON SCHEMA public TO pgblank_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pgblank_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pgblank_retention;

-- Runtime: broad DML, no DDL, append-only audit + narrowed ai_decisions.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO pgblank_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO pgblank_runtime;
REVOKE CREATE ON SCHEMA public FROM pgblank_runtime;

REVOKE UPDATE, DELETE ON TABLE audit_logs FROM pgblank_runtime;
REVOKE UPDATE, DELETE ON TABLE ai_decisions FROM pgblank_runtime;
GRANT UPDATE (provider_response) ON TABLE ai_decisions TO pgblank_runtime;

-- Retention: scheduled prune/archive jobs only.
GRANT SELECT, DELETE ON TABLE audit_logs TO pgblank_retention;
GRANT SELECT, DELETE ON TABLE jobs TO pgblank_retention;
GRANT SELECT, DELETE ON TABLE webhook_events TO pgblank_retention;
GRANT SELECT, DELETE ON TABLE notifications TO pgblank_retention;
GRANT SELECT, DELETE ON TABLE run_events TO pgblank_retention;
GRANT SELECT, DELETE ON TABLE sessions TO pgblank_retention;
GRANT SELECT, DELETE ON TABLE idempotency_keys TO pgblank_retention;
GRANT SELECT, DELETE ON TABLE rate_limits TO pgblank_retention;
GRANT SELECT, UPDATE ON TABLE ai_decisions TO pgblank_retention;
GRANT SELECT, UPDATE (provider_response) ON TABLE ai_decisions TO pgblank_retention;

-- Org purge (maintenance.purge_organization) runs as retention and needs
-- broader DELETE on tenant tables plus UPDATE for anonymization.
GRANT SELECT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO pgblank_retention;
