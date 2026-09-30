# page7-backend

FastAPI backend for [page7.io](https://page7.io).

## Local

```bash
cp .env.example .env   # fill secrets
just up                # Postgres + API via compose/
uv run alembic upgrade head
```

## Deploy (GCP)

Project: `page7-509810` · Region: `me-central1` · SQL: `page7`

| Env | Trigger | Cloud Run | DB |
|-----|---------|-----------|-----|
| Staging | push `main` | `page7-api` (+ worker/scheduler pools) | `page7-staging-db` |
| Production | tag `v*` | `page7-api-prod` (+ prod pools) | `page7-prod-db` |

One-time setup:

```bash
PROJECT=page7-509810 ./deploy/bootstrap-gcp.sh
PROJECT=page7-509810 ./deploy/create-secrets.sh
# Connect GitHub → Cloud Build triggers (see docs/RELEASE.md)
```

See [docs/RELEASE.md](docs/RELEASE.md).
