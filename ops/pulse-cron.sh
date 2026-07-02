#!/usr/bin/env bash
# Pulse daily run wrapper — meant to be called by cron or launchd.
# - Resolves the repo root from this script's own location, so the crontab
#   line only needs an absolute path to this file.
# - Appends all output to logs/cron.log.
# - collect always runs; analyze only when an ANTHROPIC_API_KEY is set
#   (without it, items simply wait in the queue — that is fine).
# - Exits non-zero on failure so schedulers can see it.
set -euo pipefail

# ops/ -> repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

mkdir -p logs

# From here on, everything (stdout + stderr) goes to logs/cron.log.
exec >>"logs/cron.log" 2>&1

echo "──────────────────────────────────────────────────────────────"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pulse dagelijkse run gestart (${REPO_ROOT})"

if [ ! -x ".venv/bin/pulse" ]; then
    echo "FOUT: .venv/bin/pulse niet gevonden."
    echo "Draai eerst de Snelstart uit PULSE.md (venv aanmaken + pip install)."
    exit 1
fi

# shellcheck disable=SC1091
source ".venv/bin/activate"

status=0
pulse collect || status=$?

# Analyze only when a key is configured (grep .env, ignore comments).
if grep -Eq '^\s*ANTHROPIC_API_KEY\s*=\s*\S' .env 2>/dev/null; then
    pulse analyze || status=$?
else
    echo "Geen ANTHROPIC_API_KEY in .env — analyse overgeslagen (items wachten in de wachtrij)."
fi

# On Mondays (ISO weekday 1), also generate the weekly report.
if [ "$(date +%u)" = "1" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Maandag: weekrapport genereren"
    pulse report --weekly || status=$?
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Klaar (exitcode ${status})"
exit "${status}"
