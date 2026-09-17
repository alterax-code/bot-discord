"""Support joueurs : tickets unifiés Discord ↔ site (décision Lucas, 17/09/2026).

Discord est le MAÎTRE des tickets, le site en est la mémoire. Un ticket,
qu'il naisse ici (bouton du salon support) ou sur nova-rp.online, a UN salon
dans la catégorie TICKETS, visible du staff et du joueur. Le bot remplace
Ticket Tool.

Ce qui traverse, et ce qui ne traverse pas :
  • ce que le joueur ou le staff transmet par le bouton « Répondre » du salon
    part sur le site (et, pour le staff, dans le chat en jeu du joueur) ;
  • ce qui s'écrit sur le site (joueur, staff, messages système) arrive dans
    le salon, posté par le bot ;
  • le bavardage libre du salon reste sur Discord : le bot ne lit pas le
    contenu des messages (pas d'intent « message content »), c'est voulu.

Le site se SONDE (GET /api/pont/tickets/sortant), comme le jeu le fait ;
rien n'est poussé vers le bot. Ce que le bot voit passer par ses boutons, il
le dépose (POST /api/pont/tickets/entrant). Même secret que la whitelist.

À la fermeture, d'où qu'elle vienne, la transcription complète (fournie par
le site, qui a tout) est déposée dans le salon transcript, puis le salon du
ticket est supprimé : Discord limite le nombre de salons, le site n'a pas de
limite.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import datetime, timezone

import aiohttp
import discord

from .config import Config
from .database import Database, now_iso

log = logging.getLogger(__name__)

BTN_OUVRIR = "grandline:support:ouvrir:"      # + identifiant de catégorie
BTN_REPONDRE = "grandline:support:repondre"
BTN_PRENDRE = "grandline:support:prendre"
BTN_FERMER = "grandline:support:fermer"

ORIGINES = {"portail": "site", "jeu": "jeu", "discord": "Discord"}
TYPES = {"joueur": "joueur", "staff": "staff", "systeme": "système"}


def _slug(texte: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", texte.lower()).strip("-")
    return s[:n] or "ticket"


def _quand(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return d.strftime("%d/%m %H:%M")
    except ValueError:
        return iso


# ── Interface ────────────────────────────────────────────────────────────────


class OuvrirModal(discord.ui.Modal):
    def __init__(self, service: "SupportService", categorie: str, label: str) -> None:
        super().__init__(title=f"Ouvrir un ticket · {label}"[:45])
        self.service = service
        self.categorie = categorie
        self.sujet = discord.ui.TextInput(label="Sujet", max_length=120, placeholder="En quelques mots")
        self.message = discord.ui.TextInput(
            label="Ta demande", style=discord.TextStyle.paragraph, max_length=2000,
            placeholder="Explique ce qui se passe, avec le nom de ton personnage si ça aide.",
        )
        self.add_item(self.sujet)
        self.add_item(self.message)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.ouvrir(interaction, self.categorie, str(self.sujet.value), str(self.message.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Ouverture de ticket en erreur", exc_info=error)
        await _dire(interaction, "Le ticket n'a pas pu être ouvert. Réessaie dans une minute, ou écris au staff.")


class RepondreModal(discord.ui.Modal, title="Répondre dans le ticket"):
    texte = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=2000,
                                 placeholder="Ce message part sur le site et, si tu es staff, dans le chat en jeu du joueur.")

    def __init__(self, service: "SupportService") -> None:
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.repondre(interaction, str(self.texte.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Réponse de ticket en erreur", exc_info=error)
        await _dire(interaction, "La réponse n'a pas été transmise. Réessaie dans une minute.")


class PanelSupportView(discord.ui.View):
    """Le message permanent du salon support : un bouton par catégorie."""

    def __init__(self, service: "SupportService") -> None:
        super().__init__(timeout=None)
        self.service = service
        for cid, label, emoji in service.cfg.categories:
            bouton = discord.ui.Button(label=label, emoji=emoji or None, style=discord.ButtonStyle.primary, custom_id=BTN_OUVRIR + cid)
            bouton.callback = self._callback(cid, label)
            self.add_item(bouton)

    def _callback(self, cid: str, label: str):
        async def cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(OuvrirModal(self.service, cid, label))
        return cb


class TicketView(discord.ui.View):
    """Les trois boutons en tête de chaque salon de ticket."""

    def __init__(self, service: "SupportService") -> None:
        super().__init__(timeout=None)
        self.service = service

    @discord.ui.button(label="Répondre", emoji="✉️", style=discord.ButtonStyle.primary, custom_id=BTN_REPONDRE)
    async def on_repondre(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await self.service.peut_ecrire(interaction):
            await interaction.response.send_modal(RepondreModal(self.service))
        else:
            await _dire(interaction, "Seuls le joueur du ticket et le staff peuvent répondre ici.")

    @discord.ui.button(label="Prendre en charge", emoji="🧭", style=discord.ButtonStyle.secondary, custom_id=BTN_PRENDRE)
    async def on_prendre(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.service.prendre(interaction)

    @discord.ui.button(label="Fermer", emoji="🔒", style=discord.ButtonStyle.danger, custom_id=BTN_FERMER)
    async def on_fermer(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.service.fermer(interaction)


async def _dire(interaction: discord.Interaction, texte: str) -> None:
    """Réponse privée, jamais bloquante : une interaction expirée ne doit pas masquer l'erreur d'origine."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(texte, ephemeral=True)
        else:
            await interaction.response.send_message(texte, ephemeral=True)
    except discord.HTTPException:
        pass


# ── Le service ───────────────────────────────────────────────────────────────


class SupportService:
    def __init__(self, bot: discord.Client, config: Config, db: Database) -> None:
        self.bot = bot
        self.config = config
        self.cfg = config.support
        self.portail = config.whitelist            # url + secret, même pont que la whitelist
        self.db = db
        self.staff_roles = config.whitelist.staff_roles
        self._loop: asyncio.Task | None = None
        self._echec_dit = False

    @property
    def enabled(self) -> bool:
        return self.cfg.configured and self.portail.enabled

    # -- portail ------------------------------------------------------------

    async def _api(self, methode: str, chemin: str, corps: dict | None = None) -> tuple[int, dict]:
        url = self.portail.portail_url.rstrip("/") + chemin
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.request(methode, url, json=corps, headers={"Authorization": f"Bearer {self.portail.secret}"}) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = {}
                return r.status, data if isinstance(data, dict) else {}

    # -- Discord : repères -------------------------------------------------

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.config.guild_id)

    def est_staff(self, member: discord.abc.User | None) -> bool:
        return isinstance(member, discord.Member) and any(r.id in self.staff_roles for r in member.roles)

    def _label(self, categorie: str) -> str:
        for cid, label, _ in self.cfg.categories:
            if cid == categorie:
                return label
        return categorie

    async def _ticket_du_salon(self, channel_id: int) -> tuple[int, int | None] | None:
        row = await self.db._fetchone("SELECT ticket_id, opener_id FROM support_tickets WHERE channel_id = ?", (channel_id,))
        return (int(row["ticket_id"]), row["opener_id"]) if row else None

    async def _memoriser(self, ticket_id: int, channel_id: int, opener_id: int | None) -> None:
        await self.db.conn.execute(
            "INSERT OR REPLACE INTO support_tickets(ticket_id, channel_id, opener_id, created_at) VALUES (?, ?, ?, ?)",
            (ticket_id, channel_id, opener_id, now_iso()),
        )
        await self.db.conn.commit()

    async def _oublier(self, ticket_id: int) -> None:
        await self.db.conn.execute("DELETE FROM support_tickets WHERE ticket_id = ?", (ticket_id,))
        await self.db.conn.commit()

    # -- le message permanent ---------------------------------------------

    async def ensure_panel(self, view: discord.ui.View) -> None:
        channel = self.bot.get_channel(self.cfg.panel_channel)
        if not isinstance(channel, discord.TextChannel):
            log.error("Support : le salon du message permanent est introuvable (%s).", self.cfg.panel_channel)
            return
        row = await self.db._fetchone("SELECT value FROM meta WHERE key = 'support_panel'")
        if row:
            try:
                await channel.fetch_message(int(row["value"]))
                return
            except discord.NotFound:
                log.warning("Support : le message permanent a disparu, republication.")
            except discord.HTTPException as exc:
                log.warning("Support : message permanent illisible (%s), on continue.", exc)
                return
        embed = discord.Embed(
            title="Support Grand Line RP",
            description=(
                "Un souci, une question, une demande au staff ? Choisis la catégorie : un salon privé "
                "s'ouvre pour toi, visible du staff seulement. Le même ticket se suit aussi sur "
                "nova-rp.online si ton Discord y est lié."
            ),
            colour=discord.Colour.gold(),
        )
        msg = await channel.send(embed=embed, view=view)
        await self.db.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('support_panel', ?)", (str(msg.id),))
        await self.db.conn.commit()
        log.info("Support : message permanent publié (id %s).", msg.id)

    # -- création d'un salon ----------------------------------------------

    async def _creer_salon(self, ticket_id: int, sujet: str, ouvreur: discord.Member | None) -> discord.TextChannel:
        guild = self.guild()
        assert guild is not None
        category = guild.get_channel(self.cfg.category)
        if not isinstance(category, discord.CategoryChannel):
            raise RuntimeError("catégorie TICKETS introuvable")
        # Les droits de la catégorie (le staff) plus le joueur : personne d'autre.
        overwrites = dict(category.overwrites)
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        if ouvreur is not None:
            overwrites[ouvreur] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        return await guild.create_text_channel(
            f"ticket-{ticket_id}-{_slug(sujet, 30)}", category=category, overwrites=overwrites,
            reason=f"Ticket #{ticket_id}",
        )

    def _entete(self, ticket: dict, joueur: str) -> discord.Embed:
        e = discord.Embed(
            title=f"Ticket #{ticket['ticket_id']} · {self._label(str(ticket.get('categorie', '')))}",
            description=str(ticket.get("sujet", "")),
            colour=discord.Colour.gold(),
        )
        e.add_field(name="Joueur", value=joueur or "?", inline=True)
        if ticket.get("assigne_nom"):
            e.add_field(name="Pris en charge par", value=str(ticket["assigne_nom"]), inline=True)
        e.set_footer(text="Répondre avec le bouton : seul ce qui passe par lui arrive sur le site et en jeu. Le reste du salon reste ici.")
        return e

    def _message_embed(self, m: dict) -> discord.Embed:
        t = str(m.get("auteur_type", ""))
        if t == "systeme":
            return discord.Embed(description=f"*{m.get('corps', '')}*", colour=discord.Colour.dark_grey())
        couleur = discord.Colour.gold() if t == "staff" else discord.Colour.blue()
        e = discord.Embed(description=str(m.get("corps", ""))[:4000], colour=couleur)
        e.set_author(name=f"{m.get('auteur_nom', '?')} · {TYPES.get(t, t)} · {ORIGINES.get(str(m.get('origine', '')), '')}")
        return e

    async def _poster_messages(self, channel: discord.TextChannel, messages: list[dict]) -> list[int]:
        postes: list[int] = []
        for m in messages:
            await channel.send(embed=self._message_embed(m))
            postes.append(int(m["id"]))
        return postes

    # -- ouverture depuis Discord ----------------------------------------

    async def ouvrir(self, interaction: discord.Interaction, categorie: str, sujet: str, message: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.enabled:
            await _dire(interaction, "Le support n'est pas disponible pour le moment.")
            return
        member = interaction.user
        salon = await self._creer_salon(0, sujet, member if isinstance(member, discord.Member) else None)
        statut, data = await self._api("POST", "/api/pont/tickets/entrant", {
            "type": "ouverture", "discord_user_id": str(member.id), "discord_user_name": member.display_name,
            "categorie": categorie, "sujet": sujet, "message": message, "channel_id": str(salon.id),
        })
        if statut != 200 or "ticket_id" not in data:
            log.warning("Support : le portail refuse l'ouverture (HTTP %s) : %s", statut, data)
            await salon.delete(reason="ouverture refusée par le portail")
            await _dire(interaction, "Le portail n'a pas pu enregistrer le ticket. Réessaie dans une minute.")
            return
        ticket_id = int(data["ticket_id"])
        await salon.edit(name=f"ticket-{ticket_id}-{_slug(sujet, 30)}")
        await self._memoriser(ticket_id, salon.id, member.id)
        entete = self._entete({"ticket_id": ticket_id, "categorie": categorie, "sujet": sujet}, member.display_name)
        await salon.send(content=member.mention, embed=entete, view=TicketView(self))
        await salon.send(embed=self._message_embed({"auteur_type": "joueur", "auteur_nom": member.display_name, "corps": message, "origine": "discord"}))
        if not data.get("lie"):
            await salon.send(embed=discord.Embed(
                description="Ce Discord n'est pas lié à un compte sur nova-rp.online : le ticket vit ici. Lie ton compte sur le site pour le suivre aussi là-bas et recevoir les réponses en jeu.",
                colour=discord.Colour.dark_grey()))
        log.info("Support : ticket #%d ouvert sur Discord par %s (salon %s).", ticket_id, member, salon.id)
        await _dire(interaction, f"Ton ticket est ouvert : {salon.mention}")

    # -- boutons du salon ---------------------------------------------------

    async def peut_ecrire(self, interaction: discord.Interaction) -> bool:
        t = await self._ticket_du_salon(interaction.channel_id or 0)
        if t is None:
            return False
        return self.est_staff(interaction.user) or t[1] == interaction.user.id

    async def repondre(self, interaction: discord.Interaction, texte: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = await self._ticket_du_salon(interaction.channel_id or 0)
        if t is None:
            await _dire(interaction, "Ce salon n'est pas un ticket connu.")
            return
        staff = self.est_staff(interaction.user)
        statut, data = await self._api("POST", "/api/pont/tickets/entrant", {
            "type": "message", "ticket_id": str(t[0]), "discord_user_id": str(interaction.user.id),
            "nom": interaction.user.display_name, "staff": staff, "corps": texte,
        })
        if statut != 200:
            log.warning("Support : réponse refusée par le portail (HTTP %s) : %s", statut, data)
            await _dire(interaction, "Le portail n'a pas pris la réponse (ticket fermé ?). Rien n'a été envoyé.")
            return
        salon = interaction.channel
        if isinstance(salon, discord.TextChannel):
            await salon.send(embed=self._message_embed({
                "auteur_type": "staff" if staff else "joueur", "auteur_nom": interaction.user.display_name,
                "corps": texte, "origine": "discord",
            }))
        await _dire(interaction, "Transmis." + (" Le joueur le reçoit sur le site et en jeu." if staff else ""))

    async def prendre(self, interaction: discord.Interaction) -> None:
        if not self.est_staff(interaction.user):
            await _dire(interaction, "Réservé au staff.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = await self._ticket_du_salon(interaction.channel_id or 0)
        if t is None:
            await _dire(interaction, "Ce salon n'est pas un ticket connu.")
            return
        statut, data = await self._api("POST", "/api/pont/tickets/entrant", {
            "type": "prendre", "ticket_id": str(t[0]), "discord_user_id": str(interaction.user.id), "nom": interaction.user.display_name,
        })
        if statut != 200:
            await _dire(interaction, "Le portail a refusé (ticket fermé ?).")
            return
        salon = interaction.channel
        if isinstance(salon, discord.TextChannel):
            await salon.send(embed=self._message_embed({"auteur_type": "systeme", "corps": f"{interaction.user.display_name} a pris le ticket en charge."}))
        await _dire(interaction, "C'est à toi.")

    async def fermer(self, interaction: discord.Interaction) -> None:
        if not await self.peut_ecrire(interaction):
            await _dire(interaction, "Seuls le joueur du ticket et le staff peuvent fermer.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = await self._ticket_du_salon(interaction.channel_id or 0)
        if t is None:
            await _dire(interaction, "Ce salon n'est pas un ticket connu.")
            return
        statut, data = await self._api("POST", "/api/pont/tickets/entrant", {
            "type": "fermer", "ticket_id": str(t[0]), "nom": interaction.user.display_name,
        })
        if statut != 200:
            await _dire(interaction, "Le portail n'a pas pu fermer le ticket. Réessaie.")
            return
        salon = interaction.channel
        if isinstance(salon, discord.TextChannel):
            await self._clore_salon(t[0], salon, {"ticket_id": t[0], "transcript": data.get("transcript", [])}, par=interaction.user.display_name)
        await _dire(interaction, "Ticket fermé. La transcription est dans le salon transcript.")

    # -- fermeture : transcription puis suppression -------------------------

    async def _clore_salon(self, ticket_id: int, salon: discord.TextChannel, ticket: dict, par: str | None = None) -> None:
        lignes = [f"Ticket #{ticket_id} · {ticket.get('sujet', salon.name)} · {self._label(str(ticket.get('categorie', '')))}"]
        if ticket.get("joueur_nom"):
            lignes.append(f"Joueur : {ticket['joueur_nom']}")
        lignes.append("")
        for m in ticket.get("transcript", []) or []:
            lignes.append(f"[{_quand(m.get('created_at'))}] {m.get('auteur_nom', '?')} ({TYPES.get(str(m.get('auteur_type')), '?')}, {ORIGINES.get(str(m.get('origine')), '?')}) : {m.get('corps', '')}")
        texte = "\n".join(lignes)
        transcript = self.bot.get_channel(self.cfg.transcript_channel)
        if isinstance(transcript, discord.TextChannel):
            try:
                fichier = discord.File(io.BytesIO(texte.encode("utf-8")), filename=f"ticket-{ticket_id}.txt")
                await transcript.send(content=f"Ticket #{ticket_id} fermé" + (f" par {par}" if par else "") + f" — {len(ticket.get('transcript') or [])} message(s).", file=fichier)
            except discord.HTTPException as exc:
                log.warning("Support : transcription du ticket #%d non déposée : %s", ticket_id, exc)
        try:
            await salon.send(embed=discord.Embed(description="*Ticket fermé. Ce salon disparaît dans quelques secondes ; l'historique reste sur nova-rp.online.*", colour=discord.Colour.dark_grey()))
            await asyncio.sleep(8)
            await salon.delete(reason=f"Ticket #{ticket_id} fermé")
        except discord.HTTPException as exc:
            log.warning("Support : salon du ticket #%d non supprimé : %s", ticket_id, exc)
        await self._oublier(ticket_id)

    # -- sondage du site --------------------------------------------------

    async def sonder(self) -> None:
        statut, data = await self._api("GET", "/api/pont/tickets/sortant")
        if statut != 200:
            if not self._echec_dit:
                log.warning("Support : le portail répond HTTP %s au sondage.", statut)
                self._echec_dit = True
            return
        self._echec_dit = False
        guild = self.guild()
        if guild is None:
            return
        ack: dict = {"crees": [], "messages": [], "fermes": [], "salons_disparus": []}

        for t in data.get("a_creer", []) or []:
            try:
                ticket_id = int(t["ticket_id"])
                joueur = t.get("joueur") or {}
                membre = guild.get_member(int(joueur["discord_id"])) if joueur.get("discord_id") else None
                salon = await self._creer_salon(ticket_id, str(t.get("sujet", "")), membre)
                await self._memoriser(ticket_id, salon.id, membre.id if membre else None)
                await salon.send(content=membre.mention if membre else None, embed=self._entete(t, str(joueur.get("nom", "?"))), view=TicketView(self))
                if membre is None:
                    await salon.send(embed=discord.Embed(description="*Le joueur n'est pas sur ce Discord (ou pas lié) : il lit les réponses sur le site et en jeu.*", colour=discord.Colour.dark_grey()))
                postes = await self._poster_messages(salon, t.get("messages", []) or [])
                ack["crees"].append({"ticket_id": ticket_id, "channel_id": str(salon.id), "message_ids": postes})
                log.info("Support : salon créé pour le ticket #%d du site.", ticket_id)
            except Exception:
                log.exception("Support : création du salon impossible pour %s", t.get("ticket_id"))

        for m in data.get("messages", []) or []:
            try:
                salon = guild.get_channel(int(m["discord_channel_id"]))
                if not isinstance(salon, discord.TextChannel):
                    ack["salons_disparus"].append(int(m["ticket_id"]))
                    continue
                await salon.send(embed=self._message_embed(m))
                ack["messages"].append(int(m["id"]))
            except Exception:
                log.exception("Support : message %s non posté", m.get("id"))

        for t in data.get("a_fermer", []) or []:
            try:
                ticket_id = int(t["ticket_id"])
                salon = guild.get_channel(int(t["discord_channel_id"]))
                if isinstance(salon, discord.TextChannel):
                    await self._clore_salon(ticket_id, salon, t)
                else:
                    await self._oublier(ticket_id)
                ack["fermes"].append(ticket_id)
            except Exception:
                log.exception("Support : fermeture du salon impossible pour %s", t.get("ticket_id"))

        if any(ack.values()):
            statut, _ = await self._api("POST", "/api/pont/tickets/sortant", ack)
            if statut != 200:
                log.warning("Support : accusé refusé par le portail (HTTP %s) ; le tour suivant recommencera.", statut)

    async def demarrer(self, view: discord.ui.View) -> None:
        if not self.enabled:
            log.info("Support : désactivé ([support] incomplet ou portail non configuré).")
            return
        await self.ensure_panel(view)
        self._loop = asyncio.create_task(self._boucle())
        log.info("Support : sondage du portail toutes les %d s.", self.cfg.poll_seconds)

    async def _boucle(self) -> None:
        while True:
            try:
                await self.sonder()
            except Exception:
                log.exception("Support : sondage en erreur")
            await asyncio.sleep(self.cfg.poll_seconds)
