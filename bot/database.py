"""Couche de persistance (SQLite).

La base est la SOURCE DE VÉRITÉ du bot, pas Discord. C'est ce qui rend
l'exigence « aucune perte de ticket » tenable : le message d'origine n'est
supprimé qu'une fois le contenu écrit ici, et un redémarrage au mauvais moment
retrouve l'état exact où on en était.

Deux réglages comptent :
  - WAL      : les lectures ne bloquent plus les écritures, et une coupure
               brutale ne corrompt pas le fichier.
  - FOREIGN KEYS : SQLite ne les applique PAS par défaut. Sans cette ligne,
               supprimer un ticket laisserait ses participants orphelins.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)


def now_iso() -> str:
    """Horodatage de stockage, toujours en UTC.

    Les dates sont stockées en UTC et converties à l'affichage : c'est la seule
    façon qu'un changement d'heure d'été ne réordonne pas l'historique.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

SCHEMA_VERSION = 1

# --- États possibles d'un ticket -------------------------------------------
# open       : publié dans fix-bug, en attente
# validating : une coche verte autorisée vient d'être posée, traitement en cours
# archived   : écrit dans l'archive du jour
# published  : annoncé dans le salon joueurs
# done       : message d'origine supprimé, cycle terminé
# cancelled  : validation annulée par un retour arrière
STATES = ("open", "validating", "archived", "published", "done", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Le message permanent à deux boutons. Une seule ligne, garantie par la
-- contrainte sur l'identifiant : impossible d'en avoir deux par accident.
CREATE TABLE IF NOT EXISTS panel (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL CHECK (kind IN ('bug', 'feature')),
    title        TEXT    NOT NULL,
    fields_json  TEXT    NOT NULL,   -- les champs du formulaire, tels que saisis
    author_id    INTEGER NOT NULL,
    author_name  TEXT    NOT NULL,
    message_id   INTEGER UNIQUE,     -- message dans fix-bug ; NULL une fois supprimé
    state        TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    validated_at TEXT,
    validated_by INTEGER,
    archive_day        TEXT,
    archive_page       INTEGER,
    public_message_id  INTEGER,
    cancelled_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tickets_state   ON tickets(state);
CREATE INDEX IF NOT EXISTS idx_tickets_message ON tickets(message_id);
CREATE INDEX IF NOT EXISTS idx_tickets_day     ON tickets(archive_day);

-- Qui a réagi, sur quel émoji. Cumulatif et jamais purgé : le retrait d'une
-- réaction ne supprime pas la ligne, conformément à la spécification.
CREATE TABLE IF NOT EXISTS participants (
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    emoji     TEXT    NOT NULL,
    user_id   INTEGER NOT NULL,
    user_name TEXT    NOT NULL,
    added_at  TEXT    NOT NULL,
    PRIMARY KEY (ticket_id, emoji, user_id)
);

-- Les pages du message d'archive, une ligne par page et par jour.
CREATE TABLE IF NOT EXISTS archive_pages (
    day        TEXT    NOT NULL,
    page_no    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    char_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, page_no)
);

-- Les « couacs » : annulations de validation. Conservés pour que l'archive
-- puisse les mentionner, même si le ticket n'est jamais revalidé.
CREATE TABLE IF NOT EXISTS incidents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL,
    actor_id   INTEGER NOT NULL,
    actor_name TEXT    NOT NULL,
    at         TEXT    NOT NULL,
    details    TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_ticket ON incidents(ticket_id);
"""


class Database:
    """Enveloppe fine autour d'aiosqlite. Une instance pour toute la vie du bot."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("La base n'est pas ouverte : appeler connect() d'abord.")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row

        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        # Compromis durabilité/vitesse recommandé avec WAL : on ne perd rien
        # en cas de plantage du processus, seulement en cas de coupure système.
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.commit()

        await self._migrate()
        log.info("Base ouverte : %s (schéma v%d)", self.path.resolve(), SCHEMA_VERSION)

    async def _migrate(self) -> None:
        await self.conn.executescript(_SCHEMA)
        await self.conn.commit()

        row = await self._fetchone("SELECT value FROM meta WHERE key = 'schema_version'")
        current = int(row["value"]) if row else 0

        if current == 0:
            await self.conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            await self.conn.commit()
        elif current > SCHEMA_VERSION:
            raise RuntimeError(
                f"La base est en schéma v{current}, ce bot n'en connaît que v{SCHEMA_VERSION}. "
                "Version du code trop ancienne pour ces données — ne pas continuer."
            )
        elif current < SCHEMA_VERSION:
            # Emplacement des futures migrations, une par palier de version.
            log.info("Migration du schéma v%d vers v%d", current, SCHEMA_VERSION)
            await self.conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
            )
            await self.conn.commit()

    async def _fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    # -- le message permanent à boutons ------------------------------------

    async def get_panel(self) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT channel_id, message_id FROM panel WHERE id = 1")

    async def save_panel(self, channel_id: int, message_id: int) -> None:
        await self.conn.execute(
            "INSERT INTO panel(id, channel_id, message_id, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET channel_id = ?, message_id = ?, updated_at = ?",
            (channel_id, message_id, now_iso(), channel_id, message_id, now_iso()),
        )
        await self.conn.commit()

    # -- tickets -----------------------------------------------------------

    async def create_ticket(
        self, *, kind: str, title: str, fields: dict[str, str], author_id: int, author_name: str
    ) -> int:
        """Crée le ticket EN BASE, avant toute publication sur Discord.

        L'ordre compte : si Discord échoue juste après, le contenu saisi par
        le joueur est déjà sauvé et le ticket est rattrapable.
        """
        cur = await self.conn.execute(
            "INSERT INTO tickets(kind, title, fields_json, author_id, author_name, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (kind, title, json.dumps(fields, ensure_ascii=False), author_id, author_name, now_iso()),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def attach_message(self, ticket_id: int, message_id: int) -> None:
        await self.conn.execute(
            "UPDATE tickets SET message_id = ? WHERE id = ?", (message_id, ticket_id)
        )
        await self.conn.commit()

    async def get_ticket(self, ticket_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM tickets WHERE id = ?", (ticket_id,))

    async def get_ticket_by_message(self, message_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM tickets WHERE message_id = ?", (message_id,))

    async def open_tickets(self) -> list[aiosqlite.Row]:
        """Les tickets encore présents dans le salon, pour la réconciliation au démarrage."""
        return await self._fetchall(
            "SELECT * FROM tickets WHERE state = 'open' AND message_id IS NOT NULL ORDER BY id"
        )

    async def orphan_tickets(self) -> list[aiosqlite.Row]:
        """Tickets créés en base mais jamais publiés : une panne a interrompu la création."""
        return await self._fetchall(
            "SELECT * FROM tickets WHERE state = 'open' AND message_id IS NULL ORDER BY id"
        )

    # -- participants ------------------------------------------------------

    async def add_participant(
        self, ticket_id: int, emoji: str, user_id: int, user_name: str
    ) -> bool:
        """Enregistre une participation. Rend True si elle est nouvelle.

        Cumulatif et jamais retiré : conformément à la spécification, ôter une
        réaction ne supprime pas la trace de qui l'avait posée.
        """
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO participants(ticket_id, emoji, user_id, user_name, added_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticket_id, emoji, user_id, user_name, now_iso()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def participants(self, ticket_id: int) -> dict[str, list[str]]:
        """Les noms des participants, groupés par émoji, dans l'ordre d'arrivée."""
        rows = await self._fetchall(
            "SELECT emoji, user_name FROM participants WHERE ticket_id = ? ORDER BY added_at, rowid",
            (ticket_id,),
        )
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["emoji"], []).append(row["user_name"])
        return out

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            log.info("Base fermée proprement.")

    async def healthcheck(self) -> dict[str, int]:
        """Quelques compteurs, pour le diagnostic au démarrage."""
        out: dict[str, int] = {}
        for table in ("tickets", "participants", "archive_pages", "incidents"):
            row = await self._fetchone(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = int(row["n"]) if row else 0
        return out
