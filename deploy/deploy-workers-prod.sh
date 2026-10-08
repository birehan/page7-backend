#!/usr/bin/env bash
# Deploy page7-worker-prod and page7-scheduler-prod.
# Callers: cloudbuild.prod.yaml. Uses DATABASE_URL_PROD.
set -euo pipefail

PROJECT=${PROJECT:-page7-509810}
REGION=${REGION:-me-central1}
AR_REPO=${AR_REPO:-page7}
SQL_INSTANCE=${SQL_INSTANCE:-page7}
IMAGE_TAG=${IMAGE_TAG:?IMAGE_TAG required}
APP_ENV=${APP_ENV:-production}
DB_SECRET=${DB_SECRET:-DATABASE_URL_PROD}
CONNECTION_NAME="${PROJECT}:${REGION}:${SQL_INSTANCE}"

gcloud config set project "$PROJECT" --quiet

deploy_pool() {
  local name=$1
  local target=$2
  local image="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/${target}:${IMAGE_TAG}"

  SECRETS=()
  add_secret_env() {
    local env_name=$1
    local secret_name=$2
    if gcloud secrets describe "$secret_name" --project="$PROJECT" &>/dev/null; then
      SECRETS+=("${env_name}=${secret_name}:latest")
    fi
  }
  add_secret_env DATABASE__URL "$DB_SECRET"
  add_secret_env APP_ENCRYPTION_KEY_CURRENT APP_ENCRYPTION_KEY_CURRENT
  add_secret_env APP_ENCRYPTION_KEY__k1 APP_ENCRYPTION_KEY__k1
  add_secret_env LLM__OPENAI_API_KEY LLM__OPENAI_API_KEY
  add_secret_env LLM__ANTHROPIC_API_KEY LLM__ANTHROPIC_API_KEY
  add_secret_env IMAGEGEN__API_KEY IMAGEGEN__API_KEY
  # email_send jobs need SMTP (API already sets EMAIL__PROVIDER=smtp).
  add_secret_env EMAIL__SMTP_PASSWORD EMAIL__SMTP_PASSWORD
  add_secret_env EMAIL__RESEND_API_KEY EMAIL__RESEND_API_KEY
  add_secret_env STORAGE__R2_ACCOUNT_ID STORAGE__R2_ACCOUNT_ID
  add_secret_env STORAGE__R2_ACCESS_KEY_ID STORAGE__R2_ACCESS_KEY_ID
  add_secret_env STORAGE__R2_SECRET_ACCESS_KEY STORAGE__R2_SECRET_ACCESS_KEY
  add_secret_env STORAGE__PUBLIC_BUCKET STORAGE__PUBLIC_BUCKET
  add_secret_env STORAGE__PUBLIC_BASE_URL STORAGE__PUBLIC_BASE_URL
  add_secret_env ZERNIO_API_KEY__t1 ZERNIO_API_KEY__t1
  add_secret_env ZERNIO_API_KEY__t2 ZERNIO_API_KEY__t2
  add_secret_env ZERNIO_API_KEY__t3 ZERNIO_API_KEY__t3
  add_secret_env ZERNIO_API_KEY__t4 ZERNIO_API_KEY__t4
  add_secret_env ZERNIO_WEBHOOK_SECRET__t1 ZERNIO_WEBHOOK_SECRET__t1
  add_secret_env ZERNIO_WEBHOOK_SECRET__t2 ZERNIO_WEBHOOK_SECRET__t2
  add_secret_env ZERNIO_WEBHOOK_SECRET__t3 ZERNIO_WEBHOOK_SECRET__t3
  add_secret_env ZERNIO_WEBHOOK_SECRET__t4 ZERNIO_WEBHOOK_SECRET__t4

  SECRET_FLAGS=()
  if ((${#SECRETS[@]})); then
    SECRET_FLAGS=(--set-secrets="$(IFS=,; echo "${SECRETS[*]}")")
  fi

  echo "==> Deploying worker pool $name from $image"
  gcloud run worker-pools deploy "$name" \
    --image="$image" \
    --region="$REGION" \
    --instances=1 \
    --cpu=1 \
    --memory=1Gi \
    --set-cloudsql-instances="$CONNECTION_NAME" \
    --set-env-vars="^|^APP_ENV=${APP_ENV}|DATABASE__POOL_SIZE=2|STORAGE__PROVIDER=r2|LLM__PROVIDER=auto|SOCIAL__PROVIDER=auto|EMAIL__PROVIDER=smtp|EMAIL__SMTP_HOST=smtp.hostinger.com|EMAIL__SMTP_PORT=465|EMAIL__SMTP_USERNAME=contact@page7.io|EMAIL__SMTP_USE_TLS=true|ZERNIO_CREDENTIAL_ALIASES=t1,t2,t3,t4|SOCIAL__FILL_TO_TIER=3" \
    "${SECRET_FLAGS[@]}" \
    --quiet
}

deploy_pool page7-worker-prod worker
deploy_pool page7-scheduler-prod scheduler

echo "Prod worker pools deployed."
