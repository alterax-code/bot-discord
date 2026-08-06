# Bot de tickets — Grand Line RP

Bot Discord de gestion des signalements de bugs et des demandes d'ajout pour le
serveur Garry's Mod **Grand Line RP**.

## Le principe

```
[fix-bug]   message permanent à 2 boutons  →  formulaire  →  ticket publié + ❌ posée
                 ↓  le staff réagit (❌ ⚠️ ✅) — tous les participants sont enregistrés
                 ↓  ✅ posée par un rôle autorisé
     capture → archive du jour (paginée) → annonce publique → suppression du ticket
                      ↑ si une étape échoue, le ticket n'est PAS supprimé
```

La base SQLite est la **source de vérité**, pas Discord. Le message d'origine
n'est supprimé qu'une fois son contenu écrit en base, et un redémarrage au
mauvais moment reprend exactement où il en était.

## Les trois salons

| Salon | Public | Contenu |
|---|---|---|
| `fix-bug / add-feature` | Staff | Les tickets en cours de traitement |
| `archives` | Staff | La traçabilité complète, un message par jour |
| `ajout / correctif` | Joueurs | Uniquement ce qui a été corrigé ou ajouté |

## Configuration

Rien de spécifique au serveur n'est écrit en dur dans le code.

- **`config.toml`** — identifiants des salons, des rôles, préfixes publics,
  fuseau horaire. Publics par nature, donc versionnés.
- **`.env`** — le jeton Discord. **Jamais versionné**, exclu par `.gitignore`.

```bash
cp .env.example .env      # puis coller le vrai jeton dedans
```

## Lancer en local

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt
python -m bot
```

Au démarrage, le bot effectue un **autodiagnostic** : il vérifie que les trois
salons existent, qu'il y détient les permissions nécessaires, et que les rôles
validateurs sont reconnus. Un problème de configuration apparaît immédiatement
dans les journaux au lieu de se révéler le jour d'une validation.

## Structure

```
bot/
├── config.py          chargement et validation de la configuration
├── database.py        schéma SQLite et persistance
├── logging_setup.py   journaux horodatés à l'heure de Paris
├── client.py          client Discord et autodiagnostic
└── __main__.py        point d'entrée
```

## Intents et permissions

Le bot demande le strict nécessaire :

- Intents : `guilds`, `guild_messages`, `guild_reactions`, `members`.
  **Pas** `message_content` — un bot reçoit toujours le contenu de ses propres
  messages, et c'est tout ce que nous relisons.
- Permissions : voir les salons, envoyer des messages, intégrer des liens,
  ajouter des réactions, lire l'historique, gérer les messages. **Pas**
  d'Administrateur.
