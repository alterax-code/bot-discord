"""Journalisation.

Tout part sur la sortie standard : c'est Docker qui collecte, applique la
rotation et sert « docker compose logs ». Écrire nous-mêmes dans des fichiers
ferait doublon et remplirait le disque deux fois.

Les horodatages sont dans le fuseau configuré (Europe/Paris), pas en UTC :
quand tu lis un journal à 3 h du matin pour comprendre un incident, tu ne veux
pas avoir à faire la conversion de tête.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


class _LocalFormatter(logging.Formatter):
    """Formateur qui horodate dans le fuseau du serveur RP plutôt qu'en UTC."""

    def __init__(self, fmt: str, tz: ZoneInfo) -> None:
        super().__init__(fmt, datefmt="%Y-%m-%d %H:%M:%S")
        self._tz = tz

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        moment = datetime.fromtimestamp(record.created, tz=self._tz)
        return moment.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def setup(level: str, tz: ZoneInfo) -> None:
    """Configure la journalisation globale. À appeler une seule fois, au démarrage."""
    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        numeric = logging.INFO

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _LocalFormatter("%(asctime)s  %(levelname)-8s %(name)-24s %(message)s", tz)
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric)

    # discord.py est très bavard en DEBUG (chaque battement de cœur de la
    # connexion y passe). On le maintient plus haut tant qu'on ne débogue
    # pas le réseau lui-même.
    logging.getLogger("discord").setLevel(max(numeric, logging.INFO))
    logging.getLogger("discord.http").setLevel(max(numeric, logging.WARNING))
    logging.getLogger("discord.gateway").setLevel(max(numeric, logging.WARNING))
