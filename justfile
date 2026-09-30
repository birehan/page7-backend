# No `set dotenv-load` here on purpose: Settings (app/core/config.py) already
# loads .env itself via pydantic-settings. Having `just` *also* export it as real
# process env vars is redundant and, for a JSON-valued field like
# CORS__ALLOWED_ORIGINS, actively wrong — just's simpler dotenv parser mangles the
# array literal's quoting in a way pydantic-settings' own parser does not.

# Bring up Postgres + the API from a clean checkout with no manual steps.
up:
    docker compose -f compose/docker-compose.yml up -d --build
    just ensure-test-db
    just wait-ready

# Drain job queues (visuals/generate, brand research, media, …). Required when
# running the API via host uvicorn instead of `just up` (compose already starts
# its own worker service).
worker:
    uv run python -m app.workers.worker

# Poll /health/ready until the stack answers or this gives up.
wait-ready:
    #!/usr/bin/env bash
    set -euo pipefail
    for _ in $(seq 1 30); do
        if curl -sf http://localhost:8000/health/ready > /dev/null; then
            echo "ready"
            exit 0
        fi
        sleep 1
    done
    echo "never became ready" >&2
    exit 1

down:
    docker compose -f compose/docker-compose.yml down

# Create pgblank_test if missing (idempotent). Pytest + alembic roundtrip use this
# DB exclusively so downgrade-to-base never wipes the live acceptance volume.
ensure-test-db:
    #!/usr/bin/env bash
    set -euo pipefail
    compose=(docker compose -f compose/docker-compose.yml)
    "${compose[@]}" up -d postgres
    for _ in $(seq 1 30); do
        if "${compose[@]}" exec -T postgres pg_isready -U pgblank -d pgblank >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    exists="$("${compose[@]}" exec -T postgres \
        psql -U pgblank -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='pgblank_test'")"
    if [[ "$exists" != "1" ]]; then
        "${compose[@]}" exec -T postgres \
            psql -U pgblank -d postgres -c "CREATE DATABASE pgblank_test OWNER pgblank;"
        echo "created database pgblank_test"
    else
        echo "pgblank_test already exists"
    fi

lint:
    uv run ruff check .
    uv run mypy .
    uv run lint-imports

fmt:
    uv run ruff format .

test: ensure-test-db
    #!/usr/bin/env bash
    set -euo pipefail
    # Force the test DB even if .env points at live pgblank.
    export DATABASE__URL="${DATABASE__URL:-postgresql+asyncpg://pgblank:pgblank@localhost:5544/pgblank}"
    if [[ "$DATABASE__URL" == */pgblank ]]; then
      export DATABASE__URL="${DATABASE__URL%/pgblank}/pgblank_test"
    elif [[ "$DATABASE__URL" != */pgblank_test ]]; then
      export DATABASE__URL="postgresql+asyncpg://pgblank:pgblank@localhost:5544/pgblank_test"
    fi
    uv run alembic upgrade head
    uv run pytest

migrate *ARGS:
    uv run alembic {{ARGS}}

migrate-up:
    uv run alembic upgrade head

migrate-down:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Refusing to downgrade the live DB." >&2
    echo "Destructive alembic roundtrips run only against pgblank_test via \`just test\`." >&2
    echo "To wipe the test DB: DATABASE__URL=.../pgblank_test uv run alembic downgrade base" >&2
    exit 1

# Apply CORS rules from infra/r2/cors.json to the configured public bucket.
# Requires wrangler and Cloudflare credentials (R2_API_TOKEN / wrangler login).
r2-cors:
    #!/usr/bin/env bash
    set -euo pipefail
    bucket="${STORAGE__PUBLIC_BUCKET:-pgblank}"
    wrangler r2 bucket cors set "$bucket" --file ../r2/cors.json

# Apply lifecycle rules from infra/r2/lifecycle.json (tmp/ 2d, gen/pending/ 7d).
# Accepts either CLOUDFLARE_API_TOKEN or R2_API_TOKEN (mapped for wrangler).
r2-lifecycle:
    #!/usr/bin/env bash
    set -euo pipefail
    bucket="${STORAGE__PUBLIC_BUCKET:-pgblank}"
    if [[ -z "${CLOUDFLARE_API_TOKEN:-}" && -n "${R2_API_TOKEN:-}" ]]; then
      export CLOUDFLARE_API_TOKEN="$R2_API_TOKEN"
    fi
    wrangler r2 bucket lifecycle set "$bucket" --file ../r2/lifecycle.json
