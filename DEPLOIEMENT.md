# Exploitation du bot sur le VPS

Le bot tourne dans un conteneur Docker sur `grandline-01` (`167.233.221.51`),
dans `/srv/grandline/bot`.

Se connecter :

```bash
ssh alterax@167.233.221.51
```

---

## Lire les journaux

```bash
cd /srv/grandline/bot && docker compose logs -f
```

`-f` suit en direct ; `Ctrl+C` pour sortir sans rien arrêter.

Les 200 dernières lignes seulement :

```bash
cd /srv/grandline/bot && docker compose logs --tail 200
```

Uniquement les problèmes :

```bash
cd /srv/grandline/bot && docker compose logs --tail 500 | grep -E 'WARNING|ERROR'
```

Ce qu'on doit voir au démarrage d'un bot en bonne santé :

```
Autodiagnostic : tout est en ordre.
Message permanent retrouvé (id …).
Bot opérationnel : les joueurs peuvent ouvrir des tickets.
```

---

## Déployer une mise à jour

```bash
cd /srv/grandline/bot
git pull
docker compose up -d --build
```

`--build` reconstruit l'image ; `-d` la relance en arrière-plan. Le bot
redémarre en quelques secondes. **La base n'est jamais touchée** : elle vit
dans `data/`, hors de l'image.

Vérifier avant de reconstruire, sans rien casser :

```bash
docker compose run --rm bot python -m bot --check
```

Cette commande se connecte à Discord, contrôle salons, permissions et rôles,
puis s'arrête. Son code de sortie dit ce qui ne va pas :

| Code | Signification |
|---|---|
| 0 | Tout est en ordre |
| 2 | `config.toml` invalide |
| 3 | Jeton refusé par Discord |
| 4 | Intent privilégié manquant |
| 5 | Salons ou permissions incorrects |

---

## Arrêter, relancer

```bash
cd /srv/grandline/bot
docker compose stop      # arrêt volontaire — ne repart PAS au reboot
docker compose start     # relance
docker compose restart   # redémarre sans reconstruire
```

Après un `stop`, le bot reste éteint même si le serveur redémarre. C'est
voulu : `restart: unless-stopped` distingue un arrêt délibéré d'une panne.

---

## Sauvegardes

Une sauvegarde par jour à 3 h du matin, conservée 14 jours, dans
`/srv/grandline/backups/`.

```bash
ls -lh /srv/grandline/backups/           # les sauvegardes existantes
systemctl status grandline-backup.timer  # la minuterie
systemctl list-timers grandline-backup*  # la prochaine exécution
sudo systemctl start grandline-backup    # en déclencher une tout de suite
```

### Restaurer

```bash
cd /srv/grandline/bot
docker compose stop                                   # 1. arrêter le bot
cp data/grandline.db data/grandline.db.avant-restau   # 2. garder l'état actuel
gunzip -c /srv/grandline/backups/grandline-AAAAMMJJ-HHMMSS.db.gz > data/grandline.db
docker compose start                                  # 3. relancer
```

L'étape 2 n'est pas décorative : si la sauvegarde restaurée n'est pas la
bonne, elle est le seul moyen de revenir en arrière.

---

## Le jeton

Il vit dans `/srv/grandline/bot/.env`, **hors du dépôt Git**. Il n'est ni
versionné, ni présent dans l'image Docker.

Le changer :

```bash
cd /srv/grandline/bot
nano .env                  # remplacer la valeur de DISCORD_TOKEN
docker compose up -d       # recharger
```

En cas de fuite : régénérer le jeton sur
<https://discord.com/developers/applications> → Bot → *Reset Token*.
L'ancien meurt immédiatement.

---

## Diagnostic rapide

| Symptôme | À faire |
|---|---|
| Le bot est hors ligne sur Discord | `docker compose ps` puis `docker compose logs --tail 100` |
| Les boutons ne répondent plus | Vérifier que le conteneur tourne ; les vues persistantes se rechargent au démarrage |
| Un ticket n'a pas été publié | Chercher `orphelin` dans les journaux : son contenu est en base |
| Une validation semble bloquée | Redémarrer : la reprise termine les validations interrompues |
| Plus de place sur le disque | `docker system prune -a` puis vérifier `/srv/grandline/backups/` |
