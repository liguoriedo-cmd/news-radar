#!/usr/bin/env python3
"""
Verifica della classificazione d'urgenza: e' valida? e' affidabile?

Si esegue con:  python3 verifica.py     (serve la rete: usa dati veri)

E' una cosa diversa dagli autotest dentro filtro.py e pagina.py. Quelli
controllano che il codice faccia cio' che ho scritto; questo controlla che
cio' che ho scritto sia GIUSTO. Un filtro puo' superare tutti i suoi test ed
essere comunque tarato male: non da' errore, manda semplicemente l'avviso
sbagliato, e te ne accorgi settimane dopo.

Quattro domande, in ordine di importanza:

1. Il livello misura l'importanza o solo il tipo di fonte?
   Se ogni tipo finisse tutto nello stesso livello, "alta/media/bassa"
   sarebbe solo un altro nome per "banca centrale/calendario/giornale", e
   il colore non aggiungerebbe niente.

2. Quanto e' arbitrario il confine fra media e bassa?
   Se molte voci stanno a ridosso della soglia, spostarla di poco ribalta
   molte etichette.

3. Su casi la cui importanza non e' opinabile, l'etichetta e' quella giusta?

4. A parita' di ingresso l'uscita e' sempre la stessa?

UN LIMITE DA TENERE PRESENTE
----------------------------
I casi noti del punto 3 li ho scelti io, che ho anche scritto il
classificatore. Valgono come controllo di coerenza, non come giudizio
indipendente: un classificatore e il suo autore possono sbagliarsi insieme.
Il punto 1, che lavora su dati veri non scelti da nessuno, e' meno
compiacente — e infatti e' quello che ha trovato i difetti veri.
"""
from __future__ import annotations

import logging
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import yaml

import filtro as F
import pagina
from sources import Raccolta, fnum

RADICE = Path(__file__).resolve().parent
RIGA = "=" * 74


def campione(cfg: dict) -> tuple[list[dict], dict]:
    """Voci vere, appena raccolte, classificate come a radar avviato.

    Non si legge l'archivio su disco: quello puo' venire da un primo avvio,
    dove per costruzione nessuna voce diventa "alta". Verificare la
    classificazione su un campione privo della classe piu' importante
    sarebbe un modo elegante di non verificare niente.
    """
    f = F.Filtro(cfg, Path(tempfile.mkdtemp()) / "m.json")
    f.primo_avvio = False
    alert, diario, stat = f.passa(F.prepara(Raccolta(cfg).tutto()))
    return alert + diario, stat


def _voce(**kw) -> dict:
    b = {"tipo": "notizia", "titolo": "", "fonte": "X", "testo": "", "dati": {},
         "impronta": kw.get("titolo", "")[:20]}
    b.update(kw)
    if b["tipo"] == "evento":
        b["dati"] = dict(b["dati"], _titolo=b["titolo"])
    return b


def _evento(titolo, paese, atteso, effettivo, precedente=None) -> dict:
    return _voce(tipo="evento", titolo=titolo, fonte="Nasdaq calendario", dati={
        "paese": paese, "atteso": atteso, "effettivo": effettivo,
        "precedente": precedente, "atteso_num": fnum(atteso),
        "effettivo_num": fnum(effettivo), "precedente_num": fnum(precedente)})


CASI = [
    # --- devono essere ALTA -------------------------------------------------
    ("decisione Fed sui tassi", _voce(tipo="ufficiale", fonte="Federal Reserve",
        titolo="Federal Reserve issues FOMC statement"), "alta"),
    ("conferenza stampa di Powell", _voce(tipo="notizia", fonte="CNBC",
        titolo="Powell says inflation risks have shifted"), "alta"),
    ("CPI USA molto sopra le attese",
        _evento("CPI", "United States", "2.9", "3.6", "2.8"), "alta"),
    ("occupazione crollata",
        _evento("Nonfarm Payrolls", "United States", "180K", "12K", "175K"), "alta"),
    ("taglio dei tassi BCE", _voce(tipo="ufficiale", fonte="BCE",
        titolo="Monetary policy decisions: ECB cuts rates by 50 basis points"), "alta"),
    ("blocco all'export su una societa' seguita", _voce(tipo="notizia", fonte="CNBC",
        titolo="Nvidia halts shipments after export ban"), "alta"),
    ("dazi nuovi", _voce(tipo="notizia", fonte="MarketWatch",
        titolo="White House announces tariffs on all Chinese imports"), "alta"),

    # --- NON devono essere alta: rumore travestito da notizia ---------------
    ("concerto della BCE", _voce(tipo="ufficiale", fonte="BCE",
        titolo="ECB and Frankfurt Radio Symphony invite the public to Europa "
               "Open Air"), "non-alta"),
    ("nomina amministrativa Fed", _voce(tipo="ufficiale", fonte="Federal Reserve",
        titolo="Federal Reserve announces task force leadership on monetary "
               "policy"), "non-alta"),
    ("stima giornaliera di Cleveland",
        _evento("Cleveland CPI", "United States", "0.2", "0.3", "0.2"), "non-alta"),
    ("discorso in agenda",
        _evento("FOMC Member Barkin Speaks", "United States", None, None), "non-alta"),
    ("asta di titoli ordinaria",
        _evento("4-Week Bill Auction", "United States", None, "3.85", "3.84"),
        "non-alta"),
    ("dato di un paese non seguito",
        _evento("CPI", "New Zealand", "2.0", "3.5", "2.0"), "non-alta"),
    ("entusiasmo da forum", _voce(tipo="forum", fonte="Reddit r/stocks",
        titolo="Powell just announced a massive rate cut, NVDA to the moon"),
        "non-alta"),
    ("cronaca di giornata", _voce(tipo="notizia", fonte="Yahoo Finance",
        titolo="Stock market today: S&P 500, Nasdaq rise on inflation data"),
        "non-alta"),
    ("titolo generico di borsa", _voce(tipo="notizia", fonte="Investing.com",
        titolo="Nasdaq futures edge higher in premarket"), "non-alta"),
]


def classifica(v: dict, cfg: dict, soglia: int):
    f = F.Filtro(cfg, Path(tempfile.mkdtemp()) / "m.json")
    f.primo_avvio = False
    g = f.valuta(v)
    return pagina.livello({"esito": g.esito, "punteggio": g.punteggio}, soglia), g


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    cfg = yaml.safe_load((RADICE / "data/config.yaml").read_text())
    soglia = int((cfg.get("dashboard") or {}).get("soglia_media", 55))

    voci, stat = campione(cfg)
    print(f"Campione: {len(voci)} voci vere tenute su {stat['totali']} raccolte, "
          f"soglia media/bassa = {soglia}")

    esiti: list[bool] = []

    # ── 1 ────────────────────────────────────────────────────────────────
    print(f"\n{RIGA}\n1. IL LIVELLO MISURA L'IMPORTANZA O IL TIPO DI FONTE?\n{RIGA}\n")
    tab = defaultdict(Counter)
    for v in voci:
        tab[v.get("tipo")][pagina.livello(v, soglia)] += 1
    print(f"  {'tipo':<12}{'alta':>6}{'media':>7}{'bassa':>7}")
    for tipo, c in sorted(tab.items(), key=lambda x: -sum(x[1].values())):
        print(f"  {tipo:<12}{c['alta']:>6}{c['media']:>7}{c['bassa']:>7}")

    base = Counter(v.get("tipo") for v in voci)
    tot = sum(base.values())
    print("\n  il campione stesso e': "
          + "  ".join(f"{k} {v * 100 // tot}%" for k, v in base.most_common()))

    # La prova decisiva. Se un tipo finisse INTERO in un solo livello, quel
    # livello non sarebbe altro che il nome di quel tipo.
    print("\n  ogni tipo si distribuisce su piu' livelli?")
    for tipo in base:
        c = Counter(pagina.livello(v, soglia) for v in voci if v.get("tipo") == tipo)
        n = sum(c.values())
        if n < 3:
            continue
        pieno = max(c.values()) * 100 // n
        ok = pieno < 100
        esiti.append(ok)
        print(f"  {'ok ' if ok else 'NO '} {tipo:<11} su {len(c)} livelli "
              f"(il piu' pieno ne tiene il {pieno}%)")

    # Sui livelli bassi vogliamo varieta'. Su "alta" no: concentrarsi E' il
    # lavoro, e un valore alto qui e' la prova che il filtro sta scegliendo.
    print("\n  concentrazione rispetto al campione:")
    for liv in ("alta", "media", "bassa"):
        tipi = Counter(v.get("tipo") for v in voci if pagina.livello(v, soglia) == liv)
        if not tipi:
            print(f"      '{liv}': nessuna voce in questo giro")
            continue
        primo, quante = tipi.most_common(1)[0]
        quota = quante * 100 / sum(tipi.values())
        atteso = base[primo] * 100 / tot
        lift = quota / atteso if atteso else 0
        if liv == "alta":
            print(f"      '{liv}': {quota:.0f}% '{primo}' contro il {atteso:.0f}% "
                  f"del campione = {lift:.1f}x  (qui concentrarsi e' giusto)")
        else:
            ok = lift <= 1.4
            esiti.append(ok)
            print(f"  {'ok ' if ok else 'NO '} '{liv}': {quota:.0f}% '{primo}' "
                  f"contro il {atteso:.0f}% del campione = {lift:.2f}x")

    # ── 2 ────────────────────────────────────────────────────────────────
    print(f"\n{RIGA}\n2. QUANTO E' ARBITRARIO IL CONFINE MEDIA/BASSA?\n{RIGA}\n")
    punti = sorted(int(v.get("punteggio", 0)) for v in voci
                   if pagina.livello(v, soglia) != "alta")
    vicini = sum(1 for p in punti if abs(p - soglia) <= 5)
    quota = vicini * 100 // max(1, len(punti))
    print(f"  punteggi presenti: {sorted(set(punti))}")
    print(f"  voci entro +-5 dalla soglia: {vicini} su {len(punti)} ({quota}%)")
    print("\n  se la soglia fosse:")
    for s in (45, 50, 55, 60, 65):
        m = sum(1 for p in punti if p >= s)
        print(f"    {s}: media={m:>3}  bassa={len(punti) - m:>3}"
              + ("   <- attuale" if s == soglia else ""))
    print("\n  Questo confine e' MORBIDO per costruzione, e va letto come tale:")
    print("  distingue 'piu' o meno da scorrere', non fa un'affermazione")
    print("  sull'importanza. La riga che conta e' alta / non-alta, che non")
    print("  dipende da nessuna soglia: viene dalla decisione del filtro.")

    # ── 3 ────────────────────────────────────────────────────────────────
    print(f"\n{RIGA}\n3. CASI LA CUI IMPORTANZA NON E' OPINABILE\n{RIGA}\n")
    giudicati = []
    for nome, v, atteso in CASI:
        liv, g = classifica(v, cfg, soglia)
        ok = (liv == "alta") if atteso == "alta" else (liv != "alta")
        esiti.append(ok)
        giudicati.append((nome, liv, g, atteso))
        print(f"  {'ok ' if ok else 'NO '} {nome:<40} -> {liv:<6} ({g.punteggio:>3})")
        if not ok:
            print(f"       motivi: {' | '.join(g.motivi)}")

    print("\n  ordine, che decide cosa leggi per primo:")
    for nome, liv, g, _ in sorted(giudicati, key=lambda x: -x[2].punteggio):
        colore = {"alta": "ROSSO ", "media": "giallo", "bassa": "verde "}[liv]
        print(f"    {g.punteggio:>3}  {colore}  {nome}")
    alti = [g.punteggio for _, l, g, a in giudicati if a == "alta"]
    ok = min(alti) > max(g.punteggio for _, l, g, a in giudicati
                         if a != "alta" and l == "alta") if any(
        l == "alta" for _, l, g, a in giudicati if a != "alta") else True
    esiti.append(ok)
    print(f"  {'ok ' if ok else 'NO '} nessun rumore e' finito fra i rossi")

    # ── 4 ────────────────────────────────────────────────────────────────
    print(f"\n{RIGA}\n4. AFFIDABILITA'\n{RIGA}\n")
    uno = [classifica(v, cfg, soglia)[0] for _, v, _ in CASI]
    due = [classifica(v, cfg, soglia)[0] for _, v, _ in CASI]
    esiti.append(uno == due)
    print(f"  {'ok ' if uno == due else 'NO '} deterministico: due esecuzioni "
          f"danno lo stesso risultato su {len(CASI)} casi")

    for nome, a, b in [
        ("maiuscole", _voce(titolo="POWELL SAYS INFLATION RISKS SHIFTED"),
         _voce(titolo="Powell says inflation risks shifted")),
        ("spazi doppi", _voce(titolo="Powell  says   inflation risks"),
         _voce(titolo="Powell says inflation risks")),
    ]:
        la = classifica(a, cfg, soglia)[0]
        lb = classifica(b, cfg, soglia)[0]
        esiti.append(la == lb)
        print(f"  {'ok ' if la == lb else 'NO '} insensibile a: {nome} "
              f"({la} = {lb})")

    print(f"\n{RIGA}")
    print(f"{sum(esiti)}/{len(esiti)} controlli superati")
    print(RIGA)
    return 0 if all(esiti) else 1


if __name__ == "__main__":
    sys.exit(main())
