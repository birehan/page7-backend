# Release and deploy

Trunk-based CD:

| Env | Git event | Config | Frontend origin |
|-----|-----------|--------|-----------------|
| Staging | push `main` | `cloudbuild.staging.yaml` | `https://page7-git-staging-birehans-projects.vercel.app` |
| Production | tag `vX.Y.Z` | `cloudbuild.prod.yaml` | `https://page7.io` |

## Cloud Build triggers

| Trigger | Event | Config |
|---------|-------|--------|
| `backend-staging` | push `^main$` | `cloudbuild.staging.yaml` |
| `backend-prod` | tag `^v[0-9]+.*` | `cloudbuild.prod.yaml` |

Migrations run in Cloud Build before Cloud Run deploy. Secrets:

- Staging: Secret Manager `DATABASE_URL` → env `DATABASE__URL`
- Prod: Secret Manager `DATABASE_URL_PROD` → env `DATABASE__URL`

## What a build checks before it ships

Both configs start with the same quality gate; nothing is built, migrated or deployed unless
it passes: `mypy app`, `lint-imports`, `ruff check` (reported, not yet blocking), and the full
`pytest` suite against a throwaway Postgres 18, with a coverage floor of 68% (measured 71%).
If you raise coverage, raise the floor in both files.

Before a pending migration runs, Cloud SQL takes an on-demand backup. Staging continues if the
backup fails; **production refuses to migrate** (`REQUIRE_PRE_MIGRATE_BACKUP=1`). The build
account needs `cloudsql.backupRuns.create` (Cloud SQL Editor or Admin) or prod deploys stop
at the migrate step.

## Environment checks

- `TRUSTED_PROXY_HOPS=1` is set by the API deploy scripts (Cloud Run with no load balancer in
  front). It makes rate limits and audit rows use the real client IP. Set it to `2` if a global
  load balancer is ever put in front. After the first deploy, check that rows in `rate_limits`
  and `audit_logs.ip` show real client addresses, not one Google address.
- `STORAGE__PROVIDER=r2` outside development. The unauthenticated local-storage routes only
  exist when `APP_ENV` is development or testing.
- The frontend's `NEXT_PUBLIC_API_URL` must point at the API and the API's `CORS__ALLOWED_ORIGINS`
  must include the frontend origin. A cross-site API needs `AUTH__COOKIE_SAMESITE=none`.

## Cut a release

1. Merge to `main` → wait for the staging build (the quality gate runs first)
2. Smoke staging:
   - `curl -sfS "$STAGING_URL/health/live"` and `/health/ready`
   - `curl -sS "$STAGING_URL/health/deep"` → expect `"status":"ok"`. A 503 names the failing
     check: `database`, `jobQueue` (jobs overdue, workers not draining) or `scheduler` (no
     per-minute run for 5 minutes).
3. `git tag v0.1.0 && git push origin v0.1.0` (the gate runs again on the tag)
4. Smoke production with the same three health URLs

## Monitoring

Point an uptime monitor at `/health/deep` and alert on any non-200. Set
`OBSERVABILITY__SENTRY_DSN` for error reports (off by default). Tracing is off unless
`OBSERVABILITY__OTEL_ENABLED` is set; the service name is `page7-api`.

## Roll back

- **Bad code:** redeploy the previous image tag with `IMAGE_TAG=<previous sha>
  ./deploy/deploy-api.sh` (and the workers script).
- **Bad migration:** restore from the on-demand backup taken just before it. Backups are named
  `pre-migrate <short sha>`; list them with `gcloud sql backups list --instance=page7`.
  Restoring replaces the database, so redeploy the previous image first.

## Manual emergency

```bash
DATABASE_URL_SECRET=DATABASE_URL ./deploy/migrate-cloudsql.sh
IMAGE_TAG=<sha> ./deploy/deploy-api.sh
IMAGE_TAG=<sha> ./deploy/deploy-workers.sh

DATABASE_URL_SECRET=DATABASE_URL_PROD ./deploy/migrate-cloudsql.sh
IMAGE_TAG=v0.1.0 ./deploy/deploy-api-prod.sh
IMAGE_TAG=v0.1.0 ./deploy/deploy-workers-prod.sh
```
