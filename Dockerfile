# Radar notizie — immagine per il NAS (DS218, ARM64).
FROM python:3.12-slim

# tzdata NON e' incluso nelle immagini slim, e qui non e' un dettaglio
# estetico: senza, zoneinfo non trova "Europe/Rome", il programma ripiega su
# UTC e le ore di silenzio si spostano di due ore. Il radar continuerebbe a
# funzionare mandandoti avvisi alle cinque del mattino, e la causa sarebbe
# quasi impossibile da indovinare guardando i log.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Rome \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    CARTELLA_SITO=/sito

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Tutti i moduli, non un elenco. Elencarli a mano significa che il giorno in
# cui se ne aggiunge uno il container va in crash loop all'avvio — e lo
# scopri solo leggendo i log, perche' la build riesce lo stesso.
COPY *.py ./

# Utente non privilegiato: se qualcosa va storto, il danno resta qui dentro.
RUN useradd -u 1000 -m radar && mkdir -p /data /sito && chown -R radar /data /sito
USER radar

# Un radar che smette di raccogliere e' indistinguibile da uno che non ha
# niente da dire. Questo controllo guarda l'ora dell'ultima pagina scritta:
# se supera i venti minuti, qualcosa si e' fermato.
HEALTHCHECK --interval=5m --timeout=20s --start-period=2m \
  CMD python3 -c "import os,sys,time; p='/sito/index.html'; \
sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 1200 else 1)"

CMD ["python3", "radar.py", "--continuo"]
