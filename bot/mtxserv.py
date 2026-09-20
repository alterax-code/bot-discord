"""Le serveur de jeu vu du bot : l'API mTxServ et la sonde A2S.

Deux sources, parce qu'aucune ne suffit seule :
  - l'API mTxServ démarre, arrête, redémarre, envoie une commande console et
    dit quel a été le dernier geste (game_start / game_stop) ; mais son
    « state » répond « dispo » même serveur éteint (vu le 16/09/2026) ;
  - la sonde A2S dit si le serveur RÉPOND réellement, avec la map et le
    nombre de joueurs.

Les identifiants API vivent dans .env (MTXSERV_*) : jamais dans config.toml,
jamais dans un message Discord. Même logique que la veille du VPS
(grandline-ops/veille/veille.py), reprise ici en asynchrone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime

import aiohttp

log = logging.getLogger(__name__)

BASE = "https://app.mtxserv.com"
UA = "GrandLineBot/1.0 (+https://nova-rp.online)"
TIMEOUT = aiohttp.ClientTimeout(total=20)

# Un jeton vit 3600 s ; on le renouvelle un peu avant.
MARGE_JETON_S = 120

A2S_INFO = b"\xff\xff\xff\xff\x54Source Engine Query\x00"


@dataclass(frozen=True, slots=True)
class EtatA2S:
    joueurs: int
    maximum: int
    map: str
    nom: str


def _a2s_sync(host: str, port: int, timeout: float) -> EtatA2S | None:
    """Une requête A2S_INFO bloquante ; None si le serveur ne répond pas."""
    try:
        addr = (socket.gethostbyname(host), port)
    except OSError:
        return None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(A2S_INFO, addr)
        data, _ = s.recvfrom(4096)
        if len(data) >= 9 and data[4] == 0x41:          # challenge : on rejoue une fois
            s.sendto(A2S_INFO + data[5:9], addr)
            data, _ = s.recvfrom(4096)
        if len(data) < 6 or data[4] != 0x49:
            return None
        o = 6
        champs = []
        for _ in range(4):                               # name, map, folder, game
            fin = data.index(b"\x00", o)
            champs.append(data[o:fin].decode("utf-8", "replace"))
            o = fin + 1
        o += 2                                           # app id
        return EtatA2S(joueurs=data[o], maximum=data[o + 1], map=champs[1], nom=champs[0])
    except (OSError, ValueError):
        return None
    finally:
        s.close()


async def sonder(host: str, port: int, timeout: float = 3.0) -> EtatA2S | None:
    """La sonde A2S, sans bloquer la boucle du bot."""
    if not host or not port:
        return None
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _a2s_sync, host, port, timeout)


class PanneauMTx:
    """L'API mTxServ d'un serveur de jeu. Inerte si les identifiants manquent."""

    def __init__(self) -> None:
        self.api_key = os.environ.get("MTXSERV_API_KEY", "").strip()
        self.client_id = os.environ.get("MTXSERV_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("MTXSERV_CLIENT_SECRET", "").strip()
        self.game_id = os.environ.get("MTXSERV_GAME_ID", "").strip()
        self._jeton: str | None = None
        self._jeton_expire = 0.0
        self._session: aiohttp.ClientSession | None = None

    @property
    def arme(self) -> bool:
        return bool(self.api_key and self.client_id and self.client_secret and self.game_id)

    async def fermer(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA})
        return self._session

    async def _jeton_valide(self) -> str | None:
        if self._jeton and time.monotonic() < self._jeton_expire:
            return self._jeton
        corps = {
            "grant_type": "https://mtxserv.com/grants/api_key",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "api_key": self.api_key,
        }
        try:
            async with self._http().post(f"{BASE}/oauth/v2/token", data=corps) as r:
                if r.status != 200:
                    log.warning("Jeton mTxServ refusé : HTTP %s %s", r.status, (await r.text())[:120])
                    return None
                payload = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Jeton mTxServ injoignable : %r", exc)
            return None
        self._jeton = payload.get("access_token")
        self._jeton_expire = time.monotonic() + int(payload.get("expires_in", 3600)) - MARGE_JETON_S
        return self._jeton

    async def _appel(self, methode: str, chemin: str, **kw) -> tuple[int, str]:
        jeton = await self._jeton_valide()
        if not jeton:
            return 0, "pas de jeton"
        url = f"{BASE}/api/v1/game/{self.game_id}/{chemin}"
        try:
            async with self._http().request(
                methode, url, headers={"Authorization": f"Bearer {jeton}"}, **kw
            ) as r:
                return r.status, await r.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return 0, repr(exc)

    async def action(self, nom: str) -> bool:
        """start | stop | restart | restart_force | kill."""
        st, corps = await self._appel("POST", f"actions/{nom}")
        log.info("mTxServ action %s : HTTP %s %s", nom, st, corps[:120])
        return 200 <= st < 300

    async def commande(self, texte: str) -> bool:
        """Une commande dans la console du serveur (HTTP 201 attendu)."""
        st, corps = await self._appel("POST", "command", json={"command": texte})
        log.info("mTxServ command %r : HTTP %s %s", texte, st, corps[:120])
        return 200 <= st < 300

    async def dernier_geste(self) -> tuple[str | None, datetime | None]:
        """Le dernier game_start / game_stop réussi dans l'historique du panneau."""
        st, corps = await self._appel("GET", "histories")
        if st != 200:
            return None, None
        try:
            hist = json.loads(corps)
        except ValueError:
            return None, None
        if not isinstance(hist, list):
            return None, None
        for h in hist:
            action = str(h.get("action", ""))
            if action.startswith("game_") and h.get("state") == "success":
                try:
                    quand = datetime.fromisoformat(h["created_at"])
                except (KeyError, ValueError):
                    quand = None
                return action, quand
        return None, None
