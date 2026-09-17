"""Audit LECTURE SEULE du serveur Discord Grand Line RP, avec le bot.

    .venv/Scripts/python.exe tools/audit_discord.py        (depuis la racine du dépôt)

Sort deux fichiers HORS dépôt, dans ../audit/ : audit_discord.json (brut) et
audit_discord.md (lisible) — rôles et permissions, membres et leurs rôles,
salons et qui y voit quoi. Le jeton (DISCORD_TOKEN du .env) n'est jamais
affiché. Le bot n'écrit rien sur Discord. Premier audit : 17 septembre 2026.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

BOT_DIR = Path(__file__).resolve().parent.parent
OUT = BOT_DIR.parent / "audit"
OUT.mkdir(exist_ok=True)
load_dotenv(BOT_DIR / ".env", override=False)
TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
if not TOKEN:
    sys.exit("DISCORD_TOKEN absent")

import tomllib
with open(BOT_DIR / "config.toml", "rb") as f:
    CFG = tomllib.load(f)
GUILD_ID = int(CFG["discord"]["guild_id"])
STAFF_ROLES = set(CFG.get("whitelist", {}).get("staff_roles", []))
VALIDATORS = set(CFG.get("roles", {}).get("validators", []))

intents = discord.Intents.none()
intents.guilds = True
intents.members = True

DANGEREUSES = {
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "manage_webhooks", "kick_members", "ban_members", "moderate_members",
    "manage_messages", "mention_everyone", "manage_nicknames", "view_audit_log",
    "manage_events", "manage_threads", "move_members", "mute_members", "deafen_members",
}


def perms_list(p: discord.Permissions) -> list[str]:
    return sorted(name for name, val in p if val)


class Audit(discord.Client):
    async def on_ready(self):
        try:
            await self.run_audit()
        finally:
            await self.close()

    async def run_audit(self):
        g = self.get_guild(GUILD_ID)
        if g is None:
            print("Serveur introuvable pour ce bot. Serveurs visibles :", [(x.name, x.id) for x in self.guilds])
            return
        members = [m async for m in g.fetch_members(limit=None)]
        by_role: dict[int, list[discord.Member]] = {}
        for m in members:
            for r in m.roles:
                by_role.setdefault(r.id, []).append(m)

        roles = []
        for r in sorted(g.roles, key=lambda r: r.position, reverse=True):
            roles.append({
                "id": r.id, "nom": r.name, "position": r.position, "couleur": str(r.color),
                "hoist": r.hoist, "mentionable": r.mentionable, "managed": r.managed,
                "bot_role": r.is_bot_managed(), "integration": r.is_integration(),
                "membres": sorted(m.display_name for m in by_role.get(r.id, []) if not m.bot),
                "bots": sorted(m.display_name for m in by_role.get(r.id, []) if m.bot),
                "perms": perms_list(r.permissions),
                "perms_dangereuses": sorted(p for p in perms_list(r.permissions) if p in DANGEREUSES),
                "dans_whitelist_bot": r.id in STAFF_ROLES,
                "validateur_bot": r.id in VALIDATORS,
            })

        def ow(ch):
            out = []
            for target, po in ch.overwrites.items():
                allow, deny = po.pair()
                out.append({
                    "cible": target.name, "type": "role" if isinstance(target, discord.Role) else "membre",
                    "allow": perms_list(allow), "deny": perms_list(deny),
                })
            return out

        channels = []
        for cat, chans in g.by_category():
            channels.append({
                "categorie": cat.name if cat else "(sans catégorie)",
                "categorie_overwrites": ow(cat) if cat else [],
                "salons": [{
                    "nom": c.name, "id": c.id, "type": str(c.type),
                    "overwrites": ow(c),
                    "visible_everyone": c.permissions_for(g.default_role).view_channel,
                } for c in chans],
            })

        humains = [m for m in members if not m.bot]
        data = {
            "guild": {"nom": g.name, "id": g.id, "proprietaire": str(g.owner), "proprietaire_id": g.owner_id,
                       "membres": len(humains), "bots": len(members) - len(humains),
                       "roles": len(g.roles), "salons": len(g.channels)},
            "roles": roles,
            "salons": channels,
            "membres_avec_roles": sorted(
                [{"nom": m.display_name, "user": str(m), "id": m.id,
                  "roles": [r.name for r in sorted(m.roles, key=lambda r: r.position, reverse=True) if r != g.default_role],
                  "perms_globales_dangereuses": sorted(p for p in perms_list(m.guild_permissions) if p in DANGEREUSES)}
                 for m in humains if len(m.roles) > 1],
                key=lambda x: x["nom"].lower()),
            "bots": [{"nom": m.display_name, "id": m.id, "roles": [r.name for r in m.roles if r != g.default_role]} for m in members if m.bot],
        }
        (OUT / "audit_discord.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

        L = []
        L.append(f"# Audit Discord « {g.name} » ({g.id}) — propriétaire : {g.owner}")
        L.append(f"{len(humains)} humains, {len(members)-len(humains)} bots, {len(g.roles)} rôles, {len(g.channels)} salons\n")
        L.append("## Rôles (du plus haut au plus bas)\n")
        L.append("| pos | rôle | membres | WL bot | valid. bot | perms dangereuses |")
        L.append("|---|---|---|---|---|---|")
        for r in roles:
            L.append(f"| {r['position']} | {r['nom']} | {len(r['membres'])}{' +'+str(len(r['bots']))+' bot' if r['bots'] else ''} | {'oui' if r['dans_whitelist_bot'] else ''} | {'oui' if r['validateur_bot'] else ''} | {', '.join(r['perms_dangereuses'])} |")
        L.append("\n## Qui porte quoi\n")
        for r in roles:
            if r["membres"]:
                L.append(f"- **{r['nom']}** : {', '.join(r['membres'])}")
        L.append("\n## Membres et leurs rôles\n")
        for m in data["membres_avec_roles"]:
            L.append(f"- {m['nom']} ({m['user']}) : {', '.join(m['roles'])}" + (f" — perms globales : {', '.join(m['perms_globales_dangereuses'])}" if m['perms_globales_dangereuses'] else ""))
        L.append("\n## Salons par catégorie\n")
        for cat in channels:
            L.append(f"### {cat['categorie']}")
            if cat["categorie_overwrites"]:
                for o in cat["categorie_overwrites"]:
                    L.append(f"  - (catégorie) {o['cible']} : allow {', '.join(o['allow']) or '-'} / deny {', '.join(o['deny']) or '-'}")
            for c in cat["salons"]:
                L.append(f"- #{c['nom']} [{c['type']}] {'public' if c['visible_everyone'] else 'restreint'}")
                for o in c["overwrites"]:
                    L.append(f"    - {o['cible']} ({o['type']}) : allow {', '.join(o['allow']) or '-'} / deny {', '.join(o['deny']) or '-'}")
        (OUT / "audit_discord.md").write_text("\n".join(L), encoding="utf-8")
        print("OK :", len(roles), "rôles,", len(humains), "humains,", sum(len(c['salons']) for c in channels), "salons")


Audit(intents=intents).run(TOKEN, log_handler=None)
