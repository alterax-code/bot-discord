"""Point d'entrée : python -m bot

Traduit les pannes courantes en messages compréhensibles plutôt qu'en traces
d'exception. Une erreur de configuration doit se lire, pas se déchiffrer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import discord

from . import __version__
from .client import GrandLineBot
from .config import ConfigError, load
from .database import Database
from .logging_setup import setup as setup_logging

log = logging.getLogger("bot")


async def _run(check_only: bool) -> int:
    try:
        config = load(os.environ.get("CONFIG_PATH", "config.toml"))
    except ConfigError as exc:
        print(f"\n[CONFIGURATION INVALIDE]\n{exc}\n", file=sys.stderr)
        return 2

    setup_logging(config.log_level, config.archive.timezone)
    log.info("Bot de tickets Grand Line RP v%s", __version__)
    log.info("Fuseau de découpage de l'archive : %s", config.archive.timezone_name)
    if check_only:
        log.info("Mode DIAGNOSTIC : vérification de l'environnement puis arrêt.")

    db = Database(Path(os.environ.get("DATA_DIR", "data")) / "grandline.db")
    await db.connect()

    bot = GrandLineBot(config, db, check_only=check_only)
    try:
        await bot.start(config.token)
    except discord.LoginFailure:
        log.error(
            "Discord a refusé le jeton.\n"
            "  Il est invalide ou a été régénéré. Reprends-en un sur le portail "
            "développeur (Bot -> Reset Token) et remets-le dans .env."
        )
        return 3
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Discord refuse l'intent privilégié « Server Members ».\n"
            "  Va sur https://discord.com/developers/applications -> Grand Line RP\n"
            "  -> onglet Bot -> Privileged Gateway Intents -> active SERVER MEMBERS INTENT,\n"
            "  puis enregistre et relance le bot."
        )
        return 4
    except asyncio.CancelledError:
        log.info("Arrêt demandé.")
    finally:
        if not bot.is_closed():
            await bot.close()
        await db.close()

    # En mode diagnostic, le code de sortie EST le verdict : 0 si tout va bien,
    # 5 sinon. Un script de déploiement peut donc s'arrêter avant de casser
    # la production.
    if check_only and not bot.check_passed:
        return 5
    return 0


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    try:
        return asyncio.run(_run(check_only))
    except KeyboardInterrupt:
        print("\nArrêt manuel.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
