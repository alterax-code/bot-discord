"""Création et publication des tickets.

Règle qui gouverne tout ce fichier : **le contenu saisi par le joueur est
écrit en base AVANT d'être publié sur Discord.** Si Discord tombe entre les
deux, le ticket existe toujours et reste rattrapable — il apparaîtra comme
« orphelin » au prochain démarrage. L'inverse perdrait la saisie.
"""

from __future__ import annotations

import json
import logging

import discord

from .config import Config
from .database import Database

log = logging.getLogger(__name__)

# Champ du formulaire réservé au lien facultatif : affiché à part, pas comme
# un champ de contenu ordinaire.
CAPTURE_KEY = "Capture"
TITLE_KEY = "Titre"


class TicketService:
    """Tout ce qui touche à la vie d'un ticket, du formulaire à la publication."""

    def __init__(self, bot: discord.Client, config: Config, db: Database) -> None:
        self.bot = bot
        self.config = config
        self.db = db

    # -- utilitaires -------------------------------------------------------

    def tickets_channel(self) -> discord.TextChannel:
        channel = self.bot.get_channel(self.config.channels.tickets)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("Le salon des tickets est introuvable ou n'est pas textuel.")
        return channel

    # -- le message permanent ---------------------------------------------

    async def ensure_panel(self, view: discord.ui.View) -> None:
        """Garantit qu'il existe exactement UN message permanent à boutons.

        Au démarrage, on tente de retrouver celui enregistré en base. S'il a
        été supprimé entre-temps, on en republie un — sans quoi plus personne
        ne pourrait ouvrir de ticket.
        """
        channel = self.tickets_channel()
        stored = await self.db.get_panel()

        if stored is not None:
            try:
                message = await channel.fetch_message(int(stored["message_id"]))
            except discord.NotFound:
                log.warning("Le message permanent a disparu du salon : republication.")
            except discord.Forbidden:
                log.error("Pas le droit de lire l'historique du salon des tickets.")
                return
            else:
                log.info("Message permanent retrouvé (id %s).", message.id)
                await self._pin_if_needed(message)
                return

        embed = discord.Embed(
            title=self.config.display.panel_title,
            description=self.config.display.panel_text,
            colour=discord.Colour.blurple(),
        )
        message = await channel.send(embed=embed, view=view)
        await self.db.save_panel(channel.id, message.id)
        log.info("Message permanent publié (id %s).", message.id)
        await self._pin_if_needed(message)

    async def _pin_if_needed(self, message: discord.Message) -> None:
        if not self.config.behaviour.pin_panel or message.pinned:
            return
        try:
            await message.pin(reason="Message permanent des tickets Grand Line RP")
            log.info("Message permanent épinglé.")
        except discord.HTTPException as exc:
            # Non bloquant : Discord limite à 50 messages épinglés par salon.
            log.warning("Impossible d'épingler le message permanent : %s", exc)

    # -- création d'un ticket ---------------------------------------------

    async def _notify(self, interaction: discord.Interaction, acknowledged: bool, text: str) -> None:
        """Confirmation privée au joueur. JAMAIS bloquante.

        Si l'interaction a expiré, le ticket existe quand même — et c'est cela
        qui compte. Perdre un message de politesse est sans conséquence,
        perdre un signalement ne l'est pas.
        """
        if not acknowledged:
            return
        try:
            await interaction.followup.send(text, ephemeral=True)
        except discord.HTTPException as exc:
            log.warning("Confirmation non délivrée (%s) — le ticket, lui, est intact.", exc)

    async def create_from_modal(
        self, interaction: discord.Interaction, kind: str, fields: dict[str, str]
    ) -> None:
        # 1. Accuser réception AVANT toute autre chose.
        #    Discord détruit le jeton d'interaction au bout de 3 secondes.
        #    L'accusé de réception ouvre une fenêtre de 15 minutes pour répondre,
        #    ce qui rend le reste insensible à une latence réseau passagère.
        acknowledged = False
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            acknowledged = True
        except discord.HTTPException as exc:
            log.warning(
                "Accusé de réception impossible (%s). Le ticket sera tout de même "
                "enregistré et publié ; seule la confirmation privée sera perdue.",
                exc,
            )

        title = fields.get(TITLE_KEY, "").strip() or "(sans titre)"
        author = interaction.user

        # 2. Écrire. Tout ce qui suit peut échouer sans rien perdre.
        ticket_id = await self.db.create_ticket(
            kind=kind,
            title=title,
            fields=fields,
            author_id=author.id,
            author_name=author.display_name,
        )
        log.info("Ticket #%d créé en base (%s) par %s.", ticket_id, kind, author)

        # 3. Publier — y compris si l'accusé de réception a échoué.
        try:
            channel = self.tickets_channel()
            message = await channel.send(
                embed=self.build_embed(ticket_id, kind, title, fields, author)
            )
        except (discord.HTTPException, RuntimeError):
            log.exception(
                "Ticket #%d écrit en base mais NON publié. Son contenu est intact ; "
                "il ressortira comme orphelin au prochain démarrage.",
                ticket_id,
            )
            await self._notify(
                interaction,
                acknowledged,
                f"⚠️ Le ticket **#{ticket_id}** est bien enregistré, mais sa publication "
                "a échoué. Rien n'est perdu — préviens l'équipe technique.",
            )
            return

        await self.db.attach_message(ticket_id, message.id)

        # 4. L'auteur est le découvreur : il compte d'office sur la croix rouge.
        await self.db.add_participant(
            ticket_id, self.config.reactions.reported, author.id, author.display_name
        )

        # 5. Les trois réactions de statut, dans l'ordre.
        for emoji in self.config.reactions.all:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException as exc:
                log.warning("Réaction %s non posée sur le ticket #%d : %s", emoji, ticket_id, exc)

        log.info("Ticket #%d publié (message %s).", ticket_id, message.id)
        await self._notify(
            interaction,
            acknowledged,
            f"Merci — ton signalement a été enregistré sous le **#{ticket_id}**. "
            "L'équipe technique le suivra dans ce salon.",
        )

    # -- rendu -------------------------------------------------------------

    def build_embed(
        self,
        ticket_id: int,
        kind: str,
        title: str,
        fields: dict[str, str],
        author: discord.abc.User | None,
        *,
        participants: dict[str, list[str]] | None = None,
    ) -> discord.Embed:
        """L'encart d'un ticket. Le type est lisible dans l'en-tête et la couleur,
        jamais déduit d'une réaction."""
        display = self.config.display
        embed = discord.Embed(
            title=f"{display.label(kind)} — {title}"[:256],
            colour=discord.Colour(display.color(kind)),
        )

        for key, value in fields.items():
            if key in (TITLE_KEY, CAPTURE_KEY) or not value:
                continue
            embed.add_field(name=key, value=value[:1024], inline=False)

        capture = fields.get(CAPTURE_KEY, "").strip()
        if capture:
            embed.add_field(name="Capture", value=capture[:1024], inline=False)

        # Après une annulation, les participations déjà enregistrées sont
        # réaffichées ici : un bot ne peut pas reposer une réaction à la place
        # d'un utilisateur, mais l'information, elle, n'est pas perdue.
        if participants:
            lines = []
            labels = {
                self.config.reactions.reported: "Signalé par",
                self.config.reactions.fixing: "Corrigé par",
                self.config.reactions.validated: "Validé par",
            }
            for emoji, label in labels.items():
                names = participants.get(emoji)
                if names:
                    lines.append(f"{emoji} **{label}** : {', '.join(names)}")
            if lines:
                embed.add_field(name="Suivi déjà enregistré", value="\n".join(lines)[:1024], inline=False)

        if author is not None:
            embed.set_author(name=author.display_name, icon_url=author.display_avatar.url)
        embed.set_footer(text=f"Ticket #{ticket_id}")
        return embed

    def embed_from_row(self, row, author: discord.abc.User | None = None, **kwargs) -> discord.Embed:
        """Reconstruit l'encart d'un ticket depuis sa ligne en base."""
        return self.build_embed(
            int(row["id"]),
            row["kind"],
            row["title"],
            json.loads(row["fields_json"]),
            author,
            **kwargs,
        )
