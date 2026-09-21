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
class Display:
    bug_label: str
    bug_color: int
    feature_label: str
    feature_color: int
    panel_title: str
    panel_text: str

    def label(self, kind: str) -> str:
        return self.bug_label if kind == "bug" else self.feature_label

    def color(self, kind: str) -> int:
        return self.bug_color if kind == "bug" else self.feature_color


@dataclass(frozen=True, slots=True)
class Behaviour:
    pin_panel: bool


@dataclass(frozen=True, slots=True)
class Whitelist:
    """Rôles staff → portail. Désactivée si le secret ou les rôles manquent :
    le bot de tickets doit tourner même sans portail."""
    staff_roles: frozenset[int]
    portail_url: str
    secret: str
    resync_seconds: int

    @property
    def enabled(self) -> bool:
        return bool(self.staff_roles and self.portail_url and self.secret)


@dataclass(frozen=True, slots=True)
class Sanctions:
    """Miroir des bans Discord → jeu. Les maîtres ne sont jamais transmis."""
    maitres: frozenset[int]


@dataclass(frozen=True, slots=True)
class Support:
    """Tickets de support unifiés Discord ↔ site. Désactivé si la section
    manque ou si le portail n'est pas configuré ([whitelist])."""
    enabled: bool
    panel_channel: int
    category: int
    transcript_channel: int
    poll_seconds: int
    categories: tuple[tuple[str, str, str], ...]   # (id, label, emoji)

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.panel_channel and self.category and self.transcript_channel and self.categories)


@dataclass(frozen=True, slots=True)
class Serveur:
    """Pilotage du serveur de jeu (/serveur) depuis un salon dédié, par une
    liste nominative d'opérateurs. Les identifiants API mTxServ sont dans
    l'environnement (MTXSERV_*), jamais ici. Désactivé sans salon ni opérateur."""
    salon: int
    operateurs: frozenset[int]
    mainteneurs: frozenset[int]   # les seuls à voir et lancer « update »
    game_host: str
    game_port: int
    avertissement_secondes: int

    @property
    def enabled(self) -> bool:
        return bool(self.salon and self.operateurs)


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
    display: Display
    behaviour: Behaviour
    whitelist: Whitelist
    sanctions: Sanctions
    support: Support
    serveur: Serveur

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


def _parse_color(table: dict, key: str) -> int:
    """Lit une couleur au format « #RRGGBB » et la rend sous forme d'entier."""
    raw = _require(table, "display", key, str).strip().lstrip("#")
    try:
        value = int(raw, 16)
    except ValueError:
        raise ConfigError(
            f"config.toml : [display].{key} doit être une couleur hexadécimale, "
            f'par exemple "#E74C3C" — reçu "{raw}".'
        ) from None
    if not 0 <= value <= 0xFFFFFF:
        raise ConfigError(f"config.toml : [display].{key} sort de la plage des couleurs.")
    return value


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

    dis = _section(data, "display")
    display = Display(
        bug_label=_require(dis, "display", "bug_label", str),
        bug_color=_parse_color(dis, "bug_color"),
        feature_label=_require(dis, "display", "feature_label", str),
        feature_color=_parse_color(dis, "feature_color"),
        panel_title=_require(dis, "display", "panel_title", str),
        panel_text=_require(dis, "display", "panel_text", str).strip(),
    )

    beh = data.get("behaviour", {})
    behaviour = Behaviour(pin_panel=bool(beh.get("pin_panel", True)))

    # --- whitelist : section optionnelle, secret dans l'environnement ---
    wl = data.get("whitelist", {})
    if not isinstance(wl, dict):
        raise ConfigError("config.toml : [whitelist] doit être une section.")
    staff_roles = wl.get("staff_roles", [])
    if not isinstance(staff_roles, list) or not all(
        isinstance(r, int) and not isinstance(r, bool) for r in staff_roles
    ):
        raise ConfigError("config.toml : [whitelist].staff_roles ne doit contenir que des identifiants numériques.")
    resync = wl.get("resync_seconds", 600)
    if not isinstance(resync, int) or isinstance(resync, bool) or resync < 60:
        raise ConfigError("config.toml : [whitelist].resync_seconds doit être un entier ≥ 60.")
    whitelist = Whitelist(
        staff_roles=frozenset(staff_roles),
        portail_url=str(wl.get("portail_url", "")).strip(),
        secret=os.environ.get("PORTAIL_SECRET", "").strip(),
        resync_seconds=resync,
    )

    # --- sanctions : les maîtres, jamais transmis au jeu ---
    sa = data.get("sanctions", {})
    if not isinstance(sa, dict):
        raise ConfigError("config.toml : [sanctions] doit être une section.")
    maitres = sa.get("maitres", [])
    if not isinstance(maitres, list) or not all(isinstance(m, int) and not isinstance(m, bool) for m in maitres):
        raise ConfigError("config.toml : [sanctions].maitres ne doit contenir que des identifiants numériques.")
    sanctions = Sanctions(maitres=frozenset(maitres))

    # --- support : section optionnelle ---
    su = data.get("support", {})
    if not isinstance(su, dict):
        raise ConfigError("config.toml : [support] doit être une section.")
    def _id(key: str) -> int:
        v = su.get(key, 0)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ConfigError(f"config.toml : [support].{key} doit être un identifiant numérique.")
        return v
    cats: list[tuple[str, str, str]] = []
    for c in su.get("categories", []):
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not isinstance(c.get("label"), str):
            raise ConfigError("config.toml : [support].categories attend des tables {id, label, emoji}.")
        cats.append((c["id"], c["label"], str(c.get("emoji", ""))))
    poll = su.get("poll_seconds", 15)
    if not isinstance(poll, int) or isinstance(poll, bool) or poll < 5:
        raise ConfigError("config.toml : [support].poll_seconds doit être un entier ≥ 5.")
    support = Support(
        enabled=bool(su.get("enabled", False)),
        panel_channel=_id("panel_channel"),
        category=_id("category"),
        transcript_channel=_id("transcript_channel"),
        poll_seconds=poll,
        categories=tuple(cats),
    )

    # --- serveur : section optionnelle, secrets dans l'environnement ---
    sv = data.get("serveur", {})
    if not isinstance(sv, dict):
        raise ConfigError("config.toml : [serveur] doit être une section.")
    salon = sv.get("salon", 0)
    if not isinstance(salon, int) or isinstance(salon, bool) or salon < 0:
        raise ConfigError("config.toml : [serveur].salon doit être un identifiant numérique.")
    operateurs = sv.get("operateurs", [])
    if not isinstance(operateurs, list) or not all(
        isinstance(o, int) and not isinstance(o, bool) for o in operateurs
    ):
        raise ConfigError("config.toml : [serveur].operateurs ne doit contenir que des identifiants numériques.")
    mainteneurs = sv.get("mainteneurs", [])
    if not isinstance(mainteneurs, list) or not all(
        isinstance(o, int) and not isinstance(o, bool) for o in mainteneurs
    ):
        raise ConfigError("config.toml : [serveur].mainteneurs ne doit contenir que des identifiants numériques.")
    game_port = sv.get("game_port", 0)
    if not isinstance(game_port, int) or isinstance(game_port, bool) or not 0 <= game_port <= 65535:
        raise ConfigError("config.toml : [serveur].game_port doit être un port valide.")
    avert = sv.get("avertissement_secondes", 60)
    if not isinstance(avert, int) or isinstance(avert, bool) or not 0 <= avert <= 600:
        raise ConfigError("config.toml : [serveur].avertissement_secondes doit être un entier entre 0 et 600.")
    serveur = Serveur(
        salon=salon,
        operateurs=frozenset(operateurs),
        mainteneurs=frozenset(mainteneurs) & frozenset(operateurs),
        game_host=str(sv.get("game_host", "")).strip(),
        game_port=game_port,
        avertissement_secondes=avert,
    )

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
        display=display,
        behaviour=behaviour,
        whitelist=whitelist,
        sanctions=sanctions,
        support=support,
        serveur=serveur,
    )
