"""Suivi collaboratif des réactions.

Deux chemins mènent à l'enregistrement d'une participation, et il faut les
deux :

1. **L'événement**, quand le bot tourne. Discord envoie l'information même
   pour un message publié il y a des semaines, hors de tout cache.

2. **La réconciliation au démarrage.** C'est le vrai trou : une réaction posée
   pendant que le bot est ÉTEINT ne génère aucun événement, ni sur le moment
   ni plus tard. Sans relecture au démarrage, elle serait perdue à jamais.

Le retrait d'une réaction ne déclenche rien et n'efface rien : deux personnes
sur la croix rouge signifient que le bug a été trouvé à deux, et un clic
retiré ne réécrit pas cette histoire.
"""

from __future__ import annotations

import logging

import discord

from .config import Config
from .database import Database

log = logging.getLogger(__name__)


class ReactionTracker:
    def __init__(self, bot: discord.Client, config: Config, db: Database) -> None:
        self.bot = bot
        self.config = config
        self.db = db
        # Rempli par l'incrément 4 : appelé quand une coche verte autorisée
        # est constatée. Laissé vide ici pour que le suivi fonctionne seul.
        self.on_validated = None

    # -- outils -----------------------------------------------------------

    def is_tracked(self, emoji: str) -> bool:
        return emoji in self.config.reactions.all

    def is_validator(self, member: discord.Member | None) -> bool:
        """Le membre porte-t-il un rôle autorisé à valider ?"""
        if member is None:
            return False
        return any(role.id in self.config.validator_roles for role in member.roles)

    async def _member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None

    # -- chemin 1 : l'événement -------------------------------------------

    async def handle_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.channel_id != self.config.channels.tickets:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return  # les réactions posées par le bot ne comptent jamais

        emoji = str(payload.emoji)
        if not self.is_tracked(emoji):
            log.debug("Réaction ignorée (émoji non suivi) : %s", emoji)
            return

        ticket = await self.db.get_ticket_by_message(payload.message_id)
        if ticket is None:
            return  # message hors périmètre : message permanent, ancien post…

        guild = self.bot.get_guild(self.config.guild_id)
        if guild is None:
            return
        member = payload.member or await self._member(guild, payload.user_id)
        name = member.display_name if member else str(payload.user_id)

        added = await self.db.add_participant(int(ticket["id"]), emoji, payload.user_id, name)
        if added:
            log.info("Ticket #%s : %s ajouté sur %s.", ticket["id"], name, emoji)

        if emoji == self.config.reactions.validated:
            await self._maybe_validate(ticket, member, emoji)

    async def _maybe_validate(self, ticket, member: discord.Member | None, emoji: str) -> None:
        """Une coche verte ne déclenche la publication que si son auteur y a droit."""
        if not self.is_validator(member):
            log.info(
                "Ticket #%s : coche verte posée par %s, sans rôle autorisé — "
                "enregistrée comme participation, aucune publication déclenchée.",
                ticket["id"],
                member.display_name if member else "inconnu",
            )
            return
        if self.on_validated is None:
            log.warning(
                "Ticket #%s : validation autorisée par %s, mais la publication "
                "n'est pas encore branchée (incrément 4).",
                ticket["id"],
                member.display_name if member else "?",
            )
            return
        await self.on_validated(ticket, member)

    # -- chemin 2 : la réconciliation au démarrage ------------------------

    async def reconcile(self) -> None:
        """Relit les réactions réellement présentes sur les tickets ouverts.

        Rattrape tout ce qui a été posé pendant que le bot était arrêté.
        """
        tickets = await self.db.open_tickets()
        if not tickets:
            log.info("Réconciliation : aucun ticket ouvert à relire.")
            return

        guild = self.bot.get_guild(self.config.guild_id)
        channel = self.bot.get_channel(self.config.channels.tickets)
        if guild is None or not isinstance(channel, discord.TextChannel):
            log.error("Réconciliation impossible : serveur ou salon introuvable.")
            return

        nouvelles, disparus, a_valider = 0, [], []

        for ticket in tickets:
            try:
                message = await channel.fetch_message(int(ticket["message_id"]))
            except discord.NotFound:
                disparus.append(int(ticket["id"]))
                continue
            except discord.HTTPException as exc:
                log.warning("Ticket #%s illisible (%s), ignoré ce tour-ci.", ticket["id"], exc)
                continue

            for reaction in message.reactions:
                emoji = str(reaction.emoji)
                if not self.is_tracked(emoji):
                    continue
                async for user in reaction.users():
                    if self.bot.user is not None and user.id == self.bot.user.id:
                        continue
                    member = await self._member(guild, user.id)
                    name = member.display_name if member else user.name
                    if await self.db.add_participant(int(ticket["id"]), emoji, user.id, name):
                        nouvelles += 1
                        log.info(
                            "Réconciliation — ticket #%s : %s sur %s (posée hors ligne).",
                            ticket["id"], name, emoji,
                        )
                    if emoji == self.config.reactions.validated and self.is_validator(member):
                        a_valider.append((ticket, member))

        log.info(
            "Réconciliation terminée : %d ticket(s) relu(s), %d participation(s) rattrapée(s).",
            len(tickets), nouvelles,
        )
        if disparus:
            log.warning(
                "Ticket(s) dont le message a disparu du salon : %s. "
                "Leur contenu reste en base, mais plus personne ne peut réagir dessus.",
                ", ".join(f"#{i}" for i in disparus),
            )

        # Une coche verte autorisée posée hors ligne doit produire le même
        # effet que si le bot avait été présent.
        for ticket, member in a_valider:
            if self.on_validated is None:
                log.warning(
                    "Ticket #%s : validation en attente (posée hors ligne par %s), "
                    "publication pas encore branchée (incrément 4).",
                    ticket["id"], member.display_name if member else "?",
                )
            else:
                await self.on_validated(ticket, member)
