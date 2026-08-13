#!/usr/bin/env python3
"""
Filtro di rilevanza: decide che cosa ti interrompe e che cosa no.

E' il pezzo piu' importante del radar. Le fonti restituiscono ~750 voci ogni
dieci minuti, cioe' circa centomila al giorno: un bot che le notifica tutte
viene silenziato entro sera e da quel momento non serve piu' a niente. Il
valore di questo strumento non sta in quanto raccoglie, sta in quanto e'
bravo a buttare via.

Tre esiti possibili per ogni voce:

  avviso  — ti arriva subito su Telegram, con suono. Deve essere raro.
  diario  — finisce sul dashboard e nella panoramica del mattino.
  scarta  — non lo vedi. E' la destinazione della stragrande maggioranza.

Perche' a regole e non con un modello
-------------------------------------
Un modello linguistico che classifica centomila voci al giorno costa, e' lento
e, soprattutto, non e' ispezionabile: quando ti sveglia alle tre di notte non
puoi chiedergli perche'. Qui ogni decisione porta con se' i motivi in chiaro
("parola forte: powell", "sorpresa 34%"), il dashboard li mostra, e se un
avviso e' sbagliato sai esattamente quale riga di configurazione alzare. Il
modello resta dov'e' utile e dove un errore costa poco: la panoramica delle
07:30.

I contenuti che arrivano qui sono DATI, non istruzioni. Un titolo puo'
contenere qualsiasi frase, comprese quelle costruite per farsi interpretare
come comandi: qui viene solo cercato, contato e classificato, mai eseguito.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("filtro")

# Punteggi di partenza per tipo. Non decidono l'esito — quello lo decidono le
# regole piu' sotto — servono a ordinare: quando arrivano sei avvisi insieme,
# il comunicato della Fed deve stare sopra il titolo di Yahoo Finance.
PESO_TIPO = {
    "ufficiale": 40,    # Fed, BCE: fonte primaria, nessun intermediario
    "evento": 35,       # dato macro uscito
    "deposito": 25,     # 8-K depositato dalla societa'
    "notizia": 20,      # testata
    "trimestrale": 15,  # in calendario, non ancora uscita
    "forum": 5,         # conferma, mai innesco
}

# Quanto perde una voce quando il veto le toglie il diritto di interromperti.
# Serve a tenere coerenti decisione e ordinamento: 25 punti bastano a far
# scendere un comunicato amministrativo sotto un dato macro fuori linea.
PENALITA_VETO = 25


def adesso() -> datetime:
    return datetime.now(timezone.utc)


def _norm(testo: str) -> str:
    return re.sub(r"\s+", " ", str(testo or "").lower())


def _cerca(testo: str, termini) -> list[str]:
    """Termini trovati nel testo, con confine di parola.

    Due dettagli che sembrano pedanteria e non lo sono:

    - il confine e' fatto con lookaround e non con \\b, perche' i termini
      contengono simboli: su "s&p 500" il \\b cade dove non ci si aspetta e la
      ricerca fallisce in silenzio, il modo peggiore in cui possa fallire.
    - la "s" finale e' facoltativa. Senza, "tariff" non trova "tariffs" e
      "rate cut" non trova "rate cuts": in un titolo di giornale il plurale
      e' la forma normale, e cercare solo il singolare vuol dire non trovare
      quasi niente.
    """
    trovati = []
    for t in termini or []:
        t = str(t).strip().lower()
        if not t:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(t)}s?(?![a-z0-9])", testo):
            trovati.append(t)
    return trovati


def _simboli(testo_originale: str, simboli) -> list[str]:
    """Ticker citati, con la ricerca sensibile alle maiuscole.

    Volutamente: "SPY" e' un ticker, "spy" e' una spia. Cercare senza
    distinguere le maiuscole trasformerebbe ogni articolo di spionaggio
    industriale in un avviso sull'S&P 500.
    """
    trovati = []
    for s in simboli or []:
        s = str(s).strip()
        if not s:
            continue
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])", testo_originale):
            trovati.append(s)
    return trovati


def passo(testo) -> float:
    """Il minimo scarto rappresentabile nel dato, letto da come e' scritto.

    Se un indicatore viene pubblicato come "0.2", la differenza piu' piccola
    che puo' esistere fra due letture e' 0,1: e' la precisione con cui viene
    diffuso. Serve perche' la percentuale, su numeri piccoli, mente — da 0,2 a
    0,3 sono "+50%" e sembra un terremoto, ma e' un singolo scatto, il rumore
    di fondo di qualunque serie. Su 202K contro 209K lo scatto e' mille, e la
    differenza ne vale settemila: quella si' che e' una notizia.
    """
    s = str(testo or "").strip().strip("()")
    s = s.replace("−", "-").replace("%", "").replace("$", "").replace(",", "")
    moltiplicatore = 1.0
    for suffisso, m in (("K", 1e3), ("M", 1e6), ("B", 1e9), ("T", 1e12)):
        if s.upper().endswith(suffisso):
            moltiplicatore, s = m, s[:-1]
            break
    decimali = len(s.split(".")[-1]) if "." in s else 0
    return (10.0 ** -decimali) * moltiplicatore


def scostamento(dati: dict) -> tuple[float, str, float] | None:
    """Di quanto il dato uscito si discosta, e da che cosa.

    Restituisce (percentuale, riferimento, scatti). Il riferimento e' "attese"
    oppure "precedente", perche' i due non valgono uguale:

      - dalle ATTESE e' il segnale vero. Il valore atteso e' gia' nei prezzi,
        quindi cio' che muove i mercati e' esattamente lo scarto.
      - dal PRECEDENTE e' un ripiego per i dati che nessuno stima. "Diverso
        dal mese scorso" e' normale: molti indicatori oscillano sempre. Per
        questo chi chiama pretende una soglia piu' alta quando il confronto
        e' con il precedente.

    `scatti` e' la differenza misurata in unita' di `passo`, e serve a
    correggere il difetto della percentuale: quando il riferimento e' vicino
    a zero la percentuale esplode (da 0,1 a 0,3 sono "+200%") pur trattandosi
    di due scatti. La misura statisticamente corretta sarebbe lo scarto
    rapportato alla dispersione storica della serie, che queste fonti
    gratuite non danno: percentuale e scatti insieme sono l'approssimazione
    migliore che si puo' fare con i dati disponibili.
    """
    effettivo = dati.get("effettivo_num")
    if effettivo is None:
        return None
    atteso = dati.get("atteso_num")
    prec = dati.get("precedente_num")

    if atteso is not None:
        base, riferimento, scritto = atteso, "attese", dati.get("atteso")
    elif prec is not None:
        base, riferimento, scritto = prec, "precedente", dati.get("precedente")
    else:
        return None

    differenza = abs(effettivo - base)
    p = passo(scritto)
    scatti = differenza / p if p > 0 else 0.0

    scala = max(abs(atteso or 0.0), abs(prec or 0.0))
    if scala < 1e-9:
        # Riferimenti entrambi a zero: la percentuale non esiste. Qualsiasi
        # valore diverso da zero e' per definizione uno scostamento.
        pct = 999.0 if differenza > 1e-9 else 0.0
    else:
        pct = differenza / scala * 100.0
    return pct, riferimento, scatti


# --------------------------------------------------------------------------
# Memoria di cio' che e' gia' passato
# --------------------------------------------------------------------------

class Memoria:
    """Ricorda le voci gia' valutate, cosi' non ti arrivano ogni dieci minuti.

    Senza questa classe il filtro sarebbe inutile: le fonti ripropongono le
    stesse voci a ogni ciclo, e lo stesso titolo verrebbe notificato 144 volte
    al giorno.
    """

    def __init__(self, path: Path, giorni: int = 7) -> None:
        self.path = Path(path)
        self.giorni = int(giorni)
        self.voci: dict[str, str] = {}
        self._carica()

    def _carica(self) -> None:
        try:
            self.voci = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.voci = {}
        except (ValueError, OSError) as exc:
            # Memoria illeggibile: si riparte da zero. E' un primo avvio, non
            # un errore fatale — meglio una mattina silenziosa che un crash.
            log.warning("memoria illeggibile (%s): riparto da vuota", exc)
            self.voci = {}
        self.pota()

    def pota(self) -> None:
        taglio = (adesso() - timedelta(days=self.giorni)).isoformat()
        self.voci = {k: v for k, v in self.voci.items() if v >= taglio}

    @property
    def vuota(self) -> bool:
        return not self.voci

    def conosce(self, chiave: str) -> bool:
        return chiave in self.voci

    def segna(self, chiave: str) -> None:
        self.voci[chiave] = adesso().isoformat()

    def salva(self) -> None:
        self.pota()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.voci))
            tmp.replace(self.path)   # scrittura atomica: niente file mezzo scritto
        except OSError as exc:
            log.warning("memoria non salvata: %s", exc)


def chiave(voce: dict) -> str:
    """Identita' della voce ai fini della memoria.

    Per gli eventi di calendario NON basta l'impronta del titolo. "US CPI"
    compare nel calendario ore prima con il solo valore atteso e poi di nuovo,
    stesso titolo, con il valore effettivo: se le due versioni condividessero
    la chiave, la memoria zittirebbe proprio il momento della pubblicazione,
    cioe' l'unico che conta. Sono due voci diverse e vanno ricordate a parte.
    """
    imp = voce.get("impronta", "")
    if voce.get("tipo") == "evento":
        uscito = (voce.get("dati") or {}).get("effettivo") not in (None, "")
        return f"{imp}|{'uscito' if uscito else 'atteso'}"
    return imp


# --------------------------------------------------------------------------
# Il giudizio
# --------------------------------------------------------------------------

@dataclass
class Giudizio:
    esito: str                                   # avviso | diario | scarta
    punteggio: int = 0
    motivi: list[str] = field(default_factory=list)

    @property
    def avvisa(self) -> bool:
        return self.esito == "avviso"


class Filtro:
    """Applica le regole di `avvisi` in config.yaml a una voce alla volta."""

    def __init__(self, cfg: dict, path_memoria: Path) -> None:
        self.cfg = cfg
        a = cfg.get("avvisi", {}) or {}
        self.a = a
        self.forti = [str(p).lower() for p in a.get("parole_forti", [])]
        self.chiave_deboli = [str(p).lower() for p in a.get("parole_chiave", [])]
        self.sempre = [str(p).lower() for p in a.get("eventi_sempre", [])]
        self.paesi = [str(p).lower() for p in a.get("paesi", [])]
        self.watchlist = [str(s) for s in a.get("watchlist", [])]
        self.alias = {str(k): [str(x).lower() for x in v]
                      for k, v in (a.get("alias", {}) or {}).items()}
        self.proprie = {str(k): [str(x).lower() for x in v]
                        for k, v in (a.get("parole_proprie", {}) or {}).items()}
        self.veto = [str(p).lower() for p in a.get("parole_veto", [])]
        self.soglia = float(a.get("sorpresa_minima_pct", 15))
        self.min_scatti = float(a.get("scatti_minimi", 2))
        self.max_ciclo = int(a.get("max_avvisi_per_ciclo", 5))
        self.solo_wl_depositi = bool(a.get("solo_watchlist_per_depositi", True))

        self.memoria = Memoria(path_memoria, int(cfg.get("memoria_giorni", 7)))
        # Al primissimo avvio la memoria e' vuota e le fonti restituiscono
        # tutto l'arretrato della giornata: senza questa guardia riceveresti
        # trenta avvisi nel primo minuto, su dati usciti sei ore fa.
        self.primo_avvio = self.memoria.vuota
        if self.primo_avvio:
            log.info("primo avvio: nessun avviso immediato, riempio la memoria")

    # -- riconoscimento dei riferimenti alla watchlist ---------------------

    def _riferimenti(self, testo_orig: str, testo_norm: str) -> list[str]:
        trovati = _simboli(testo_orig, self.watchlist)
        for sym, nomi in self.alias.items():
            if sym in trovati:
                continue
            if _cerca(testo_norm, nomi):
                trovati.append(sym)
        return trovati

    # -- le regole ---------------------------------------------------------

    def valuta(self, voce: dict) -> Giudizio:
        tipo = voce.get("tipo", "notizia")
        testo_orig = f"{voce.get('titolo', '')} {voce.get('testo', '')}"
        testo = _norm(testo_orig)
        dati = voce.get("dati") or {}

        if self.memoria.conosce(chiave(voce)):
            return Giudizio("scarta", 0, ["gia' vista"])

        punti = PESO_TIPO.get(tipo, 10)
        motivi: list[str] = []

        forti = _cerca(testo, self.forti)
        deboli = _cerca(testo, self.chiave_deboli)
        riferimenti = self._riferimenti(testo_orig, testo)

        # Una parola chiave che coincide col nome di chi pubblica non
        # distingue niente: sul canale della BCE ogni singolo comunicato
        # contiene "ECB", compreso l'invito al concerto all'aperto. Qui viene
        # tolta, cosi' sulle fonti primarie a contare e' cio' che dicono.
        propri = self.proprie.get(str(voce.get("fonte", "")), [])
        if propri:
            forti = [p for p in forti if p not in propri]
            deboli = [p for p in deboli if p not in propri]

        if forti:
            punti += 30
            motivi.append("parola forte: " + ", ".join(forti[:3]))
        if deboli:
            punti += min(10 * len(deboli), 20)
            motivi.append("parole rilevanti: " + ", ".join(deboli[:3]))
        if riferimenti:
            punti += 25
            motivi.append("watchlist: " + ", ".join(riferimenti))

        esito, extra = self._esito(tipo, dati, forti, deboli, riferimenti, motivi)
        punti += extra

        # Il veto agisce dopo, non prima: la voce resta sul dashboard, perde
        # solo il diritto di interromperti. Serve per i comunicati che
        # contengono le parole giuste ma non sono decisioni — "la Fed annuncia
        # i responsabili della sua task force" contiene "monetary policy" e
        # non muove un prezzo.
        if esito == "avviso":
            bloccanti = _cerca(testo, self.veto)
            if bloccanti:
                motivi.append("veto: " + ", ".join(bloccanti[:2]))
                esito = "diario"
                # Il veto abbassa anche il punteggio, e non e' un dettaglio.
                # Il punteggio decide l'ORDINE sul dashboard: senza questa
                # penalita' "la Fed annuncia i responsabili della task force"
                # conserva i 70 punti che gli ha dato la parola "monetary
                # policy" e si piazza sopra a un dato macro fuori linea. Il
                # veto e' un giudizio sul contenuto — dire "non merita di
                # interromperti" e poi metterlo in cima sarebbe contraddirsi.
                punti -= PENALITA_VETO

        if self.primo_avvio and esito == "avviso":
            esito = "diario"
            motivi.append("primo avvio: declassato")

        return Giudizio(esito, min(int(punti), 100), motivi)

    def _esito(self, tipo: str, dati: dict, forti: list, deboli: list,
               riferimenti: list, motivi: list) -> tuple[str, int]:
        """La decisione, un tipo alla volta, con l'aggiustamento del punteggio.

        Il secondo valore serve a una cosa sola ma importante: sui dati macro
        il punteggio deve sapere QUANTO il dato ha sorpreso. Senza, un dato
        sull'inflazione che esce di un punto sopra le attese e un'asta di
        buoni del tesoro valgono uguale, perche' hanno lo stesso peso di
        partenza e nessuna parola chiave. E quando arrivano piu' avvisi
        insieme, l'ordine con cui li leggi e' esattamente quel punteggio.
        """

        # --- dati macro dal calendario -----------------------------------
        if tipo == "evento":
            uscito = dati.get("effettivo") not in (None, "")
            paese = str(dati.get("paese", "")).lower()
            interessa = any(p in paese for p in self.paesi) if self.paesi else True
            titolo = _norm(dati.get("_titolo", "")) or ""
            e_sempre = bool(_cerca(titolo, self.sempre))

            if not uscito:
                # Non ancora pubblicato: sta in calendario, non e' una notizia.
                return ("diario" if (interessa and e_sempre) else "scarta"), 0
            if not interessa:
                return "scarta", 0
            if dati.get("effettivo_num") is None:
                # C'e' un valore, ma non e' un numero. Nel calendario Nasdaq
                # succede per i discorsi ("FOMC Member Barkin Speaks"), dove
                # il campo contiene un orario o del testo. Senza un numero non
                # esiste sorpresa da misurare, e un discorso in agenda non e'
                # una notizia: e' un promemoria. Va sul dashboard.
                motivi.append("in agenda, nessun dato numerico")
                return "diario", 0

            misura = scostamento(dati)
            if misura is None:
                motivi.append("nessun termine di paragone")
                return "diario", 0
            s, riferimento, scatti = misura

            # Sugli eventi che contano di piu' la soglia scende a un terzo:
            # su un dato sull'occupazione anche uno scarto modesto muove i
            # prezzi. Quando pero' il paragone e' con il valore precedente e
            # non con le attese, la soglia raddoppia, perche' oscillare da un
            # mese all'altro e' la normalita' e non una notizia.
            soglia = self.soglia / 3.0 if e_sempre else self.soglia
            if riferimento == "precedente":
                soglia *= 2.0

            # Quanto ha sorpreso entra nel punteggio, non solo nella
            # decisione. E' cio' che fa stare un dato sull'occupazione fuori
            # linea sopra a un'asta di buoni ordinaria.
            premio = 25 if s >= soglia else (10 if s >= soglia / 2 else 0)

            n = int(round(scatti))
            quanti = f"{n} scatto" if n == 1 else f"{n} scatti"
            if s >= soglia and scatti >= self.min_scatti:
                motivi.append(f"scostamento {s:.0f}% dalle {riferimento} "
                              f"(soglia {soglia:.0f}%, {quanti})")
                return "avviso", premio
            if s >= soglia:
                # Percentuale alta ma su un dato minuscolo: da 0,2 a 0,3 e'
                # "+50%" ed e' un solo scatto. Non e' una notizia, e non deve
                # nemmeno guadagnare punti: il premio scende con la decisione.
                motivi.append(f"scarto di appena {quanti}, nonostante il {s:.0f}%")
                return "diario", 0
            motivi.append(f"in linea con le {riferimento} ({s:.0f}%)")
            return "diario", premio

        # --- comunicati di banca centrale ---------------------------------
        if tipo == "ufficiale":
            # Il flusso della Fed pubblica anche nomine e note amministrative:
            # fonte primaria non vuol dire automaticamente rilevante.
            if forti or riferimenti:
                return "avviso", 0
            return ("diario" if deboli else "scarta"), 0

        # --- depositi SEC --------------------------------------------------
        if tipo == "deposito":
            if riferimenti:
                return "avviso", 0
            # Di 8-K se ne depositano centinaia al giorno: senza un legame con
            # la watchlist non finiscono nemmeno sul dashboard.
            return ("scarta" if self.solo_wl_depositi else "diario"), 0

        # --- forum -----------------------------------------------------------
        if tipo == "forum":
            # Sono i piu' rapidi sulle notizie improvvise e i piu' rumorosi su
            # tutto il resto. Servono a confermare, mai a innescare: nessun
            # percorso qui dentro porta a un avviso.
            return ("diario" if (forti or riferimenti) else "scarta"), 0

        # --- trimestrali in calendario ---------------------------------------
        if tipo == "trimestrale":
            return ("diario" if riferimenti else "scarta"), 0

        # --- notizie di testata ----------------------------------------------
        if riferimenti or forti:
            return "avviso", 0
        # Una sola parola debole non basta: "nasdaq" compare in un titolo su
        # tre. Due termini distinti sono gia' un argomento.
        if len(deboli) >= 2:
            return "avviso", 0
        return ("diario" if deboli else "scarta"), 0

    # -- passata completa ---------------------------------------------------

    def passa(self, voci: list[dict]) -> tuple[list[dict], list[dict], dict]:
        """Valuta tutte le voci di un ciclo.

        Restituisce (avvisi, diario, statistiche). Gli avvisi sono ordinati
        per punteggio e limitati a `max_avvisi_per_ciclo`: se una fonte
        impazzisce o cambia formato, il tetto e' cio' che impedisce al bot di
        mandarti duecento messaggi prima che tu te ne accorga.
        """
        avvisi, diario = [], []
        scartate = 0

        for v in voci:
            g = self.valuta(v)
            self.memoria.segna(chiave(v))
            v = dict(v, punteggio=g.punteggio, motivi=g.motivi, esito=g.esito)
            if g.esito == "avviso":
                avvisi.append(v)
            elif g.esito == "diario":
                diario.append(v)
            else:
                scartate += 1

        avvisi.sort(key=lambda v: -v["punteggio"])
        troppi = avvisi[self.max_ciclo:]
        avvisi = avvisi[:self.max_ciclo]
        for v in troppi:
            v["esito"] = "diario"
            v["motivi"] = list(v["motivi"]) + ["oltre il tetto per ciclo"]
        diario = troppi + diario
        diario.sort(key=lambda v: -v["punteggio"])

        self.memoria.salva()
        self.primo_avvio = False

        stat = {
            "totali": len(voci), "avvisi": len(avvisi),
            "diario": len(diario), "scartate": scartate,
            "oltre_tetto": len(troppi),
        }
        log.info("filtro: %(totali)d viste, %(avvisi)d avvisi, "
                 "%(diario)d a diario, %(scartate)d scartate", stat)
        return avvisi, diario, stat


def prepara(voci) -> list[dict]:
    """Da `Voce` a dizionario, portandosi dietro il titolo per gli eventi.

    Il titolo dell'evento sta in `titolo`, ma `_esito` guarda solo `dati`:
    lo si copia li' dentro cosi' la regola su `eventi_sempre` puo' leggerlo
    senza che la funzione debba conoscere la forma dell'intera voce.
    """
    out = []
    for v in voci:
        d = v.to_dict() if hasattr(v, "to_dict") else dict(v)
        if d.get("tipo") == "evento":
            d["dati"] = dict(d.get("dati") or {}, _titolo=d.get("titolo", ""))
        out.append(d)
    return out


# --------------------------------------------------------------------------
# Controllo delle regole. Si esegue con:  python3 filtro.py
#
# Non serve la rete: sono casi costruiti a mano sulle regole piu' delicate,
# quelle che se si rompono lo fanno in silenzio. Un filtro che sbaglia non da'
# errore, manda semplicemente l'avviso sbagliato o non ne manda nessuno, e te
# ne accorgi settimane dopo.
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile

    cfg = {
        "memoria_giorni": 7,
        "avvisi": {
            "sorpresa_minima_pct": 15, "scatti_minimi": 2, "max_avvisi_per_ciclo": 5,
            "parole_forti": ["powell", "monetary policy", "rate cut"],
            "parole_chiave": ["inflation", "nasdaq", "tariff"],
            "eventi_sempre": ["cpi", "jobless claims"],
            "paesi": ["United States"],
            "watchlist": ["SPY", "NVDA"],
            "alias": {"NVDA": ["nvidia"]},
            "parole_proprie": {"BCE": ["ecb"]},
            "parole_veto": ["task force"],
        },
    }

    def nuovo() -> Filtro:
        f = Filtro(cfg, Path(tempfile.mkdtemp()) / "m.json")
        f.primo_avvio = False
        return f

    def voce(**kw) -> dict:
        base = {"tipo": "notizia", "titolo": "", "fonte": "X", "testo": "",
                "dati": {}, "impronta": kw.get("titolo", "")[:16] or "vuota"}
        base.update(kw)
        if base["tipo"] == "evento":
            base["dati"] = dict(base["dati"], _titolo=base["titolo"])
        return base

    def evento(titolo, paese, atteso, effettivo, precedente=None) -> dict:
        from sources import fnum
        return voce(tipo="evento", titolo=titolo, fonte="Nasdaq calendario", dati={
            "paese": paese, "atteso": atteso, "effettivo": effettivo,
            "precedente": precedente, "atteso_num": fnum(atteso),
            "effettivo_num": fnum(effettivo), "precedente_num": fnum(precedente),
        })

    prove: list[tuple[str, str, str]] = []

    def atteso_e(nome: str, giudizio: Giudizio, voluto: str) -> None:
        prove.append((nome, giudizio.esito, voluto))

    # -- i due criteri si coprono a vicenda i punti deboli ------------------
    # La percentuale sbaglia sui numeri piccoli, gli scatti sui numeri
    # grandi. Servono entrambi, e questi tre casi lo dimostrano.
    atteso_e("sussidi 202K -> 250K: il dato e' fuori linea davvero",
             nuovo().valuta(evento("Jobless Claims", "United States", "202K", "250K")),
             "avviso")
    atteso_e("sussidi 202K -> 209K: 7.000 scatti, ma solo il 3%",
             nuovo().valuta(evento("Jobless Claims", "United States", "202K", "209K")),
             "diario")
    atteso_e("nowcast 0.2 -> 0.3: +50%, ma un solo scatto",
             nuovo().valuta(evento("Cleveland CPI", "United States", "0.2", "0.3")),
             "diario")

    # -- un discorso in agenda non e' un dato ------------------------------
    atteso_e("discorso senza numeri",
             nuovo().valuta(evento("FOMC Member Speaks", "United States", None, "13:00")),
             "diario")

    # -- confronto col precedente: soglia doppia ---------------------------
    atteso_e("senza consenso, scarto piccolo dal precedente",
             nuovo().valuta(evento("CPI", "United States", None, "3.1", "3.0")),
             "diario")

    # -- paesi fuori elenco -------------------------------------------------
    atteso_e("stesso dato ma paese non seguito",
             nuovo().valuta(evento("Jobless Claims", "New Zealand", "202K", "209K")),
             "scarta")

    # -- plurali ------------------------------------------------------------
    atteso_e("'tariff' deve trovare 'tariffs'",
             nuovo().valuta(voce(titolo="New tariffs hit chip inflation hard")),
             "avviso")

    # -- maiuscole nei ticker ------------------------------------------------
    atteso_e("SPY come ticker",
             nuovo().valuta(voce(titolo="SPY hits record")), "avviso")
    atteso_e("'spy' minuscolo non e' un ticker",
             nuovo().valuta(voce(titolo="Corporate spy arrested in Milan")), "scarta")
    atteso_e("Nvidia riconosciuta come NVDA",
             nuovo().valuta(voce(titolo="Nvidia beats estimates")), "avviso")

    # -- una parola debole sola non basta, due si' ---------------------------
    atteso_e("una sola parola rilevante",
             nuovo().valuta(voce(titolo="Nasdaq opens flat")), "diario")
    atteso_e("due parole rilevanti",
             nuovo().valuta(voce(titolo="Nasdaq slides as inflation bites")), "avviso")

    # -- il veto toglie l'avviso ma non la voce ------------------------------
    atteso_e("comunicato amministrativo",
             nuovo().valuta(voce(tipo="ufficiale", fonte="Federal Reserve",
                                 titolo="Fed announces task forces on monetary policy")),
             "diario")

    # -- la fonte non puo' qualificarsi da sola ------------------------------
    atteso_e("'ecb' sul canale della BCE non conta",
             nuovo().valuta(voce(tipo="ufficiale", fonte="BCE",
                                 titolo="ECB publishes banking data")), "scarta")

    # -- i forum non innescano mai -------------------------------------------
    atteso_e("forum con parola forte",
             nuovo().valuta(voce(tipo="forum", fonte="Reddit r/stocks",
                                 titolo="Powell just announced a rate cut")), "diario")

    # -- la memoria distingue l'attesa dalla pubblicazione --------------------
    prima = evento("CPI", "United States", "3.0", None)
    dopo = evento("CPI", "United States", "3.0", "3.9")
    dopo["impronta"] = prima["impronta"]      # stesso titolo, stessa impronta
    f = nuovo()
    f.valuta(prima)
    f.memoria.segna(chiave(prima))
    atteso_e("il dato pubblicato non viene zittito dalla versione in agenda",
             f.valuta(dopo), "avviso")

    # -- il primo avvio non sveglia nessuno -----------------------------------
    f = Filtro(cfg, Path(tempfile.mkdtemp()) / "m.json")
    atteso_e("primo avvio",
             f.valuta(voce(titolo="Powell signals rate cut")), "diario")

    falliti = [(n, o, v) for n, o, v in prove if o != v]
    for n, o, v in prove:
        print(f"  {'ok ' if o == v else 'NO '} {n}: {o}" + ("" if o == v else f" (atteso {v})"))
    print(f"\n{len(prove) - len(falliti)}/{len(prove)} regole verificate")
    sys.exit(1 if falliti else 0)
