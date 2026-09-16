"""Whitelist du serveur de jeu : les rôles staff Discord, envoyés au portail.

Le rôle Discord est la vérité. Le bot regarde qui porte l'un des rôles staff
de config.toml et dépose la liste COMPLÈTE de leurs identifiants Discord sur
le portail (PUT /api/pont/staff). Le portail traduit chaque identifiant en
SteamID grâce à la liaison Discord ↔ Steam du compte, et le serveur de jeu
vient lire la liste qui en résulte. Un membre qui n'a pas lié son Discord sur
le portail n'entre pas — c'est voulu, c'est la seule preuve d'identité.

Quand envoyer : au démarrage, à chaque changement de rôle ou départ d'un
membre (regroupés sur cinq secondes : une réorganisation de rôles fait dix
événements, pas dix envois), et toutes les dix minutes par sécurité — si un
envoi s'est perdu, le suivant remet tout d'équerre.

La liste envoyée remplace la précédente côté portail. C'est pour ça qu'on
envoie toujours tout : aucun état à réconcilier, aucune suppression à
rejouer.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import discord

from .config import Config

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 5


class StaffSync:
    def __init__(self, client: discord.Client, config: Config):
        self.client = client
        self.config = config
        self.cfg = config.whitelist
        self._debounce: asyncio.Task | None = None
        self._loop: asyncio.Task | None = None
        self.dernier_envoi: int | None = None

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    # -- lecture ----------------------------------------------------------

    def staff_ids(self, guild: discord.Guild) -> list[str]:
        """Les membres humains qui portent au moins un rôle staff."""
        roles = self.cfg.staff_roles
        return sorted(
            {str(m.id) for m in guild.members if not m.bot and any(r.id in roles for r in m.roles)}
        )

    def roles_staff_changent(self, before: discord.Member, after: discord.Member) -> bool:
        roles = self.cfg.staff_roles
        avant = {r.id for r in before.roles} & roles
        apres = {r.id for r in after.roles} & roles
        return avant != apres

    # -- envoi ------------------------------------------------------------

    async def push(self, motif: str) -> None:
        if not self.enabled:
            return
        guild = self.client.get_guild(self.config.guild_id)
        if guild is None:
            log.warning("Whitelist (%s) : serveur Discord introuvable, rien envoyé.", motif)
            return

        ids = self.staff_ids(guild)
        url = self.cfg.portail_url.rstrip("/") + "/api/pont/staff"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.put(
                    url,
                    json={"discord_ids": ids},
                    headers={"Authorization": f"Bearer {self.cfg.secret}"},
                ) as r:
                    corps = (await r.text())[:200]
                    if r.status == 200:
                        self.dernier_envoi = len(ids)
                        log.info("Whitelist (%s) : %d staff Discord envoyés au portail → %s", motif, len(ids), corps)
                    else:
                        log.warning("Whitelist (%s) : le portail répond HTTP %s → %s", motif, r.status, corps)
        except Exception as exc:  # réseau, DNS, portail en redéploiement
            log.warning("Whitelist (%s) : portail injoignable (%s) : %s", motif, url, exc)

    def planifier(self, motif: str) -> None:
        """Regroupe les changements rapprochés en un seul envoi."""
        if not self.enabled:
            return
        if self._debounce and not self._debounce.done():
            self._debounce.cancel()

        async def plus_tard() -> None:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            await self.push(motif)

        self._debounce = asyncio.create_task(plus_tard())

    async def demarrer(self) -> None:
        if not self.enabled:
            log.info("Whitelist : désactivée (PORTAIL_SECRET absent, ou [whitelist] sans rôles).")
            return
        await self.push("démarrage")
        self._loop = asyncio.create_task(self._resynchroniser())

    async def _resynchroniser(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.resync_seconds)
            await self.push("resynchronisation")
