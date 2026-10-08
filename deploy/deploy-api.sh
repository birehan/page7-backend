#!/usr/bin/env bash
# Deploy page7-api (staging) to Cloud Run.
# Usage:
#   PROJECT=page7-509810 REGION=me-central1 IMAGE_TAG=<sha> ./deploy/deploy-api.sh
set -euo pipefail

PROJECT=${PROJECT:-page7-509810}
REGION=${REGION:-me-central1}
AR_REPO=${AR_REPO:-page7}
SQL_INSTANCE=${SQL_INSTANCE:-page7}
SERVICE=${SERVICE:-page7-api}
IMAGE_TAG=${IMAGE_TAG:?IMAGE_TAG (git sha) is required}
FRONTEND_ORIGIN=${FRONTEND_ORIGIN:-https://page7-git-staging-birehans-projects.vercel.app}
APP_ENV=${APP_ENV:-staging}
COOKIE_SAMESITE=${COOKIE_SAMESITE:-none}
OAUTH_CALLBACK_URL="${FRONTEND_ORIGIN}/api/integrations/zernio/callback"
CONNECTION_NAME="${PROJECT}:${REGION}:${SQL_INSTANCE}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/api:${IMAGE_TAG}"

gcloud config set project "$PROJECT" --quiet

if ! gcloud artifacts docker images describe "$IMAGE" --project="$PROJECT" &>/dev/null; then
  echo "Image not found: $IMAGE" >&2
  exit 1
fi

SECRETS=()
add_secret_env() {
  local env_name=$1
  local secret_name=$2
  if gcloud secrets describe "$secret_name" --project="$PROJECT" &>/dev/null; then
    SECRETS+=("${env_name}=${secret_name}:latest")
  fi
}

add_secret_env DATABASE__URL DATABASE_URL
add_secret_env APP_ENCRYPTION_KEY_CURRENT APP_ENCRYPTION_KEY_CURRENT
add_secret_env APP_ENCRYPTION_KEY__k1 APP_ENCRYPTION_KEY__k1
add_secret_env STORAGE__R2_ACCOUNT_ID STORAGE__R2_ACCOUNT_ID
add_secret_env STORAGE__R2_ACCESS_KEY_ID STORAGE__R2_ACCESS_KEY_ID
add_secret_env STORAGE__R2_SECRET_ACCESS_KEY STORAGE__R2_SECRET_ACCESS_KEY
add_secret_env STORAGE__PUBLIC_BUCKET STORAGE__PUBLIC_BUCKET
add_secret_env STORAGE__PUBLIC_BASE_URL STORAGE__PUBLIC_BASE_URL
add_secret_env LLM__OPENAI_API_KEY LLM__OPENAI_API_KEY
add_secret_env LLM__ANTHROPIC_API_KEY LLM__ANTHROPIC_API_KEY
add_secret_env IMAGEGEN__API_KEY IMAGEGEN__API_KEY
add_secret_env EMAIL__RESEND_API_KEY EMAIL__RESEND_API_KEY
add_secret_env EMAIL__SMTP_PASSWORD EMAIL__SMTP_PASSWORD
add_secret_env AUTH__GOOGLE_CLIENT_ID AUTH__GOOGLE_CLIENT_ID
add_secret_env AUTH__GOOGLE_CLIENT_SECRET AUTH__GOOGLE_CLIENT_SECRET
add_secret_env STOCK__UNSPLASH_ACCESS_KEY STOCK__UNSPLASH_ACCESS_KEY
add_secret_env OBSERVABILITY__SENTRY_DSN OBSERVABILITY__SENTRY_DSN
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

echo "==> Deploying $SERVICE from $IMAGE"
gcloud run deploy "$SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --min-instances=0 \
  --cpu=1 \
  --memory=1Gi \
  --timeout=300 \
  --port=8080 \
  --add-cloudsql-instances="$CONNECTION_NAME" \
  --set-env-vars="^|^APP_ENV=${APP_ENV}|DATABASE__POOL_SIZE=2|CORS__ALLOWED_ORIGINS=[\"${FRONTEND_ORIGIN}\"]|AUTH__FRONTEND_URL=${FRONTEND_ORIGIN}|AUTH__COOKIE_SAMESITE=${COOKIE_SAMESITE}|TRUSTED_PROXY_HOPS=1|STORAGE__PROVIDER=r2|LLM__PROVIDER=auto|SOCIAL__PROVIDER=auto|EMAIL__PROVIDER=smtp|ZERNIO_CREDENTIAL_ALIASES=t1,t2,t3,t4|SOCIAL__CALLBACK_REDIRECT_ORIGIN=${FRONTEND_ORIGIN}|SOCIAL__OAUTH_CALLBACK_URL=${OAUTH_CALLBACK_URL}|SOCIAL__FILL_TO_TIER=3" \
  "${SECRET_FLAGS[@]}" \
  --quiet

URL=$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')
gcloud run services update "$SERVICE" \
  --region="$REGION" \
  --update-env-vars="AUTH__GOOGLE_REDIRECT_URI=${URL}/v1/auth/google/callback" \
  --quiet

echo "Deployed: $URL"
