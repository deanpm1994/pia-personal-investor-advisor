#!/usr/bin/env sh
set -eu

key_file="supabase/signing_keys.json"

if [ -f "$key_file" ] && jq -e 'type == "array" and length > 0' "$key_file" >/dev/null; then
  exit 0
fi

printf '[]\n' >"$key_file"
supabase gen signing-key --algorithm ES256 --append >/dev/null
jq -e 'type == "array" and length > 0' "$key_file" >/dev/null
