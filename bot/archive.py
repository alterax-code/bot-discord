"""L'archive quotidienne : un message par jour, modifié à chaque validation.

Deux principes gouvernent ce fichier.

**Le contenu des pages est toujours reconstruit depuis la base**, jamais
accumulé en mémoire ni relu depuis Discord. Une page abîmée se répare donc
d'elle-même à la prochaine écriture, et une annulation se reflète sans
bricolage.

**On modifie, on ne republie pas.** Le message du jour garde son identité :
les liens vers lui restent valides, et l'historique du salon ne se remplit
pas de doublons.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import discord

from .config import Config
from .database import Database

log = logging.getLogger(__name__)

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]

SEPARATEUR = "\n\n"


class ArchiveService:
    def __init__(self, bot: discord.Client, config: Config, db: Database) -> None:
        self.bot = bot
        self.config = config
        self.db = db
        # Posée par le client : la vue à accrocher sur la dernière page.
        self.undo_view_factory = None

    # -- dates -------------------------------------------------------------

    def now(self) -> datetime:
        return datetime.now(self.config.archive.timezone)

    def day_key(self, moment: datetime | None = None) -> str:
        """La journée d'archive, découpée sur le fuseau configuré."""
        return (moment or self.now()).astimezone(self.config.archive.timezone).strftime("%Y-%m-%d")

    def day_label(self, day: str) -> str:
        """« jeudi 6 août 2026 » — sans dépendre de la locale du système,
        qui n'est pas la même sur Windows et dans un conteneur."""
        d = datetime.strptime(day, "%Y-%m-%d")
        return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"

    def local(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso).astimezone(self.config.archive.timezone)

    # -- rendu -------------------------------------------------------------

    def render_entry(self, entry) -> str:
        snap = json.loads(entry["snapshot_json"])
        kind = snap.get("kind", "bug")
        label = self.config.publication.prefix_bug if kind == "bug" else self.config.publication.prefix_feature
        icone = "🐛" if kind == "bug" else "✨"
        titre = snap.get("title", "(sans titre)")

        if entry["cancelled"]:
            quand = self.local(entry["cancelled_at"]).strftime("%d/%m à %H:%M") if entry["cancelled_at"] else "?"
            return (
                f"~~{icone} **{label} {titre}**~~ · `#{entry['ticket_id']}`\n"
                f"↩️ **ANNULÉE** le {quand} par **{entry['cancelled_by_name']}** — "
                "le ticket est reparti en traitement."
            )

        lignes = [f"{icone} **{label} {titre}** · `#{entry['ticket_id']}`"]

        # Le contenu complet du ticket, borné pour qu'une entrée bavarde ne
        # puisse jamais dépasser une page entière. Le texte intégral reste
        # dans la base : rien n'est perdu, seul l'affichage est tronqué.
        corps = []
        for cle, valeur in snap.get("fields", {}).items():
            if cle == "Titre" or not valeur:
                continue
            corps.append(f"**{cle}** : {valeur}")
        texte = "\n".join(corps)
        limite = self.config.archive.max_entry_chars
        if len(texte) > limite:
            texte = texte[: limite - 1].rstrip() + "…"
        if texte:
            lignes.append(texte)

        # Qui a fait quoi — le cœur de la traçabilité.
        parts = snap.get("participants", {})
        rx = self.config.reactions
        suivi = [f"👤 Signalé par **{snap.get('author_name', '?')}**"]
        for emoji, intitule in (
            (rx.reported, "découvert par"),
            (rx.fixing, "corrigé par"),
            (rx.validated, "validé par"),
        ):
            noms = parts.get(emoji)
            if noms:
                suivi.append(f"{emoji} {intitule} **{', '.join(noms)}**")
        lignes.append(" · ".join(suivi))

        quand = self.local(entry["validated_at"]).strftime("%d/%m/%Y à %H:%M")
        pied = f"🕗 Validé le {quand} par **{entry['validated_by_name']}**"
        if snap.get("apres_annulation"):
            pied += f" — *revalidation après l'annulation du {snap['apres_annulation']}*"
        lignes.append(pied)

        return "\n".join(lignes)

    async def render_page(self, day: str, page_no: int) -> str:
        entries = await self.db.entries_on_page(day, page_no)
        if not entries:
            return "*Aucune entrée sur cette page.*"
        return SEPARATEUR.join(self.render_entry(e) for e in entries)

    def page_embed(self, day: str, page_no: int, total: int, description: str) -> discord.Embed:
        embed = discord.Embed(
            title=f"📅 {self.day_label(day)} — {page_no}/{total}",
            description=description,
            colour=discord.Colour.dark_grey(),
        )
        embed.set_footer(text="Archive Grand Line RP")
        return embed

    # -- écriture ----------------------------------------------------------

    def channel(self) -> discord.TextChannel:
        ch = self.bot.get_channel(self.config.channels.archives)
        if not isinstance(ch, discord.TextChannel):
            raise RuntimeError("Le salon des archives est introuvable ou n'est pas textuel.")
        return ch

    async def choose_page(self, day: str, entry_text: str) -> int:
        """Sur quelle page cette entrée doit-elle atterrir ?

        Tant qu'elle tient sur la dernière page, on l'y met. Sinon on en ouvre
        une nouvelle. C'est cette décision qui rend impossible l'échec par
        dépassement de la limite de longueur de Discord.
        """
        pages = await self.db.pages_for_day(day)
        if not pages:
            return 1
        dernier = int(pages[-1]["page_no"])
        actuel = await self.render_page(day, dernier)
        if actuel.startswith("*Aucune"):
            actuel = ""
        projete = len(actuel) + len(SEPARATEUR) + len(entry_text)
        if projete <= self.config.archive.max_page_chars:
            return dernier
        return dernier + 1

    async def refresh_pages(self, day: str) -> None:
        """Réécrit TOUTES les pages du jour : contenu et pagination.

        C'est ce qui produit le passage de « 1/1 » à « 1/2 » puis « 1/3 » sur
        les pages déjà publiées. Le bouton d'annulation ne vit que sur la
        dernière page — celle qui porte la validation la plus récente.
        """
        pages = await self.db.pages_for_day(day)
        if not pages:
            return
        total = len(pages)
        canal = self.channel()

        for index, page in enumerate(pages, start=1):
            page_no = int(page["page_no"])
            try:
                message = await canal.fetch_message(int(page["message_id"]))
            except discord.NotFound:
                log.warning("Page %d du %s introuvable dans le salon — ignorée.", page_no, day)
                continue

            description = await self.render_page(day, page_no)
            embed = self.page_embed(day, page_no, total, description)
            derniere = index == total
            vue = self.undo_view_factory() if (derniere and self.undo_view_factory) else None
            try:
                await message.edit(embed=embed, view=vue)
            except discord.HTTPException as exc:
                log.error("Impossible de réécrire la page %d/%d du %s : %s", page_no, total, day, exc)

    async def ensure_page(self, day: str, page_no: int) -> None:
        """Crée le message de la page s'il n'existe pas encore, sans réécrire le reste."""
        pages = await self.db.pages_for_day(day)
        existantes = {int(p["page_no"]) for p in pages}
        if page_no in existantes:
            return
        canal = self.channel()
        total = len(existantes) + 1
        embed = self.page_embed(day, page_no, total, "*Page en cours d'écriture…*")
        message = await canal.send(embed=embed)
        await self.db.add_page(day, page_no, message.id)
        log.info("Archive du %s : ouverture de la page %d (total %d).", day, page_no, total)

    async def append(self, entry_id: int, day: str, page_no: int) -> None:
        """Publie ou modifie la page qui accueille l'entrée qu'on vient de créer.

        La réécriture qui suit remplit la nouvelle page ET corrige la
        pagination affichée sur toutes les précédentes.
        """
        await self.ensure_page(day, page_no)
        await self.refresh_pages(day)
