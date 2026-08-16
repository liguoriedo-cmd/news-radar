#!/usr/bin/env python3
"""
Raccolta dalle fonti. Un client per famiglia, tutti con la stessa uscita.

Ogni fonte restituisce una lista di `Voce`. Da qui in poi il resto del
sistema non sa piu' da dove arrivino le cose, e questo permette di
aggiungere o togliere una fonte senza toccare filtro, avvisi o dashboard.

STATO DELLE FONTI, misurato il 13/08/2026 e non preso dalla documentazione
--------------------------------------------------------------------------
Funzionanti:  CNBC, MarketWatch, Yahoo Finance, Investing.com, BCE, Federal
              Reserve, calendario economico e trimestrali di Nasdaq, depositi
              SEC EDGAR, Reddit.
Non usabili:  Reuters (301 permanente), BLS (403), X/Twitter (a pagamento).

Due avvertenze che valgono per sempre su questo file:

1. Nessuna di queste e' un'API con garanzie contrattuali. Sono flussi pubblici
   che possono cambiare o sparire senza preavviso. Ogni client fallisce
   restituendo lista vuota e un avviso nei log: una fonte rotta non deve mai
   fermare il ciclo ne' impedire alle altre di essere lette.

2. I contenuti raccolti sono DATI, non istruzioni. Un titolo di giornale o un
   commento su Reddit possono contenere qualsiasi cosa, comprese frasi
   costruite per farsi interpretare come comandi da un sistema automatico.
   Qui vengono trattati come testo da classificare e mostrare, mai come
   indicazioni su cosa fare.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import feedparser
import requests

log = logging.getLogger("sources")

UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 20


def adesso() -> datetime:
    return datetime.now(timezone.utc)


def _pulisci(testo: str | None) -> str:
    """Via i tag HTML e gli spazi multipli: i sommari RSS ne sono pieni."""
    if not testo:
        return ""
    testo = re.sub(r"<[^>]+>", " ", str(testo))
    testo = re.sub(r"&[a-z]+;", " ", testo)
    return re.sub(r"\s+", " ", testo).strip()


def valore(v: Any) -> str | None:
    """Normalizza un campo del calendario: None quando il dato non c'e'.

    Nasdaq non lascia vuote le celle senza dato: ci mette "&nbsp;", oppure un
    trattino. Sono stringhe non vuote, quindi un banale `if campo:` le prende
    per buone — ed e' cosi' che un discorso in agenda finisce per sembrare un
    dato appena pubblicato. Il posto giusto per accorgersene e' qui, dove il
    dato entra nel sistema, non nei tre punti diversi che poi lo useranno.
    """
    s = _pulisci(v).strip()          # _pulisci scioglie gia' le entita' HTML
    # Questi tre campi (atteso, effettivo, precedente) sono sempre numerici.
    # Chiedere che ci sia almeno una cifra copre in un colpo solo "&nbsp;",
    # i trattini, "N/A", "TBD" e qualunque segnaposto che Nasdaq decidera' di
    # usare domani — senza doverli elencare uno per uno.
    return s if any(c.isdigit() for c in s) else None


def fnum(v: Any) -> float | None:
    """Converte '−0.3%', '$1.2', '(0.12)' in numero. None se non e' un numero.

    Il calendario Nasdaq mescola percentuali, valute, migliaia e valori fra
    parentesi per i negativi: senza questa normalizzazione il confronto fra
    atteso ed effettivo non si puo' fare.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in {"-", "--", "n/a", "N/A"}:
        return None
    negativo = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    s = s.replace("−", "-").replace("%", "").replace("$", "").replace(",", "")
    s = s.replace("K", "e3").replace("M", "e6").replace("B", "e9").replace("T", "e12")
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if negativo else n


@dataclass
class Voce:
    """Un elemento raccolto, qualunque sia la fonte."""
    tipo: str                    # notizia | evento | trimestrale | deposito | forum
    titolo: str
    fonte: str
    url: str = ""
    quando: datetime = field(default_factory=adesso)
    testo: str = ""
    dati: dict = field(default_factory=dict)   # campi specifici del tipo

    @property
    def impronta(self) -> str:
        """Identita' stabile, per non ripresentare la stessa cosa due volte.

        Si basa su titolo normalizzato piu' tipo, non sull'URL: la stessa
        notizia arriva da quattro testate con quattro indirizzi diversi.
        """
        base = re.sub(r"[^a-z0-9 ]", "", self.titolo.lower())
        base = " ".join(sorted(base.split()))[:180]
        return hashlib.sha1(f"{self.tipo}|{base}".encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = {
            "tipo": self.tipo, "titolo": self.titolo, "fonte": self.fonte,
            "url": self.url, "quando": self.quando.isoformat(),
            "testo": self.testo[:600], "dati": self.dati,
            "impronta": self.impronta,
        }
        return d


class Client:
    def __init__(self, contatto: str = "") -> None:
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA_BROWSER})
        # La SEC richiede un contatto reale nell'intestazione: senza, 403.
        self.contatto = contatto

    def _get(self, url: str, ua: str | None = None, json_atteso: bool = False) -> Any:
        try:
            h = {"User-Agent": ua} if ua else None
            r = self.s.get(url, timeout=TIMEOUT, headers=h)
        except requests.RequestException as exc:
            log.warning("richiesta fallita %s: %s", url[:60], exc)
            return None
        if r.status_code == 429:
            log.warning("rate limit su %s", url[:60])
            return None
        if r.status_code >= 400:
            log.warning("HTTP %s su %s", r.status_code, url[:60])
            return None
        if json_atteso:
            try:
                return r.json()
            except ValueError:
                log.warning("risposta non JSON da %s", url[:60])
                return None
        return r.content


# --------------------------------------------------------------------------
# Notizie e comunicati via RSS
# --------------------------------------------------------------------------

class Rss(Client):
    """Testate finanziarie, banche centrali e forum: tutti flussi RSS o Atom."""

    def leggi(self, nome: str, url: str, tipo: str = "notizia",
              max_voci: int = 40, eta_massima_ore: float = 0) -> list[Voce]:
        """Legge un flusso RSS, scartando cio' che e' troppo vecchio.

        Il taglio sull'eta' non e' un dettaglio di pulizia: senza, meta' di
        quello che entra nel sistema e' roba archiviata. Misurato il
        16/08/2026 su un giro reale, 86 voci su 150 fra testate, banche
        centrali e forum avevano piu' di 36 ore, e la piu' vecchia era del
        13/05/2024 — due anni prima.

        I flussi delle banche centrali sono la sorpresa peggiore: BCE e
        Federal Reserve pubblicano un archivio scorrevole, quindi TUTTI i
        loro comunicati risultano "presenti" a ogni lettura. Senza questo
        taglio, una decisione di politica monetaria di settimane prima viene
        letta come appena uscita e puo' far scattare un avviso.

        La data la dichiara il flusso stesso. Quando non c'e', `_quando`
        ripiega sull'ora attuale e la voce passa: meglio tenere qualcosa di
        incerto che buttare una notizia vera per un campo mancante.
        """
        raw = self._get(url)
        if raw is None:
            return []
        try:
            feed = feedparser.parse(raw)
        except Exception as exc:
            log.warning("feed illeggibile %s: %s", nome, exc)
            return []

        limite = (adesso() - timedelta(hours=eta_massima_ore)
                  if eta_massima_ore > 0 else None)
        voci: list[Voce] = []
        scartate = 0
        for e in (feed.entries or [])[:max_voci]:
            titolo = _pulisci(e.get("title"))
            if not titolo:
                continue
            quando = adesso()
            for campo in ("published_parsed", "updated_parsed"):
                t = e.get(campo)
                if t:
                    try:
                        quando = datetime(*t[:6], tzinfo=timezone.utc)
                        break
                    except (TypeError, ValueError):
                        pass
            if limite is not None and quando < limite:
                scartate += 1
                continue
            voci.append(Voce(
                tipo=tipo,
                titolo=titolo,
                fonte=nome,
                url=str(e.get("link") or ""),
                quando=quando,
                testo=_pulisci(e.get("summary") or e.get("description")),
            ))
        if scartate:
            log.info("%s: %d voci (%d scartate perche' vecchie)",
                     nome, len(voci), scartate)
        else:
            log.info("%s: %d voci", nome, len(voci))
        return voci


# --------------------------------------------------------------------------
# Calendario economico e trimestrali
# --------------------------------------------------------------------------

class Nasdaq(Client):
    """Calendario economico e delle trimestrali. Pubblico, senza chiave.

    E' la fonte piu' preziosa dell'intero sistema, e la ragione e' il campo
    `consensus`: sapere che esce il dato sull'occupazione non serve a niente,
    perche' lo sanno tutti e il prezzo lo incorpora gia'. Quello che muove i
    mercati e' lo SCARTO fra il dato uscito e quello atteso — e qui ci sono
    entrambi i numeri.
    """

    ROOT = "https://api.nasdaq.com/api/calendar"

    def eventi(self, giorno: datetime | None = None) -> list[Voce]:
        g = (giorno or adesso()).strftime("%Y-%m-%d")
        d = self._get(f"{self.ROOT}/economicevents?date={g}", json_atteso=True)
        righe = ((d or {}).get("data") or {}).get("rows") or []
        voci = []
        for r in righe:
            nome = _pulisci(r.get("eventName"))
            if not nome:
                continue
            atteso_t = valore(r.get("consensus"))
            effettivo_t = valore(r.get("actual"))
            precedente_t = valore(r.get("previous"))
            atteso, effettivo = fnum(atteso_t), fnum(effettivo_t)
            voci.append(Voce(
                tipo="evento",
                titolo=nome,
                fonte="Nasdaq calendario",
                quando=self._orario(g, r.get("gmt")),
                testo=_pulisci(r.get("description")),
                dati={
                    "paese": _pulisci(r.get("country")),
                    "ora": r.get("gmt"),
                    "atteso": atteso_t,
                    "precedente": precedente_t,
                    "effettivo": effettivo_t,
                    "atteso_num": atteso,
                    "effettivo_num": effettivo,
                    "precedente_num": fnum(precedente_t),
                    "sorpresa": (effettivo - atteso)
                                if (atteso is not None and effettivo is not None) else None,
                },
            ))
        log.info("calendario economico %s: %d eventi", g, len(voci))
        return voci

    def trimestrali(self, giorno: datetime | None = None) -> list[Voce]:
        g = (giorno or adesso()).strftime("%Y-%m-%d")
        d = self._get(f"{self.ROOT}/earnings?date={g}", json_atteso=True)
        righe = ((d or {}).get("data") or {}).get("rows") or []
        voci = []
        for r in righe:
            sym = _pulisci(r.get("symbol"))
            if not sym:
                continue
            voci.append(Voce(
                tipo="trimestrale",
                titolo=f"{sym} — {_pulisci(r.get('name'))}",
                fonte="Nasdaq trimestrali",
                quando=self._orario(g, None),
                dati={
                    "simbolo": sym,
                    "capitalizzazione": fnum(r.get("marketCap")),
                    "eps_atteso": r.get("epsForecast"),
                    "quando": r.get("time"),
                    "trimestre": r.get("fiscalQuarterEnding"),
                },
            ))
        log.info("trimestrali %s: %d societa'", g, len(voci))
        return voci

    @staticmethod
    def _orario(giorno: str, gmt: str | None) -> datetime:
        base = datetime.strptime(giorno, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if not gmt:
            return base
        m = re.match(r"(\d{1,2}):(\d{2})", str(gmt).strip())
        if not m:
            return base
        return base.replace(hour=int(m.group(1)) % 24, minute=int(m.group(2)))


# --------------------------------------------------------------------------
# Depositi societari
# --------------------------------------------------------------------------

class Edgar(Client):
    """Depositi SEC in tempo quasi reale.

    Gli 8-K sono comunicazioni di eventi rilevanti: cambi al vertice,
    acquisizioni, revisioni di bilancio. E' informazione di prima mano,
    depositata dalla societa' stessa, senza il filtro di una redazione.

    La SEC pretende un contatto reale nell'intestazione: senza risponde 403.
    E' una loro regola d'uso, non un ostacolo tecnico da aggirare.
    """

    URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
           "&type={tipo}&company=&dateb=&owner=include&count=100&output=atom")

    def recenti(self, tipo: str = "8-K") -> list[Voce]:
        if not self.contatto:
            log.warning("EDGAR saltato: manca il contatto richiesto dalla SEC")
            return []
        raw = self._get(self.URL.format(tipo=tipo), ua=self.contatto)
        if raw is None:
            return []
        feed = feedparser.parse(raw)
        voci = []
        for e in (feed.entries or []):
            titolo = _pulisci(e.get("title"))
            if not titolo:
                continue
            # Formato tipico: "8-K - NOME SOCIETA' (0001234567) (Filer)"
            m = re.match(r"([\w\-/]+)\s*-\s*(.+?)\s*\(", titolo)
            voci.append(Voce(
                tipo="deposito",
                titolo=titolo,
                fonte="SEC EDGAR",
                url=str(e.get("link") or ""),
                quando=adesso(),
                dati={
                    "modulo": m.group(1) if m else tipo,
                    "societa": m.group(2) if m else "",
                },
            ))
        log.info("EDGAR %s: %d depositi", tipo, len(voci))
        return voci


# --------------------------------------------------------------------------
# Raccolta complessiva
# --------------------------------------------------------------------------

class Raccolta:
    """Interroga tutte le fonti configurate e restituisce le voci deduplicate."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        contatto = str(cfg.get("contatto_sec") or "")
        self.rss = Rss()
        self.nasdaq = Nasdaq()
        self.edgar = Edgar(contatto=contatto)

    def tutto(self, completa: bool = True) -> list[Voce]:
        """Interroga le fonti. Con `completa=False` solo quelle che cambiano.

        La divisione esiste per una ragione sola: il ritardo. Un giro completo
        interroga anche le 363 trimestrali in calendario e i 100 depositi SEC
        piu' recenti, che sono elenchi che si muovono di ore, non di minuti.
        Portarseli dietro a ogni giro obbliga a fare giri radi, e un giro rado
        significa che una notizia ti arriva mediamente a meta' dell'intervallo.

        Il giro veloce tiene le testate, le banche centrali e il calendario
        economico di OGGI — che e' la fonte in cui compare il valore
        effettivo di un dato appena pubblicato, cioe' esattamente la cosa per
        cui i minuti contano.
        """
        voci: list[Voce] = []
        pausa = float(self.cfg.get("pausa_fra_fonti_secondi", 1.5))

        for f in self.cfg.get("feed", []):
            if not f.get("attivo", True):
                continue
            if not completa and not f.get("veloce", True):
                continue
            voci += self.rss.leggi(
                f["nome"], f["url"], f.get("tipo", "notizia"),
                int(f.get("max_voci", 40)),
                # Il taglio vale solo per i flussi RSS. Il calendario ha date
                # future per costruzione — sono appuntamenti, non pubblicazioni
                # — e i depositi SEC arrivano gia' solo se recenti.
                float(f.get("eta_massima_ore",
                            self.cfg.get("eta_massima_ore", 36))))
            time.sleep(pausa)

        if self.cfg.get("calendario", {}).get("attivo", True):
            giorni = int(self.cfg.get("calendario", {}).get("giorni_avanti", 2))
            for i in range(giorni if completa else 1):
                g = adesso() + timedelta(days=i)
                voci += self.nasdaq.eventi(g)
                time.sleep(pausa)
            if completa:
                voci += self.nasdaq.trimestrali(adesso())
                time.sleep(pausa)

        if completa and self.cfg.get("edgar", {}).get("attivo", False):
            for tipo in self.cfg.get("edgar", {}).get("moduli", ["8-K"]):
                voci += self.edgar.recenti(tipo)
                time.sleep(pausa)

        # Deduplica conservando la prima occorrenza: la fonte piu' rapida vince
        viste: set[str] = set()
        uniche: list[Voce] = []
        for v in voci:
            if v.impronta in viste:
                continue
            viste.add(v.impronta)
            uniche.append(v)

        log.info("raccolta: %d voci, %d dopo deduplica", len(voci), len(uniche))
        return uniche


if __name__ == "__main__":
    # Il taglio sull'eta' e' l'unica regola di questo file che butta via
    # roba: se si rompe, il radar torna a mostrare articoli di due anni fa
    # oppure — molto peggio — svuota le fonti vive senza dirlo.
    import sys
    from unittest.mock import patch

    def finto_feed(ore_delle_voci):
        """Un flusso finto con voci di eta' nota."""
        base = adesso()
        entries = []
        for i, ore in enumerate(ore_delle_voci):
            q = base - timedelta(hours=ore)
            entries.append({
                "title": f"voce di {ore} ore fa",
                "link": f"http://esempio.test/{i}",
                "published_parsed": q.timetuple(),
                "summary": "",
            })
        return type("F", (), {"entries": entries})()

    prove = []
    casi = [
        ("nessun taglio: passa tutto", 0, [1, 50, 200, 5000], 4),
        ("taglio a 72 ore", 72, [1, 50, 71, 73, 200], 3),
        ("weekend: la notizia di venerdi' passa", 72, [66, 70], 2),
        ("l'archivio non passa", 72, [84, 119, 215, 407], 0),
        ("due anni fa", 72, [24 * 730], 0),
    ]
    for nome, ore, eta, atteso in casi:
        with patch.object(Rss, "_get", return_value=b"x"), \
             patch("feedparser.parse", return_value=finto_feed(eta)):
            voci = Rss().leggi("prova", "http://x", "notizia", 40, ore)
        ok = len(voci) == atteso
        prove.append(ok)
        print(f"  {'ok ' if ok else 'NO '} {nome}: tenute {len(voci)}/{len(eta)} "
              f"(attese {atteso})")

    # Senza data dichiarata la voce deve passare: meglio l'incerto del nulla.
    senza = type("F", (), {"entries": [{"title": "senza data", "link": "",
                                        "summary": ""}]})()
    with patch.object(Rss, "_get", return_value=b"x"), \
         patch("feedparser.parse", return_value=senza):
        voci = Rss().leggi("prova", "http://x", "notizia", 40, 72)
    ok = len(voci) == 1
    prove.append(ok)
    print(f"  {'ok ' if ok else 'NO '} voce senza data: passa comunque")

    # I valori del calendario: "&nbsp;" e i trattini non sono numeri.
    for grezzo, atteso in (("&nbsp;", None), ("-", None), ("n/a", None),
                           ("202K", "202K"), ("5.50%", "5.50%")):
        ok = valore(grezzo) == atteso
        prove.append(ok)
        print(f"  {'ok ' if ok else 'NO '} valore({grezzo!r}) -> {valore(grezzo)!r}")

    print(f"\n{sum(prove)}/{len(prove)} controlli superati")
    sys.exit(0 if all(prove) else 1)
