#!/usr/bin/env bash
# Upsert Secret Manager secrets for page7 Cloud Run from local .env.
# Usage: PROJECT=page7-509810 ./deploy/create-secrets.sh
#
# Callers: operators after bootstrap (docs/RELEASE.md); bootstrap-gcp.sh prints this as next step.
# Confirmed: only bootstrap-gcp.sh exists under deploy/ — no prior create-secrets.
# Reads .env KEY=VALUE (no dates). Secrets upserted: DATABASE_URL, DATABASE_URL_PROD,
# APP_ENCRYPTION_KEY_*, STORAGE__*, LLM__*, ZERNIO_*, etc. Never prints values.
set -euo pipefail

PROJECT=${PROJECT:-page7-509810}
REGION=${REGION:-me-central1}
SQL_INSTANCE=${SQL_INSTANCE:-page7}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE=${ENV_FILE:-"$ROOT/.env"}
CONNECTION_NAME="${PROJECT}:${REGION}:${SQL_INSTANCE}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

load_env_file() {
  local file=$1
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" != *=* ]] && continue
    key="${line%%=*}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#"${key%%[![:space:]]*}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    if [[ ${#value} -ge 2 ]]; then
      local first="${value:0:1}"
      local last="${value: -1}"
      if [[ ( "$first" == '"' && "$last" == '"' ) || ( "$first" == "'" && "$last" == "'" ) ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi
    printf -v "$key" '%s' "$value"
    export "$key"
  done < "$file"
}

load_env_file "$ENV_FILE"

gcloud config set project "$PROJECT" --quiet

upsert_secret() {
  local name=$1
  local value=${2:-}
  if [[ -z "$value" ]]; then
    echo "  skip $name (empty)"
    return 0
  fi
  if gcloud secrets describe "$name" --project="$PROJECT" &>/dev/null; then
    echo -n "$value" | gcloud secrets versions add "$name" --data-file=- --project="$PROJECT" --quiet
    echo "  updated $name"
  else
    echo -n "$value" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic --project="$PROJECT" --quiet
    echo "  created $name"
  fi
}

# Convert TCP postgres URL → Cloud SQL Unix socket URL for Cloud Run.
to_cloudsql_socket_url() {
  local raw=$1
  RAW_URL="$raw" CONN="$CONNECTION_NAME" python3 - <<'PY'
import os, re
from urllib.parse import unquote, quote
raw = os.environ["RAW_URL"].strip()
conn = os.environ["CONN"]
m = re.match(r"^(postgresql(?:\+[^:]+)?:\/\/)([^@]+)@([^\/?]*)([^?]*)(\?.*)?$", raw, re.I)
if not m:
    raise SystemExit("cannot parse database URL for socket rewrite")
scheme, userinfo, _host, path, query = m.groups()
if ":" in userinfo:
    user, pw = userinfo.split(":", 1)
    userinfo = f"{quote(unquote(user), safe='')}:{quote(unquote(pw), safe='')}"
scheme = "postgresql+asyncpg://"
parts = [p for p in (query or "").lstrip("?").split("&") if p and not p.startswith("host=")]
parts.append(f"host=/cloudsql/{conn}")
print(f"{scheme}{userinfo}@{path}?{'&'.join(parts)}")
PY
}

echo "==> Upserting secrets from $ENV_FILE"

STAGING_SRC="${CLOUD_SQL_DATABASE_URL:-}"
if [[ -n "$STAGING_SRC" ]]; then
  STAGING_SOCKET=$(to_cloudsql_socket_url "$STAGING_SRC")
  upsert_secret DATABASE_URL "$STAGING_SOCKET"
else
  echo "  skip DATABASE_URL (no CLOUD_SQL_DATABASE_URL)"
fi

if [[ -n "${DATABASE_URL_PROD:-}" ]]; then
  PROD_SOCKET=$(to_cloudsql_socket_url "$DATABASE_URL_PROD")
  upsert_secret DATABASE_URL_PROD "$PROD_SOCKET"
else
  echo "  skip DATABASE_URL_PROD (empty)"
fi

upsert_secret APP_ENCRYPTION_KEY_CURRENT "${APP_ENCRYPTION_KEY_CURRENT:-}"
upsert_secret APP_ENCRYPTION_KEY__k1 "${APP_ENCRYPTION_KEY__k1:-}"
upsert_secret STORAGE__R2_ACCOUNT_ID "${STORAGE__R2_ACCOUNT_ID:-}"
upsert_secret STORAGE__R2_ACCESS_KEY_ID "${STORAGE__R2_ACCESS_KEY_ID:-}"
upsert_secret STORAGE__R2_SECRET_ACCESS_KEY "${STORAGE__R2_SECRET_ACCESS_KEY:-}"
upsert_secret STORAGE__PUBLIC_BUCKET "${STORAGE__PUBLIC_BUCKET:-}"
upsert_secret STORAGE__PUBLIC_BASE_URL "${STORAGE__PUBLIC_BASE_URL:-}"
upsert_secret LLM__OPENAI_API_KEY "${LLM__OPENAI_API_KEY:-}"
upsert_secret LLM__ANTHROPIC_API_KEY "${LLM__ANTHROPIC_API_KEY:-}"
upsert_secret IMAGEGEN__API_KEY "${IMAGEGEN__API_KEY:-}"
upsert_secret EMAIL__RESEND_API_KEY "${EMAIL__RESEND_API_KEY:-}"
upsert_secret EMAIL__SMTP_PASSWORD "${EMAIL__SMTP_PASSWORD:-}"
upsert_secret AUTH__GOOGLE_CLIENT_ID "${AUTH__GOOGLE_CLIENT_ID:-}"
upsert_secret AUTH__GOOGLE_CLIENT_SECRET "${AUTH__GOOGLE_CLIENT_SECRET:-}"
upsert_secret STOCK__UNSPLASH_ACCESS_KEY "${STOCK__UNSPLASH_ACCESS_KEY:-}"
upsert_secret OBSERVABILITY__SENTRY_DSN "${OBSERVABILITY__SENTRY_DSN:-}"

upsert_secret ZERNIO_API_KEY__t1 "${ZERNIO_API_KEY__t1:-}"
upsert_secret ZERNIO_API_KEY__t2 "${ZERNIO_API_KEY__t2:-}"
upsert_secret ZERNIO_API_KEY__t3 "${ZERNIO_API_KEY__t3:-}"
upsert_secret ZERNIO_API_KEY__t4 "${ZERNIO_API_KEY__t4:-}"
upsert_secret ZERNIO_WEBHOOK_SECRET__t1 "${ZERNIO_WEBHOOK_SECRET__t1:-}"
upsert_secret ZERNIO_WEBHOOK_SECRET__t2 "${ZERNIO_WEBHOOK_SECRET__t2:-}"
upsert_secret ZERNIO_WEBHOOK_SECRET__t3 "${ZERNIO_WEBHOOK_SECRET__t3:-}"
upsert_secret ZERNIO_WEBHOOK_SECRET__t4 "${ZERNIO_WEBHOOK_SECRET__t4:-}"

echo "==> Done"
