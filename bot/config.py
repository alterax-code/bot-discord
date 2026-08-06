"""Chargement et validation de la configuration.

Deux sources, volontairement séparées :
  - config.toml : les identifiants Discord, publics, versionnés dans Git.
  - .env        : le jeton, secret, jamais versionné.

Toute erreur de configuration est détectée ICI, au démarrage, avec un message
explicite — plutôt que de produire un plantage incompréhensible trois heures
plus tard au moment où quelqu'un clique sur un bouton.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Configuration invalide : le bot refuse de démarrer."""


@dataclass(frozen=True, slots=True)
class Channels:
    tickets: int
    archives: int
    public: int


@dataclass(frozen=True, slots=True)
class Reactions:
    reported: str
    fixing: str
    validated: str

    @property
    def all(self) -> tuple[str, str, str]:
        """Les trois émojis dans l'ordre où le bot les pose."""
        return (self.reported, self.fixing, self.validated)


@dataclass(frozen=True, slots=True)
class Publication:
    prefix_bug: str
    prefix_feature: str


@dataclass(frozen=True, slots=True)
class Archive:
    timezone: ZoneInfo
    timezone_name: str
    max_page_chars: int
    max_entry_chars: int


@dataclass(frozen=True, slots=True)
class Behaviour:
    pin_panel: bool


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    log_level: str
    guild_id: int
    channels: Channels
    validator_roles: frozenset[int]
    reactions: Reactions
    publication: Publication
    archive: Archive
    behaviour: Behaviour

    @property
    def channel_ids(self) -> dict[str, int]:
        """Les salons indexés par un nom lisible, pour les diagnostics."""
        return {
            "tickets": self.channels.tickets,
            "archives": self.channels.archives,
            "public": self.channels.public,
        }


def _require(table: dict, section: str, key: str, kind: type):
    """Lit une clé obligatoire et vérifie son type."""
    if key not in table:
        raise ConfigError(f"config.toml : la clé « {key} » manque dans la section [{section}].")
    value = table[key]
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ConfigError(
            f"config.toml : [{section}].{key} devrait être de type {kind.__name__}, "
            f"pas {type(value).__name__}."
        )
    return value


def _section(data: dict, name: str) -> dict:
    if name not in data or not isinstance(data[name], dict):
        raise ConfigError(f"config.toml : la section [{name}] est absente.")
    return data[name]


def load(config_path: Path | str = "config.toml", *, env_file: str | None = ".env") -> Config:
    """Charge et valide toute la configuration. Lève ConfigError si quoi que ce soit cloche."""
    # --- le jeton, depuis l'environnement (ou .env en développement local) ---
    if env_file:
        load_dotenv(env_file, override=False)

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "Le jeton Discord est introuvable.\n"
            "  En local  : copie « .env.example » en « .env » et colle le jeton dedans.\n"
            "  En Docker : passe DISCORD_TOKEN par le fichier d'environnement du service."
        )
    if token.startswith("colle_ton_jeton"):
        raise ConfigError("Le fichier .env contient encore le texte d'exemple, pas un vrai jeton.")

    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()

    # --- le reste, depuis config.toml ---
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Fichier de configuration introuvable : {path.resolve()}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml est syntaxiquement invalide : {exc}") from exc

    guild_id = _require(_section(data, "discord"), "discord", "guild_id", int)

    ch = _section(data, "channels")
    channels = Channels(
        tickets=_require(ch, "channels", "tickets", int),
        archives=_require(ch, "channels", "archives", int),
        public=_require(ch, "channels", "public", int),
    )
    # Trois salons distincts : sinon le bot publierait les annonces joueurs
    # dans le salon staff, ou écraserait l'archive avec les tickets.
    if len({channels.tickets, channels.archives, channels.public}) != 3:
        raise ConfigError(
            "config.toml : les trois salons doivent être différents "
            f"(tickets={channels.tickets}, archives={channels.archives}, public={channels.public})."
        )

    roles = _section(data, "roles")
    validators = roles.get("validators")
    if not isinstance(validators, list) or not validators:
        raise ConfigError("config.toml : [roles].validators doit être une liste non vide.")
    if not all(isinstance(r, int) and not isinstance(r, bool) for r in validators):
        raise ConfigError("config.toml : [roles].validators ne doit contenir que des identifiants numériques.")

    rx = _section(data, "reactions")
    reactions = Reactions(
        reported=_require(rx, "reactions", "reported", str),
        fixing=_require(rx, "reactions", "fixing", str),
        validated=_require(rx, "reactions", "validated", str),
    )
    if len(set(reactions.all)) != 3:
        raise ConfigError("config.toml : les trois émojis de [reactions] doivent être différents.")

    pub = _section(data, "publication")
    publication = Publication(
        prefix_bug=_require(pub, "publication", "prefix_bug", str),
        prefix_feature=_require(pub, "publication", "prefix_feature", str),
    )

    arc = _section(data, "archive")
    tz_name = _require(arc, "archive", "timezone", str)
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"config.toml : fuseau horaire « {tz_name} » introuvable.\n"
            "  Soit le nom est erroné (il doit suivre la nomenclature IANA, ex. « Europe/Paris »),\n"
            "  soit la base des fuseaux manque : vérifie que le paquet « tzdata » est installé\n"
            "  (pip install -r requirements.txt). Windows et les images Docker minimales\n"
            "  n'en fournissent aucune."
        ) from exc

    max_page = _require(arc, "archive", "max_page_chars", int)
    max_entry = _require(arc, "archive", "max_entry_chars", int)
    # Discord plafonne la description d'un encart à 4096 caractères.
    if not 500 <= max_page <= 3900:
        raise ConfigError("config.toml : [archive].max_page_chars doit être compris entre 500 et 3900.")
    if not 200 <= max_entry <= max_page:
        raise ConfigError(
            "config.toml : [archive].max_entry_chars doit valoir au moins 200 "
            "et ne pas dépasser max_page_chars — sinon une entrée ne rentrerait dans aucune page."
        )

    beh = data.get("behaviour", {})
    behaviour = Behaviour(pin_panel=bool(beh.get("pin_panel", True)))

    return Config(
        token=token,
        log_level=log_level,
        guild_id=guild_id,
        channels=channels,
        validator_roles=frozenset(validators),
        reactions=reactions,
        publication=publication,
        archive=Archive(
            timezone=tz,
            timezone_name=tz_name,
            max_page_chars=max_page,
            max_entry_chars=max_entry,
        ),
        behaviour=behaviour,
    )
