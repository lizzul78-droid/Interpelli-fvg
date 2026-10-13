#!/usr/bin/env python3
"""
Dopo ogni esecuzione del programma:
  1. segna quali interpelli sono NUOVI (non visti nei giorni scorsi) e ricorda da quando li conosciamo;
  2. se sono impostati TELEGRAM_TOKEN e TELEGRAM_CHAT, manda un messaggio con le novita'.

Uso:  python finalize.py docs/data            (normale)
      python finalize.py docs/data --test     (manda solo un messaggio di prova)

La prima volta (nessun seen.json) non manda niente: memorizza lo stato di partenza,
cosi' non arriva un diluvio di messaggi con tutti gli avvisi gia' presenti.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

PROVINCE = {"UD": "Udine", "TS": "Trieste", "GO": "Gorizia", "PN": "Pordenone"}
MAX_NOTIFICHE = 60  # oltre questo numero il messaggio rimanda all'app


def pulisci(t, n=110):
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    lettere = re.sub(r"[^A-Za-zÀ-ÿ]", "", t)
    maiuscole = re.sub(r"[^A-ZÀ-Þ]", "", lettere)
    if len(lettere) > 12 and len(maiuscole) / len(lettere) > 0.6:
        t = t.lower().capitalize()
        t = re.sub(r"\b([ab]\d{3})\b", lambda m: m.group(1).upper(), t)
        t = re.sub(r"\b(irc|ata|dsga|td|ti|aa|ee|admm|adee|adss|adaa|eeee|om|o\.m\.)\b", lambda m: m.group(1).upper(), t)
    return t if len(t) <= n else t[: n - 1] + "…"


def invia(token, chat, testo):
    dati = urllib.parse.urlencode({"chat_id": chat, "text": testo, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    for tentativo in range(3):
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", dati, timeout=30):
                return True
        except urllib.error.HTTPError as e:
            corpo = e.read().decode("utf-8", "ignore")
            if e.code == 429:  # troppi messaggi: aspetto quanto chiesto
                m = re.search(r'"retry_after":\s*(\d+)', corpo)
                time.sleep(int(m.group(1)) + 1 if m else 10)
                continue
            print(f"Telegram ha risposto {e.code}: {corpo[:200]}")
            return False
        except Exception as e:
            print("Telegram non raggiungibile:", e)
            time.sleep(3)
    return False


def costruisci_messaggi(nuovi, app_url):
    """Un messaggio (o piu') con i nuovi interpelli, raggruppati per provincia."""
    tot = len(nuovi)
    mostrati = sorted(nuovi, key=lambda i: (i.get("provincia", ""), i.get("scuola", ""), i.get("scadenza") or "9999"))[:MAX_NOTIFICHE]
    blocchi, prov_corrente, righe = [], None, []
    for it in mostrati:
        p = it.get("provincia", "")
        if p != prov_corrente:
            prov_corrente = p
            righe.append(f"\n<b>{html.escape(PROVINCE.get(p, p or 'Altro'))}</b>")
        scad = f" · scade {it['scadenza'][8:10]}/{it['scadenza'][5:7]}" if it.get("scadenza") else ""
        righe.append(f"• <b>{html.escape(it.get('scuola', ''))}</b> ({html.escape(it.get('comune', ''))}) – "
                     f"{html.escape(pulisci(it.get('titolo')))}{scad} <a href=\"{html.escape(it['url'])}\">apri</a>")
    testo = f"🔔 <b>Nuovi interpelli ({tot})</b>" + "\n".join([""] + righe)
    if tot > len(mostrati):
        testo += f"\n\n…e altri {tot - len(mostrati)}."
    if app_url:
        testo += f"\n\nTutti gli avvisi: {html.escape(app_url)}"
    testo += "\n\n<i>Controlla sempre l'avviso originale: puo' essere cambiato o gia' coperto.</i>"
    # Telegram accetta al massimo 4096 caratteri per messaggio
    if len(testo) <= 4000:
        return [testo]
    parti, corrente = [], ""
    for riga in testo.split("\n"):
        if len(corrente) + len(riga) + 1 > 3800:
            parti.append(corrente)
            corrente = ""
        corrente += riga + "\n"
    parti.append(corrente)
    return parti


def main():
    cartella = sys.argv[1] if len(sys.argv) > 1 else "docs/data"
    token, chat, app_url = (os.environ.get(k, "").strip() for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT", "APP_URL"))

    if "--test" in sys.argv:
        if not (token and chat):
            sys.exit("Mancano TELEGRAM_TOKEN e/o TELEGRAM_CHAT.")
        ok = invia(token, chat, "✅ Prova riuscita: il bot degli interpelli e' collegato al canale.")
        sys.exit(0 if ok else "Invio non riuscito: controlla token e canale.")

    percorso = os.path.join(cartella, "interpelli.json")
    visti_path = os.path.join(cartella, "seen.json")
    dati = json.load(open(percorso, encoding="utf-8"))
    oggi = date.today()
    prima_volta = not os.path.exists(visti_path)
    visti = {} if prima_volta else json.load(open(visti_path, encoding="utf-8"))

    nuovi = []
    for chiave in ("interpelli", "pagine_interpelli"):
        for it in dati.get(chiave, []):
            u = it["url"]
            if u not in visti:
                # alla prima esecuzione gli avvisi gia' presenti non sono "nuovi": li faccio risalire a 30 giorni fa
                visti[u] = {"primo_visto": (oggi - timedelta(days=30) if prima_volta else oggi).isoformat()}
                if chiave == "interpelli":
                    nuovi.append(it)
            visti[u]["ultimo_visto"] = oggi.isoformat()
            primo = date.fromisoformat(visti[u]["primo_visto"])
            it["primo_visto"] = primo.isoformat()
            it["nuovo"] = (oggi - primo).days <= 1 and not prima_volta

    limite = (oggi - timedelta(days=30)).isoformat()  # dimentico gli avvisi non piu' visti da 30 giorni
    visti = {u: v for u, v in visti.items() if v.get("ultimo_visto", "") >= limite}
    json.dump(dati, open(percorso, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(visti, open(visti_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"Interpelli: {len(dati.get('interpelli', []))}, nuovi: {len(nuovi)}"
          + (" (prima esecuzione: nessun messaggio)" if prima_volta else ""))
    if prima_volta or not nuovi or not (token and chat):
        if nuovi and not (token and chat) and not prima_volta:
            print("Telegram non configurato: nessun messaggio inviato.")
        return
    for messaggio in costruisci_messaggi(nuovi, app_url):
        invia(token, chat, messaggio)
        time.sleep(3)


if __name__ == "__main__":
    main()
