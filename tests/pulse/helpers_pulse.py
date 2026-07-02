"""Shared constants for the Pulse test suite.

In its own module (not conftest.py) so test files can import it by a
unique name — the repo has two conftest.py files (adscout + pulse) and a
plain `import conftest` could resolve to either.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

TESTS_DIR = Path(__file__).parent.parent
REPO_ROOT = TESTS_DIR.parent
PULSE_FIXTURE_DIR = TESTS_DIR / "fixtures" / "pulse"

# The day this repo's Pulse fixtures were written around. All fixture dates
# are in the (recent) past relative to this day, so assertions stay
# deterministic.
RUN_DAY = date(2026, 7, 2)
