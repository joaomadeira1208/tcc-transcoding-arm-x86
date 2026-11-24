#!/usr/bin/env bash

set -euo pipefail

{
  printf '%s\0' "$@"
  printf '\n'
} >>"$SMOKE_ARGV_DIR/ffprobe.argv"
printf 'ffprobe\n' >>"$SMOKE_ARGV_DIR/sequence"

for last in "$@"; do :; done

response=$SMOKE_PROBE_DIR/$(basename "$last").json
if [[ ! -r $response ]]; then
  printf 'smoke ffprobe: sem resposta preparada para %s\n' "$last" >&2
  exit 255
fi

cat "$response"
