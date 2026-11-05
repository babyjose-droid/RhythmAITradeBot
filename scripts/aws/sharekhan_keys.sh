#!/usr/bin/env bash
# Store the Sharekhan API Key and Secure Key in .env on THIS server and restart the app.
# The keys are typed at hidden prompts: they are not shown, not kept in shell history and
# never leave the server. The link is read-only: no order code exists.
#
# Usage (on the server, from the repository folder):  bash scripts/aws/sharekhan_keys.sh
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -f .env ]] || { echo "No .env: run scripts/aws/setup.sh first." >&2; exit 1; }

read -r -s -p "Sharekhan API Key: " api_key; echo
read -r -s -p "Sharekhan Secure Key (32 characters): " secure_key; echo
read -r -s -p "Vendor key (press Enter if you have none): " vendor_key; echo

api_key="$(printf '%s' "$api_key" | tr -d '[:space:]')"
secure_key="$(printf '%s' "$secure_key" | tr -d '[:space:]')"
vendor_key="$(printf '%s' "$vendor_key" | tr -d '[:space:]')"
[[ -n "$api_key" ]] || { echo "API Key is empty." >&2; exit 1; }
for v in "$api_key" "$secure_key" "$vendor_key"; do
  if [[ "$v" == *"'"* ]]; then echo "A key contains a quote character; check what you pasted." >&2; exit 1; fi
done
if [[ "${#secure_key}" -ne 32 ]]; then
  echo "The Secure Key must be exactly 32 characters (got ${#secure_key}). Nothing changed." >&2
  exit 1
fi

set_var() {  # replace or append KEY=VALUE without echoing the value
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  grep -v -E "^#?[[:space:]]*${key}=" .env > "$tmp" || true
  printf "%s='%s'\n" "$key" "$value" >> "$tmp"
  cat "$tmp" > .env
  rm -f "$tmp"
}
umask 077
set_var RHYTHM_BROKER_ADAPTER sharekhan
set_var RHYTHM_SHAREKHAN__API_KEY "$api_key"
set_var RHYTHM_SHAREKHAN__SECRET_KEY "$secure_key"
if [[ -n "$vendor_key" ]]; then set_var RHYTHM_SHAREKHAN__VENDOR_KEY "$vendor_key"; fi
chmod 600 .env
unset api_key secure_key vendor_key

echo "Saved. Restarting the API and scheduler..."
sudo docker compose --env-file .env up -d api scheduler
sleep 8
if sudo docker compose ps --status running --services | grep -qx api; then
  echo "Running. In the dashboard open 'Sharekhan (read only)' and click Connect Sharekhan."
else
  echo "The API did not start. Last log lines (no secrets are logged):" >&2
  sudo docker compose logs --tail 20 api >&2
  exit 1
fi
