"""Notifier interface — channels (e-mail, Slack) can be added later.

Deliberately minimal for now: the weekly report lands on disk and the
NullNotifier logs where it is. A future EmailNotifier/SlackNotifier
implements the same interface and is selected via NOTIFY_CHANNEL in .env,
without touching report generation.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send_report(self, subject: str, markdown: str, attachments: list[Path]) -> None:
        """Deliver a generated report to the configured channel."""


class NullNotifier(Notifier):
    def send_report(self, subject: str, markdown: str, attachments: list[Path]) -> None:
        logger.info("Geen notificatiekanaal geconfigureerd. Rapport staat klaar: %s",
                    ", ".join(str(p) for p in attachments))


def get_notifier() -> Notifier:
    """Channel selection point. For now always NullNotifier; later:
    read NOTIFY_CHANNEL from settings and return the matching implementation."""
    return NullNotifier()
