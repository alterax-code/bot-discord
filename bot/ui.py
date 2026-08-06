"""Interface : le message permanent à boutons et les deux formulaires.

La vue est « persistante » : elle n'expire jamais et ses boutons portent un
identifiant fixe. C'est ce qui permet au message de survivre à un redémarrage
du bot — sans identifiant fixe, Discord ne saurait plus à quel code rattacher
un clic après relance, et les boutons deviendraient inertes.

Contrainte Discord : un formulaire accepte au maximum 5 champs. Le formulaire
de bug en utilise exactement 5.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .tickets import TicketService

log = logging.getLogger(__name__)

# Identifiants stables des boutons. Les modifier casserait le message permanent
# déjà publié : ses boutons ne seraient plus reconnus après un redémarrage.
BTN_BUG = "grandline:ticket:bug"
BTN_FEATURE = "grandline:ticket:feature"


class _TicketModal(discord.ui.Modal):
    """Base commune aux deux formulaires."""

    kind: str = ""

    def __init__(self, service: TicketService) -> None:
        super().__init__()
        self.service = service

    def collect(self) -> dict[str, str]:  # pragma: no cover - redéfini
        raise NotImplementedError

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.create_from_modal(interaction, self.kind, self.collect())

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Erreur pendant la soumission d'un formulaire %s", self.kind, exc_info=error)
        message = (
            "Une erreur est survenue pendant l'enregistrement. "
            "Ton signalement n'a **pas** été perdu s'il a déjà été écrit : "
            "préviens l'équipe technique en citant l'heure exacte."
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class BugModal(_TicketModal, title="Signaler un bug"):
    kind = "bug"

    titre = discord.ui.TextInput(
        label="Titre court",
        placeholder="Ex : le fruit Loup ne consomme pas d'endurance",
        max_length=100,
        required=True,
    )
    description = discord.ui.TextInput(
        label="Description du problème",
        style=discord.TextStyle.paragraph,
        placeholder="Que se passe-t-il exactement ?",
        max_length=1000,
        required=True,
    )
    reproduction = discord.ui.TextInput(
        label="Étapes pour le reproduire",
        style=discord.TextStyle.paragraph,
        placeholder="1. Prendre le fruit Loup\n2. Utiliser la transformation\n3. ...",
        max_length=1000,
        required=True,
    )
    attendu = discord.ui.TextInput(
        label="Comportement attendu",
        style=discord.TextStyle.paragraph,
        placeholder="Ce qui aurait dû se produire.",
        max_length=500,
        required=True,
    )
    lien = discord.ui.TextInput(
        label="Lien capture ou vidéo (facultatif)",
        placeholder="Discord n'accepte pas les pièces jointes dans un formulaire.",
        max_length=300,
        required=False,
    )

    def collect(self) -> dict[str, str]:
        return {
            "Titre": self.titre.value.strip(),
            "Description du problème": self.description.value.strip(),
            "Étapes de reproduction": self.reproduction.value.strip(),
            "Comportement attendu": self.attendu.value.strip(),
            "Capture": self.lien.value.strip(),
        }


class FeatureModal(_TicketModal, title="Proposer un ajout"):
    kind = "feature"

    titre = discord.ui.TextInput(
        label="Titre court",
        placeholder="Ex : ajouter un système de primes pour les pirates",
        max_length=100,
        required=True,
    )
    description = discord.ui.TextInput(
        label="Description de la fonctionnalité",
        style=discord.TextStyle.paragraph,
        placeholder="En quoi consiste ton idée ?",
        max_length=1000,
        required=True,
    )
    interet = discord.ui.TextInput(
        label="Intérêt pour le serveur",
        style=discord.TextStyle.paragraph,
        placeholder="Qu'est-ce que ça apporterait aux joueurs ?",
        max_length=1000,
        required=True,
    )
    lien = discord.ui.TextInput(
        label="Lien capture ou vidéo (facultatif)",
        placeholder="Une référence, un exemple visuel…",
        max_length=300,
        required=False,
    )

    def collect(self) -> dict[str, str]:
        return {
            "Titre": self.titre.value.strip(),
            "Description de la fonctionnalité": self.description.value.strip(),
            "Intérêt pour le serveur": self.interet.value.strip(),
            "Capture": self.lien.value.strip(),
        }


class PanelView(discord.ui.View):
    """Le message permanent en tête du salon des tickets."""

    def __init__(self, service: TicketService) -> None:
        super().__init__(timeout=None)  # jamais d'expiration : vue persistante
        self.service = service

    @discord.ui.button(
        label="Signaler un bug",
        emoji="🐛",
        style=discord.ButtonStyle.danger,
        custom_id=BTN_BUG,
    )
    async def on_bug(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(BugModal(self.service))

    @discord.ui.button(
        label="Proposer un ajout",
        emoji="✨",
        style=discord.ButtonStyle.primary,
        custom_id=BTN_FEATURE,
    )
    async def on_feature(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(FeatureModal(self.service))
