#!/usr/bin/env bash
# Bootstrap GCP resources for page7-backend (idempotent).
# Usage: PROJECT=page7-509810 REGION=me-central1 ./deploy/bootstrap-gcp.sh
#
# Called by: operators once (docs/RELEASE.md). Not invoked by Cloud Build.
# No existing bootstrap script in this repo (deploy/ was empty).
set -euo pipefail

PROJECT=${PROJECT:-page7-509810}
REGION=${REGION:-me-central1}
AR_REPO=${AR_REPO:-page7}
SQL_INSTANCE=${SQL_INSTANCE:-page7}

gcloud config set project "$PROJECT" --quiet

echo "==> Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  compute.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  --quiet

echo "==> Artifact Registry repo: $AR_REPO ($REGION)"
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="page7 backend images" \
  --quiet 2>/dev/null || echo "  (repo already exists)"

echo "==> Cloud SQL instance (must already exist): $SQL_INSTANCE"
gcloud sql instances describe "$SQL_INSTANCE" --project="$PROJECT" \
  --format='value(name,region,state,connectionName)'

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
DEPLOY_SA="cloudbuild-deploy@${PROJECT}.iam.gserviceaccount.com"

echo "==> Ensure deploy service account: $DEPLOY_SA"
gcloud iam service-accounts create cloudbuild-deploy \
  --display-name="page7 Cloud Build deploy" \
  --quiet 2>/dev/null || echo "  (SA already exists)"

bind_role() {
  local member=$1
  local role=$2
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="$member" \
    --role="$role" \
    --condition=None \
    --quiet >/dev/null
}

echo "==> IAM for Cloud Build default SA and deploy SA"
for MEMBER in "serviceAccount:${CB_SA}" "serviceAccount:${DEPLOY_SA}"; do
  bind_role "$MEMBER" roles/run.admin
  bind_role "$MEMBER" roles/artifactregistry.writer
  bind_role "$MEMBER" roles/secretmanager.secretAccessor
  bind_role "$MEMBER" roles/secretmanager.viewer
  bind_role "$MEMBER" roles/cloudsql.client
  bind_role "$MEMBER" roles/iam.serviceAccountUser
  bind_role "$MEMBER" roles/logging.logWriter
  bind_role "$MEMBER" roles/cloudbuild.builds.builder
done

echo "==> IAM for runtime compute SA"
bind_role "serviceAccount:${COMPUTE_SA}" roles/secretmanager.secretAccessor
bind_role "serviceAccount:${COMPUTE_SA}" roles/cloudsql.client

gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --member="serviceAccount:${CB_SA}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet >/dev/null 2>&1 || true

echo
echo "Next:"
echo "  1. PROJECT=$PROJECT ./deploy/create-secrets.sh"
echo "  2. Connect GitHub repo birehan/page7-backend to Cloud Build and create triggers"
echo "  3. Push main → staging; tag v* → prod"
echo "Connection name: ${PROJECT}:${REGION}:${SQL_INSTANCE}"
