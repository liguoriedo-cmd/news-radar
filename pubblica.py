#!/usr/bin/env python3
"""
Pubblicazione della pagina su GitHub Pages.

Perche' l'API e non git
-----------------------
Il NAS non ha git, e installarlo su DSM significa pacchetti di terze parti,
chiavi SSH da generare e un pezzo in piu' che si puo' rompere. L'API dei
contenuti di GitHub fa la stessa cosa con una richiesta HTTPS e un token:
niente da installare, niente da configurare, e se fallisce fallisce in modo
visibile invece di lasciare un repository a meta'.

Perche' non si pubblica a ogni giro
-----------------------------------
Ogni pubblicazione e' un commit, e ogni commit conserva una copia della
pagina per sempre. A un giro ogni tre minuti sarebbero 480 commit al giorno,
cioe' 24 MB al giorno di roba che nessuno rileggera' mai. Qui si pubblica
solo quando il CONTENUTO e' cambiato davvero: l'orario di aggiornamento
viene tolto prima del confronto, altrimenti la pagina risulterebbe sempre
diversa e non avremmo risolto niente. Una notte tranquilla produce zero
commit.

Resta un limite, dichiarato: il repository cresce comunque, di qualche
centinaio di MB l'anno. Se un giorno diventa scomodo, si cancella e si
ricrea — non c'e' niente dentro che valga la pena conservare, la pagina si
riscrive da sola al giro successivo.

IL TOKEN
--------
Va creato su GitHub come "fine-grained personal access token", ristretto al
solo repository del radar e con l'unico permesso "Contents: Read and write".
Un token cosi' non puo' toccare nient'altro del tuo account: se finisse in
mano a qualcuno, il danno massimo e' una pagina di notizie modificata.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger("pubblica")

API = "https://api.github.com/repos/{repo}/contents/{percorso}"

# L'orario di aggiornamento cambia a ogni giro: se restasse nel confronto,
# ogni pagina risulterebbe diversa dalla precedente e il freno sui commit
# non servirebbe a niente.
VOLATILE = re.compile(r'<div class="sotto">.*?</div>', re.S)


def _impronta(testo: str) -> str:
    return hashlib.sha1(VOLATILE.sub("", testo).encode()).hexdigest()[:16]


class Pubblicatore:
    def __init__(self, cfg: dict, stato_path: Path) -> None:
        p = (cfg.get("pubblicazione") or {})
        self.attiva = bool(p.get("attiva", False))
        self.repo = str(p.get("repo", ""))          # es. "edoliguo/news-radar"
        self.ramo = str(p.get("ramo", "main"))
        self.file = list(p.get("file", ["index.html"]))
        # Cartella dentro il repository. GitHub Pages sa servire la radice
        # oppure "/docs", e "/docs" e' preferibile: tiene la pagina separata
        # dal codice invece di mescolarli nella stessa cartella.
        self.cartella_remota = str(p.get("cartella_remota", "docs")).strip("/")
        # Ogni quanto ripubblicare comunque, anche se il contenuto non e'
        # cambiato. Senza, in una notte tranquilla la pagina online resterebbe
        # ferma per ore e il segnalatore in cima la dichiarerebbe "offline"
        # pur essendo il radar perfettamente vivo: il battito e' cio' che
        # distingue "non e' successo niente" da "si e' fermato tutto".
        self.battito_minuti = float(p.get("battito_minuti", 30))
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.stato_path = Path(stato_path)
        self.s = requests.Session()
        self.s.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

        # Una pubblicazione accesa ma incompleta e' il caso peggiore: il
        # radar continua a funzionare, la pagina online si congela, e non
        # lo dice nessuno. Qui la si dichiara subito, all'avvio, una volta
        # sola — la configurazione non cambia da sola durante l'esecuzione,
        # quindi ripeterlo a ogni giro sarebbe solo rumore nei log.
        if self.attiva and not self.configurato:
            manca = "GITHUB_TOKEN" if not self.token else "il nome del repository"
            log.warning(
                "pubblicazione ACCESA ma non utilizzabile: manca %s. "
                "La pagina resta scritta sul NAS e non viene aggiornata online. "
                "Se il token e' nel file .env, controlla che docker-compose.yml "
                "lo passi al container: le variabili non elencate li' dentro "
                "non esistono per il programma.", manca)

    @property
    def configurato(self) -> bool:
        return bool(self.attiva and self.repo and self.token)

    # -- memoria di cosa e' gia' online ------------------------------------

    def _impronte(self) -> dict:
        try:
            return json.loads(self.stato_path.read_text()).get("pubblicate", {})
        except (OSError, ValueError):
            return {}

    def _minuti_dall_ultima(self) -> float:
        try:
            q = json.loads(self.stato_path.read_text()).get("ultima_pubblicazione")
            passato = datetime.now(timezone.utc) - datetime.fromisoformat(q)
            return passato.total_seconds() / 60.0
        except (OSError, ValueError, TypeError):
            return 1e9          # mai pubblicato: il battito scatta subito

    def _ricorda(self, nuove: dict) -> None:
        try:
            stato = json.loads(self.stato_path.read_text())
        except (OSError, ValueError):
            stato = {}
        stato["pubblicate"] = {**stato.get("pubblicate", {}), **nuove}
        stato["ultima_pubblicazione"] = datetime.now(timezone.utc).isoformat()
        try:
            tmp = self.stato_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(stato, ensure_ascii=False))
            tmp.replace(self.stato_path)
        except OSError as exc:
            log.warning("stato non salvato: %s", exc)

    # -- una richiesta sola verso GitHub -----------------------------------

    def _sha_remoto(self, percorso: str) -> str | None:
        """Lo sha del file gia' online. GitHub lo pretende per sovrascrivere."""
        try:
            r = self.s.get(API.format(repo=self.repo, percorso=percorso),
                           params={"ref": self.ramo},
                           headers={"Authorization": f"Bearer {self.token}"},
                           timeout=25)
        except requests.RequestException as exc:
            log.warning("GitHub non raggiungibile: %s", exc)
            return None
        if r.status_code == 404:
            return None                      # prima pubblicazione: va bene
        if r.status_code >= 400:
            log.warning("GitHub ha risposto %s: %s", r.status_code, r.text[:180])
            return None
        return (r.json() or {}).get("sha")

    def _carica(self, sorgente: Path, percorso: str, messaggio: str) -> bool:
        corpo = {
            "message": messaggio,
            "content": base64.b64encode(sorgente.read_bytes()).decode(),
            "branch": self.ramo,
        }
        sha = self._sha_remoto(percorso)
        if sha:
            corpo["sha"] = sha
        try:
            r = self.s.put(API.format(repo=self.repo, percorso=percorso),
                           json=corpo,
                           headers={"Authorization": f"Bearer {self.token}"},
                           timeout=30)
        except requests.RequestException as exc:
            log.warning("pubblicazione fallita: %s", exc)
            return False
        if r.status_code >= 400:
            log.warning("GitHub ha rifiutato %s (%s): %s",
                        percorso, r.status_code, r.text[:180])
            return False
        return True

    # -- il punto d'ingresso -------------------------------------------------

    def pubblica(self, cartella: Path) -> dict:
        """Manda su GitHub i file cambiati. Non solleva mai eccezioni.

        Una pubblicazione fallita non deve fermare il radar: la pagina resta
        scritta sul NAS e il giro dopo ci riprova. Perdere il dashboard
        remoto e' un fastidio, perdere gli avvisi sarebbe un guasto.
        """
        if not self.configurato:
            return {"pubblicati": 0, "motivo": "non configurata"}

        note = self._impronte()
        battito = self._minuti_dall_ultima() >= self.battito_minuti
        nuove, fatti, errori = {}, 0, []
        for nome in self.file:
            f = Path(cartella) / nome
            if not f.exists():
                continue
            try:
                imp = _impronta(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if note.get(nome) == imp and not battito:
                continue                     # identico a quello gia' online
            remoto = f"{self.cartella_remota}/{nome}" if self.cartella_remota else nome
            motivo = "battito" if (note.get(nome) == imp) else "aggiornamento"
            if self._carica(f, remoto, f"radar: {motivo} {nome}"):
                nuove[nome] = imp
                fatti += 1
            else:
                errori.append(nome)

        if nuove:
            self._ricorda(nuove)
            log.info("pubblicati su GitHub: %s%s", ", ".join(nuove),
                     " (battito)" if battito else "")
        return {"pubblicati": fatti, "invariati": len(self.file) - fatti - len(errori),
                "errore": f"caricamento fallito: {', '.join(errori)}" if errori else None}


if __name__ == "__main__":
    # L'impronta deve ignorare l'orario e accorgersi di tutto il resto.
    a = ('<h1>x</h1><div class="sotto">aggiornato 13/08/2026 alle 10:00 · '
         'ultime 24 ore</div><p>stessa notizia</p>')
    b = a.replace("10:00", "10:03")
    c = a.replace("stessa notizia", "notizia diversa")
    prove = [
        ("solo l'orario cambiato: non si ripubblica", _impronta(a) == _impronta(b)),
        ("contenuto cambiato: si ripubblica", _impronta(a) != _impronta(c)),
    ]
    for nome, ok in prove:
        print(f"  {'ok ' if ok else 'NO '} {nome}")
    print(f"\n{sum(1 for _, o in prove if o)}/{len(prove)} controlli superati")
    raise SystemExit(0 if all(o for _, o in prove) else 1)
