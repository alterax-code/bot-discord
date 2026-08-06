#!/bin/bash
# Sauvegarde de la base du bot Grand Line RP.
#
# Utilise « sqlite3 .backup » et NON une simple copie de fichier : la base
# tourne en mode WAL pendant que le bot écrit dedans. Un « cp » attraperait un
# instantané incohérent, à moitié écrit — une sauvegarde qui ne se restaure pas
# est pire que pas de sauvegarde, parce qu'on croit en avoir une.
set -euo pipefail

SRC=/srv/grandline/bot/data/grandline.db
DEST=/srv/grandline/backups
RETENTION_JOURS=14

if [ ! -f "$SRC" ]; then
  echo "Base introuvable : $SRC" >&2
  exit 1
fi

mkdir -p "$DEST"
STAMP=$(date +%Y%m%d-%H%M%S)
CIBLE="$DEST/grandline-$STAMP.db"

sqlite3 "$SRC" ".backup '$CIBLE'"

# Vérifier la sauvegarde AVANT de la garder : une archive corrompue passerait
# inaperçue jusqu'au jour où on en aurait besoin.
if ! sqlite3 "$CIBLE" "PRAGMA integrity_check;" | grep -q '^ok$'; then
  echo "Sauvegarde corrompue, supprimée : $CIBLE" >&2
  rm -f "$CIBLE"
  exit 1
fi

gzip -f "$CIBLE"
echo "Sauvegarde : $CIBLE.gz ($(du -h "$CIBLE.gz" | cut -f1))"

# Purge des anciennes.
find "$DEST" -name 'grandline-*.db.gz' -mtime +"$RETENTION_JOURS" -print -delete
