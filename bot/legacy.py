"""Lecture des anciens signalements postés à la main, avant le bot.

Opération ponctuelle et STRICTEMENT en lecture : elle ne publie rien, ne
supprime rien, n'écrit qu'un fichier local destiné à être relu.

Le but est de récupérer la vérité plutôt qu'une reconstitution : le véritable
auteur de chaque signalement, et surtout les véritables personnes derrière
chaque réaction — l'information exacte que l'archive doit conserver.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import discord

log = logging.getLogger(__name__)


async def scan_channel(
    bot: discord.Client, channel_id: int, out_path: Path
) -> dict[str, int]:
    """Parcourt l'historique d'un salon et écrit ce qu'on y trouve.

    Les messages écrits par le bot lui-même sont ignorés : ce sont ses propres
    tickets et son message permanent, pas d'anciens signalements.
    """
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError(f"Salon {channel_id} introuvable ou non textuel.")

    log.info("Lecture de l'historique de #%s…", channel.name)

    items: list[dict] = []
    stats = {"total": 0, "du_bot": 0, "retenus": 0, "sans_texte": 0}

    async for message in channel.history(limit=None, oldest_first=True):
        stats["total"] += 1
        if bot.user is not None and message.author.id == bot.user.id:
            stats["du_bot"] += 1
            continue

        # Les réactions : qui exactement, pas seulement combien.
        reactions: dict[str, list[dict]] = {}
        for reaction in message.reactions:
            users = []
            async for user in reaction.users():
                users.append({"id": user.id, "nom": getattr(user, "display_name", user.name)})
            reactions[str(reaction.emoji)] = users

        content = message.content or ""
        if not content.strip():
            stats["sans_texte"] += 1

        items.append(
            {
                "message_id": message.id,
                "date": message.created_at.isoformat(timespec="seconds"),
                "auteur_id": message.author.id,
                "auteur_nom": message.author.display_name,
                "texte": content,
                "longueur_texte": len(content),
                "pieces_jointes": [a.url for a in message.attachments],
                "reactions": reactions,
            }
        )
        stats["retenus"] += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Écrit : %s", out_path.resolve())
    return stats


def summarise(stats: dict[str, int]) -> str:
    lines = [
        f"messages parcourus            : {stats['total']}",
        f"  dont écrits par le bot      : {stats['du_bot']} (ignorés)",
        f"  retenus comme anciens       : {stats['retenus']}",
        f"  dont au texte VIDE          : {stats['sans_texte']}",
    ]
    if stats["sans_texte"]:
        lines.append("")
        lines.append(
            "  ⚠ Un texte vide signifie que Discord ne transmet pas le contenu :"
        )
        lines.append(
            "    l'intent « Message Content » est nécessaire pour lire les messages"
        )
        lines.append("    écrits par d'autres que le bot.")
    return "\n".join(lines)
