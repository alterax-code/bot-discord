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

import logging
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

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
