# Release and deploy

Trunk-based CD (same pattern as glasshouse-backend):

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

## Cut a release

1. Merge to `main` → wait for staging build
2. Smoke: `curl -sfS "$STAGING_URL/health"`
3. `git tag v0.1.0 && git push origin v0.1.0`
4. Smoke prod health endpoint

## Manual emergency

```bash
DATABASE_URL_SECRET=DATABASE_URL ./deploy/migrate-cloudsql.sh
IMAGE_TAG=<sha> ./deploy/deploy-api.sh
IMAGE_TAG=<sha> ./deploy/deploy-workers.sh

DATABASE_URL_SECRET=DATABASE_URL_PROD ./deploy/migrate-cloudsql.sh
IMAGE_TAG=v0.1.0 ./deploy/deploy-api-prod.sh
IMAGE_TAG=v0.1.0 ./deploy/deploy-workers-prod.sh
```
