"""Miroir des bans Discord vers le serveur de jeu, par le portail.

Décision Lucas, 17 septembre 2026 : un ban posé sur le Discord Grand Line RP
entraîne le même ban sur le serveur de jeu ; une levée le lève. Une expulsion,
une sourdine ou un retrait de rôle ne font rien. Sens unique : rien ne
revient du jeu vers Discord.

Le bot ne bannit personne et ne connaît pas les SteamID : il dit au portail
QUI est banni sur Discord (PUT /api/pont/bans), le portail traduit en SteamID
par le compte lié et dépose un ordre au jeu. Deux envois :

  • un ÉVÉNEMENT à chaque ban ou levée (on_member_ban / on_member_unban) ;
  • la LISTE COMPLÈTE à intervalle, si Discord nous laisse la lire : un
    événement perdu pendant un redémarrage est rattrapé au passage suivant.
    Lire la liste des bans exige la permission « Bannir des membres » sur le
    rôle du bot ; sans elle, seuls les événements partent, et on le dit.

Les maîtres (config [sanctions].maitres) ne sont jamais transmis : même
bannis du Discord, ils restent maîtres du jeu. Le portail les exclut aussi.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import discord

from .config import Config

log = logging.getLogger(__name__)


class BanSync:
    def __init__(self, client: discord.Client, config: Config):
        self.client = client
        self.config = config
        self.cfg = config.whitelist          # même portail, même secret, même cadence
        self.maitres = config.sanctions.maitres
        self._loop: asyncio.Task | None = None
        self._liste_refusee_dite = False

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    # -- envoi ------------------------------------------------------------

    async def _envoyer(self, motif: str, *, bans: list[dict], leves: list[str], complet: bool) -> None:
        url = self.cfg.portail_url.rstrip("/") + "/api/pont/bans"
        corps = {"complet": complet, "bans": bans, "leves": leves}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.put(url, json=corps, headers={"Authorization": f"Bearer {self.cfg.secret}"}) as r:
                    texte = (await r.text())[:200]
                    if r.status == 200:
                        log.info("Bans (%s) : %d ban(s), %d levée(s) envoyés au portail → %s", motif, len(bans), len(leves), texte)
                    else:
                        log.warning("Bans (%s) : le portail répond HTTP %s → %s", motif, r.status, texte)
        except Exception as exc:  # réseau, DNS, portail en redéploiement
            log.warning("Bans (%s) : portail injoignable (%s) : %s", motif, url, exc)

    async def _motif(self, guild: discord.Guild, user: discord.abc.Snowflake) -> str | None:
        """Le motif du ban, si Discord nous laisse le lire (permission Bannir)."""
        try:
            ban = await guild.fetch_ban(user)
            return ban.reason
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None

    # -- événements -------------------------------------------------------

    async def on_ban(self, guild: discord.Guild, user: discord.User | discord.Member) -> None:
        if not self.enabled or guild.id != self.config.guild_id:
            return
        if user.id in self.maitres:
            log.warning("Bans : %s est maître, le ban Discord n'est PAS transmis au jeu.", user)
            return
        motif = await self._motif(guild, user)
        await self._envoyer(f"ban de {user}", bans=[{"discord_id": str(user.id), "motif": motif}], leves=[], complet=False)

    async def on_unban(self, guild: discord.Guild, user: discord.User) -> None:
        if not self.enabled or guild.id != self.config.guild_id:
            return
        if user.id in self.maitres:
            return
        await self._envoyer(f"levée pour {user}", bans=[], leves=[str(user.id)], complet=False)

    # -- liste complète ---------------------------------------------------

    async def resynchroniser(self, motif: str) -> None:
        if not self.enabled:
            return
        guild = self.client.get_guild(self.config.guild_id)
        if guild is None:
            return
        bans: list[dict] = []
        try:
            async for entry in guild.bans(limit=None):
                if entry.user.id in self.maitres:
                    continue
                bans.append({"discord_id": str(entry.user.id), "motif": entry.reason})
        except discord.Forbidden:
            if not self._liste_refusee_dite:
                log.warning(
                    "Bans : le bot n'a pas la permission « Bannir des membres », la liste complète "
                    "n'est pas relue. Seuls les bans et levées vus en direct partent au portail."
                )
                self._liste_refusee_dite = True
            return
        except discord.HTTPException as exc:
            log.warning("Bans (%s) : liste illisible : %s", motif, exc)
            return
        self._liste_refusee_dite = False
        await self._envoyer(motif, bans=bans, leves=[], complet=True)

    async def demarrer(self) -> None:
        if not self.enabled:
            log.info("Bans : miroir désactivé (même condition que la whitelist).")
            return
        await self.resynchroniser("démarrage")
        self._loop = asyncio.create_task(self._boucle())

    async def _boucle(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.resync_seconds)
            await self.resynchroniser("resynchronisation")
