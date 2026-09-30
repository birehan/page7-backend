#!/usr/bin/env bash
# Run Alembic against Cloud SQL via Auth Proxy.
# Callers: cloudbuild.staging.yaml / cloudbuild.prod.yaml; local ops.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROJECT=${PROJECT:-page7-509810}
REGION=${REGION:-me-central1}
SQL_INSTANCE=${SQL_INSTANCE:-page7}
DATABASE_URL_SECRET=${DATABASE_URL_SECRET:-DATABASE_URL}
PROXY_PORT=${PROXY_PORT:-5432}
CONNECTION_NAME="${PROJECT}:${REGION}:${SQL_INSTANCE}"
PROXY_PID=""

cleanup() {
  if [[ -n "${PROXY_PID}" ]] && kill -0 "${PROXY_PID}" 2>/dev/null; then
    kill "${PROXY_PID}" 2>/dev/null || true
    wait "${PROXY_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

fetch_secret() {
  local name=$1
  if command -v gcloud >/dev/null 2>&1; then
    gcloud secrets versions access latest --secret="$name" --project="$PROJECT"
    return
  fi
  PROJECT="$PROJECT" SECRET_NAME="$name" python3 - <<'PY'
import json, os, urllib.request
project = os.environ["PROJECT"]
name = os.environ["SECRET_NAME"]
req = urllib.request.Request(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
    headers={"Metadata-Flavor": "Google"},
)
tok = json.load(urllib.request.urlopen(req))["access_token"]
url = f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/{name}/versions/latest:access"
req2 = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
body = json.load(urllib.request.urlopen(req2))
import base64
print(base64.b64decode(body["payload"]["data"]).decode("utf-8"), end="")
PY
}

RAW_URL=$(fetch_secret "$DATABASE_URL_SECRET")

PROXY_BIN=$(command -v cloud-sql-proxy || true)
if [[ -z "$PROXY_BIN" ]]; then
  mkdir -p "${ROOT}/.tmp"
  PROXY_BIN="${ROOT}/.tmp/cloud-sql-proxy"
  if [[ ! -x "$PROXY_BIN" ]]; then
    curl -fsSL -o "$PROXY_BIN" \
      "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.15.2/cloud-sql-proxy.linux.amd64"
    chmod +x "$PROXY_BIN"
  fi
fi

PROXY_ARGS=("${CONNECTION_NAME}" --address 127.0.0.1 --port "${PROXY_PORT}" --quiet)
if [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]] && command -v gcloud >/dev/null 2>&1; then
  PROXY_ARGS+=(--token="$(gcloud auth print-access-token)")
fi
"$PROXY_BIN" "${PROXY_ARGS[@]}" &
PROXY_PID=$!
sleep 3

export DATABASE__URL
DATABASE__URL=$(RAW_URL="$RAW_URL" PROXY_PORT="$PROXY_PORT" python3 - <<'PY'
import os, re
from urllib.parse import unquote, quote
raw = os.environ["RAW_URL"].strip()
port = os.environ["PROXY_PORT"]
m = re.match(r"^(postgresql(?:\+[^:]+)?:\/\/)([^@]+)@([^\/?]*)([^?]*)(\?.*)?$", raw, re.I)
if not m:
    raise SystemExit(f"cannot parse DATABASE_URL: {raw[:32]}...")
scheme, userinfo, _host, path, query = m.groups()
if ":" in userinfo:
    user, pw = userinfo.split(":", 1)
    userinfo = f"{quote(unquote(user), safe='')}:{quote(unquote(pw), safe='')}"
query = query or ""
parts = [p for p in query.lstrip("?").split("&") if p and not p.startswith("host=")]
q = ("?" + "&".join(parts)) if parts else ""
if scheme.startswith("postgresql://"):
    scheme = "postgresql+asyncpg://"
print(f"{scheme}{userinfo}@127.0.0.1:{port}{path}{q}")
PY
)

# Ensure uv is available
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/0.12.9/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "==> ensure uuidv7() polyfill (noop on PG18+)"
uv run python - <<'PY'
import asyncio
from urllib.parse import urlparse, unquote
from pathlib import Path
import asyncpg, os

url = os.environ["DATABASE__URL"].replace("postgresql+asyncpg://", "postgresql://")
u = urlparse(url)
sql = Path("sql/uuidv7_polyfill.sql").read_text()

async def main() -> None:
    conn = await asyncpg.connect(
        user=unquote(u.username or ""),
        password=unquote(u.password or ""),
        host=u.hostname,
        port=u.port,
        database=(u.path or "/").lstrip("/") or "postgres",
        ssl=False,
    )
    await conn.execute(sql)
    await conn.close()

asyncio.run(main())
PY

echo "==> alembic upgrade head"
APP_ENV=${APP_ENV:-development} uv run alembic upgrade head
echo "==> Migrations applied"
