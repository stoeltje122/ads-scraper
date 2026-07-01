#!/usr/bin/env bash
# AdScout daily collect wrapper — meant to be called by cron or launchd.
# - Resolves the repo root from this script's own location, so the crontab
#   line only needs an absolute path to this file.
# - Appends all output to logs/cron.log.
# - Exits with the exit code of `adscout collect` (or the weekly report),
#   so schedulers can see failures.
set -euo pipefail

# ops/ -> repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

mkdir -p logs

# From here on, everything (stdout + stderr) goes to logs/cron.log.
exec >>"logs/cron.log" 2>&1

echo "──────────────────────────────────────────────────────────────"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] AdScout dagelijkse run gestart (${REPO_ROOT})"

if [ ! -x ".venv/bin/adscout" ]; then
    echo "FOUT: .venv/bin/adscout niet gevonden."
    echo "Draai eerst de Snelstart uit README.md (venv aanmaken + pip install)."
    exit 1
fi

# shellcheck disable=SC1091
source ".venv/bin/activate"

status=0
adscout collect || status=$?

# On Mondays (ISO weekday 1), also generate the weekly report.
if [ "$(date +%u)" = "1" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Maandag: weekrapport genereren"
    adscout report --weekly || status=$?
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Klaar (exitcode ${status})"
exit "${status}"
