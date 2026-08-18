#!/usr/bin/env python3
"""
Il dashboard, generato come pagina statica.

Non c'e' nessun server. Ogni ciclo riscrive un file HTML completo, con i dati
gia' dentro. La scelta ha tre conseguenze pratiche, tutte volute:

  - la stessa pagina funziona su GitHub Pages, dentro un container sul NAS o
    aperta con un doppio clic dal disco. Dove gira il radar diventa una
    decisione reversibile invece di un vincolo architetturale;
  - non esiste superficie d'attacco. Un dashboard statico non ha endpoint,
    non ha query, non esegue niente lato server: al massimo qualcuno legge
    notizie che erano gia' pubbliche;
  - niente dipendenze esterne nella pagina. CSS e JavaScript sono dentro il
    file: nessun font remoto, nessuna libreria da CDN. Si apre anche senza
    rete, e nessuno puo' cambiare cio' che vedi modificando una risorsa
    altrove.

DUE LIVELLI, NON TRE
--------------------
  rosso   ha superato la soglia dell'interruzione: se non fosse notte ti
          sarebbe arrivato addosso.
  verde   tutto il resto.

Il livello viene dalla DECISIONE del filtro, non dal punteggio, ed e'
l'unica cosa con un significato univoco: un 60 su una notizia e un 60 su un
dato macro non vogliono dire la stessa cosa.

C'era un terzo livello intermedio ed e' stato tolto dopo averlo misurato.
Il confine cadeva a un punteggio fisso, ma il punteggio parte dal peso del
TIPO di fonte — 35 per un dato di calendario, 20 per una notizia — quindi
finiva per separare le fonti invece dell'importanza. In un campione vero,
"CFTC S&P 500 speculative net positions" stava in giallo a 55 e "Trump's
new global tariff draws rebukes from trade partners" in verde a 50: il
contrario di quello che un lettore si aspetta. Un livello che ordina al
rovescio e' peggio di un livello in meno.

Il punteggio resta e continua a ordinare le voci dentro ciascun livello:
li' fa il suo mestiere, perche' confronta cose omogenee.

I contenuti mostrati arrivano da fonti pubbliche e sono DATI: ogni testo
passa da `_e()` prima di finire nell'HTML. Un titolo che contiene "<script>"
deve apparire come testo, non essere eseguito.
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("pagina")

# VERSIONE — compare in alto sul dashboard.
#
# Va alzata a ogni modifica che cambia cosa vedi o come viene deciso. Serve a
# rispondere alla domanda che arriva sempre: "questa pagina e' gia' quella
# nuova o sto guardando la vecchia?". Senza un numero visibile, dopo un
# aggiornamento sul NAS non c'e' modo di saperlo se non a memoria.
#
# Storico, dalla piu' recente:
#   V2.1 18/08/2026  il dashboard mostrava 165 voci verdi. Due cause: una
#                    sola parola debole bastava a tenere una notizia — la
#                    regola dei due termini valeva solo per gli avvisi, non
#                    per cio' che si tiene, e 115 delle 148 notizie erano
#                    entrate cosi'. E le agenzie riscrivono lo stesso fatto:
#                    dodici titoli raccontavano il riprezzamento delle
#                    probabilita' di rialzo. Ora i titoli che condividono
#                    piu' di meta' delle parole significative diventano una
#                    voce sola, con scritto su quante fonti e' uscita.
#   V2.0 16/08/2026  il radar guarda anche i mercati, non solo i giornali.
#                    Fino a ieri sapeva soltanto cosa SCRIVEVANO le testate:
#                    non poteva dirti che l'oro si era mosso del 2%. Ora
#                    segue SPY, QQQ, NVDA e GLD da api.nasdaq.com — lo
#                    stesso host del calendario, nessuna chiave, nessun
#                    credito — e avvisa a fasce dell'1,5%. Quotazioni
#                    differite di ~15 minuti, ed e' scritto sulla pagina.
#                    Quattro fonti nuove: discorsi e testimonianze della
#                    Fed, Seeking Alpha, EIA energia.
#   V1.6 16/08/2026  meta' di cio' che il radar mostrava era roba archiviata.
#                    I feed non sono code di novita' ma archivi scorrevoli:
#                    su un giro reale, 86 voci su 150 avevano piu' di 36 ore
#                    e la piu' vecchia era del 13/05/2024. BCE e Fed erano il
#                    caso peggiore — tutti i loro comunicati risultavano
#                    presenti a ogni lettura, quindi una decisione di
#                    settimane prima poteva far scattare un avviso. Ora le
#                    voci RSS oltre le 72 ore vengono scartate alla fonte.
#   V1.5 15/08/2026  la pagina si aggiorna da sola. Essendo statica restava
#                    ferma una volta caricata: ora ogni 90 secondi chiede al
#                    server l'impronta del file (HEAD + ETag, poche decine di
#                    byte invece di 50 KB) e si ricarica se e' cambiata. Se
#                    stai leggendo a meta' pagina non ti strappa la lettura:
#                    compare un avviso da toccare.
#   V1.4 15/08/2026  tolto il livello intermedio. Il confine cadeva a un
#                    punteggio fisso, ma il punteggio parte dal peso del
#                    TIPO di fonte: separava le fonti, non l'importanza.
#                    Misurato: "CFTC S&P 500 speculative net positions" in
#                    giallo a 55, "Trump's new global tariff draws rebukes"
#                    in verde a 50. Restano due livelli: urgente e il resto.
#   V1.3 14/08/2026  la classificazione era tarata male: 97 avvisi in un
#                    giorno, 89 dei quali titoli di cronaca finanziaria.
#                    Causa: l'alias "SPY -> s&p 500" faceva scattare la
#                    watchlist su ogni articolo di borsa, e bastava un solo
#                    segnale per interrompere. Ora una testata interrompe
#                    solo nominando una societa' seguita insieme a un fatto,
#                    e nel calendario solo gli indicatori di primo piano.
#                    Di notte passano solo le banche centrali, e gli arretrati
#                    non vengono piu' riversati alle 07:30.
#   V1.2 14/08/2026  segnalatore di stato in cima: la freschezza della
#                    pagina la misura il browser, non il generatore, cosi'
#                    una pagina ferma riesce a dichiararsi ferma. Stato dei
#                    due canali di uscita, con la causa in chiaro quando
#                    uno si rompe.
#   V1.1 14/08/2026  il token GitHub non arrivava al container e la
#                    pubblicazione taceva; il registro dichiarava
#                    "silenzio: False" di notte quando non c'erano avvisi.
#   V1   13/08/2026  prima versione completa: dieci fonti, filtro a tre
#                    livelli, panoramica del mattino, avvisi Telegram con
#                    silenzio notturno.
VERSIONE = "V2.1"

ETICHETTA = {
    "evento": "dato macro", "ufficiale": "banca centrale", "deposito": "deposito SEC",
    "notizia": "notizia", "trimestrale": "trimestrale", "forum": "forum",
    "prezzo": "movimento di prezzo",
}
SIMBOLO = {"evento": "📊", "ufficiale": "🏛", "deposito": "📄",
           "notizia": "📰", "trimestrale": "💵", "forum": "💬",
           "prezzo": "📈"}

LIVELLI = {
    "alta": ("Urgenti", "Meritavano di interromperti"),
    "resto": ("Il resto", "Contesto, da scorrere quando vuoi"),
}

STILE = """
:root{
  --sf:#0f1115; --ca:#171a21; --ca2:#1c2029; --bo:#252a34;
  --te:#e6e9ef; --gr:#8b93a7; --ac:#5b9dff;
  --alta:#ff5c4d; --resto:#4ade80; --tarda:#f5c542;
}
*{box-sizing:border-box}
body{margin:0;padding:16px 14px 40px;background:var(--sf);color:var(--te);
     font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
     -webkit-text-size-adjust:100%}
.guscio{max-width:760px;margin:0 auto}
h1{font-size:20px;margin:0 0 3px;letter-spacing:-.2px}
.ver{font-size:11px;font-weight:600;letter-spacing:.7px;color:var(--gr);
     border:1px solid var(--bo);border-radius:5px;padding:2px 6px;
     vertical-align:middle;margin-left:5px}
.sotto{color:var(--gr);font-size:12.5px;margin-bottom:18px}

/* ---- contatori, uno per livello ---- */
.conta{display:flex;gap:8px;margin-bottom:22px}
.conta div{background:var(--ca);border:1px solid var(--bo);border-radius:11px;
           padding:9px 6px;flex:1 1 0;text-align:center;border-top:2px solid var(--bo)}
.conta div.alta{border-top-color:var(--alta)}
.conta div.resto{border-top-color:var(--resto)}
.conta b{display:block;font-size:21px;line-height:1.2}
.conta div.alta b{color:var(--alta)}
.conta div.resto b{color:var(--resto)}
.conta span{color:var(--gr);font-size:10.5px;text-transform:uppercase;letter-spacing:.6px}

h2{font-size:12.5px;text-transform:uppercase;letter-spacing:1.1px;color:var(--gr);
   margin:26px 0 11px;border-bottom:1px solid var(--bo);padding-bottom:7px;
   display:flex;justify-content:space-between;align-items:baseline;gap:10px}
h2 em{font-style:normal;font-size:11px;text-transform:none;letter-spacing:0;
      color:var(--gr);opacity:.75;font-weight:400}

/* ---- striscia dei prezzi ---- */
.prezzi{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
.prezzi div{background:var(--ca);border:1px solid var(--bo);border-radius:10px;
            padding:9px 12px;flex:1 1 120px;min-width:0}
.prezzi .nome{color:var(--gr);font-size:11px;text-transform:uppercase;
              letter-spacing:.5px;white-space:nowrap;overflow:hidden;
              text-overflow:ellipsis}
.prezzi .val{font-size:16px;font-weight:640;font-variant-numeric:tabular-nums;
             margin-top:2px}
.prezzi .var{font-size:12.5px;font-variant-numeric:tabular-nums;margin-left:6px;
             font-weight:600}
.prezzi .su{color:var(--resto)}
.prezzi .giu{color:var(--alta)}
.prezzi .fermo{color:var(--gr)}
.prezzi .nota{color:var(--gr);font-size:10.5px;margin-top:2px}

/* ---- segnalatore di stato ---- */
.stato{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
       background:var(--ca);border:1px solid var(--bo);border-radius:11px;
       padding:11px 14px;margin-bottom:18px;font-size:13px}
.stato .spia{width:9px;height:9px;border-radius:50%;flex:0 0 auto;
             background:var(--gr);box-shadow:0 0 0 3px transparent}
.stato.viva .spia{background:var(--resto);box-shadow:0 0 0 3px rgba(74,222,128,.15);
                  animation:battito 2.6s ease-in-out infinite}
.stato.tarda .spia{background:var(--tarda);box-shadow:0 0 0 3px rgba(245,197,66,.15)}
.stato.ferma .spia{background:var(--alta);box-shadow:0 0 0 3px rgba(255,92,77,.15)}
@keyframes battito{0%,100%{opacity:1}50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){.stato.viva .spia{animation:none}}
.stato .titolo{font-weight:640;letter-spacing:.01em}
.stato.viva .titolo{color:var(--resto)}
.stato.tarda .titolo{color:var(--tarda)}
.stato.ferma .titolo{color:var(--alta)}
.stato .eta{color:var(--gr);font-variant-numeric:tabular-nums}
.stato .canali{margin-left:auto;display:flex;gap:12px;flex-wrap:wrap;
               color:var(--gr);font-size:12px}
.stato .canali b{font-weight:600}
.stato .canali .ko{color:var(--alta)}
.stato .canali .ok{color:var(--resto)}
.guasto{background:rgba(255,92,77,.09);border:1px solid var(--alta);
        border-radius:10px;padding:11px 14px;margin-bottom:18px;font-size:13.5px}
.guasto b{color:var(--alta)}

/* ---- riepilogo ---- */
.riep{background:var(--ca2);border:1px solid var(--bo);border-radius:12px;
      padding:15px 16px;line-height:1.62;font-size:14.5px}
.riep p{margin:0 0 11px}
.riep p:last-child{margin-bottom:0}
.riep .fonte{color:var(--gr);font-size:11.5px;margin-top:13px;
             border-top:1px solid var(--bo);padding-top:9px}

/* ---- una voce ---- */
.v{background:var(--ca);border:1px solid var(--bo);border-radius:10px;
   padding:11px 13px 11px 15px;margin-bottom:8px;position:relative;overflow:hidden}
.v::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px}
.v.alta::before{background:var(--alta)}
.v.resto::before{background:var(--resto)}
.cap{display:flex;align-items:center;gap:7px;margin-bottom:6px;
     font-size:10.5px;text-transform:uppercase;letter-spacing:.55px;color:var(--gr)}
.cap .punto{width:7px;height:7px;border-radius:50%;flex:0 0 auto}
.v.alta .cap .punto{background:var(--alta)}
.v.resto .cap .punto{background:var(--resto)}
.cap .quando{margin-left:auto;text-transform:none;letter-spacing:0;
             font-variant-numeric:tabular-nums;white-space:nowrap}
.t{font-weight:600;font-size:14.5px;line-height:1.4;overflow-wrap:anywhere}
.t a{color:inherit;text-decoration:none}
.t a:hover{color:var(--ac)}
.cifre{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.cifre span{background:var(--sf);border:1px solid var(--bo);border-radius:6px;
            padding:2px 8px;font-size:12px;color:var(--gr);
            font-variant-numeric:tabular-nums}
.cifre span b{color:var(--te);font-weight:600}
.perche{color:var(--gr);font-size:11.5px;margin-top:8px;overflow-wrap:anywhere}

/* ---- filtri ---- */
.f{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:13px}
.f button{background:var(--ca);color:var(--gr);border:1px solid var(--bo);
          border-radius:16px;padding:6px 13px;font-size:12.5px;cursor:pointer;
          font-family:inherit;display:flex;align-items:center;gap:6px}
.f button i{font-style:normal;opacity:.6;font-size:11px;
            font-variant-numeric:tabular-nums}
.f button.on{background:var(--ac);color:#fff;border-color:var(--ac)}
.f button.on i{opacity:.85}
.vuoto{color:var(--gr);font-style:italic;padding:10px 0;font-size:13.5px}
.aggiorna{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;
          background:var(--ac);color:#fff;border:none;border-radius:20px;
          padding:11px 18px;font:600 13px/1 inherit;cursor:pointer;
          box-shadow:0 6px 20px rgba(0,0,0,.45);z-index:9}
.aggiorna:active{transform:translateX(-50%) scale(.97)}
footer{color:var(--gr);font-size:11.5px;margin-top:34px;border-top:1px solid var(--bo);
       padding-top:13px;line-height:1.6}
"""

SCRIPT = """
/* Il segnalatore lo decide il BROWSER, non il generatore.
   Una pagina ferma non puo' scriversi da sola "sono ferma": se il radar si
   spegne, l'ultima pagina che ha prodotto continuera' a dire che va tutto
   bene, e sara' una bugia che invecchia. Qui invece si confronta l'ora di
   generazione, incisa nella pagina, con l'ora di chi la sta guardando: la
   diagnosi si fa al momento della lettura, e vale anche se il file e' fermo
   da giorni. */
(function(){
  var el = document.getElementById('stato');
  if (!el) return;
  var nato = new Date(el.dataset.quando);
  var soglia = parseFloat(el.dataset.soglia || '45');

  function aggiorna(){
    var min = (Date.now() - nato.getTime()) / 60000;
    var eta = min < 1.5 ? 'adesso'
            : min < 60  ? 'di ' + Math.round(min) + ' minuti fa'
            : min < 48*60 ? 'di ' + Math.round(min/60) + ' ore fa'
            : 'di ' + Math.round(min/1440) + ' giorni fa';
    var classe, titolo;
    if (min < soglia)        { classe='viva';  titolo='In linea'; }
    else if (min < soglia*4) { classe='tarda'; titolo='In ritardo'; }
    else                     { classe='ferma'; titolo='Non aggiornato'; }
    el.className = 'stato ' + classe;
    el.querySelector('.titolo').textContent = titolo;
    el.querySelector('.eta').textContent = 'lettura ' + eta;
    /* Orologio del lettore indietro rispetto alla generazione: succede quando
       il fuso del dispositivo e' sballato. Meglio dirlo che dare una diagnosi
       basata su un tempo negativo. */
    if (min < -5) {
      el.className = 'stato';
      el.querySelector('.titolo').textContent = 'Ora incoerente';
      el.querySelector('.eta').textContent = "l'orologio di questo dispositivo "
        + 'sembra indietro';
    }
  }
  aggiorna();
  setInterval(aggiorna, 30000);
})();

/* AGGIORNAMENTO AUTOMATICO
   Una pagina statica, una volta caricata, resta ferma per sempre: il NAS ne
   pubblica una nuova ogni pochi minuti e il browser continua a mostrare
   quella vecchia. Qui la pagina si controlla da sola.

   Si usa una richiesta HEAD e si confronta l'ETag, cioe' l'impronta che il
   server associa al file: sono poche decine di byte a giro, contro i ~50 KB
   che costerebbe riscaricare tutta la pagina per accorgersi che non e'
   cambiata. Se l'ETag non fosse disponibile si ripiega sulla data di
   ultima modifica.

   Il ricaricamento non e' mai brusco: se stai leggendo a meta' pagina
   compare un avviso da toccare, invece di strapparti la lettura. */
(function(){
  var iniziale = null, avvisato = false;

  function impronta(r){
    return r.headers.get('etag') || r.headers.get('last-modified') || null;
  }

  function mostraAvviso(){
    if (avvisato) return;
    avvisato = true;
    var b = document.createElement('button');
    b.className = 'aggiorna';
    b.textContent = '↻ Nuovi dati disponibili — tocca per aggiornare';
    b.onclick = function(){ location.reload(); };
    document.body.appendChild(b);
  }

  function controlla(){
    fetch(location.pathname + '?_=' + Date.now(), {method:'HEAD', cache:'no-store'})
      .then(function(r){
        var ora = impronta(r);
        if (!ora) return;
        if (iniziale === null) { iniziale = ora; return; }
        if (ora === iniziale) return;
        /* Se la pagina non e' sotto gli occhi di nessuno, o se sei in cima
           e quindi non stai leggendo un punto preciso, si ricarica e basta. */
        if (document.hidden || window.scrollY < 300) {
          /* Freno contro il ricaricamento a ripetizione. GitHub Pages
             dichiara max-age=600: se dopo il ricaricamento la rete servisse
             ancora la copia vecchia, la pagina troverebbe di nuovo l'ETag
             diverso e ripartirebbe, all'infinito. Un giro al minuto al
             massimo: se il contenuto nuovo non arriva, resta l'avviso da
             toccare. */
          var ultimo = 0;
          try { ultimo = parseInt(sessionStorage.getItem('radar-ricarica')||'0',10); }
          catch(e){}
          if (Date.now() - ultimo < 60000) { mostraAvviso(); return; }
          try { sessionStorage.setItem('radar-ricarica', String(Date.now())); }
          catch(e){}
          location.reload();
        } else {
          mostraAvviso();
        }
      })
      .catch(function(){ /* rete assente: si riprova al giro dopo */ });
  }

  controlla();
  setInterval(controlla, 90000);
  /* Tornando sulla scheda dopo un po', il controllo e' subito. */
  document.addEventListener('visibilitychange', function(){
    if (!document.hidden) controlla();
  });
})();

document.querySelectorAll('.f button').forEach(b=>{
  b.onclick=()=>{
    const q=b.dataset.q;
    b.parentElement.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
    let n=0;
    document.querySelectorAll('#diario .v').forEach(v=>{
      const ok=(q==='*'||v.dataset.tipo===q);
      v.style.display=ok?'':'none'; if(ok)n++;
    });
    document.getElementById('nessuno').style.display=n?'none':'';
    try{ sessionStorage.setItem('radar-filtro', q); }catch(e){}
  };
});

/* Il filtro scelto sopravvive al ricaricamento automatico: senza, ogni
   aggiornamento ti riporterebbe a "tutto" mentre stavi guardando i dati
   macro. Va DOPO il ciclo qui sopra: prima che i gestori siano agganciati,
   un click() non farebbe niente. */
(function(){
  var salvato = null;
  try{ salvato = sessionStorage.getItem('radar-filtro'); }catch(e){}
  if (!salvato || salvato === '*') return;
  var b = document.querySelector('.f button[data-q="' + salvato + '"]');
  if (b) b.click();
})();
"""


def _e(t) -> str:
    return html.escape(str(t if t is not None else ""), quote=True)


def livello(voce: dict, _soglia_non_usata: int = 0) -> str:
    """Urgente o no. Dalla decisione del filtro, mai dal punteggio.

    Due soli valori, di proposito. Un avviso e' urgente perche' ha superato
    una soglia pensata per l'interruzione; tutto il resto e' contesto. Un
    livello intermedio esisteva e ordinava al rovescio: il punteggio parte
    dal peso del TIPO di fonte, quindi un dato di calendario marginale
    scavalcava una notizia sostanziale, e il colore raccontava da dove
    veniva la voce invece di quanto contasse.

    Il parametro resta nella firma solo per non rompere i richiami esistenti
    ed e' volutamente ignorato.
    """
    return "alta" if (voce.get("esito") == "avviso"
                      or voce.get("trattenuto")) else "resto"


def _ora(voce: dict, fuso: ZoneInfo) -> str:
    for campo in ("quando", "archiviata"):
        try:
            return datetime.fromisoformat(voce[campo]).astimezone(fuso).strftime("%d/%m %H:%M")
        except (KeyError, ValueError, TypeError):
            continue
    return ""


def _voce(v: dict, fuso: ZoneInfo, liv: str) -> str:
    d = v.get("dati") or {}
    titolo = _e(v.get("titolo"))
    if v.get("url"):
        # rel="noopener noreferrer": la pagina di destinazione non deve poter
        # toccare questa. Vale anche per un dashboard personale.
        titolo = (f'<a href="{_e(v["url"])}" target="_blank" '
                  f'rel="noopener noreferrer">{titolo}</a>')

    tipo = v.get("tipo", "")
    cappello = [
        '<div class="cap"><span class="punto"></span>',
        f'<span>{SIMBOLO.get(tipo, "•")} {ETICHETTA.get(tipo, tipo)}</span>',
        f'<span>· {_e(v.get("fonte"))}</span>',
        f'<span class="quando">{_ora(v, fuso)}</span></div>',
    ]

    pezzi = [f'<div class="v {liv}" data-liv="{liv}" data-tipo="{_e(tipo)}">',
             "".join(cappello), f'<div class="t">{titolo}</div>']

    if tipo == "evento":
        cifre = []
        if d.get("paese"):
            cifre.append(f"<span>{_e(d['paese'])}</span>")
        if d.get("effettivo") not in (None, ""):
            cifre.append(f"<span>uscito <b>{_e(d['effettivo'])}</b></span>")
        if d.get("atteso") not in (None, ""):
            cifre.append(f"<span>atteso <b>{_e(d['atteso'])}</b></span>")
        if d.get("precedente") not in (None, ""):
            cifre.append(f"<span>prima {_e(d['precedente'])}</span>")
        if cifre:
            pezzi.append(f'<div class="cifre">{"".join(cifre)}</div>')

    if v.get("simili"):
        n = int(v["simili"]) + 1
        pezzi.append(f'<div class="cifre"><span>ripresa da <b>{n}</b> fonti</span>'
                     f'</div>')
    if v.get("motivi"):
        pezzi.append(f'<div class="perche">{_e(" · ".join(v["motivi"]))}</div>')
    pezzi.append("</div>")
    return "".join(pezzi)


def _riepilogo(testo: str, fuso: ZoneInfo) -> str:
    """Il riassunto, spezzato in paragrafi.

    Arriva come testo con i ritorni a capo del modello. Va protetto (e' pur
    sempre generato a partire da contenuto di terzi) e poi ricomposto in
    paragrafi: un blocco unico di dieci righe su un telefono non lo legge
    nessuno.
    """
    testo = re.sub(r"</?b>", "", testo or "")          # via i marcatori Telegram
    testo = re.sub(r"^\s*PANORAMICA DEL MATTINO\s*", "", testo).strip()
    if not testo:
        return ""
    blocchi = [b.strip() for b in re.split(r"\n\s*\n", testo) if b.strip()]
    return "".join(f"<p>{_e(b)}</p>" for b in blocchi)


def _prezzi(quotazioni: list[dict]) -> str:
    """La striscia dei prezzi, in cima.

    E' l'unica parte della pagina che non parla di notizie ma di mercati, e
    serve a rispondere alla domanda che viene prima di tutte: "si e' mosso
    qualcosa?". Le notizie qui sotto dicono poi perche'.

    Il ritardo va scritto, non sottinteso: una quotazione differita di un
    quarto d'ora presentata come attuale sarebbe un'informazione falsa in un
    contesto dove i minuti contano.
    """
    if not quotazioni:
        return ""
    fuori = ['<div class="prezzi">']
    differite = False
    for q in quotazioni:
        v = float(q.get("variazione") or 0)
        classe = "su" if v > 0.005 else ("giu" if v < -0.005 else "fermo")
        segno = "+" if v > 0 else ""
        differite = differite or bool(q.get("differito"))
        chiuso = "" if q.get("aperto") else ' <span class="nota">· chiuso</span>'
        fuori.append(
            f'<div><div class="nome">{_e(q.get("nome") or q.get("simbolo"))}</div>'
            f'<div class="val">{float(q.get("prezzo") or 0):,.2f}'
            f'<span class="var {classe}">{segno}{v:.2f}%</span></div>'
            f'<div class="nota">{_e(q.get("simbolo"))}{chiuso}</div></div>')
    fuori.append("</div>")
    if differite:
        fuori.append('<div class="sotto" style="margin-top:-12px">'
                     'Quotazioni differite di circa 15 minuti: servono a sapere '
                     'che qualcosa si è mosso, non a entrare su un movimento '
                     'mentre accade.</div>')
    return "".join(fuori)


def _segnalatore(adesso: datetime, salute: dict, cfg: dict) -> str:
    """La striscia di stato in cima, piu' un riquadro se qualcosa e' rotto.

    La classe iniziale e' volutamente neutra: il colore lo assegna il
    browser dopo aver misurato l'eta' della pagina. Se il JavaScript non
    girasse, si vedrebbe uno stato spento invece di un verde bugiardo.
    """
    soglia = float((cfg.get("dashboard") or {}).get("minuti_prima_di_ritardo", 45))
    righe = [f'<div class="stato" id="stato" data-quando="{adesso.isoformat()}" '
             f'data-soglia="{soglia:g}">',
             '<span class="spia"></span>',
             '<span class="titolo">Verifica…</span>',
             '<span class="eta"></span>',
             '<span class="canali">']

    for nome, etichetta in (("telegram", "Telegram"), ("github", "GitHub")):
        s = (salute or {}).get(nome) or {}
        rotto = int(s.get("falliti_di_fila", 0)) > 0
        classe = "ko" if rotto else ("ok" if s.get("ultimo_ok") else "")
        simbolo = "✕" if rotto else ("✓" if s.get("ultimo_ok") else "–")
        righe.append(f'<span class="{classe}">{etichetta} <b>{simbolo}</b></span>')
    righe.append("</span></div>")

    # Un canale rotto merita una spiegazione, non un simbolo. Vale soprattutto
    # per Telegram: se e' lui a non funzionare, questa pagina e' l'unico posto
    # in cui puoi venirlo a sapere.
    for nome, etichetta in (("telegram", "Gli avvisi non partono"),
                            ("github", "La pagina non si aggiorna online")):
        s = (salute or {}).get(nome) or {}
        n = int(s.get("falliti_di_fila", 0))
        if n <= 0:
            continue
        motivo = _e(s.get("motivo") or "motivo non riportato")
        coda = ("Il radar continua a raccogliere e filtrare: si e' rotto "
                "solo il canale." if nome == "telegram" else
                "Gli avvisi su Telegram continuano ad arrivare.")
        righe.append(f'<div class="guasto"><b>{etichetta}</b> — da {n} '
                     f'{"giro" if n == 1 else "giri"}: {motivo}. {coda}</div>')
    return "".join(righe)


def costruisci(voci: list[dict], cfg: dict, quando: datetime | None = None,
               riepilogo: dict | None = None, salute: dict | None = None,
               quotazioni: list[dict] | None = None) -> str:
    """L'intera pagina come stringa. Nessun file toccato: cosi' e' provabile."""
    try:
        fuso = ZoneInfo(str(((cfg.get("avvisi") or {}).get("silenzio") or {})
                            .get("fuso", "Europe/Rome")))
    except Exception:
        fuso = ZoneInfo("UTC")
    adesso = (quando or datetime.now(timezone.utc)).astimezone(fuso)
    for v in voci:
        v["_liv"] = livello(v)
    ordine = {"alta": 0, "resto": 1}
    voci.sort(key=lambda v: (ordine[v["_liv"]], -int(v.get("punteggio", 0)),
                             v.get("archiviata", "")), reverse=False)

    alte = [v for v in voci if v["_liv"] == "alta"]
    conte = {liv: sum(1 for v in voci if v["_liv"] == liv) for liv in LIVELLI}

    corpo = ['<div class="guscio">',
             f'<h1>📡 Radar notizie <span class="ver">{_e(VERSIONE)}</span></h1>',
             f'<div class="sotto">generata {adesso.strftime("%d/%m/%Y alle %H:%M")}'
             f' · voci raccolte nelle ultime 24 ore</div>',
             _segnalatore(adesso, salute or {}, cfg),
             _prezzi(quotazioni or []),
             '<div class="conta">']
    for liv, (nome, _) in LIVELLI.items():
        corpo.append(f'<div class="{liv}"><b>{conte[liv]}</b>'
                     f'<span>{nome.lower()}</span></div>')
    corpo.append("</div>")

    # ---- Riepilogo ----------------------------------------------------
    r = riepilogo or {}
    corpo.append("<h2>Riepilogo</h2>")
    testo = _riepilogo(r.get("testo", ""), fuso)
    if testo:
        firma = []
        try:
            q = datetime.fromisoformat(r["quando"]).astimezone(fuso)
            firma.append(q.strftime("scritto il %d/%m alle %H:%M"))
        except (KeyError, ValueError, TypeError):
            pass
        # La spesa in chiaro, non una stima: e' l'unico modo per accorgersi
        # se una previsione di costo era sbagliata.
        if r.get("costo"):
            firma.append(f'{float(r["costo"]) * 100:.1f} centesimi')
        elif r.get("dal_modello") is False:
            firma.append("scritto senza modello, costo zero")
        firma.append("Stesso testo che arriva su Telegram alle "
                     f"{_e((cfg.get('panoramica') or {}).get('ora', '07:30'))}, "
                     "una volta al giorno. Gli avvisi qui sopra sono invece "
                     "riportati come arrivano dalla fonte, senza parafrasi.")
        corpo.append(f'<div class="riep">{testo}'
                     f'<div class="fonte">{_e(" · ".join(firma))}</div></div>')
    else:
        corpo.append('<div class="vuoto">Il riepilogo compare con la panoramica '
                     'del mattino. Fino ad allora quello che succede lo trovi '
                     'qui sotto, negli avvisi.</div>')

    # ---- Importanza alta ------------------------------------------------
    corpo.append('<h2>Urgenti <em>meritavano di interromperti</em></h2>')
    corpo.append("".join(_voce(v, fuso, "alta") for v in alte)
                 or '<div class="vuoto">Niente di urgente nelle ultime 24 ore. '
                    'È una buona notizia, non un guasto.</div>')

    # ---- Tutto il resto, filtrabile --------------------------------------
    resto = [v for v in voci if v["_liv"] != "alta"]
    corpo.append('<h2>Il resto <em>contesto, in ordine di rilevanza</em></h2>')
    # Il filtro e' per tipo di fonte, non per livello: il livello si vede gia'
    # a colpo d'occhio dal colore di ogni scheda, mentre "fammi vedere solo i
    # dati macro" e' una domanda che il colore non sa rispondere.
    per_tipo: dict[str, int] = {}
    for v in resto:
        per_tipo[v.get("tipo", "")] = per_tipo.get(v.get("tipo", ""), 0) + 1
    bottoni = [f'<button class="on" data-q="*">tutto <i>{len(resto)}</i></button>']
    for tipo, quante in sorted(per_tipo.items(), key=lambda x: -x[1]):
        if not tipo:
            continue
        bottoni.append(f'<button data-q="{_e(tipo)}">{SIMBOLO.get(tipo, "•")} '
                       f'{ETICHETTA.get(tipo, tipo)} <i>{quante}</i></button>')
    corpo.append(f'<div class="f">{"".join(bottoni)}</div>')
    corpo.append('<div id="diario">')
    corpo.append("".join(_voce(v, fuso, v["_liv"]) for v in resto)
                 or '<div class="vuoto">Niente da mostrare.</div>')
    corpo.append('<div class="vuoto" id="nessuno" style="display:none">'
                 'Nessuna voce di questo tipo.</div>')
    corpo.append("</div>")

    corpo.append(
        "<footer>🔴 urgente: ha superato la soglia dell'interruzione, e su "
        "Telegram \u00e8 arrivato subito · 🟢 il resto: contesto, ordinato "
        "per rilevanza.<br>"
        "Le voci arrivano da fonti pubbliche e sono riportate come sono. "
        "Questo strumento non dà consigli operativi e non esegue nessuna "
        "operazione: raccoglie, filtra e mostra.</footer></div>")

    return (
        "<!doctype html>\n<html lang='it'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex'>"
        "<meta name='color-scheme' content='dark'>"
        "<title>Radar notizie</title>"
        f"<style>{STILE}</style></head><body>"
        + "".join(corpo)
        + f"<script>{SCRIPT}</script></body></html>"
    )


def scrivi(voci: list[dict], cfg: dict, cartella: Path,
           riepilogo: dict | None = None, salute: dict | None = None,
           quotazioni: list[dict] | None = None) -> Path:
    """Scrive index.html e dati.json. Restituisce il percorso della pagina."""
    cartella = Path(cartella)
    cartella.mkdir(parents=True, exist_ok=True)

    pagina = cartella / "index.html"
    pagina.write_text(costruisci(voci, cfg, riepilogo=riepilogo, salute=salute,
                                 quotazioni=quotazioni), encoding="utf-8")

    # I dati anche in JSON: se un domani vuoi farci un grafico o leggerli da
    # un altro programma, non devi rileggerli dall'HTML.
    (cartella / "dati.json").write_text(
        json.dumps({"aggiornato": datetime.now(timezone.utc).isoformat(),
                    "riepilogo": riepilogo or {}, "salute": salute or {},
                    "quotazioni": quotazioni or [],
                    "voci": voci}, ensure_ascii=False),
        encoding="utf-8")

    # GitHub Pages passa i file per Jekyll, che ignora tutto cio' che comincia
    # per underscore e a volte rielabora l'HTML. Questo file vuoto lo disattiva.
    (cartella / ".nojekyll").write_text("")

    log.info("pagina scritta: %s (%d voci)", pagina, len(voci))
    return pagina


if __name__ == "__main__":
    # 1) Il contenuto ostile va neutralizzato, non eseguito.
    ostile = [{
        "tipo": "notizia", "esito": "avviso", "punteggio": 70,
        "titolo": "<script>alert('x')</script> & <b>grassetto</b>",
        "fonte": "Fonte \"strana\"", "url": "https://esempio.test/?a=1&b=2",
        "quando": datetime.now(timezone.utc).isoformat(),
        "motivi": ["<img src=x onerror=alert(1)>"], "dati": {},
    }]
    h = costruisci(ostile, {}, riepilogo={"testo": "Riassunto <img src=x> ostile.",
                                          "quando": datetime.now(timezone.utc).isoformat()})
    # La proprieta' che conta e' una sola: nessuna parentesi angolare venuta
    # dai dati deve sopravvivere. La parola "onerror" puo' benissimo restare
    # nel testo — senza il "<" non e' un tag, e' una stringa. Cercare la
    # parola invece del tag vorrebbe dire controllare la cosa sbagliata.
    prove = [
        ("nessun tag script eseguibile dal titolo", "<script>alert" not in h),
        ("il titolo appare come testo", "&lt;script&gt;" in h),
        ("la e commerciale e' protetta", "&amp;" in h),
        ("nessun tag img iniettato dai motivi", "<img" not in h),
        ("nemmeno dal riepilogo", "&lt;img src=x&gt; ostile" in h),
        ("i link esterni sono isolati", 'rel="noopener noreferrer"' in h),
    ]

    # 2) I livelli devono seguire la decisione del filtro, non il punteggio.
    casi = [
        ("un avviso e' sempre urgente", {"esito": "avviso", "punteggio": 20}, "alta"),
        ("non inviato di notte resta urgente", {"esito": "diario",
                                                "trattenuto": True,
                                                "punteggio": 10}, "alta"),
        # Il punteggio non promuove piu' niente: era il difetto del vecchio
        # livello intermedio, che finiva per separare i tipi di fonte.
        ("punteggio alto ma non avviso: resto", {"esito": "diario",
                                                 "punteggio": 90}, "resto"),
        ("punteggio basso: resto", {"esito": "diario", "punteggio": 30}, "resto"),
    ]
    for nome, v, voluto in casi:
        prove.append((nome, livello(v) == voluto))

    # 3) Il segnalatore non deve mai partire "verde".
    sano = {"telegram": {"ultimo_ok": "x", "falliti_di_fila": 0},
            "github": {"ultimo_ok": "x", "falliti_di_fila": 0}}
    s = costruisci(ostile, {}, salute=sano)
    prove += [
        ("lo stato parte neutro, non verde",
         'class="stato" id="stato"' in s and "stato viva" not in s),
        ("la pagina incide l'ora di generazione",
         bool(re.search(r'data-quando="\d{4}-\d\d-\d\dT', s))),
        ("il browser riceve la soglia", 'data-soglia="45"' in s),
        ("nessun riquadro di guasto quando va tutto bene",
         'class="guasto"' not in s),
    ]

    rotto = {"telegram": {"falliti_di_fila": 3, "motivo": "<b>ostile</b> & rotto"},
             "github": {"ultimo_ok": "x", "falliti_di_fila": 0}}
    r = costruisci(ostile, {}, salute=rotto)
    prove += [
        ("un canale rotto compare col suo motivo",
         'class="guasto"' in r and "da 3 giri" in r),
        ("anche il motivo di guasto viene protetto",
         "&lt;b&gt;ostile&lt;/b&gt;" in r),
        ("il guasto di Telegram spiega che il radar continua",
         "si e' rotto solo il canale" in r or "solo il canale" in r),
    ]

    # 4) Aggiornamento automatico: i pezzi che lo reggono devono esserci.
    prove += [
        ("la pagina si ricontrolla da sola", "setInterval(controlla, 90000)" in s),
        ("usa HEAD, non riscarica tutto", "method:'HEAD'" in s),
        ("aggira la cache con un parametro", "'?_=' + Date.now()" in s),
        ("confronta ETag, con ripiego su last-modified",
         "headers.get('etag')" in s and "last-modified" in s),
        ("non ricarica se stai leggendo a meta' pagina",
         "window.scrollY < 300" in s and "mostraAvviso" in s),
        ("freno contro il ricaricamento a ripetizione",
         "radar-ricarica" in s and "60000" in s),
        ("ricontrolla tornando sulla scheda", "visibilitychange" in s),
        ("il filtro scelto sopravvive al ricaricamento",
         "radar-filtro" in s),
    ]

    for nome, ok in prove:
        print(f"  {'ok ' if ok else 'NO '} {nome}")
    print(f"\n{sum(1 for _, o in prove if o)}/{len(prove)} controlli superati")
    raise SystemExit(0 if all(o for _, o in prove) else 1)
