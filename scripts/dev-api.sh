#!/usr/bin/env sh
set -eu

status="$(supabase status -o env)"
anon_key="$(
  printf '%s\n' "$status" |
    awk -F= '$1 == "ANON_KEY" { sub(/^[^=]*=/, ""); gsub(/^"|"$/, ""); print; exit }'
)"

if [ -z "$anon_key" ]; then
  printf '%s\n' "Unable to read the local Supabase anon key. Start Supabase first." >&2
  exit 1
fi

PIA_SUPABASE_ANON_KEY="$anon_key" \
  exec uv run --directory apps/api uvicorn pia_api.main:app --reload
