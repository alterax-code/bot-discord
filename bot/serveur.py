"""/serveur : piloter le serveur de jeu depuis Discord, sans rien voir de sensible.

Un opérateur (liste nominative dans [serveur] de config.toml) tape /serveur
dans le salon dédié : statut, start, stop ou restart. Le bot vérifie le salon
et la personne, sonde le serveur en A2S, puis parle à l'API mTxServ avec les
identifiants de son .env. L'opérateur ne manipule jamais le panneau, le FTP,
RCON ni la base : retirer son identifiant de la liste lui retire la main.

Garde-fous, tous vécus :
  - `start` sur un serveur déjà allumé le coupe et le relance (16/09/2026) :
    on sonde d'abord, et on refuse s'il répond.
  - `stop`, `restart` et `update` : s'il y a des joueurs, confirmation par
    bouton puis bandeau de compte à rebours en jeu (le message « mtxserv.com -
    Server stop in N seconds. » que le panneau envoie lui-même, reconnu par
    modules/announce/cl_shutdown.lua), annulable jusqu'à la dernière seconde.
    Personne en ligne : le geste part tout de suite, sans question (demande
    Lucas, 21/09).
  - une seule opération à la fois (verrou) : deux opérateurs qui cliquent en
    même temps ne font pas deux gestes.

La veille du VPS reste maîtresse des horaires : un stop demandé ici pendant la
plage est lu comme un arrêt voulu (dernier geste game_stop), elle annonce
« Fermé par le staff » et ne relance pas ; à 17 h elle allume, à la fermeture
elle éteint, comme d'habitude.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import discord
from discord import app_commands

from .config import Config
from .mtxserv import EtatA2S, PanneauMTx, sonder

log = logging.getLogger(__name__)

# Les quatre actions, telles que Discord les propose dans le menu.
CHOIX = [
    app_commands.Choice(name="statut — le serveur répond-il, combien de joueurs", value="statut"),
    app_commands.Choice(name="start — démarrer le serveur (s'il est éteint)", value="start"),
    app_commands.Choice(name="stop — arrêter le serveur (confirmation)", value="stop"),
    app_commands.Choice(name="restart — redémarrer le serveur (confirmation)", value="restart"),
    app_commands.Choice(name="update — mettre à jour le serveur, comme « Mise à jour » du panneau", value="update"),
]
VERBES = {
    "stop": ("Arrêter", "Arrêt"),
    "restart": ("Redémarrer", "Redémarrage"),
    "update": ("Mettre à jour", "Mise à jour"),
}
# Le bandeau du jeu ne connaît que « stop » et « restart » : une mise à jour
# coupe puis relance, on l'annonce comme un redémarrage.
BANDEAU = {"stop": "stop", "restart": "restart", "update": "restart"}
SUITE = {
    "stop": "",
    "restart": " Compter une à deux minutes avant qu'il réponde.",
    "update": " Le serveur est indisponible le temps de la mise à jour ; `statut` dira quand il répond.",
}
ATTENTE_CONFIRMATION_S = 60


class ConfirmationView(discord.ui.View):
    """Deux boutons : confirmer ou annuler. Seuls les opérateurs peuvent cliquer."""

    def __init__(self, service: "ServeurService") -> None:
        super().__init__(timeout=ATTENTE_CONFIRMATION_S)
        self.service = service
        self.choix: bool | None = None
        self.par = ""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.service.clic_autorise(interaction)

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.danger)
    async def confirmer(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.choix, self.par = True, interaction.user.display_name
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def annuler(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.choix, self.par = False, interaction.user.display_name
        await interaction.response.defer()
        self.stop()


class AnnulationView(discord.ui.View):
    """Pendant le compte à rebours : un seul bouton, annuler."""

    def __init__(self, service: "ServeurService", secondes: int) -> None:
        super().__init__(timeout=secondes)
        self.service = service
        self.annule = False
        self.par = ""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.service.clic_autorise(interaction)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def annuler(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.annule, self.par = True, interaction.user.display_name
        await interaction.response.defer()
        self.stop()


class ServeurService:
    def __init__(self, bot: discord.Client, config: Config) -> None:
        self.bot = bot
        self.config = config
        self.cfg = config.serveur
        self.panneau = PanneauMTx()
        self._verrou = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    # -- enregistrement ----------------------------------------------------

    def enregistrer(self, tree: app_commands.CommandTree) -> None:
        """Déclare /serveur sur le serveur Discord (commande de guilde : visible
        aussitôt après la synchronisation, pas d'attente d'une heure)."""
        if not self.enabled:
            log.info("Pilotage du serveur : [serveur] non configuré, /serveur absente.")
            return

        @tree.command(
            name="serveur",
            description="Pilote le serveur de jeu : statut, start, stop, restart, update.",
            guild=discord.Object(id=self.config.guild_id),
        )
        @app_commands.guild_only()
        @app_commands.describe(action="Ce qu'on demande au serveur")
        @app_commands.choices(action=CHOIX)
        async def serveur(interaction: discord.Interaction, action: app_commands.Choice[str]) -> None:
            await self.commande(interaction, action.value)

    async def demarrer(self) -> None:
        if not self.enabled:
            return
        salon = self.bot.get_channel(self.cfg.salon)
        if not isinstance(salon, discord.TextChannel):
            log.warning("Pilotage du serveur : salon %s invisible — /serveur refusera tout.", self.cfg.salon)
        else:
            log.info("Pilotage du serveur : /serveur dans #%s, %d opérateur(s), API mTxServ %s.",
                     salon.name, len(self.cfg.operateurs), "armée" if self.panneau.arme else "SANS IDENTIFIANTS")
        if not self.panneau.arme:
            log.warning("Pilotage du serveur : MTXSERV_* manquent dans .env — /serveur répondra « non configuré ».")

    async def fermer(self) -> None:
        await self.panneau.fermer()

    # -- autorisations -----------------------------------------------------

    def refus(self, interaction: discord.Interaction) -> str | None:
        """Pourquoi cette personne, ici, n'a pas la main — ou None."""
        if interaction.channel_id != self.cfg.salon:
            return f"Cette commande s'utilise dans <#{self.cfg.salon}>."
        if interaction.user.id not in self.cfg.operateurs:
            return "Réservé aux opérateurs du serveur."
        return None

    async def clic_autorise(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.cfg.operateurs:
            return True
        await interaction.response.send_message("Réservé aux opérateurs du serveur.", ephemeral=True)
        return False

    # -- la commande -------------------------------------------------------

    async def commande(self, interaction: discord.Interaction, action: str) -> None:
        motif = self.refus(interaction)
        if motif:
            await interaction.response.send_message(motif, ephemeral=True)
            return
        if not self.panneau.arme:
            await interaction.response.send_message(
                "Pilotage non configuré : les identifiants mTxServ manquent dans le .env du bot.", ephemeral=True
            )
            return
        if self._verrou.locked():
            await interaction.response.send_message(
                "Une opération est déjà en cours, attends qu'elle se termine.", ephemeral=True
            )
            return

        # Sonde + API : plus que les 3 s que Discord accorde. On diffère, en
        # public : chaque geste reste lisible par tout le salon.
        await interaction.response.defer(thinking=True)
        log.info("/serveur %s demandé par %s (%s).", action, interaction.user, interaction.user.id)
        async with self._verrou:
            try:
                if action == "statut":
                    await self._statut(interaction)
                elif action == "start":
                    await self._start(interaction)
                elif action in VERBES:
                    await self._couper(interaction, action)
            except Exception:
                log.exception("/serveur %s : erreur inattendue.", action)
                await interaction.followup.send("💥 Erreur inattendue, voir les journaux du bot.")

    # -- les gestes --------------------------------------------------------

    def _heure(self) -> str:
        return datetime.now(self.config.archive.timezone).strftime("%H:%M")

    async def _sonde(self) -> EtatA2S | None:
        return await sonder(self.cfg.game_host, self.cfg.game_port)

    @staticmethod
    def _decrire(info: EtatA2S | None) -> str:
        if info is None:
            return "le serveur ne répond pas (éteint, en train de démarrer, ou bloqué)"
        j = info.joueurs
        return f"{j} joueur{'s' if j > 1 else ''} en ligne sur `{info.map}`"

    async def _statut(self, interaction: discord.Interaction) -> None:
        info = await self._sonde()
        geste, quand = await self.panneau.dernier_geste()
        lignes = [("🟢 " if info else "🔴 ") + self._decrire(info).capitalize() + "."]
        if geste:
            libelle = {"game_start": "démarrage", "game_stop": "arrêt", "game_restart": "redémarrage"}.get(geste, geste)
            date = ""
            if quand is not None:
                date = " le " + quand.astimezone(self.config.archive.timezone).strftime("%d/%m à %H:%M")
            lignes.append(f"Dernier geste du panneau : {libelle}{date}.")
        await interaction.followup.send("\n".join(lignes))

    async def _start(self, interaction: discord.Interaction) -> None:
        info = await self._sonde()
        qui = interaction.user.display_name
        if info is not None:
            await interaction.followup.send(
                f"🟢 Le serveur répond déjà ({self._decrire(info)}). Un `start` le couperait : rien envoyé. "
                "Pour le relancer, utilise `restart`."
            )
            return
        if await self.panneau.action("start"):
            await interaction.followup.send(
                f"🟢 Démarrage demandé par **{qui}** à {self._heure()}. Compter une à deux minutes avant qu'il réponde."
            )
        else:
            await interaction.followup.send("💥 L'API mTxServ refuse le `start`. Voir les journaux du bot.")

    async def _couper(self, interaction: discord.Interaction, action: str) -> None:
        verbe, nom = VERBES[action]
        qui = interaction.user.display_name
        info = await self._sonde()

        # Personne en ligne : rien à protéger, le geste part tout de suite.
        if info is None or info.joueurs == 0:
            await interaction.followup.send(await self._executer(action, qui, info))
            return

        preavis = self.cfg.avertissement_secondes
        question = f"⚠️ {verbe} le serveur ? Actuellement : {self._decrire(info)}."
        if preavis:
            question += f" Les joueurs auront {preavis} s de préavis en jeu."
        confirmation = ConfirmationView(self)
        message = await interaction.followup.send(question, view=confirmation, wait=True)
        await confirmation.wait()
        if not confirmation.choix:
            raison = "pas de réponse en 60 s" if confirmation.choix is None else f"par {confirmation.par}"
            await message.edit(content=f"↩️ {nom} annulé ({raison}).", view=None)
            return

        if preavis:
            # Le même message que le panneau : le jeu affiche son bandeau doré.
            await self.panneau.commande(f"say mtxserv.com - Server {BANDEAU[action]} in {preavis} seconds.")
            annulation = AnnulationView(self, preavis)
            await message.edit(
                content=f"⏳ {nom} dans {preavis} s, demandé par **{qui}**. Les joueurs sont prévenus en jeu.",
                view=annulation,
            )
            try:
                await asyncio.wait_for(annulation.wait(), timeout=preavis + 5)
            except asyncio.TimeoutError:
                pass
            if annulation.annule:
                await self.panneau.commande("say Annulé : le serveur reste en ligne.")
                await message.edit(content=f"↩️ {nom} annulé par {annulation.par}, le serveur reste en ligne.", view=None)
                return

        await message.edit(content=await self._executer(action, qui, info), view=None)

    async def _executer(self, action: str, qui: str, info: EtatA2S | None) -> str:
        """Envoie le geste au panneau et rend la ligne à afficher dans le salon."""
        nom = VERBES[action][1]
        if not await self.panneau.action(action):
            return f"💥 L'API mTxServ refuse le `{action}`. Voir les journaux du bot."
        etat = "personne en ligne" if (info is None or info.joueurs == 0) else self._decrire(info)
        genre = "demandée" if action == "update" else "demandé"
        return f"🔴 {nom} {genre} par **{qui}** à {self._heure()} ({etat}).{SUITE[action]}"
