#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH=/data/options.json

export ANTHROPIC_API_KEY="$(jq -r '.anthropic_api_key' "$CONFIG_PATH")"
export HA_BASE_URL="http://supervisor/core"
export HA_LONG_LIVED_TOKEN="$(jq -r '.ha_long_lived_token' "$CONFIG_PATH")"
export WHATSAPP_PHONE_NUMBER_ID="$(jq -r '.whatsapp_phone_number_id' "$CONFIG_PATH")"
export WHATSAPP_ACCESS_TOKEN="$(jq -r '.whatsapp_access_token' "$CONFIG_PATH")"
export WHATSAPP_VERIFY_TOKEN="$(jq -r '.whatsapp_verify_token' "$CONFIG_PATH")"
export WHATSAPP_APP_SECRET="$(jq -r '.whatsapp_app_secret' "$CONFIG_PATH")"
export ALLOWED_SENDER_NUMBER="$(jq -r '.allowed_sender_number' "$CONFIG_PATH")"
export CONFIRMATION_TTL_SECONDS="$(jq -r '.confirmation_ttl_seconds' "$CONFIG_PATH")"
export ENTITIES_CONFIG_PATH="/app/config/entities.yaml"

cd /app
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
