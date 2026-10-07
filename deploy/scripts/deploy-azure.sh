#!/usr/bin/env bash
# ==============================================================================
# SupportDesk — Automated Deployment Script for Azure Container Apps (ACA)
# Deploys backend API service with scale-to-zero (minReplicas=0) for cost efficiency
# ==============================================================================

set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-portfolio-prod}"
LOCATION="${LOCATION:-southeastasia}"
ACR_NAME="${ACR_NAME:-crportfolioa3deec}"
CONTAINERAPPS_ENVIRONMENT="${CONTAINERAPPS_ENVIRONMENT:-cae-portfolio-env}"
APP_NAME="supportdesk-api"
TARGET_PORT=8000
IMAGE_TAG="${IMAGE_TAG:-latest}"

echo "=========================================================="
echo "🚀 Deploying SupportDesk API to Azure Container Apps"
echo "Resource Group: $RESOURCE_GROUP | Location: $LOCATION"
echo "=========================================================="

if ! command -v az &> /dev/null; then
    echo "❌ Error: Azure CLI (az) is not installed."
    exit 1
fi

ACR_LOGIN_SERVER=$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query loginServer -o tsv)
ACR_ADMIN_PASSWORD=$(az acr credential show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query "passwords[0].value" -o tsv)
ACR_ADMIN_USERNAME=$(az acr credential show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query username -o tsv)

echo "🌐 Ensuring Container Apps Environment [$CONTAINERAPPS_ENVIRONMENT] exists..."
if ! az containerapp env show --name "$CONTAINERAPPS_ENVIRONMENT" --resource-group "$RESOURCE_GROUP" &> /dev/null; then
    az containerapp env create \
        --name "$CONTAINERAPPS_ENVIRONMENT" \
        --resource-group "$RESOURCE_GROUP" \
        --location "$LOCATION" \
        --output table
fi

# Load database URL and JWT secret from local .env if not provided in environment
if [ -z "${DATABASE_URL:-}" ] && [ -f "$(dirname "$0")/../../.env" ]; then
    DATABASE_URL=$(grep "^DATABASE_URL=" "$(dirname "$0")/../../.env" | cut -d'=' -f2-)
fi
if [ -z "${JWT_SECRET:-}" ] && [ -f "$(dirname "$0")/../../.env" ]; then
    JWT_SECRET=$(grep "^JWT_SECRET=" "$(dirname "$0")/../../.env" | cut -d'=' -f2-)
fi

echo "🚀 Deploying Container App [$APP_NAME] with Scale-to-Zero (min-replicas=0)..."

az containerapp create \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --environment "$CONTAINERAPPS_ENVIRONMENT" \
    --image "${ACR_LOGIN_SERVER}/${APP_NAME}:${IMAGE_TAG}" \
    --registry-server "$ACR_LOGIN_SERVER" \
    --registry-username "$ACR_ADMIN_USERNAME" \
    --registry-password "$ACR_ADMIN_PASSWORD" \
    --target-port "$TARGET_PORT" \
    --ingress external \
    --min-replicas 0 \
    --max-replicas 3 \
    --cpu 0.5 \
    --memory 1.0Gi \
    --env-vars \
        "PORT=8000" \
        "ENVIRONMENT=production" \
        "AI_PROVIDER=stub" \
        "AI_EMBED_PROVIDER=bow" \
        "DATABASE_URL=${DATABASE_URL:-sqlite:///./supportdesk.db}" \
        "JWT_SECRET=${JWT_SECRET:-supportdesk-default-secret-production}" \
    --output table || {
    echo "⚠️ Updating existing container app..."
    az containerapp update \
        --name "$APP_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --image "${ACR_LOGIN_SERVER}/${APP_NAME}:${IMAGE_TAG}" \
        --output table
}

APP_URL=$(az containerapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query "properties.configuration.ingress.fqdn" -o tsv)

echo "=========================================================="
echo "✅ DEPLOYMENT SUCCESSFUL!"
echo "🔗 Public Live API: https://${APP_URL}/docs"
echo "🔗 Health Check: https://${APP_URL}/api/health"
echo "=========================================================="
