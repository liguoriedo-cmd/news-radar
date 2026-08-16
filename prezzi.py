#!/usr/bin/env python3
"""
Monitor dei prezzi: l'unico pezzo che guarda i mercati e non i giornali.

Fino alla V1.6 il radar sapeva soltanto cosa SCRIVEVANO le testate. Non
poteva dirti che l'oro si era mosso del 2% o che Nvidia stava crollando —
e per chi fa trading il prezzo e' il segnale primario, mentre la notizia
spesso arriva dopo, a spiegare un movimento gia' avvenuto.

LA FONTE
--------
`api.nasdaq.com/api/quote`, cioe' lo stesso host che gia' interroghiamo per
il calendario economico. Nessuna chiave, nessuna registrazione, nessun
credito da consumare. Provata contro Twelve Data il 16/08/2026: i prezzi
coincidono al centesimo (SPY 776,34 su entrambe).

Non usa alcun modello linguistico: qui si confrontano numeri, e Claude
resta dov'era — una sola chiamata al giorno, per la panoramica.

IL LIMITE, DICHIARATO
---------------------
La risposta contiene `isRealTime: false`: le quotazioni sono DIFFERITE, in
genere di un quarto d'ora. Vuol dire che questo non e' uno strumento per
entrare su un movimento mentre accade — per quello serve un terminale vero,
a pagamento. Serve a sapere che qualcosa si e' mosso e a metterlo accanto
alle notizie di quella mezz'ora, che e' una domanda diversa e altrettanto
utile: "perche' si e' mosso?".

QUANDO AVVISA
-------------
Solo a mercato aperto — lo dice la fonte stessa col campo `marketStatus`,
e fidarsi di quello e' piu' solido che dedurlo da un orologio e da un
calendario di festivita' che nessuno aggiorna.

E avvisa a fasce: superato l'1,5% parte un avviso, poi si tace fino al 3%,
poi fino al 4,5%. Senza le fasce, un titolo fermo a -1,6% per tre ore
manderebbe un avviso ogni cinque minuti.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger("prezzi")

URL = "https://api.nasdaq.com/api/quote/{simbolo}/info"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _numero(testo) -> float | None:
    """Da '$776.34' o '-0.20%' al numero. None se non e' un numero."""
    if testo is None:
        return None
    s = str(testo).replace("$", "").replace("%", "").replace(",", "").strip()
    s = s.replace("−", "-").strip("()")
    try:
        return float(s)
    except ValueError:
        return None


class Prezzi:
    """Legge le quotazioni della watchlist e segnala i movimenti forti."""

    def __init__(self, cfg: dict, stato_path: Path) -> None:
        p = cfg.get("prezzi") or {}
        self.attivo = bool(p.get("attivo", False))
        self.simboli = list(p.get("simboli", []))
        self.soglia = float(p.get("soglia_pct", 1.5))
        self.ogni_minuti = float(p.get("ogni_minuti", 5))
        self.anche_a_mercato_chiuso = bool(p.get("anche_a_mercato_chiuso", False))
        self.stato_path = Path(stato_path)
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json"})

    # -- lettura -----------------------------------------------------------

    def _uno(self, voce: dict) -> dict | None:
        simbolo = str(voce.get("simbolo", "")).strip()
        if not simbolo:
            return None
        try:
            r = self.s.get(URL.format(simbolo=simbolo),
                           params={"assetclass": voce.get("classe", "stocks")},
                           timeout=15)
        except requests.RequestException as exc:
            log.warning("quotazione %s non raggiungibile: %s", simbolo, exc)
            return None
        if r.status_code >= 400:
            log.warning("quotazione %s: HTTP %s", simbolo, r.status_code)
            return None
        try:
            d = (r.json() or {}).get("data") or {}
        except ValueError:
            return None
        p = d.get("primaryData") or {}
        prezzo = _numero(p.get("lastSalePrice"))
        variazione = _numero(p.get("percentageChange"))
        if prezzo is None or variazione is None:
            # Succede sui future e sui simboli sbagliati: la risposta arriva
            # con HTTP 200 e i campi vuoti. Meglio saltare che inventare.
            log.warning("quotazione %s senza dati utilizzabili", simbolo)
            return None
        return {
            "simbolo": simbolo,
            "nome": voce.get("nome") or d.get("companyName") or simbolo,
            "prezzo": prezzo,
            "variazione": variazione,
            "aperto": str(d.get("marketStatus", "")).lower() != "closed",
            "differito": not bool(p.get("isRealTime")),
            "quando": str(p.get("lastTradeTimestamp") or ""),
        }

    def leggi(self) -> list[dict]:
        if not self.attivo:
            return []
        fuori = []
        for voce in self.simboli:
            q = self._uno(voce)
            if q:
                fuori.append(q)
        return fuori

    # -- memoria delle fasce gia' segnalate --------------------------------

    def _fasce(self) -> dict:
        # La data va messa SEMPRE, anche quando il file non esiste ancora.
        # Uscendo prima con un dizionario vuoto, la lettura successiva non
        # trovava la data, concludeva che le fasce erano di ieri e le
        # buttava tutte: il risultato era un avviso a ogni giro su un titolo
        # rimasto fermo. Un difetto di stato, invisibile finche' non lo si
        # prova due volte di seguito.
        oggi = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            s = json.loads(self.stato_path.read_text()).get("fasce_prezzi") or {}
        except (OSError, ValueError):
            s = {}
        # Le fasce valgono per la giornata di borsa: il giorno dopo si
        # riparte da zero, altrimenti un titolo sceso ieri del 3% non
        # segnalerebbe mai piu' niente.
        return s if s.get("giorno") == oggi else {"giorno": oggi}

    def _ricorda(self, fasce: dict) -> None:
        try:
            stato = json.loads(self.stato_path.read_text())
        except (OSError, ValueError):
            stato = {}
        stato["fasce_prezzi"] = fasce
        try:
            tmp = self.stato_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(stato, ensure_ascii=False))
            tmp.replace(self.stato_path)
        except OSError as exc:
            log.warning("fasce non salvate: %s", exc)

    # -- il giudizio -------------------------------------------------------

    def movimenti(self, quotazioni: list[dict]) -> list[dict]:
        """Quali movimenti meritano un avviso, adesso.

        A fasce: superata la soglia parte un avviso, poi si tace fino al
        doppio, poi al triplo. Un titolo fermo a -1,6% per tre ore non deve
        mandare un avviso ogni cinque minuti — la notizia e' che si e'
        mosso, non che continua a essere mosso.
        """
        fasce = self._fasce()
        nuovi = []
        for q in quotazioni:
            if not q["aperto"] and not self.anche_a_mercato_chiuso:
                continue
            fascia = int(abs(q["variazione"]) / self.soglia)
            if fascia < 1:
                continue
            vista = int(fasce.get(q["simbolo"], 0))
            if fascia <= vista:
                continue
            fasce[q["simbolo"]] = fascia
            nuovi.append(dict(q, fascia=fascia))
        if nuovi:
            self._ricorda(fasce)
        return nuovi

    # -- forma leggibile ----------------------------------------------------

    @staticmethod
    def come_voce(q: dict) -> dict:
        """Un movimento nella stessa forma delle altre voci del radar.

        Cosi' attraversa senza modifiche archivio, dashboard e Telegram: il
        resto del sistema non ha bisogno di sapere che esistono i prezzi.
        """
        segno = "+" if q["variazione"] > 0 else ""
        verso = "sale" if q["variazione"] > 0 else "scende"
        return {
            "tipo": "prezzo",
            "titolo": f"{q['nome']} {verso} del {abs(q['variazione']):.2f}%",
            "fonte": "Nasdaq quotazioni",
            "url": f"https://www.nasdaq.com/market-activity/stocks/{q['simbolo'].lower()}",
            "quando": datetime.now(timezone.utc).isoformat(),
            "testo": "",
            "impronta": f"prezzo-{q['simbolo']}-{q['fascia']}-"
                        f"{datetime.now(timezone.utc):%Y%m%d}",
            "esito": "avviso",
            "punteggio": min(50 + q["fascia"] * 15, 100),
            "motivi": [f"variazione {segno}{q['variazione']:.2f}% dalla chiusura"
                       f" precedente",
                       "quotazione differita di ~15 minuti"],
            "dati": {"simbolo": q["simbolo"], "prezzo": q["prezzo"],
                     "variazione": q["variazione"], "differito": q["differito"]},
        }


if __name__ == "__main__":
    import sys
    import tempfile

    prove = []

    # 1. La conversione dei numeri, che arrivano come "$776.34" e "-0.20%".
    for grezzo, atteso in (("$776.34", 776.34), ("-0.20%", -0.20), ("+1.5%", 1.5),
                           ("N/A", None), (None, None), ("1,234.5", 1234.5)):
        ok = _numero(grezzo) == atteso
        prove.append(ok)
        print(f"  {'ok ' if ok else 'NO '} _numero({grezzo!r}) -> {_numero(grezzo)!r}")

    # 2. Le fasce: un titolo che resta mosso non deve avvisare in continuazione.
    cfg = {"prezzi": {"attivo": True, "soglia_pct": 1.5, "simboli": []}}
    p = Prezzi(cfg, Path(tempfile.mkdtemp()) / "stato.json")

    def q(v, aperto=True):
        return [{"simbolo": "SPY", "nome": "S&P 500", "prezzo": 700.0,
                 "variazione": v, "aperto": aperto, "differito": True,
                 "quando": ""}]

    casi = [
        ("sotto soglia: niente", 0.9, 0),
        ("supera 1,5%: avvisa", -1.6, 1),
        ("resta a -1,7%: tace", -1.7, 0),
        ("peggiora a -3,1%: avvisa di nuovo", -3.1, 1),
        ("torna a -2,0%: tace", -2.0, 0),
        ("crolla a -4,8%: avvisa", -4.8, 1),
    ]
    for nome, v, atteso in casi:
        n = len(p.movimenti(q(v)))
        ok = n == atteso
        prove.append(ok)
        print(f"  {'ok ' if ok else 'NO '} {nome}: {n} avvisi")

    # 3. A mercato chiuso non si avvisa mai: il dato e' fermo da giorni.
    p2 = Prezzi(cfg, Path(tempfile.mkdtemp()) / "stato.json")
    n = len(p2.movimenti(q(-5.0, aperto=False)))
    prove.append(n == 0)
    print(f"  {'ok ' if n == 0 else 'NO '} mercato chiuso: nessun avviso ({n})")

    # 4. La forma della voce deve essere quella che il resto del sistema usa.
    v = Prezzi.come_voce({"simbolo": "GLD", "nome": "Oro", "prezzo": 401.5,
                          "variazione": -2.3, "fascia": 1, "differito": True})
    atteso = {"tipo", "titolo", "fonte", "quando", "impronta", "esito",
              "punteggio", "motivi", "dati"}
    ok = atteso <= set(v) and v["esito"] == "avviso" and "2.30%" in v["titolo"]
    prove.append(ok)
    print(f"  {'ok ' if ok else 'NO '} voce ben formata: {v['titolo']}")

    print(f"\n{sum(prove)}/{len(prove)} controlli superati")
    sys.exit(0 if all(prove) else 1)
