#!/usr/bin/env python3
"""
Consegna: Telegram e ore di silenzio.

Il filtro decide COSA merita un'interruzione. Questo file decide SE e' il
momento di farla. Sono due domande diverse: una decisione della BCE alle tre
del mattino e' importantissima e, se non fai trading notturno, del tutto
inutile da ricevere in quel momento. Un avviso che arriva quando non puoi
farci niente non ti informa: ti insegna a ignorare le notifiche, ed e' cosi'
che questi strumenti muoiono.

Di notte gli avvisi non spariscono. Finiscono nell'archivio e li ritrovi
nella panoramica delle 07:30, segnalati come arrivati durante il silenzio.

Il testo che arriva da qui viene da titoli di giornale e comunicati: e' DATO,
non istruzione. Va anche protetto prima dell'invio, perche' Telegram
interpreta l'HTML e un titolo che contiene "<b>" o "&" romperebbe il
messaggio — o peggio, ne cambierebbe la formattazione in modo imprevisto.
"""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import datetime, time as ora_del_giorno
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("avvisi")

# Un messaggio Telegram non puo' superare i 4096 caratteri. Si taglia prima,
# a un margine di sicurezza, invece di farsi rifiutare l'invio.
MAX_TESTO = 3900

SIMBOLO = {
    "evento": "📊",
    "ufficiale": "🏛",
    "deposito": "📄",
    "notizia": "📰",
    "trimestrale": "💵",
    "forum": "💬",
}


def _e(testo) -> str:
    """Rende sicuro un testo per l'HTML di Telegram."""
    return html.escape(str(testo or ""), quote=False)


# I rifiuti di Telegram sono precisi ma criptici. Tradurli qui significa
# che il dashboard mostrera' la causa invece del codice, e la causa dice
# gia' cosa fare.
SPIEGAZIONI = {
    "CHAT_SEND_PLAIN_FORBIDDEN":
        "il gruppo vieta al bot i messaggi di testo: rendilo amministratore",
    "upgraded to a supergroup":
        "il gruppo e' diventato un supergruppo e ha cambiato identificativo",
    "chat not found":
        "destinatario inesistente: identificativo sbagliato",
    "bot was blocked":
        "il bot e' stato bloccato dal destinatario",
    "bot was kicked":
        "il bot e' stato rimosso dal gruppo",
    "not enough rights":
        "al bot mancano i permessi per scrivere",
}


def _motivo(risposta) -> str:
    """Il rifiuto di Telegram in italiano comprensibile."""
    try:
        grezzo = str((risposta.json() or {}).get("description", ""))
    except ValueError:
        grezzo = risposta.text[:120]
    for chiave, spiegazione in SPIEGAZIONI.items():
        if chiave.lower() in grezzo.lower():
            return spiegazione
    return grezzo[:120] or f"errore {risposta.status_code}"


def _orario(voce: dict, fuso: ZoneInfo) -> str:
    try:
        q = datetime.fromisoformat(voce["quando"]).astimezone(fuso)
        return q.strftime("%H:%M")
    except (KeyError, ValueError, TypeError):
        return ""


def componi(voce: dict, fuso: ZoneInfo) -> str:
    """Un avviso, in forma leggibile su un telefono.

    Volutamente grezzo: titolo, fonte, numeri, motivo, link. Nessuna
    parafrasi e nessun riassunto. Su un dato che muove i prezzi la
    riformulazione fa perdere secondi e puo' introdurre errori — il riassunto
    e' un lusso che ci si permette solo la mattina, sulla panoramica.
    """
    dati = voce.get("dati") or {}
    tipo = voce.get("tipo", "")

    # Il pallino rosso corrisponde all'importanza "alta" del dashboard: cio'
    # che arriva su Telegram e' per definizione quello che ha superato la
    # soglia dell'interruzione, e vederlo scritto uguale nei due posti evita
    # di doversi chiedere se stiano dicendo la stessa cosa.
    righe = [f"🔴 <b>{_e(voce.get('titolo'))}</b>"]

    if tipo == "evento":
        # Il numero prima di tutto: su un dato macro la notizia e' lo scarto
        # fra quanto e' uscito e quanto ci si aspettava, non il titolo.
        if dati.get("effettivo") not in (None, ""):
            riga = f"→ uscito <b>{_e(dati['effettivo'])}</b>"
            if dati.get("atteso") not in (None, ""):
                riga += f", atteso {_e(dati['atteso'])}"
            if dati.get("precedente") not in (None, ""):
                riga += f", prima {_e(dati['precedente'])}"
            righe.append(riga)
        if dati.get("paese"):
            righe.append(f"{SIMBOLO.get(tipo, '•')} {_e(dati['paese'])}")

    coda = [x for x in (_e(voce.get("fonte")), _orario(voce, fuso)) if x]
    if coda:
        righe.append("<i>" + (SIMBOLO.get(tipo, "•") + " " if tipo != "evento" else "")
                     + " · ".join(coda) + "</i>")

    motivi = voce.get("motivi") or []
    if motivi:
        righe.append("<i>perché: " + _e(" · ".join(motivi)) + "</i>")

    if voce.get("url"):
        righe.append(_e(voce["url"]))

    return "\n".join(righe)


class Silenzio:
    """Le ore in cui gli avvisi non devono suonare.

    Gestisce l'intervallo che scavalca la mezzanotte (23:00 -> 07:00), che e'
    il caso normale e anche quello in cui e' facile sbagliare: un banale
    `da <= adesso <= a` restituisce sempre falso quando `da` e' maggiore di
    `a`, e il silenzio non scatterebbe mai.
    """

    def __init__(self, cfg: dict) -> None:
        s = (cfg.get("avvisi", {}) or {}).get("silenzio", {}) or {}
        self.attivo = bool(s.get("attivo", True))
        self.da = self._ora(s.get("da", "23:00"), 23)
        self.a = self._ora(s.get("a", "07:00"), 7)
        # Che cosa ha il diritto di svegliarti. Di notte la soglia non e' la
        # stessa del giorno: passano solo le fonti primarie delle banche
        # centrali, dove una pubblicazione E' la decisione. Tutto il resto
        # non viene inviato — ne' subito ne' dopo — e resta sul dashboard.
        self.passano = [str(t).strip().lower()
                        for t in (s.get("passano_di_notte") or ["ufficiale"])]
        try:
            self.fuso = ZoneInfo(str(s.get("fuso", "Europe/Rome")))
        except Exception:
            # Immagini minimali senza il database dei fusi orari: meglio
            # lavorare in UTC che non partire.
            log.warning("fuso orario non disponibile: uso UTC")
            self.fuso = ZoneInfo("UTC")

    @staticmethod
    def _ora(testo, ripiego: int) -> ora_del_giorno:
        m = re.match(r"(\d{1,2}):(\d{2})", str(testo or ""))
        if not m:
            return ora_del_giorno(ripiego, 0)
        return ora_del_giorno(int(m.group(1)) % 24, int(m.group(2)) % 60)

    def adesso(self, quando: datetime | None = None) -> bool:
        if not self.attivo:
            return False
        t = (quando or datetime.now(self.fuso)).astimezone(self.fuso).time()
        if self.da <= self.a:
            return self.da <= t < self.a
        return t >= self.da or t < self.a     # intervallo a cavallo della notte


class Telegram:
    """Invio. Se il canale non e' configurato, il resto del radar funziona."""

    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str = "", chat: str = "") -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        # Piu' destinatari separati da virgola. Un bot non puo' scrivere a chi
        # non l'ha mai avviato: condividerne il link non basta, serve l'Id di
        # ciascuno. In alternativa si mette qui l'Id di un gruppo (comincia
        # per "-") e ricevono tutti quelli che ci sono dentro, senza dover
        # toccare la configurazione ogni volta che si aggiunge qualcuno.
        grezzo = chat or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.chat = [c.strip() for c in str(grezzo).split(",") if c.strip()]
        self.s = requests.Session()
        # Motivo dell'ultimo fallimento, per poterlo mostrare altrove. Quando
        # e' il canale Telegram a rompersi non lo si puo' segnalare via
        # Telegram: l'unico posto dove quel messaggio arriva e' il dashboard.
        self.ultimo_errore: str | None = None

    @property
    def configurato(self) -> bool:
        return bool(self.token and self.chat)

    def manda(self, testo: str, silenzioso: bool = False) -> bool:
        """Vero se il messaggio e' arrivato ad almeno un destinatario.

        Con piu' destinatari il fallimento di uno non deve fermare gli altri:
        se un contatto ha bloccato il bot, gli altri devono ricevere lo
        stesso.
        """
        if not self.configurato:
            log.info("Telegram non configurato: messaggio non inviato")
            self.ultimo_errore = "bot o destinatario non configurati"
            return False
        if len(testo) > MAX_TESTO:
            testo = testo[:MAX_TESTO] + "\n<i>…messaggio troncato</i>"

        riusciti = 0
        motivi: list[str] = []
        for destinatario in self.chat:
            try:
                r = self.s.post(
                    self.API.format(token=self.token),
                    json={
                        "chat_id": destinatario,
                        "text": testo,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "disable_notification": silenzioso,
                    },
                    timeout=20,
                )
            except requests.RequestException as exc:
                log.warning("invio a %s fallito: %s", destinatario, exc)
                motivi.append(f"{destinatario}: rete non raggiungibile")
                continue
            if r.status_code >= 400:
                # Il corpo della risposta dice sempre perche': chat_id
                # sbagliato, bot mai avviato, HTML malformato. Senza, si
                # indovina.
                log.warning("Telegram ha rifiutato per %s (%s): %s",
                            destinatario, r.status_code, r.text[:200])
                motivi.append(f"{destinatario}: {_motivo(r)}")
                continue
            riusciti += 1

        self.ultimo_errore = None if riusciti else " · ".join(motivi)
        return riusciti > 0


class Consegna:
    """Mette insieme filtro, silenzio e invio."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.silenzio = Silenzio(cfg)
        self.tg = Telegram()

    def avvisi(self, voci: list[dict], quando: datetime | None = None) -> dict:
        """Manda gli avvisi. Restituisce cosa e' stato fatto e perche'.

        Di notte vale una soglia piu' alta, non un divieto: passano le
        decisioni delle banche centrali — dove la pubblicazione stessa E' il
        fatto — e suonano davvero, perche' se ti svegliano deve valerne la
        pena. Tutto il resto NON viene inviato, ne' subito ne' la mattina
        dopo: resta sul dashboard e viene raccontato nel riassunto.

        La consegna posticipata e' stata tolta di proposito. Riversare gli
        arretrati alle 07:30 trasformava la rassegna in una raffica di
        notifiche, e ogni messaggio arrivava con ore di ritardo sul fatto che
        raccontava: informazione vecchia con l'aria di essere appena arrivata.
        """
        # Lo stato del silenzio si legge dall'orologio anche quando non c'e'
        # niente da mandare. Dichiararlo "falso" solo perche' la lista e'
        # vuota renderebbe il registro bugiardo: a mezzanotte si leggerebbe
        # "silenzio: False", e chi un giorno indagasse su un avviso mancato
        # comincerebbe a cercare nel posto sbagliato.
        zitto = self.silenzio.adesso(quando)
        if not voci:
            return {"inviati": 0, "trattenuti": 0, "silenzio": zitto, "errore": None}

        if zitto:
            passa = [v for v in voci
                     if str(v.get("tipo", "")).lower() in self.silenzio.passano]
            fermi = len(voci) - len(passa)
            if fermi:
                log.info("ore di silenzio: %d avvisi non inviati, restano sul "
                         "dashboard", fermi)
            inviati = 0
            for v in passa:
                if self.tg.manda(componi(v, self.silenzio.fuso)):
                    inviati += 1
            if passa:
                log.info("ore di silenzio: %d di %d passati come fonte primaria",
                         inviati, len(passa))
            return {"inviati": inviati, "trattenuti": fermi, "silenzio": True,
                    "errore": (self.tg.ultimo_errore
                               if (passa and not inviati) else None)}

        inviati = 0
        for v in voci:
            if self.tg.manda(componi(v, self.silenzio.fuso)):
                inviati += 1
        return {"inviati": inviati, "trattenuti": len(voci) - inviati,
                "silenzio": False,
                # Se nemmeno uno e' partito, il canale e' rotto e va detto.
                "errore": self.tg.ultimo_errore if inviati == 0 else None}

    def panoramica(self, testo: str) -> bool:
        return self.tg.manda(testo)


if __name__ == "__main__":
    # Controllo delle ore di silenzio: e' logica da orologio, il posto dove
    # gli errori sono piu' facili e meno visibili.
    fuso = ZoneInfo("Europe/Rome")
    s = Silenzio({"avvisi": {"silenzio": {"da": "23:00", "a": "07:00"}}})

    casi = [
        ("03:00 di notte", 3, 0, True),
        ("23:30, appena iniziato", 23, 30, True),
        ("22:59, un minuto prima", 22, 59, False),
        ("07:00 in punto, finito", 7, 0, False),
        ("06:59, ancora silenzio", 6, 59, True),
        ("14:30, uscita dati USA", 14, 30, False),
    ]
    esiti = []
    for nome, h, m, voluto in casi:
        t = datetime(2026, 8, 13, h, m, tzinfo=fuso)
        ok = s.adesso(t) == voluto
        esiti.append(ok)
        print(f"  {'ok ' if ok else 'NO '} {nome}: silenzio={s.adesso(t)}")

    # E la protezione dell'HTML, perche' un titolo ostile non deve poter
    # cambiare la forma del messaggio.
    v = {"tipo": "notizia", "titolo": "Titolo <b>grassetto</b> & simboli",
         "fonte": "X", "quando": "", "motivi": [], "dati": {}}
    testo = componi(v, fuso)
    ok = "&lt;b&gt;" in testo and "&amp;" in testo
    esiti.append(ok)
    print(f"  {'ok ' if ok else 'NO '} HTML nel titolo neutralizzato")

    # La soglia notturna: passano le banche centrali, il resto no.
    class _FintoTg:
        def __init__(self): self.mandati = []
        configurato = True
        ultimo_errore = None
        def manda(self, testo, silenzioso=False):
            self.mandati.append(testo); return True

    notte = datetime(2026, 8, 14, 3, 0, tzinfo=fuso)
    giorno = datetime(2026, 8, 14, 15, 0, tzinfo=fuso)
    lotto = [
        {"tipo": "ufficiale", "titolo": "Monetary policy decisions",
         "fonte": "BCE", "quando": "", "motivi": [], "dati": {}},
        {"tipo": "evento", "titolo": "CPI", "fonte": "Nasdaq",
         "quando": "", "motivi": [], "dati": {}},
        {"tipo": "notizia", "titolo": "Nvidia halts shipments",
         "fonte": "CNBC", "quando": "", "motivi": [], "dati": {}},
    ]
    for nome, quando, att_inviati, att_fermi in (
            ("di notte passa solo la banca centrale", notte, 1, 2),
            ("di giorno passano tutti", giorno, 3, 0)):
        c = Consegna({"avvisi": {"silenzio": {"da": "23:00", "a": "07:00"}}})
        c.tg = _FintoTg()
        r = c.avvisi(list(lotto), quando=quando)
        ok = r["inviati"] == att_inviati and r["trattenuti"] == att_fermi
        esiti.append(ok)
        print(f"  {'ok ' if ok else 'NO '} {nome}: inviati={r['inviati']} "
              f"non inviati={r['trattenuti']}")

    print(f"\n{sum(esiti)}/{len(esiti)} controlli superati")
    raise SystemExit(0 if all(esiti) else 1)
