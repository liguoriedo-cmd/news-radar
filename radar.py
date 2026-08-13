#!/usr/bin/env python3
"""
Il radar: mette insieme raccolta, filtro, archivio e consegna.

Si usa in due modi, e sono lo stesso codice:

    python3 radar.py --ciclo         una passata sola, poi esce
    python3 radar.py --panoramica    manda la rassegna del mattino, poi esce
    python3 radar.py --continuo      resta acceso e ripete da solo

La distinzione non e' un vezzo: decide dove il radar puo' vivere. Con
`--ciclo` funziona come lavoro programmato — GitHub Actions, il pianificatore
di Synology, un cron qualunque — e non serve nessun processo sempre acceso.
Con `--continuo` funziona come servizio dentro un container. Scrivere il
motore in modo che non sappia dove gira significa poter cambiare idea
sull'hosting senza riscrivere niente.

L'archivio esiste per una ragione precisa: di notte gli avvisi non vengono
mandati, ma non devono andare persi. Restano qui e la mattina escono per
primi, prima della panoramica.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import avvisi as A
import digest
import filtro as F
import pagina
import pubblica
from sources import Nasdaq, Raccolta

log = logging.getLogger("radar")

RADICE = Path(__file__).resolve().parent
DATI = Path(os.environ.get("DATA_DIR", RADICE / "data"))

# Le voci restano in archivio due giorni: la panoramica ne guarda 24 ore, il
# margine serve perche' un fuso orario o un ritardo non facciano sparire
# qualcosa proprio la mattina in cui serve.
ORE_ARCHIVIO = 48


def carica_env(path: Path) -> None:
    """Legge un file .env senza dipendenze aggiuntive.

    Le variabili gia' presenti nell'ambiente vincono sempre: in produzione le
    chiavi arrivano dai segreti della piattaforma, e un .env dimenticato nel
    pacchetto non deve poterli sovrascrivere.
    """
    try:
        righe = path.read_text().splitlines()
    except OSError:
        return
    for riga in righe:
        riga = riga.strip()
        if not riga or riga.startswith("#") or "=" not in riga:
            continue
        nome, _, valore = riga.partition("=")
        nome, valore = nome.strip(), valore.strip().strip('"').strip("'")
        if nome and nome not in os.environ:
            os.environ[nome] = valore


def carica_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


class Radar:
    def __init__(self, cfg: dict, dati: Path) -> None:
        self.cfg = cfg
        self.dati = Path(dati)
        self.dati.mkdir(parents=True, exist_ok=True)
        self.raccolta = Raccolta(cfg)
        self.filtro = F.Filtro(cfg, self.dati / "memoria.json")
        self.consegna = A.Consegna(cfg)
        self.archivio_path = self.dati / "archivio.json"
        self.stato_path = self.dati / "stato.json"
        # `docs/` e' la cartella che GitHub Pages sa servire senza
        # configurazione. Sul NAS e' una cartella come un'altra.
        self.sito = Path(os.environ.get("CARTELLA_SITO", RADICE / "docs"))
        self.pubblicatore = pubblica.Pubblicatore(cfg, self.stato_path)

    # -- archivio -----------------------------------------------------------

    def _leggi(self, path: Path, vuoto):
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return vuoto
        except (ValueError, OSError) as exc:
            log.warning("%s illeggibile (%s): riparto da vuoto", path.name, exc)
            return vuoto

    def _scrivi(self, path: Path, contenuto) -> None:
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(contenuto, ensure_ascii=False))
            tmp.replace(path)      # atomica: mai un file mezzo scritto
        except OSError as exc:
            log.warning("%s non salvato: %s", path.name, exc)

    def archivio(self, ore: int = ORE_ARCHIVIO) -> list[dict]:
        taglio = (datetime.now(timezone.utc) - timedelta(hours=ore)).isoformat()
        return [v for v in self._leggi(self.archivio_path, [])
                if str(v.get("archiviata", "")) >= taglio]

    def _archivia(self, voci: list[dict]) -> None:
        vecchie = self.archivio()
        note = {v.get("impronta") for v in vecchie}
        adesso = datetime.now(timezone.utc).isoformat()
        for v in voci:
            if v.get("impronta") in note:
                continue
            vecchie.append(dict(v, archiviata=adesso))
        self._scrivi(self.archivio_path, vecchie)

    # -- il ciclo -----------------------------------------------------------

    def ciclo(self, completa: bool = True) -> dict:
        voci = F.prepara(self.raccolta.tutto(completa=completa))
        alert, diario, stat = self.filtro.passa(voci)

        esito = self.consegna.avvisi(alert)
        if esito["silenzio"]:
            # Non spariscono: vengono marcati e la mattina escono per primi.
            alert = [dict(v, trattenuto=True) for v in alert]

        self._archivia(alert + diario)
        self._riepilogo_iniziale()

        # Il dashboard si riscrive ogni ciclo con le ultime 24 ore, anche
        # quando non e' successo niente: una pagina ferma a ieri sembra
        # rotta, e non si distingue da un radar che si e' davvero fermato.
        pagina.scrivi(self.archivio(ore=24), self.cfg, self.sito,
                      riepilogo=self._leggi(self.stato_path, {}).get("riepilogo"))

        # La pubblicazione e' l'ultimo passo e il meno importante: se GitHub
        # non risponde, la pagina resta scritta sul NAS e il giro dopo ci
        # riprova. Perdere il dashboard remoto e' un fastidio, perdere gli
        # avvisi sarebbe un guasto — per questo sta qui in fondo.
        try:
            stat.update(pubblicati=self.pubblicatore.pubblica(self.sito)["pubblicati"])
        except Exception:
            log.exception("pubblicazione fallita, il radar continua")

        stat.update(esito, completa=completa)
        log.info("ciclo concluso: %s", stat)
        return stat

    # -- il riepilogo in cima al dashboard -----------------------------------

    def _scrivi_riepilogo(self, stato: dict, testo: str, resoconto: dict) -> None:
        """Registra il riepilogo appena scritto, con la spesa effettiva."""
        stato["riepilogo"] = {
            "testo": testo,
            "quando": datetime.now(timezone.utc).isoformat(),
            "costo": round(float(resoconto.get("costo") or 0.0), 6),
            "dal_modello": bool(resoconto.get("modello")),
        }
        self._scrivi(self.stato_path, stato)

    def _riepilogo_iniziale(self) -> None:
        """Scrive il riepilogo la prima volta in assoluto, e mai piu'.

        Il riepilogo del dashboard e' la stessa panoramica che arriva su
        Telegram alle 07:30: una scrittura al giorno, punto. Questa funzione
        esiste solo perche' il giorno dell'installazione, fino alla prima
        mattina utile, la sezione sarebbe vuota — e un riquadro vuoto su una
        pagina nuova non si distingue da un guasto.

        Una chiamata sola in tutta la vita del radar, non una al giorno.
        """
        stato = self._leggi(self.stato_path, {})
        if stato.get("riepilogo"):
            return
        voci = self.archivio(ore=24)
        if not voci:
            return
        resoconto: dict = {}
        testo = digest.costruisci(voci, self.cfg.get("panoramica", {}) or {},
                                  resoconto)
        log.info("primo riepilogo scritto: da qui in avanti solo quello "
                 "delle %s", (self.cfg.get("panoramica", {}) or {}).get("ora", "07:30"))
        self._scrivi_riepilogo(stato, testo, resoconto)

    # -- la panoramica del mattino -------------------------------------------

    def _eventi_di_oggi(self) -> list[dict]:
        """Il calendario di oggi, ripreso fresco e ristretto ai paesi seguiti.

        Non si prende dall'archivio perche' l'archivio contiene solo cio' che
        ha superato il filtro, e la sezione "cosa esce oggi" deve essere
        completa: un dato che stamattina non era ancora rilevante lo diventa
        alle 14:30.
        """
        paesi = [p.lower() for p in (self.cfg.get("avvisi", {}) or {}).get("paesi", [])]
        fuori = []
        for v in F.prepara(Nasdaq().eventi()):
            paese = str((v.get("dati") or {}).get("paese", "")).lower()
            if not paesi or any(p in paese for p in paesi):
                fuori.append(v)
        return fuori

    def panoramica(self) -> bool:
        recenti = self.archivio(ore=24)

        # 1) Prima gli avvisi trattenuti nella notte, grezzi come sarebbero
        #    arrivati sul momento. Il riassunto viene dopo: su un dato che
        #    muove i prezzi la parafrasi non e' un miglioramento.
        notturni = [v for v in recenti if v.get("trattenuto")]
        if notturni:
            notturni.sort(key=lambda v: -int(v.get("punteggio", 0)))
            testa = f"<b>ARRIVATI NELLA NOTTE ({len(notturni)})</b>"
            corpo = "\n\n".join(A.componi(v, self.consegna.silenzio.fuso)
                                for v in notturni[:8])
            self.consegna.panoramica(f"{testa}\n\n{corpo}")

        # 2) Poi la panoramica vera e propria. E' l'UNICA chiamata al modello
        #    della giornata: lo stesso testo va su Telegram e in cima al
        #    dashboard, perche' e' la stessa cosa e pagarla due volte sarebbe
        #    solo un modo elegante di sprecare.
        materiale = recenti + self._eventi_di_oggi()
        resoconto: dict = {}
        testo = digest.costruisci(materiale, self.cfg.get("panoramica", {}) or {},
                                  resoconto)
        ok = self.consegna.panoramica(testo)

        # Gli avvisi notturni sono stati consegnati: si toglie il segno, cosi'
        # domani mattina non ricompaiono.
        tutte = self._leggi(self.archivio_path, [])
        for v in tutte:
            v.pop("trattenuto", None)
        self._scrivi(self.archivio_path, tutte)

        stato = self._leggi(self.stato_path, {})
        stato["ultima_panoramica"] = datetime.now(timezone.utc).isoformat()
        self._scrivi_riepilogo(stato, testo, resoconto)
        pagina.scrivi(self.archivio(ore=24), self.cfg, self.sito,
                      riepilogo=stato["riepilogo"])
        return ok

    def panoramica_dovuta(self) -> bool:
        """Vero se oggi la panoramica non e' ancora uscita ed e' passata l'ora.

        Serve solo in modalita' `--continuo`. Come lavoro programmato l'orario
        lo decide chi lo pianifica, e questa funzione non viene usata.
        """
        p = self.cfg.get("panoramica", {}) or {}
        fuso = self.consegna.silenzio.fuso
        adesso = datetime.now(fuso)
        voluta = A.Silenzio._ora(p.get("ora", "07:30"), 7)
        if adesso.time() < voluta:
            return False
        ultima = self._leggi(self.stato_path, {}).get("ultima_panoramica")
        if not ultima:
            return True
        try:
            quando = datetime.fromisoformat(ultima).astimezone(fuso)
        except ValueError:
            return True
        return quando.date() < adesso.date()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Radar notizie")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ciclo", action="store_true",
                   help="una passata completa su tutte le fonti, poi esce")
    g.add_argument("--veloce", action="store_true",
                   help="solo le fonti che cambiano di minuto in minuto")
    g.add_argument("--panoramica", action="store_true", help="rassegna del mattino")
    g.add_argument("--continuo", action="store_true",
                   help="resta acceso e alterna i due giri da solo")
    p.add_argument("--config", default=str(DATI / "config.yaml"))
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    carica_env(RADICE / ".env")
    cfg = carica_config(Path(args.config))
    radar = Radar(cfg, DATI)

    if args.ciclo or args.veloce:
        radar.ciclo(completa=args.ciclo)
        return 0

    if args.panoramica:
        return 0 if radar.panoramica() else 1

    ritmo = cfg.get("ritmo", {}) or {}
    veloce = max(1, int(ritmo.get("veloce_minuti", 3))) * 60
    completo = max(veloce, int(ritmo.get("completo_minuti", 20)) * 60)
    log.info("radar acceso: giro veloce ogni %d min, completo ogni %d min",
             veloce // 60, completo // 60)

    ultimo_completo = 0.0
    while True:
        try:
            adesso = time.monotonic()
            tocca_completo = (adesso - ultimo_completo) >= completo
            radar.ciclo(completa=tocca_completo)
            if tocca_completo:
                ultimo_completo = adesso
            if radar.panoramica_dovuta():
                radar.panoramica()
        except KeyboardInterrupt:
            log.info("interrotto")
            return 0
        except Exception:
            # Un ciclo che fallisce non deve spegnere il radar: la causa piu'
            # probabile e' una fonte che ha cambiato formato, e le altre nove
            # continuano a funzionare.
            log.exception("ciclo fallito, riprovo al prossimo giro")
        time.sleep(veloce)


if __name__ == "__main__":
    sys.exit(main())
