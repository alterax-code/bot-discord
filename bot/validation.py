"""Validation d'un ticket, et annulation d'une validation.

L'ordre des opérations est la seule chose qui garantisse « aucune perte de
ticket » :

    capture → archive → publication publique → SUPPRESSION

La suppression vient en dernier, et uniquement si tout le reste a réussi. Si
une étape échoue, le message d'origine reste dans le salon : on préfère un
ticket en double à un ticket disparu.

Chaque étape est inscrite en base au fur et à mesure. Une coupure de courant
entre l'archive et la publication laisse le ticket à l'état « archivé » ; au
redémarrage, il reprend à la publication au lieu de tout refaire.

Deux barrières contre la double validation :
  1. un verrou par ticket, qui sérialise deux clics simultanés
  2. une contrainte d'unicité en base, qui tient même si le verrou cédait
"""

from __future__ import annotations

import asyncio
import json
import logging

import discord

from .archive import ArchiveService
from .config import Config
from .database import Database, now_iso
from .tickets import TicketService

log = logging.getLogger(__name__)


class ValidationService:
    def __init__(
        self,
        bot: discord.Client,
        config: Config,
        db: Database,
        tickets: TicketService,
        archive: ArchiveService,
    ) -> None:
        self.bot = bot
        self.config = config
        self.db = db
        self.tickets = tickets
        self.archive = archive
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, ticket_id: int) -> asyncio.Lock:
        return self._locks.setdefault(ticket_id, asyncio.Lock())

    def is_validator(self, member: discord.Member | None) -> bool:
        if member is None:
            return False
        return any(role.id in self.config.validator_roles for role in member.roles)

    # -- validation --------------------------------------------------------

    async def validate(self, ticket_row, member: discord.Member | None) -> None:
        """Point d'entrée : une coche verte autorisée vient d'être constatée."""
        ticket_id = int(ticket_row["id"])
        async with self._lock(ticket_id):
            ticket = await self.db.get_ticket(ticket_id)
            if ticket is None:
                return
            if ticket["state"] != "open":
                # Déjà en cours ou déjà traité : un second clic ne refait rien.
                log.info(
                    "Ticket #%d : validation ignorée, état actuel « %s ».",
                    ticket_id, ticket["state"],
                )
                return
            await self._pipeline(ticket, member)

    async def _pipeline(self, ticket, member: discord.Member | None = None) -> None:
        ticket_id = int(ticket["id"])
        nom = member.display_name if member else "inconnu"

        # --- 1. CAPTURE : on fige le contenu et les participants ----------
        participants = await self.db.participants(ticket_id)
        snapshot = {
            "kind": ticket["kind"],
            "title": ticket["title"],
            "fields": json.loads(ticket["fields_json"]),
            "author_name": ticket["author_name"],
            "author_id": ticket["author_id"],
            "participants": participants,
            "created_at": ticket["created_at"],
        }
        incidents = await self.db.incidents_for(ticket_id)
        if incidents:
            quand = self.archive.local(incidents[-1]["at"]).strftime("%d/%m à %H:%M")
            snapshot["apres_annulation"] = quand

        await self.db.set_state(ticket_id, "validating")

        # --- 2. ARCHIVE ---------------------------------------------------
        try:
            day = self.archive.day_key()
            entry = await self.db.active_entry(ticket_id)
            if entry is None:
                # On calcule la page d'accueil AVANT d'écrire l'entrée, sinon
                # elle se compterait elle-même dans le remplissage.
                provisoire = {
                    "ticket_id": ticket_id,
                    "snapshot_json": json.dumps(snapshot, ensure_ascii=False),
                    "cancelled": 0,
                    "validated_at": now_iso(),
                    "validated_by_name": nom,
                    "cancelled_at": None,
                    "cancelled_by_name": None,
                }
                texte = self.archive.render_entry(provisoire)
                page_no = await self.archive.choose_page(day, texte)
                entry_id = await self.db.create_entry(
                    ticket_id=ticket_id, day=day, page_no=page_no,
                    validated_by=member.id if member else None,
                    validated_by_name=nom, snapshot=snapshot,
                )
                if entry_id is None:
                    # La contrainte d'unicité a parlé : une autre validation
                    # est déjà passée. On n'insiste pas.
                    log.warning("Ticket #%d : entrée d'archive déjà existante, abandon.", ticket_id)
                    await self.db.set_state(ticket_id, "open")
                    return
            else:
                day, page_no, entry_id = entry["day"], int(entry["page_no"]), int(entry["id"])

            await self.archive.append(entry_id, day, page_no)
            await self.db.set_state(
                ticket_id, "archived",
                validated_at=now_iso(),
                validated_by=member.id if member else None,
            )
            log.info("Ticket #%d archivé (page %d du %s).", ticket_id, page_no, day)
        except Exception:
            log.exception(
                "Ticket #%d : ARCHIVAGE EN ÉCHEC. Le message d'origine n'est PAS supprimé.",
                ticket_id,
            )
            await self.db.set_state(ticket_id, "open")
            return

        # --- 3. PUBLICATION PUBLIQUE --------------------------------------
        try:
            await self._publish(ticket_id, snapshot)
        except Exception:
            log.exception(
                "Ticket #%d : PUBLICATION EN ÉCHEC. Il est archivé mais le message "
                "d'origine n'est PAS supprimé — la reprise se fera au redémarrage.",
                ticket_id,
            )
            return

        # --- 4. SUPPRESSION, en dernier -----------------------------------
        await self._remove_ticket_message(ticket_id)

    async def _publish(self, ticket_id: int, snapshot: dict) -> None:
        canal = self.bot.get_channel(self.config.channels.public)
        if not isinstance(canal, discord.TextChannel):
            raise RuntimeError("Le salon public est introuvable ou n'est pas textuel.")

        kind = snapshot["kind"]
        prefixe = (
            self.config.publication.prefix_bug if kind == "bug"
            else self.config.publication.prefix_feature
        )
        description = next(
            (v for k, v in snapshot.get("fields", {}).items() if k.startswith("Description") and v),
            "",
        )
        # Uniquement le contenu : ni auteur, ni participant, ni réaction,
        # ni numéro de ticket. Le salon joueurs n'a pas à voir la cuisine.
        embed = discord.Embed(
            title=f"{prefixe} {snapshot['title']}"[:256],
            description=description[:4000] or None,
            colour=discord.Colour(self.config.display.color(kind)),
        )
        message = await canal.send(embed=embed)
        await self.db.set_state(ticket_id, "published", public_message_id=message.id)
        log.info("Ticket #%d annoncé publiquement (message %s).", ticket_id, message.id)

    async def _remove_ticket_message(self, ticket_id: int) -> None:
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None:
            return
        message_id = ticket["message_id"]
        if message_id:
            canal = self.bot.get_channel(self.config.channels.tickets)
            if isinstance(canal, discord.TextChannel):
                try:
                    message = await canal.fetch_message(int(message_id))
                    await message.delete()
                except discord.NotFound:
                    pass  # déjà parti, le résultat est le même
                except discord.HTTPException:
                    log.exception("Ticket #%d : suppression du message impossible.", ticket_id)
                    return
        await self.db.set_state(ticket_id, "done", message_id=None)
        log.info("Ticket #%d : cycle terminé.", ticket_id)

    # -- reprise après coupure --------------------------------------------

    async def resume(self) -> None:
        """Reprend les validations interrompues, chacune à son étape exacte."""
        en_cours = await self.db.tickets_in_progress()
        if not en_cours:
            return
        log.warning(
            "%d validation(s) interrompue(s) à reprendre : %s",
            len(en_cours), ", ".join(f"#{t['id']} ({t['state']})" for t in en_cours),
        )
        for ticket in en_cours:
            ticket_id = int(ticket["id"])
            async with self._lock(ticket_id):
                courant = await self.db.get_ticket(ticket_id)
                if courant is None:
                    continue
                etat = courant["state"]
                try:
                    if etat == "validating":
                        await self.db.set_state(ticket_id, "open")
                        log.info("Ticket #%d remis en attente : l'archivage n'avait pas abouti.", ticket_id)
                    elif etat == "archived":
                        entry = await self.db.active_entry(ticket_id)
                        if entry is None:
                            await self.db.set_state(ticket_id, "open")
                            continue
                        await self._publish(ticket_id, json.loads(entry["snapshot_json"]))
                        await self._remove_ticket_message(ticket_id)
                    elif etat == "published":
                        await self._remove_ticket_message(ticket_id)
                except Exception:
                    log.exception("Reprise du ticket #%d en échec — il reste en état « %s ».", ticket_id, etat)

    # -- annulation --------------------------------------------------------

    async def handle_undo(self, interaction: discord.Interaction) -> None:
        """Bouton « annuler la dernière validation », posé sur la page du jour."""
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not self.is_validator(member):
            await interaction.response.send_message(
                "Seuls les rôles habilités à valider peuvent annuler une validation.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        day = self.archive.day_key()
        entry = await self.db.latest_active_entry(day)
        if entry is None:
            await interaction.followup.send(
                "Aucune validation à annuler pour aujourd'hui.", ephemeral=True
            )
            return

        ticket_id = int(entry["ticket_id"])
        nom = member.display_name if member else "?"
        async with self._lock(ticket_id):
            try:
                await self._undo(entry, member, nom)
            except Exception:
                log.exception("Annulation du ticket #%d en échec.", ticket_id)
                await interaction.followup.send(
                    f"⚠️ L'annulation du ticket **#{ticket_id}** a échoué. "
                    "Rien n'a été perdu, mais vérifie l'état du salon.",
                    ephemeral=True,
                )
                return

        await interaction.followup.send(
            f"Validation du ticket **#{ticket_id}** annulée. Il est de retour dans le salon "
            "avec ses participations, et l'archive en garde la trace.",
            ephemeral=True,
        )

    async def _undo(self, entry, member: discord.Member | None, nom: str) -> None:
        ticket_id = int(entry["ticket_id"])
        snapshot = json.loads(entry["snapshot_json"])
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None:
            raise RuntimeError(f"Ticket #{ticket_id} introuvable.")

        # 1. Retirer l'annonce publique : c'est elle qui ne doit plus être vue.
        if ticket["public_message_id"]:
            canal = self.bot.get_channel(self.config.channels.public)
            if isinstance(canal, discord.TextChannel):
                try:
                    msg = await canal.fetch_message(int(ticket["public_message_id"]))
                    await msg.delete()
                except discord.NotFound:
                    pass

        # 2. Marquer l'entrée annulée — elle reste dans l'archive, barrée.
        await self.db.cancel_entry(int(entry["id"]), nom)
        await self.db.log_incident(
            ticket_id,
            member.id if member else 0,
            nom,
            "Validation annulée depuis le bouton de l'archive.",
        )

        # 3. Republier le ticket dans le salon de travail.
        #    Un bot ne peut PAS reposer une réaction au nom d'un utilisateur :
        #    les participations déjà acquises sont donc réaffichées dans le
        #    corps du message, et le suivi reprend par-dessus.
        participants = await self.db.participants(ticket_id)
        canal = self.bot.get_channel(self.config.channels.tickets)
        if not isinstance(canal, discord.TextChannel):
            raise RuntimeError("Salon des tickets introuvable.")
        embed = self.tickets.build_embed(
            ticket_id, snapshot["kind"], snapshot["title"], snapshot["fields"],
            None, participants=participants,
        )
        message = await canal.send(embed=embed)
        for emoji in self.config.reactions.all:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                pass

        await self.db.set_state(
            ticket_id, "open",
            message_id=message.id,
            public_message_id=None,
            cancelled_count=int(ticket["cancelled_count"]) + 1,
        )

        # 4. Réécrire l'archive : la ligne devient barrée et marquée ANNULÉE.
        await self.archive.refresh_pages(entry["day"])
        log.warning("Ticket #%d : validation ANNULÉE par %s, ticket republié.", ticket_id, nom)
