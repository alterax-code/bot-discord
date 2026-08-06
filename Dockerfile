# Python 3.12 : la MÊME version que le poste de développement. La parité
# exacte élimine la classe de bugs « ça marchait chez moi ».
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Paris \
    DATA_DIR=/data

WORKDIR /app

# Les dépendances d'abord, dans leur propre couche : tant que
# requirements.txt ne bouge pas, une modification du code ne les réinstalle
# pas. Un déploiement passe ainsi de plusieurs minutes à quelques secondes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
COPY config.toml ./

# Le bot tourne sous un compte sans privilège. Si un jour une faille lui
# permettait d'exécuter du code, ce code n'aurait pas les droits root dans
# le conteneur.
RUN useradd --system --uid 10001 --shell /usr/sbin/nologin grandline \
    && mkdir -p /data \
    && chown -R grandline:grandline /data /app
USER grandline

# Aucun port exposé : le bot n'accepte aucune connexion entrante, il se
# connecte à Discord et c'est tout.
CMD ["python", "-m", "bot"]
