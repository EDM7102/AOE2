#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${BOT_TOKEN:-}" ]]; then
  echo "BOT_TOKEN muss gesetzt sein." >&2
  exit 1
fi

# Optional: SCRAPER_PROXY="http://proxy:3128" setzen oder SCRAPER_NO_PROXY=1,
# falls AoE2Insights nur ohne/via Proxy erreichbar ist.

CHAT_ARG=()
if [[ -n "${CHAT_ID:-}" ]]; then
  CHAT_ARG+=("CHAT_ID=${CHAT_ID}")
fi

echo "Starte AoE2 Bot..."
exec env "BOT_TOKEN=${BOT_TOKEN}" "${CHAT_ARG[@]}" python main.py
