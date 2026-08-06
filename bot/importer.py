"""Remise à zéro des essais, puis reprise des anciens signalements.

Deux opérations ponctuelles, à ne lancer qu'une fois :

  --reset   efface les tickets d'essai, dans le salon comme en base.
            Le message permanent à boutons est conservé : le supprimer
            obligerait le bot à en republier un et laisserait l'ancien
            orphelin dans le salon.

  --import  reprend les 32 signalements postés à la main avant le bot.

L'import croise deux sources :
  - legacy_import.toml   le texte, le titre et le classement bug/ajout
  - data/legacy_scan.json le vrai auteur, les vrais réacteurs, la vraie date

L'appariement des deux listes a été prouvé : les séquences d'auteurs
concordent 32/32 et les statuts déduits des réactions concordent 32/32 avec
ceux relevés à la main. La vérification est refaite ici avant d'écrire quoi
que ce soit — un import à moitié faux serait pire que pas d'import du tout.
"""

from __future__ import annotations

import json
import logging
import tomllib
import unicodedata
from pathlib import Path

import discord

log = logging.getLogger(__name__)

FICHIER_TOML = Path("legacy_import.toml")
FICHIER_SCAN = Path("data/legacy_scan.json")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip().lstrip(".")


# =============================================================================
#  REMISE À ZÉRO
# =============================================================================

async def reset(bot) -> None:
    cfg, db = bot.config, bot.db
    supprimes = 0

    async def effacer(channel_id: int, message_id, quoi: str) -> None:
        nonlocal supprimes
        if not message_id:
            return
        canal = bot.get_channel(channel_id)
        if not isinstance(canal, discord.TextChannel):
            return
        try:
            message = await canal.fetch_message(int(message_id))
            await message.delete()
            supprimes += 1
            log.info("Supprimé : %s (message %s)", quoi, message_id)
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            log.warning("Suppression impossible de %s : %s", quoi, exc)

    log.warning("REMISE À ZÉRO — suppression des essais.")

    for page in await db._fetchall("SELECT * FROM archive_pages"):
        await effacer(cfg.channels.archives, page["message_id"], f"page d'archive {page['day']} n°{page['page_no']}")

    for t in await db._fetchall("SELECT id, message_id, public_message_id FROM tickets"):
        await effacer(cfg.channels.public, t["public_message_id"], f"annonce publique du ticket #{t['id']}")
        await effacer(cfg.channels.tickets, t["message_id"], f"ticket #{t['id']}")

    for table in ("participants", "archive_entries", "archive_pages", "incidents", "tickets"):
        await db.conn.execute(f"DELETE FROM {table}")
    # Repartir de #1 : les essais ne doivent pas consommer de numéros.
    await db.conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('tickets','archive_entries')")
    await db.conn.commit()

    log.warning(
        "Remise à zéro terminée : %d message(s) supprimé(s), base vidée. "
        "Le message permanent à boutons est conservé.",
        supprimes,
    )


# =============================================================================
#  IMPORT
# =============================================================================

async def run_import(bot) -> None:
    cfg, db = bot.config, bot.db

    if not FICHIER_TOML.is_file():
        raise RuntimeError(f"{FICHIER_TOML} introuvable.")
    if not FICHIER_SCAN.is_file():
        raise RuntimeError(f"{FICHIER_SCAN} introuvable — lancer d'abord --scan-legacy.")

    lignes = tomllib.loads(FICHIER_TOML.read_text(encoding="utf-8"))["ticket"]
    scan = json.loads(FICHIER_SCAN.read_text(encoding="utf-8"))

    # --- garde-fous avant toute écriture ---------------------------------
    existants = await db._fetchone("SELECT COUNT(*) AS n FROM tickets")
    if existants and int(existants["n"]) > 0:
        raise RuntimeError(
            f"La base contient déjà {existants['n']} ticket(s). "
            "Lancer --reset d'abord, ou l'import créerait des doublons."
        )
    if len(lignes) != len(scan):
        raise RuntimeError(
            f"Les deux sources n'ont pas la même taille ({len(lignes)} vs {len(scan)}) : "
            "l'appariement serait faux."
        )
    ecarts = [
        i for i, (t, s) in enumerate(zip(lignes, scan), 1)
        if _norm(t["author"]) != _norm(s["auteur_nom"])
    ]
    if ecarts:
        raise RuntimeError(
            f"Les auteurs divergent aux lignes {ecarts} : l'appariement des deux "
            "sources n'est plus fiable, import interrompu."
        )
    log.info("Appariement vérifié : %d lignes, auteurs concordants.", len(lignes))

    # Les réactions d'origine, traduites vers nos trois statuts.
    # L'équipe utilisait un émoji personnalisé du serveur pour « en cours ».
    rx = cfg.reactions
    correspondance: dict[str, str] = {"❌": rx.reported, "✅": rx.validated}

    canal_tickets = bot.get_channel(cfg.channels.tickets)
    if not isinstance(canal_tickets, discord.TextChannel):
        raise RuntimeError("Salon des tickets introuvable.")

    a_valider: list[tuple[int, str]] = []
    publies = 0

    for ligne, source in zip(lignes, scan):
        fields = {"Titre": ligne["title"], "Signalement": ligne["detail"], "Capture": ""}
        ticket_id = await db.create_ticket(
            kind=ligne["kind"],
            title=ligne["title"],
            fields=fields,
            author_id=int(source["auteur_id"]),
            author_name=source["auteur_nom"],
        )
        # Conserver la date d'origine plutôt que celle de l'import.
        await db.conn.execute(
            "UPDATE tickets SET created_at = ? WHERE id = ?", (source["date"], ticket_id)
        )
        await db.conn.commit()

        # L'auteur compte d'office comme découvreur.
        await db.add_participant(
            ticket_id, rx.reported, int(source["auteur_id"]), source["auteur_nom"]
        )
        # Puis les vraies personnes derrière chaque réaction d'origine.
        valideur = None
        for emoji_source, users in source["reactions"].items():
            emoji = correspondance.get(emoji_source, rx.fixing)  # tout le reste = en cours
            for u in users:
                await db.add_participant(ticket_id, emoji, int(u["id"]), u["nom"])
                if emoji == rx.validated and valideur is None:
                    valideur = u["nom"]

        if ligne["statut"] == "valide":
            a_valider.append((ticket_id, valideur or source["auteur_nom"]))
            continue

        # Encore à traiter : le ticket va dans le salon de travail.
        participants = await db.participants(ticket_id)
        embed = bot.tickets.build_embed(
            ticket_id, ligne["kind"], ligne["title"], fields, None, participants=participants
        )
        message = await canal_tickets.send(embed=embed)
        await db.attach_message(ticket_id, message.id)
        for emoji in rx.all:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                pass
        publies += 1
        log.info("Importé #%d (%s, %s) — publié dans le salon.", ticket_id, ligne["kind"], ligne["statut"])

    log.info("%d ticket(s) en cours publié(s). Traitement des %d déjà validés…", publies, len(a_valider))

    # --- les déjà validés : archive + annonce, sans passer par le salon ---
    for ticket_id, valideur in a_valider:
        ticket = await db.get_ticket(ticket_id)
        await bot.validation.import_validated(ticket, valideur)

    # Une seule réécriture des pages à la fin, plutôt qu'une par entrée :
    # 13 réécritures successives se feraient étrangler par Discord.
    await bot.archive.refresh_pages(bot.archive.day_key())

    log.info(
        "IMPORT TERMINÉ : %d ticket(s) au total — %d en attente dans le salon, "
        "%d archivés et annoncés.",
        len(lignes), publies, len(a_valider),
    )


# =============================================================================
#  TRACE DE LA REPRISE, PUIS SUPPRESSION DES MESSAGES D'ORIGINE
# =============================================================================

async def cleanup_legacy(bot) -> None:
    """Dépose la note de reprise dans l'archive, PUIS supprime les originaux.

    Cet ordre n'est pas négociable : si la note échoue, rien n'est supprimé.
    Une fois les messages d'origine effacés, ils ne reviennent pas.
    """
    cfg, db = bot.config, bot.db

    lignes = tomllib.loads(FICHIER_TOML.read_text(encoding="utf-8"))["ticket"]
    scan = json.loads(FICHIER_SCAN.read_text(encoding="utf-8"))
    if len(lignes) != len(scan):
        raise RuntimeError("Sources désalignées : suppression refusée.")

    # --- 1. la note d'archive ---------------------------------------------
    canal_archives = bot.get_channel(cfg.channels.archives)
    if not isinstance(canal_archives, discord.TextChannel):
        raise RuntimeError("Salon des archives introuvable.")

    marques = {"valide": "✅", "en_cours": "⚠️", "signale": "❌"}
    corps = []
    for i, (ligne, source) in enumerate(zip(lignes, scan), start=1):
        icone = "🐛" if ligne["kind"] == "bug" else "✨"
        corps.append(
            f"`#{i:02}` {marques.get(ligne['statut'], '·')} {icone} "
            f"{ligne['title']} — *{source['auteur_nom']}*"
        )

    faits = sum(1 for l in lignes if l["statut"] == "valide")
    cours = sum(1 for l in lignes if l["statut"] == "en_cours")
    restants = len(lignes) - faits - cours
    date_debut = scan[0]["date"][:10]

    embed = discord.Embed(
        title="📜 Reprise des signalements antérieurs au bot",
        description=(
            f"Les **{len(lignes)} signalements** postés à la main dans ce salon avant la mise "
            "en place du bot ont été convertis en tickets. Leurs messages d'origine ont été "
            "supprimés après cette note.\n\n"
            f"Origine : messages du {date_debut} · "
            f"**{faits}** déjà faits · **{cours}** en cours · **{restants}** à traiter\n\n"
            + "\n".join(corps)
        )[:4096],
        colour=discord.Colour.dark_gold(),
    )
    embed.set_footer(text="Auteurs et réactions repris des messages d'origine — Archive Grand Line RP")
    note = await canal_archives.send(embed=embed)
    log.info("Note de reprise publiée dans l'archive (message %s).", note.id)

    # --- 2. la suppression, seulement maintenant --------------------------
    canal = bot.get_channel(cfg.channels.tickets)
    if not isinstance(canal, discord.TextChannel):
        raise RuntimeError("Salon des tickets introuvable.")

    supprimes, absents = 0, 0
    for source in scan:
        try:
            message = await canal.fetch_message(int(source["message_id"]))
            await message.delete()
            supprimes += 1
        except discord.NotFound:
            absents += 1
        except discord.HTTPException as exc:
            log.warning("Message d'origine %s non supprimé : %s", source["message_id"], exc)

    log.warning(
        "Messages d'origine supprimés : %d (%d déjà absents). "
        "Leur contenu, leurs auteurs et leurs réacteurs sont conservés en base.",
        supprimes, absents,
    )
