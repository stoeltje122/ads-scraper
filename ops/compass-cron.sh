#!/usr/bin/env bash
# Compass daily collect wrapper — meant to be called by cron or launchd.
# - Resolves the repo root from this script's own location, so the crontab
#   line only needs an absolute path to this file.
# - Appends all output to logs/cron-compass.log.
# - Exits with the exit code of `compass collect` (or the weekly report),
#   so schedulers can see failures.
set -euo pipefail

# ops/ -> repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

mkdir -p logs

# From here on, everything (stdout + stderr) goes to logs/cron-compass.log.
exec >>"logs/cron-compass.log" 2>&1

echo "──────────────────────────────────────────────────────────────"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Compass dagelijkse run gestart (${REPO_ROOT})"

if [ ! -x ".venv/bin/compass" ]; then
    echo "FOUT: .venv/bin/compass niet gevonden."
    echo "Draai eerst de Snelstart uit compass/README.md (venv aanmaken + pip install)."
    exit 1
fi

# shellcheck disable=SC1091
source ".venv/bin/activate"

status=0
compass collect || status=$?

# On Mondays (ISO weekday 1), also generate the weekly report.
if [ "$(date +%u)" = "1" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Maandag: weekrapport genereren"
    compass report --weekly || status=$?
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Klaar (exitcode ${status})"
exit "${status}"
