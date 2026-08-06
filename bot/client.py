"""Le client Discord et son autodiagnostic de démarrage.

À chaque démarrage, le bot vérifie qu'il peut réellement faire tout ce qu'on
attend de lui : que les trois salons existent, qu'il y a bien les permissions
nécessaires, et que les rôles validateurs sont reconnus. Un problème de
configuration se voit ainsi tout de suite, dans les journaux, plutôt que le
jour où quelqu'un pose une coche verte.
"""

from __future__ import annotations

import logging
import socket

import aiohttp
import discord

from .archive import ArchiveService
from .config import Config
from .database import Database
from .reactions import ReactionTracker
from .tickets import TicketService
from .ui import ArchiveView, PanelView
from .validation import ValidationService

log = logging.getLogger(__name__)

# Ce dont le bot a besoin, salon par salon. La clé est le nom lisible utilisé
# dans les journaux, la valeur la liste des permissions Discord requises.
REQUIRED_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "tickets": (
        "view_channel",
        "send_messages",
        "embed_links",
        "add_reactions",
        "read_message_history",
        "manage_messages",  # épingler le message permanent, nettoyer si besoin
    ),
    "archives": (
        "view_channel",
        "send_messages",
        "embed_links",
        "read_message_history",
    ),
    "public": (
        "view_channel",
        "send_messages",
        "embed_links",
    ),
}

_PERM_LABELS = {
    "view_channel": "voir le salon",
    "send_messages": "envoyer des messages",
    "embed_links": "intégrer des liens",
    "add_reactions": "ajouter des réactions",
    "read_message_history": "lire l'historique",
    "manage_messages": "gérer les messages",
}


class SelfCheckFailed(RuntimeError):
    """L'environnement Discord ne permet pas au bot de fonctionner."""


def build_intents() -> discord.Intents:
    """Les intents strictement nécessaires — et rien de plus.

    « message_content » est délibérément absent : un bot reçoit toujours le
    contenu de SES PROPRES messages, et tout ce que nous relisons a été écrit
    par lui. Le demander reviendrait à s'octroyer la lecture de toutes les
    conversations du serveur sans en avoir l'usage.
    """
    intents = discord.Intents.none()
    intents.guilds = True            # cache des serveurs, salons et rôles
    intents.guild_messages = True    # détecter la suppression manuelle d'un ticket
    intents.guild_reactions = True   # le cœur du suivi : les réactions
    intents.members = True           # PRIVILÉGIÉ : résoudre les rôles d'un réacteur
    return intents


class GrandLineBot(discord.Client):
    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        check_only: bool = False,
        force_ipv4: bool = False,
        scan_legacy: bool = False,
        oneshot: str | None = None,
    ) -> None:
        # Discord détruit un jeton d'interaction au bout de 3 SECONDES. Tout ce
        # qui peut retarder une requête au-delà rend un bouton inutilisable.
        #
        # Deux réglages, pour deux causes distinctes :
        #
        # - ttl_dns_cache : aiohttp oublie ses résolutions DNS toutes les 10
        #   secondes par défaut. Deux clics espacés de plus de 10 secondes
        #   repartent donc chacun d'une résolution à froid. On garde une heure.
        #
        # - family : sur un réseau où l'IPv6 est annoncée mais ne répond pas,
        #   la résolution se bloque une dizaine de secondes avant d'abandonner.
        #   Ne demander que l'IPv4 contourne ce trou noir.
        #
        # Le cache DNS est utile partout ; le forçage IPv4 ne sert qu'en local.
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET if force_ipv4 else socket.AF_UNSPEC,
            ttl_dns_cache=3600,
        )
        super().__init__(intents=build_intents(), connector=connector)
        self.config = config
        self.db = db
        # En mode « check_only », le bot se connecte, vérifie son environnement,
        # rend son verdict et s'arrête. Sert à valider une configuration avant
        # de relancer le service en production.
        self.check_only = check_only
        self.check_passed = False
        # Lecture ponctuelle des anciens signalements : ne publie ni ne
        # supprime rien, écrit seulement un fichier local à relire.
        self.scan_legacy = scan_legacy
        # Opération unique (« reset » ou « import ») puis arrêt.
        self.oneshot = oneshot
        self.tree = discord.app_commands.CommandTree(self)
        self._self_check_done = False

        self.tickets = TicketService(self, config, db)
        self.archive = ArchiveService(self, config, db)
        self.validation = ValidationService(self, config, db, self.tickets, self.archive)
        self.reactions = ReactionTracker(self, config, db)

        # Une coche verte autorisée déclenche la chaîne de validation.
        self.reactions.on_validated = self.validation.validate
        # L'archive doit pouvoir accrocher le bouton d'annulation sur sa
        # dernière page à chaque réécriture.
        self.archive.undo_view_factory = lambda: ArchiveView(self.validation)

        self.panel_view = PanelView(self.tickets)
        self.archive_view = ArchiveView(self.validation)

    # -- cycle de vie ------------------------------------------------------

    async def setup_hook(self) -> None:
        """Appelé une seule fois, avant la connexion à la passerelle."""
        # Enregistrer la vue AVANT la connexion : c'est ce qui permet aux
        # boutons d'un message publié il y a des semaines de redevenir actifs
        # après un redémarrage.
        self.add_view(self.panel_view)
        self.add_view(self.archive_view)
        log.info("Vues persistantes enregistrées : les boutons survivent aux redémarrages.")

    async def on_ready(self) -> None:
        # on_ready peut se déclencher plusieurs fois (reconnexions réseau).
        # L'autodiagnostic ne doit tourner qu'au premier passage.
        if self._self_check_done:
            log.info("Reconnecté à Discord en tant que %s.", self.user)
            return
        self._self_check_done = True

        log.info("Connecté en tant que %s (id %s).", self.user, self.user.id if self.user else "?")
        try:
            await self.self_check()
        except SelfCheckFailed as exc:
            log.error("AUTODIAGNOSTIC EN ÉCHEC — le bot ne peut pas fonctionner.\n%s", exc)
            await self.close()
            return

        self.check_passed = True

        counts = await self.db.healthcheck()
        log.info(
            "Base : %d ticket(s), %d participation(s), %d page(s) d'archive, %d incident(s).",
            counts["tickets"], counts["participants"], counts["archive_pages"], counts["incidents"],
        )

        if self.check_only:
            log.info("Mode diagnostic : tout est vérifié, arrêt demandé (aucune modification sur Discord).")
            await self.close()
            return

        if self.scan_legacy:
            from pathlib import Path

            from .legacy import scan_channel, summarise

            log.info("Mode LECTURE des anciens signalements (aucune écriture sur Discord).")
            try:
                stats = await scan_channel(
                    self, self.config.channels.tickets, Path("data/legacy_scan.json")
                )
                log.info("Résultat :\n%s", summarise(stats))
            except Exception:
                log.exception("La lecture a échoué.")
            await self.close()
            return

        # Un ticket écrit en base mais jamais publié signale qu'une panne a
        # interrompu une création. On le signale plutôt que de le laisser
        # dormir silencieusement.
        orphans = await self.db.orphan_tickets()
        if orphans:
            log.warning(
                "%d ticket(s) enregistré(s) mais jamais publié(s) : %s. "
                "Leur contenu est intact en base.",
                len(orphans), ", ".join(f"#{r['id']}" for r in orphans),
            )

        # Opérations ponctuelles : elles ne doivent PAS être précédées d'une
        # réconciliation, qui validerait des tickets qu'on s'apprête à effacer.
        if self.oneshot:
            from .importer import cleanup_legacy, reset, run_import

            try:
                if self.oneshot == "reset":
                    await reset(self)
                elif self.oneshot == "import":
                    await run_import(self)
                elif self.oneshot == "cleanup-legacy":
                    await cleanup_legacy(self)
            except Exception:
                log.exception("Opération « %s » en échec.", self.oneshot)
            await self.close()
            return

        # Ordre volontaire : d'abord finir ce qui avait été commencé, ensuite
        # seulement rattraper les réactions — lesquelles peuvent déclencher de
        # nouvelles validations.
        await self.validation.resume()
        await self.reactions.reconcile()

        await self.tickets.ensure_panel(self.panel_view)
        log.info("Bot opérationnel : les joueurs peuvent ouvrir des tickets.")

    # -- événements --------------------------------------------------------

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        # « raw » et non l'événement classique : celui-ci fonctionne aussi sur
        # les messages absents du cache, donc sur les tickets antérieurs au
        # démarrage du bot.
        try:
            await self.reactions.handle_add(payload)
        except Exception:
            log.exception("Erreur pendant le traitement d'une réaction (message %s).", payload.message_id)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        # Volontairement inerte : retirer une réaction ne défait rien.
        # Deux personnes sur la croix rouge ont bien trouvé le bug à deux,
        # même si l'une d'elles déclique ensuite.
        return

    # -- autodiagnostic ----------------------------------------------------

    async def self_check(self) -> None:
        problems: list[str] = []

        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            raise SelfCheckFailed(
                f"Serveur {self.config.guild_id} introuvable.\n"
                "  Soit le bot n'y a pas été invité, soit l'identifiant de config.toml est faux."
            )
        log.info("Serveur : %s (%d membres en cache).", guild.name, len(guild.members))

        me = guild.me
        if me is None:
            raise SelfCheckFailed("Impossible de déterminer l'identité du bot sur ce serveur.")
        log.info("Rôles portés par le bot : %s", ", ".join(r.name for r in me.roles if r.name != "@everyone") or "aucun")

        # --- les trois salons -------------------------------------------
        for name, channel_id in self.config.channel_ids.items():
            channel = guild.get_channel(channel_id)
            if channel is None:
                problems.append(
                    f"[{name}] salon {channel_id} invisible.\n"
                    "        Soit l'identifiant est faux, soit le bot n'a pas accès au salon "
                    "(il lui faut un rôle figurant dans les permissions du salon, ex. « bots »)."
                )
                continue
            if not isinstance(channel, discord.TextChannel):
                problems.append(f"[{name}] « {channel.name} » n'est pas un salon textuel.")
                continue

            perms = channel.permissions_for(me)
            missing = [
                _PERM_LABELS.get(p, p)
                for p in REQUIRED_PERMISSIONS[name]
                if not getattr(perms, p, False)
            ]
            if missing:
                problems.append(f"[{name}] « #{channel.name} » : permissions manquantes → {', '.join(missing)}.")
            else:
                log.info("Salon %-9s → #%-24s toutes permissions OK.", f"[{name}]", channel.name)

        # --- les rôles validateurs ---------------------------------------
        resolved, unknown = [], []
        for role_id in sorted(self.config.validator_roles):
            role = guild.get_role(role_id)
            (resolved if role else unknown).append(role.name if role else str(role_id))

        if resolved:
            log.info("Rôles autorisés à valider (%d) : %s", len(resolved), ", ".join(resolved))
        if unknown:
            # Non bloquant : un rôle supprimé ne casse rien, il ne validera juste jamais.
            log.warning(
                "Rôles validateurs introuvables sur le serveur : %s — ils sont ignorés.",
                ", ".join(unknown),
            )
        if not resolved:
            problems.append(
                "Aucun rôle validateur n'existe réellement sur le serveur : "
                "personne ne pourrait jamais valider un ticket."
            )

        if problems:
            raise SelfCheckFailed("\n  - " + "\n  - ".join(problems))

        log.info("Autodiagnostic : tout est en ordre.")
