#!/usr/bin/env bash
# Watches host memory while the local Flyte devbox (OrbStack) is running, and
# runs `flyte stop devbox` before macOS starts swapping under pressure.
#
# This is a secondary safety net. The primary one is OrbStack's own memory
# limit (Settings -> Resources -> Memory limit), which is a hard VM ceiling
# enforced by the hypervisor. This script watches the *host's* memory, which
# also covers RAM used outside the OrbStack VM (your editor, browser, etc.)
# stacking on top of it.
#
# Usage:
#   scripts/devbox-mem-guard.sh [--limit-percent 85] [--interval 10]
set -euo pipefail

LIMIT_PERCENT=85
INTERVAL=10

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit-percent)
      LIMIT_PERCENT="$2"
      shift 2
      ;;
    --interval)
      INTERVAL="$2"
      shift 2
      ;;
    -h | --help)
      echo "Usage: $0 [--limit-percent N] [--interval SECONDS]"
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$(uname)" != "Darwin" ]]; then
  echo "devbox-mem-guard.sh only supports macOS (it parses 'vm_stat')." >&2
  exit 1
fi

mem_used_percent() {
  vm_stat | awk '
    /^Pages free/               { free = $NF }
    /^Pages active/             { active = $NF }
    /^Pages inactive/           { inactive = $NF }
    /^Pages wired down/         { wired = $NF }
    /^Pages occupied by compressor/ { compressed = $NF }
    END {
      gsub("\\.", "", free); gsub("\\.", "", active); gsub("\\.", "", inactive)
      gsub("\\.", "", wired); gsub("\\.", "", compressed)
      used = active + wired + compressed
      total = free + active + inactive + wired + compressed
      if (total > 0) printf "%d", (used / total) * 100
    }'
}

echo "devbox-mem-guard: watching host memory every ${INTERVAL}s, will stop the devbox at ${LIMIT_PERCENT}% used"

while true; do
  used="$(mem_used_percent)"
  echo "$(date '+%H:%M:%S') host memory used: ${used}%"

  if [[ -n "$used" ]] && (( used >= LIMIT_PERCENT )); then
    echo "!! host memory at ${used}% (>= ${LIMIT_PERCENT}%) — stopping the devbox to protect the system" >&2
    flyte stop devbox || echo "warning: 'flyte stop devbox' failed, stop it manually" >&2
    exit 1
  fi

  sleep "$INTERVAL"
done
