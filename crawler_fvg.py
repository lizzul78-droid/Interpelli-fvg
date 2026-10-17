#!/usr/bin/env python3
"""
Crawler interpelli - scuole del Friuli Venezia Giulia.

1. Legge l'anagrafe delle scuole statali del Ministero (CSV open data) e tiene
   solo il Friuli Venezia Giulia, con il sito web di ogni istituto.
2. Visita ogni sito con educazione (robots.txt, pausa tra le richieste) e cerca
   gli interpelli in 4 modi:
     - API di WordPress (/wp-json) con ricerca "interpello"
     - sitemap.xml (pagine il cui indirizzo contiene "interpell")
     - navigazione delle sezioni tipiche (albo, bandi, avvisi, trasparenza...)
     - lettura del testo di pagine e PDF allegati
3. Salva tutto in SQLite e in un JSON pronto per l'app.

Uso:
    pip install -r requirements.txt
    python crawler_fvg.py --schools anagrafe_scuole.csv --email tua@email.it --out risultati/
    python crawler_fvg.py --schools anagrafe_scuole.csv --limit 20     # prova
    python crawler_fvg.py --only-site https://www.scuola.edu.it/        # un solo sito

Il CSV si scarica da https://dati.istruzione.it (Open Data > Scuole >
"Anagrafe scuole statali"). Puoi passare anche direttamente l'indirizzo.
Metti la tua email con --email: i gestori dei siti
possono cosi' scriverti se qualcosa non va.
"""
import argparse
import csv
import io
import json
import re
import sqlite3
import socket
import sys
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import unquote
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from html import unescape
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except ImportError:  # i PDF verranno ignorati
    PdfReader = None

BACKOFF = 15  # secondi di attesa dopo un "troppe richieste" (poi 2x)
THROTTLE = (429, 503, 509)
USER_AGENT = "InterpelliBot/0.1 (+contatto: tua@email.it)"
PROV = {"UDINE": "UD", "TRIESTE": "TS", "PORDENONE": "PN", "GORIZIA": "GO"}

# ---------------------------------------------------------------- regole
STRONG = re.compile(r"interpell", re.I)
WEAK = re.compile(
    r"supplenz|conferiment\w+\s+(?:di\s+)?incaric|disponibilit\w+\s+(?:per|a)\b|"
    r"individuazione\s+(?:di\s+)?(?:docent|personale)|ricerca\s+(?:di\s+)?(?:personale|docent)|"
    r"(?:posto|cattedra)\s+(?:di\s+)?sostegno", re.I)
ROLE_ANY = re.compile(
    r"docent|insegnant|sostegno|classe\s+di\s+concorso|\b[AB]\d{3}\b|ADMM|ADSS|ADEE|ADAA|"
    r"collaborator\w+\s+scolastic|assistente\s+(?:amministrativ|tecnic)|\bATA\b", re.I)
SOST = re.compile(r"sostegno|ADMM|ADSS|ADEE|ADAA", re.I)
ATA = re.compile(r"collaborator\w+\s+scolastic|assistente\s+(?:amministrativ|tecnic)|\bATA\b|\bDSGA\b", re.I)
CLOSED = re.compile(
    r"\besit[oi]\b|\bannullament|\bannullat|\brevoca|\bverbale\b|\bdetermin[ae]\b|"
    r"atto\s+dirigenziale|decreto\s+di\s+(?!avviso)|\brettific|\bapprovazione\b|\bnomina\b|commissione|"
    r"elenco\s+degli\s+idonei|\brisoluzione\b|destinatari[oe]|"
    r"individuazione\s+(?:da\s+interpell|dei\s+destinatari|docente\b|d.urgenza)|"
    r"conferimento\s+incarichi?\s+di\s+supplenza|"
    r"\be\s+graduatoria\b|graduatori[ae]\s+(?:provvisori|definitiv|final|di\s+merito|provincial|di\s+circolo)|"
    r"pubblicazione\s+(?:graduator|elenc)|graduatoria\s+(?:provvisoria|definitiva|finale|di\s+merito)|"
    r"valutazione\s+(?:delle\s+)?istanze|esame\s+(?:delle\s+)?istanze", re.I)
# non sono avvisi da cui candidarsi: modelli, regolamenti, incarichi per professionisti esterni
EXCLUDE = re.compile(r"\bmodell[oi]\b|\bmodulo\b|fac.?simile|regolamento|criteri|\bRSPP\b|\bDPO\b|"
                     r"medico\s+competente|collaudator|progettista|revisore|assicurativ|"
                     r"doposcuola|incarichi?\s+aggiuntiv|docente\s+esterno|\besperto\b|"
                     r"assemble|sciopero|sindacal|\bRSU\b|\bUSB\b|\bFLC\b|\bCISL\b|\bUIL\b|\bGILDA\b|\bSNALS\b", re.I)
# un titolo che COMINCIA con "Individuazione ..." di solito annuncia chi e' stato scelto (esito), non un avviso
CLOSED_START = re.compile(r"^\W*individuazione\b(?!.*(?:avviso|interpell|disponibil|candidatur|domand))", re.I | re.S)
# frasi tipiche di un ESITO scritte nel testo (prime righe) del documento
CLOSED_TEXT = re.compile(
    r"\besito\b|(?:è|e'|sono)\s+stat[aoi]\s+(?:individuat|assegnat|conferit|nominat)|"
    r"\bha\s+accettato|\bha\s+individuato|"
    r"si\s+comunica\s+(?:l.)?\s*(?:esito|individuazione|assegnazione)|"
    r"\bindividuat[aoi]\s+(?:il|la|l.)?\s*(?:docente|supplente|candidat)", re.I)
# frasi tipiche di un avviso ANCORA APERTO: se ci sono, non scartare
OPEN_WORDS = re.compile(
    r"entro\s+(?:e\s+non\s+oltre\s+)?(?:le\s+ore|il\s+giorno|il\b)|"
    r"presentare\s+(?:la\s+)?(?:domanda|candidatur|disponibil)|"
    r"i\s+docenti\s+interessati|gli\s+interessati|invitano?\b", re.I)
ROLE_WORDS = re.compile(r"sostegno|docent|infanzia|primaria|secondaria|\bATA\b|collaborator|assistente|posto|cattedra", re.I)
SECTION = re.compile(
    r"albo|bandi|avvisi|amministrazione.?trasparente|interpell|supplenz|reclutamento|"
    r"personale|graduatorie|comunicazion|news|segreteria|incarich", re.I)
EXTERNAL_ALBO = re.compile(r"argo|nuvola|madisoft|axios|spaggiari|sissiweb|trasparenzascuole|albo", re.I)
PROVIDERS = {"Nuvola": r"nuvola|madisoft", "Spaggiari": r"spaggiari", "Argo": r"argo",
             "Axios": r"axios", "Sissiweb": r"sissiweb"}
FILE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".mp4")
COMMON_PATHS = ["albo-online/", "albo-pretorio/", "amministrazione-trasparente/",
                "bandi-di-gara-e-contratti/", "interpelli/", "avvisi/", "news/"]
GENERIC_TITLE = re.compile(r"^(leggi|scarica|download|allegato|clicca|vai|continua|maggiori|pdf)", re.I)
MAD_RE = re.compile(r"\bmad\b|messa\s+a\s+disposizione|candidature\s+libere", re.I)
GENERIC_SECTION = re.compile(r"^\W*(interpell[io]|avvisi|albo(\s+online)?)"
                             r"(\s+(?:a\.?s\.?\s*)?20\d{2}\s*[/-]\s*(?:20)?\d{2})?\W*$", re.I)
SECTION_SLUGS = {"interpelli", "interpello", "avvisi", "albo-online", "albo-pretorio"}
STALE_DAYS_RELIABLE = 45   # data certa (metadati, nome file)
STALE_DAYS_TEXT = 120      # data dedotta dal testo

MONTHS = {m: i + 1 for i, m in enumerate(
    "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre".split())}
DATE_RE = re.compile(
    r"(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(20\d{2})|"
    r"(\d{1,2})\s*(?:°|º)?\s+(" + "|".join(MONTHS) + r")\s+(20\d{2})", re.I)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- scadenze
def first_date(seg):
    m = DATE_RE.search(seg)
    if not m:
        return None
    try:
        if m.group(1):
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return date(int(m.group(6)), MONTHS[m.group(5).lower()], int(m.group(4)))
    except ValueError:
        return None


def find_deadline(text):
    t = re.sub(r"\s+", " ", text)
    for m in re.finditer(r"entro|scadenz|termine|non oltre|fino\s+al", t, re.I):
        seg = t[m.start():m.start() + 120]
        d = first_date(seg)
        if d:
            return d
        m2 = re.search(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)", seg)  # 16102026
        if m2:
            try:
                return date(int(m2.group(3)), int(m2.group(2)), int(m2.group(1)))
            except ValueError:
                pass
    return None


def past_end_date(title):
    """True se il titolo dice che il posto finiva in una data gia' passata."""
    t = re.sub(r"\s+", " ", title)
    for m in re.finditer(r"(?:\bal|giornata\s+del)\s*(?:il\s+)?(?=\d)", t, re.I):
        d = first_date(t[m.end():m.end() + 30])
        if d and d < date.today():
            return True
    return False


def title_end(title):
    """Prima data futura dopo "al ..." / "per la giornata del ..." nel titolo."""
    t = re.sub(r"\s+", " ", title)
    for m in re.finditer(r"(?:\bal|giornata\s+del)\s*(?:il\s+)?(?=\d)", t, re.I):
        d = first_date(t[m.end():m.end() + 30])
        if d and d >= date.today():
            return d
    return None


def canonical(url):
    """Stesso indirizzo con/senza www, http/https, barra finale o parametri inutili."""
    p = urlparse(url)
    q = "?" + p.query if "id=" in p.query else ""
    return p.netloc.lower().removeprefix("www.") + p.path.rstrip("/") + q


def pdf_date(data):
    """Data di creazione scritta dentro il PDF (se c'e')."""
    if PdfReader is None:
        return None
    try:
        md = PdfReader(io.BytesIO(data)).metadata
        d = md.creation_date.date() if md and md.creation_date else None
        return d if d and 2015 <= d.year <= date.today().year + 1 else None
    except Exception:
        return None


def dates_in(s):
    """Date scritte come 16-10-24, 14/2/2025, 04.10.2024 (nei titoli e nei nomi dei file)."""
    out = []
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*[-/._]\s*(\d{1,2})\s*[-/._]\s*(\d{4}|\d{2})(?!\d)", s):
        dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yy = yy + 2000 if yy < 100 else yy
        try:
            d = date(yy, mm, dd)
        except ValueError:
            continue
        if 2015 <= yy <= 2035:
            out.append(d)
    for m in re.finditer(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)", s):  # 30062026
        try:
            out.append(date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
        except ValueError:
            pass
    return out


def context_dates(ctx):
    """Date scritte accanto al link (riga di tabella o elenco): 14/02/2025, 3 ottobre 2026..."""
    out = []
    for m in DATE_RE.finditer(ctx or ""):
        d = first_date(m.group(0))
        if d:
            out.append(d)
    return out


def ref_date(text, url="", soup=None, hint="", last_modified=None, doc_date=None, ctx=""):
    """(data, affidabile). Serve a capire se un avviso senza scadenza e' vecchio."""
    sure = []
    m = re.search(r"(?<!\d)(1[4-9]\d{8})(?!\d)", url)
    if m:
        try:
            sure.append(datetime.fromtimestamp(int(m.group(1))).date())
        except (ValueError, OSError):
            pass
    m = re.search(r"/(20\d{2})/(\d{2})/(?:(\d{2})/)?", url)
    if m:
        try:
            sure.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 28)))
        except ValueError:
            pass
    label = unquote(url).rsplit("/", 1)[-1] + " " + hint
    sure += dates_in(label)
    sure += context_dates(ctx)
    # anno scolastico citato (a.s. 2024/25, graduatoria 2025-2026): conto come finito il 30 giugno
    for m in re.finditer(r"(?<![a-z])(?:a\.?\s?s\.?|anno\s+scolastico)[\s_-]*(20\d{2})\s*[/_-]?\s*(20\d{2}|\d{2})(?!\d)",
                         label, re.I):
        y2 = int(m.group(2))
        sure.append(date(y2 + 2000 if y2 < 100 else y2, 6, 30))
    for m in re.finditer(r"(?<!\d)(20\d{2})\s*[/_-]\s*(20\d{2}|\d{2})(?!\d)", label):
        y1, y2 = int(m.group(1)), int(m.group(2))
        y2 = y2 + 2000 if y2 < 100 else y2
        if y2 == y1 + 1:
            sure.append(date(y2, 6, 30))
    if sure:  # date scritte nel titolo o nel nome del file: le piu' affidabili
        return max(sure), True
    if doc_date:  # data scritta dentro il PDF
        return doc_date, True
    soft = []
    if last_modified:
        soft.append(last_modified)
    if soup is not None:
        tag = soup.find("meta", property="article:published_time") or soup.find("time", attrs={"datetime": True})
        raw = ((tag.get("content") or tag.get("datetime")) if tag else "") or ""
        try:
            if raw:
                soft.append(date.fromisoformat(raw[:10]))
        except ValueError:
            pass
    if soft:
        return max(soft), True
    t = re.sub(r"\s+", " ", text)
    ds = [d for d in (first_date(t[x.start():x.start() + 30]) for x in DATE_RE.finditer(t)) if d]
    return (max(ds), False) if ds else (None, False)


# ---------------------------------------------------------------- rete
def parse_robots(text):
    """Regole di robots.txt per il nostro bot (o per tutti): lista di (permesso, percorso)."""
    groups, agents, in_rules = {}, [], False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(v.lower())
            groups.setdefault(v.lower(), [])
        elif k in ("allow", "disallow") and agents:
            in_rules = True
            if v:
                for ag in agents:
                    groups[ag].append((k == "allow", v))
    return groups.get("interpellibot") or groups.get("*") or []


def robots_allows(rules, path):
    """Vince la regola piu' lunga (la piu' specifica); capisce * e $."""
    best_len, best_allow = -1, True
    for allow, pat in rules:
        rx = re.escape(pat).replace(r"\*", ".*")
        if rx.endswith(r"\$"):
            rx = rx[:-2] + "$"
        if re.match(rx, path) and (len(pat) > best_len or (len(pat) == best_len and allow)):
            best_len, best_allow = len(pat), allow
    return best_allow

class Fetcher:
    """Richieste educate: robots.txt e pausa tra una richiesta e l'altra."""

    def __init__(self, delay, timeout):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.delay, self.timeout, self.last = delay, timeout, 0.0
        self.robots = {}
        self.crawl_delay = {}
        self.err = ""
        self.last_modified = None

 def allowed(self, url):
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        rules = self.robots.get(root)
        if rules is None:
            rules = []
            try:
                r = self.s.get(root + "/robots.txt", timeout=self.timeout)
                if r.status_code == 200:
                    rules = parse_robots(r.text)
                    m = re.search(r"(?im)^\s*crawl-delay:\s*(\d+)", r.text)
                    if m:
                        self.crawl_delay[root] = min(int(m.group(1)), 30)
            except requests.RequestException:
                pass
            self.robots[root] = rules
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        return robots_allows(rules, path)

    def get(self, url, max_bytes=3_000_000):
        """Ritorna (url_finale, content_type, bytes) oppure None (motivo in self.err).
        Se il sito dice "troppe richieste" (429/503/509) aspetta e rallenta; se va in timeout riprova una volta."""
        self.err = ""
        self.last_modified = None
        if not self.allowed(url):
            self.err = "vietato da robots.txt"
            return None
        p0 = urlparse(url)
        for attempt in range(3):
            wait = max(self.delay, self.crawl_delay.get(f"{p0.scheme}://{p0.netloc}", 0)) - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(url, timeout=self.timeout, stream=True)
                self.last = time.time()
                if r.status_code in THROTTLE:
                    self.err = f"HTTP {r.status_code}"
                    pause = BACKOFF * (attempt + 1)
                    try:
                        pause = min(max(pause, int(r.headers.get("Retry-After", 0))), 90)
                    except ValueError:
                        pass
                    r.close()
                    self.delay = min(self.delay * 2, 10)  # da ora in poi piu' piano
                    if attempt < 2:
                        time.sleep(pause)
                        continue
                    return None
                if r.status_code != 200:
                    self.err = f"HTTP {r.status_code}"
                    r.close()
                    return None
                data = b""
                for chunk in r.iter_content(65536):
                    data += chunk
                    if len(data) > max_bytes:
                        break
                ctype = r.headers.get("Content-Type", "").lower()
                try:
                    self.last_modified = parsedate_to_datetime(r.headers["Last-Modified"]).date()
                except (KeyError, TypeError, ValueError):
                    pass
                final = r.url
                r.close()
                return final, ctype, data
            except requests.exceptions.Timeout:
                self.err = "timeout"
                self.last = time.time()
                if attempt == 0:
                    continue
                return None
            except requests.exceptions.SSLError:
                self.err = "certificato SSL non valido"
                break
            except requests.RequestException:
                self.err = "connessione fallita"
                break
        self.last = time.time()
        return None


def extract_text(ctype, data, url):
    """Ritorna (testo, titolo) da una pagina HTML o da un PDF."""
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        if PdfReader is None:
            return "", ""
        try:
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:3])
            return text, ""
        except Exception:
            return "", ""
    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    h1 = soup.find("h1")
    title = (h1.get_text(" ", strip=True) if h1 else "") or (soup.title.get_text(strip=True) if soup.title else "")
    return soup.get_text(" ", strip=True), title


def classify(hint, text):
    if CLOSED.search(hint) or CLOSED_START.search(hint):
        return False
    head = re.sub(r"\s+", " ", text[:600])
    if CLOSED_TEXT.search(head) and not OPEN_WORDS.search(head):
        return False
    if STRONG.search(hint) or STRONG.search(text[:800]):
        return True
    return bool(WEAK.search(hint) and ROLE_ANY.search(hint + " " + text[:1500]))


def provider_of(url):
    host = urlparse(url).netloc.lower()
    for name, pat in PROVIDERS.items():
        if re.search(pat, host):
            return name
    return "Albo esterno"


def role_of(s):
    if SOST.search(s):
        return "Sostegno"
    if ATA.search(s):
        return "ATA"
    return "Docenti"


# ---------------------------------------------------------------- fonti "nascoste"
def wp_search(f, base):
    out = []
    for kind in ("posts", "pages"):
        res = f.get(urljoin(base, f"wp-json/wp/v2/{kind}?search=interpello&per_page=30&_fields=link,title"))
        if not res:
            continue
        try:
            items = json.loads(res[2])
        except ValueError:
            continue
        if isinstance(items, list):
            for it in items:
                title = unescape(re.sub(r"<.*?>", "", it.get("title", {}).get("rendered", "")))
                out.append((it.get("link"), title, "Ricerca interna del sito (WordPress)"))
    return [o for o in out if o[0]]


def sitemap_hits(f, base):
    out = []
    for name in ("sitemap.xml", "wp-sitemap.xml", "sitemap_index.xml"):
        res = f.get(urljoin(base, name))
        if not res:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", res[2].decode("utf-8", "ignore"))
        children = [u for u in locs if u.endswith(".xml")]
        pages = [u for u in locs if not u.endswith(".xml")]
        for child in [c for c in children if re.search(r"post|page|news|article", c, re.I)][:6]:
            r2 = f.get(child)
            if r2:
                pages += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r2[2].decode("utf-8", "ignore"))
        for u in pages:
            if STRONG.search(u):
                slug = urlparse(u).path.strip("/").split("/")[-1].replace("-", " ")
                out.append((u, slug, "Sitemap del sito"))
        if out:
            break
    return out


# ---------------------------------------------------------------- un sito
def site_variants(url):
    """Indirizzo dato, poi con/senza www, poi http/https."""
    p = urlparse(url)
    alt = p.netloc.removeprefix("www.") if p.netloc.startswith("www.") else "www." + p.netloc
    other = "http" if p.scheme == "https" else "https"
    path = p.path or "/"
    return [url, f"{p.scheme}://{alt}{path}", f"{other}://{p.netloc}{path}"]


NUVOLA_TAG = "Albo Nuvola (lettura automatica)"


def read_nuvola(f, url):
    """Legge la tabella pubblica di un albo Nuvola e tiene solo gli avvisi di interpello ancora aperti.
    Salva solo titolo, date e link: niente documenti, niente altri dati."""
    res = f.get(url)
    if not res or "html" not in res[1]:
        return []
    soup = BeautifulSoup(res[2], "html.parser")
    today, items, seen, groups = date.today(), [], set(), {}
    for a in soup.find_all("a", href=True):
        if "/bacheca-digitale/" not in a["href"] or "/documento/" not in a["href"]:
            continue
        row = a.find_parent("tr")
        if row is None or a["href"] in seen:
            continue
        seen.add(a["href"])
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cells) >= 8:
            oggetto, pub, arch = cells[2], first_date(cells[6]), first_date(cells[7])
        else:
            oggetto = max(cells, key=len) if cells else ""
            ds = context_dates(" ".join(cells))
            pub, arch = (ds[1] if len(ds) > 1 else None), (ds[2] if len(ds) > 2 else None)
        if not oggetto or not classify(oggetto, "") or EXCLUDE.search(oggetto):
            continue
        dl = find_deadline(oggetto)
        if (dl and dl < today) or past_end_date(oggetto):
            continue
        scad = dl
        if scad is None and arch and pub and 0 <= (arch - pub).days <= 60:
            if arch < today:
                continue
            scad = arch  # fine pubblicazione sull'albo: di solito coincide con la scadenza
        elif scad is None and arch and not pub:
            if arch < today:
                continue
            scad = arch
        te = title_end(oggetto)
        if scad is not None and te is not None and te < scad:
            scad = te  # es. "dal 08/10 al 16/10": conta il 16/10, non la fine pubblicazione
        # una data di archivio lontanissima (2030...) e' solo la conservazione del documento: conta la data di pubblicazione
        if scad is None:
            if pub and (today - pub).days > STALE_DAYS_RELIABLE:
                continue
            ref, sure = ref_date("", "", None, oggetto)
            if ref and sure and (today - ref).days > STALE_DAYS_RELIABLE:
                continue
        key = re.sub(r"\W+", "", oggetto.lower())
        if key in groups:
            groups[key]["_n"] += 1
            continue
        item = {"titolo": oggetto[:160], "ruolo": role_of(oggetto),
                "scadenza": scad.isoformat() if scad else None,
                "trovato_in": NUVOLA_TAG, "url": urljoin(url, a["href"]), "tipo": "interpello", "_n": 1}
        groups[key] = item
        items.append(item)
    for it in items:
        n = it.pop("_n")
        if n > 1:
            it["titolo"] = f"{it['titolo'][:140]} [{n} avvisi simili]"
    return items


_IP_LOCKS, _IP_GUARD = {}, threading.Lock()


def crawl_site_polite(school, delay, timeout, max_pages):
    """Piu' scuole possono stare sullo stesso server (es. WordPress condiviso): le visito una per volta."""
    host = urlparse(school["site"]).netloc
    try:
        key = socket.gethostbyname(host)
    except OSError:
        key = host
    with _IP_GUARD:
        lock = _IP_LOCKS.setdefault(key, threading.Lock())
    with lock:
        res = crawl_site(school, delay, timeout, max_pages)
        if res.get("error") and re.search(r"HTTP (429|503|509)", res["error"]):
            time.sleep(BACKOFF * 2)  # lascio respirare il server prima della scuola successiva
        return res


def crawl_site(school, delay, timeout, max_pages):
    f = Fetcher(delay, timeout)
    home, why = None, ""
    candidates = [v for site in [school["site"]] + school.get("alt_sites", []) for v in site_variants(site)]
    for u in candidates:
        home = f.get(u)
        if home:
            break
        why = why or f.err
        if f.err.startswith("HTTP") and int(f.err.split()[1]) in THROTTLE:
            break  # il server chiede di rallentare: inutile provare altri indirizzi dello stesso sito
    if not home:
        return {"school": school, "items": [], "external": [], "albo_esterno": None, "error": f"non raggiungibile ({why})"}
    base = home[0]
    host = urlparse(base).netloc.lower().removeprefix("www.")
    same = lambda u: urlparse(u).netloc.lower().removeprefix("www.") == host

    found, seen, visited, titles = {}, set(), set(), set()
    external = {}  # indirizzo -> (punteggio, etichetta del link)
    has_mad = [False]
    queue = deque([(base, 0, "Home")])
    queue.extend((urljoin(base, p), 1, p.strip("/")) for p in COMMON_PATHS)

    def add(url, hint, where, depth, ctx=""):
        if canonical(url) in seen:
            return
        seen.add(canonical(url))
        hint = re.sub(r"\s+", " ", hint).strip()
        low = url.lower()
        if EXCLUDE.search(hint) or EXCLUDE.search(unquote(low.rsplit("/", 1)[-1])):
            return
        # pagina permanente per le candidature libere (ex MAD): non e' un interpello
        slug = urlparse(url).path.rstrip("/").split("/")[-1].lower()
        generic = (len(hint) <= 40 and not re.search(r"\d", hint) and not ROLE_WORDS.search(hint)
                   and not GENERIC_SECTION.match(hint) and slug not in SECTION_SLUGS
                   and where != "Sitemap del sito")  # dalla sitemap il "titolo" e' solo l'indirizzo: si decide dopo aver letto la pagina
        if (len(hint) <= 60 and (MAD_RE.search(hint) or "/servizi/" in low or "/scheda/" in low)) or generic \
                or "/servizio/" in low or re.search(r"invio-mad|mad-supplenze", low):
            if not has_mad[0]:
                has_mad[0] = True
                found[canonical(url)] = {"titolo": hint[:160] or "Candidature libere (ex MAD)", "ruolo": "Docenti",
                              "scadenza": None, "trovato_in": where, "url": url, "tipo": "candidatura libera"}
            return
        # semplice contenitore ("Interpelli", "Avvisi"): si esplora, non si salva
        if GENERIC_SECTION.match(hint) or slug in SECTION_SLUGS:
            queue.append((url, depth + 1, hint[:40] or slug))
            return
        res = f.get(url)
        if not res:
            return
        final, ctype, data = res
        lm = f.last_modified
        is_pdf = "pdf" in ctype or final.lower().split("?")[0].endswith(".pdf")
        if "html" not in ctype and not is_pdf:
            return  # immagini, documenti Word, ecc.: non leggibili
        if urlparse(final).path.rstrip("/").split("/")[-1].lower() in SECTION_SLUGS:
            queue.append((final, depth + 1, hint[:40] or "sezione"))
            return
        soup = None
        if "html" in ctype:
            soup = BeautifulSoup(data, "html.parser")
            if sum(1 for a in soup.find_all("a", href=True) if STRONG.search(a.get_text(" "))) >= 3:
                queue.append((final, depth + 1, hint[:40]))
                return
        text, doc_title = extract_text(ctype, data, final)
        if not classify(hint, text):
            return
        if where == "Sitemap del sito" and doc_title:  # dalla sitemap arriva solo l'indirizzo: meglio il titolo vero
            title = doc_title
        else:
            title = hint if len(hint) >= 15 and not GENERIC_TITLE.match(hint) else (doc_title or text[:120])
        title = re.sub(r"\s+", " ", title).strip()[:160]
        key = re.sub(r"\W+", "", title.lower())
        if key in titles:
            return
        deadline, extra, n_pdf = find_deadline(title + ". " + text), "", 0
        if not deadline and soup is not None:  # la scadenza e' spesso nel PDF allegato
            for a in soup.find_all("a", href=True):
                h = urljoin(final, a["href"])
                if h.lower().split("?")[0].endswith(".pdf") and same(h):
                    n_pdf += 1
                    r2 = f.get(h)
                    if r2:
                        t2, _ = extract_text(r2[1], r2[2], r2[0])
                        extra += " " + t2
                        deadline = find_deadline(t2)
                    if deadline or n_pdf >= 2:
                        break
        today = date.today()
        if deadline and deadline < today:
            return
        if not deadline:
            ref, reliable = ref_date(text + extra, final, soup, title + " " + hint, lm,
                                     pdf_date(data) if is_pdf else None, ctx)
            if ref and (today - ref).days > (STALE_DAYS_RELIABLE if reliable else STALE_DAYS_TEXT):
                return
        titles.add(key)
        # titolo breve, senza codici ne' date, su una pagina web: e' la pagina degli interpelli della scuola
        is_page = (not deadline and not is_pdf and len(title) <= 45
                   and not re.search(r"\d+\s*ore|sostegno|posto|urgente|\bcomune\b|cattedra|supplenz", title, re.I)
                   and not re.search(r"\b[A-Z]\d{3}\b|\bn\.?\s?\d+\b|\d{1,2}\s*[/.-]\s*\d{1,2}", title))
        if not deadline and not is_pdf and re.match(r"interpelli\b", title, re.I) and not re.search(r"\d", title):
            is_page = True
        found[canonical(final)] = {"titolo": title, "ruolo": role_of(title + " " + text[:600]),
                                   "scadenza": deadline.isoformat() if deadline else None,
                                   "trovato_in": where, "url": final,
                                   "tipo": "pagina interpelli" if is_page else "interpello"}

    for url, title, where in wp_search(f, base) + sitemap_hits(f, base):
        if same(url):
            add(url, title, where, 1)

    pages = 0
    while queue and pages < max_pages:
        url, depth, label = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        res = f.get(url)
        if not res or "html" not in res[1]:
            continue
        final, _, data = res
        pages += 1
        soup = BeautifulSoup(data, "html.parser")
        where = label if label != "Home" else "Pagina iniziale"
        for a in soup.find_all("a", href=True):
            href = urljoin(final, a["href"]).split("#")[0]
            if not href.startswith("http"):
                continue
            hint = a.get_text(" ", strip=True) or a.get("title", "")
            if not same(href):
                if EXTERNAL_ALBO.search(urlparse(href).netloc) and "login" not in href.lower():
                    label = re.sub(r"\s+", " ", hint).strip()[:60]
                    if re.search(r"login|accedi|registro|archivio|area riservata", label, re.I) \
                            or urlparse(href).path in ("", "/"):
                        continue  # pagine di accesso o archivi vecchi: inutili per chi cerca un interpello
                    if re.search(r"trasparen", label, re.I):
                        score = 1                      # "Amministrazione trasparente": utile ma non e' l'albo
                    elif re.search(r"\balbo\b", label, re.I):
                        score = 4
                    elif re.search(r"interpell|bandi|avvis", label, re.I):
                        score = 3
                    elif re.search(r"albo|bacheca", href + " " + label, re.I):
                        score = 2
                    else:
                        continue  # nessun indizio che sia un albo (es. "Visualizza allegato")
                    if href not in external or score > external[href][0]:
                        external[href] = (score, label)
                continue
            if STRONG.search(hint) or STRONG.search(href) or WEAK.search(hint):
                box = a.find_parent(["tr", "li", "article", "p"])
                ctx = box.get_text(" ", strip=True) if box else ""
                add(href, hint, where, depth, ctx if len(ctx) <= 400 else "")
            elif depth < 2 and SECTION.search(hint + " " + href) and not href.lower().endswith(FILE_EXT + (".pdf",)):
                queue.append((href, depth + 1, hint[:40] or "sezione"))
    ranked = sorted(external, key=lambda u: -external[u][0])
    return {"school": school, "items": list(found.values()), "external": ranked[:5],
            "albo_esterno": ranked[0] if ranked else None,
            "albo_etichetta": external[ranked[0]][1] if ranked else "", "error": None}


# ---------------------------------------------------------------- anagrafe
def norm_site(v):
    v = (v or "").strip()
    if not v or v.upper().startswith(("NON DISP", "N.D", "-")):
        return None
    v = re.sub(r"^(?:https?(?::/*|/+))+", "", v, flags=re.I)  # toglie http://, https//, http:/ ...
    v = v.replace(" ", "")
    if "." not in v:
        return None
    host, _, rest = v.partition("/")
    host = re.sub(r"\.{2,}", ".", host.lower())
    return f"https://{host}/{rest}"


def load_schools(src, regione="FRIULI"):
    if src.startswith("http"):
        raw = requests.get(src, headers={"User-Agent": USER_AGENT}, timeout=60).content
    else:
        raw = open(src, "rb").read()
    text = raw.decode("utf-8-sig", errors="replace")
    delim = ";" if text.split("\n", 1)[0].count(";") > text.split("\n", 1)[0].count(",") else ","
    by_inst = {}
    for row in csv.DictReader(io.StringIO(text), delimiter=delim):
        if regione not in (row.get("REGIONE") or "").upper():
            continue
        site = norm_site(row.get("SITOWEBSCUOLA"))
        if not site:
            continue
        prov = (row.get("PROVINCIA") or "").strip().upper()
        code = row.get("CODICEISTITUTORIFERIMENTO") or row.get("CODICESCUOLA") or site
        rec = by_inst.setdefault(code, {
            "site": site, "alt_sites": [],
            "scuola": (row.get("DENOMINAZIONEISTITUTORIFERIMENTO") or row.get("DENOMINAZIONESCUOLA") or "").title(),
            "codice": code,
            "comune": (row.get("DESCRIZIONECOMUNE") or "").title(),
            "provincia": PROV.get(prov, prov[:2]),
            "regione": "Friuli Venezia Giulia",
        })
        if site != rec["site"] and site not in rec["alt_sites"]:
            rec["alt_sites"].append(site)
    return list(by_inst.values())


# ---------------------------------------------------------------- salvataggio
def save(results, out_dir, prev=None):
    import os
    os.makedirs(out_dir, exist_ok=True)
    db = sqlite3.connect(os.path.join(out_dir, "interpelli.db"))
    db.execute("""CREATE TABLE IF NOT EXISTS interpelli(
        url TEXT PRIMARY KEY, scuola TEXT, codice TEXT, comune TEXT, provincia TEXT, regione TEXT,
        titolo TEXT, ruolo TEXT, scadenza TEXT, trovato_in TEXT, first_seen TEXT, last_seen TEXT, tipo TEXT)""")
    try:  # vecchi database senza la colonna "tipo"
        db.execute("ALTER TABLE interpelli ADD COLUMN tipo TEXT")
    except sqlite3.OperationalError:
        pass
    today = date.today().isoformat()
    run_ts = datetime.now().isoformat(timespec="seconds")
    for r in results:
        s = r["school"]
        for it in r["items"]:
            db.execute("""INSERT INTO interpelli(url,scuola,codice,comune,provincia,regione,titolo,ruolo,
                scadenza,trovato_in,first_seen,last_seen,tipo) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET titolo=excluded.titolo, scadenza=excluded.scadenza,
                ruolo=excluded.ruolo, last_seen=excluded.last_seen, tipo=excluded.tipo""",
                       (it["url"], s["scuola"], s["codice"], s["comune"], s["provincia"], s["regione"],
                        it["titolo"], it["ruolo"], it["scadenza"], it["trovato_in"], today, run_ts, it["tipo"]))
    db.commit()
    rows = db.execute("""SELECT url,scuola,comune,provincia,regione,titolo,ruolo,scadenza,trovato_in,first_seen,tipo
                         FROM interpelli WHERE last_seen=? ORDER BY COALESCE(scadenza,'9999'), scuola""", (run_ts,)).fetchall()
    keys = ["url", "scuola", "comune", "provincia", "regione", "titolo", "ruolo", "scadenza", "trovato_in"]
    rec = [dict(zip(keys, r[:9]), nuovo=(r[9] == today), tipo=r[10] or "interpello") for r in rows]
    payload = {"generato": datetime.now().isoformat(timespec="seconds"),
               "interpelli": [x for x in rec if x["tipo"] == "interpello"],
               "pagine_interpelli": [x for x in rec if x["tipo"] == "pagina interpelli"],
               "candidature_libere": [x for x in rec if x["tipo"] == "candidatura libera"],
               "albi_esterni": [{"scuola": r["school"]["scuola"], "comune": r["school"]["comune"],
                                 "provincia": r["school"]["provincia"], "regione": r["school"]["regione"],
                                 "provider": provider_of(r["albo_esterno"]), "etichetta": r.get("albo_etichetta", ""),
                                 "url": r["albo_esterno"], "sito": r["school"]["site"]}
                                for r in results if r.get("albo_esterno")],
               "non_raggiungibili": [{"scuola": r["school"]["scuola"], "sito": r["school"]["site"],
                                      "motivo": r["error"]} for r in results if r.get("error")]}
    if prev:  # ripetizione parziale: unisco con i risultati dell'esecuzione precedente
        for k in ("interpelli", "pagine_interpelli", "candidature_libere", "albi_esterni"):
            seen, merged = set(), []
            for x in prev.get(k, []) + payload[k]:
                if x["url"] in seen:
                    continue
                seen.add(x["url"])
                merged.append(x)
            payload[k] = merged
    with open(os.path.join(out_dir, "interpelli.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return (len(payload["interpelli"]), len(payload["pagine_interpelli"]),
            len(payload["candidature_libere"]), len(payload["albi_esterni"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--schools", help="CSV (file o URL) dell'anagrafe scuole del Ministero")
    ap.add_argument("--only-site", help="prova su un solo sito")
    ap.add_argument("--out", default="risultati")
    ap.add_argument("--email", default="tua@email.it", help="il tuo contatto, scritto nello User-Agent")
    ap.add_argument("--limit", type=int, default=0, help="solo le prime N scuole (per prove)")
    ap.add_argument("--workers", type=int, default=6, help="siti diversi in parallelo")
    ap.add_argument("--delay", type=float, default=2.0, help="secondi tra due richieste allo stesso sito")
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--nuvola", action="store_true",
                    help="legge anche gli albi pubblici Nuvola (1 richiesta ogni 4 secondi, come chiede il loro robots.txt)")
    ap.add_argument("--nuvola-only", action="store_true",
                    help="non visita le scuole: rilegge solo gli albi Nuvola gia' trovati e sostituisce i vecchi risultati Nuvola")
    ap.add_argument("--retry-failed", action="store_true",
                    help="riprova solo i siti non raggiungibili dell'ultima esecuzione e unisce i risultati")
    ap.add_argument("--max-pages", type=int, default=30, help="pagine esplorate per sito")
    a = ap.parse_args()
    global USER_AGENT
    USER_AGENT = f"InterpelliBot/0.2 (+contatto: {a.email})"

    if a.nuvola_only:
        import os
        prev = json.load(open(os.path.join(a.out, "interpelli.json"), encoding="utf-8"))
        prev["interpelli"] = [i for i in prev.get("interpelli", []) if i.get("trovato_in") != NUVOLA_TAG]
        try:  # cancella dal database i vecchi risultati Nuvola (potevano contenere nomi di persone)
            db = sqlite3.connect(os.path.join(a.out, "interpelli.db"))
            db.execute("DELETE FROM interpelli WHERE trovato_in=?", (NUVOLA_TAG,))
            db.commit()
        except sqlite3.Error:
            pass
        todo = [e for e in prev.get("albi_esterni", []) if "nuvola.madisoft.it/bacheca-digitale/bacheca/" in e["url"]]
        log(f"Lettura albi Nuvola: {len(todo)} scuole, circa {len(todo) * 4 // 60 + 1} minuti")
        nf, results = Fetcher(max(a.delay, 4), a.timeout), []
        for e in todo:
            new = read_nuvola(nf, e["url"])
            school = {"scuola": e["scuola"], "codice": "", "comune": e["comune"], "provincia": e["provincia"],
                      "regione": e["regione"], "site": e["sito"]}
            results.append({"school": school, "items": new, "error": None, "albo_esterno": None})
            if new:
                log(f"  {e['scuola'][:40]:40} -> {len(new)}")
        n, pg, m, e2 = save(results, a.out, prev)
        log(f"\nFatto: {n} interpelli attivi in totale. File in {a.out}/")
        return
    if a.only_site:
        schools = [{"site": norm_site(a.only_site), "scuola": "Sito di prova", "codice": "", "comune": "",
                    "provincia": "", "regione": "Friuli Venezia Giulia"}]
    elif a.schools:
        schools = load_schools(a.schools)
    else:
        ap.error("serve --schools oppure --only-site")
    prev = None
    if a.retry_failed:
        import os
        prev = json.load(open(os.path.join(a.out, "interpelli.json"), encoding="utf-8"))
        failed = {x["sito"] for x in prev.get("non_raggiungibili", [])}
        schools = [s for s in schools if s["site"] in failed]
    if a.limit:
        schools = schools[:a.limit]
    log(f"{len(schools)} siti da controllare")

    results, errors = [], 0
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(crawl_site_polite, s, a.delay, a.timeout, a.max_pages): s for s in schools}
        for i, fut in enumerate(as_completed(futs), 1):
            s = futs[fut]
            try:
                r = fut.result()
            except Exception as e:  # un sito rotto non deve fermare gli altri
                r = {"school": s, "items": [], "external": [], "albo_esterno": None, "error": str(e)}
            errors += bool(r["error"])
            results.append(r)
            log(f"[{i}/{len(schools)}] {s['scuola'][:40]:40} -> {len(r['items'])} interpelli"
                + (f" ({r['error']})" if r["error"] else "")
                + (f" | albo esterno ({r.get('albo_etichetta') or 'senza nome'}): {r['albo_esterno']}"
                   if r.get("albo_esterno") else ""))
    if a.nuvola:
        nf = Fetcher(max(a.delay, 4), a.timeout)  # un solo lettore, una richiesta alla volta
        todo = [r for r in results if r.get("albo_esterno") and "nuvola.madisoft.it/bacheca-digitale/bacheca/" in r["albo_esterno"]]
        log(f"\nLettura albi Nuvola: {len(todo)} scuole, circa {len(todo) * 4 // 60 + 1} minuti")
        for r in todo:
            try:
                new = read_nuvola(nf, r["albo_esterno"])
            except Exception as e:
                log(f"  {r['school']['scuola'][:40]}: errore {e}")
                continue
            have = {i["url"] for i in r["items"]}
            r["items"] += [i for i in new if i["url"] not in have]
            if new:
                log(f"  {r['school']['scuola'][:40]:40} -> {len(new)} interpelli dall'albo Nuvola")
    motivi = {}
    for r in results:
        if r["error"]:
            k = re.sub(r".*\((.*)\).*", r"\1", r["error"])
            motivi[k] = motivi.get(k, 0) + 1
    if motivi:
        log("Motivi dei siti non raggiungibili: " + ", ".join(f"{k}: {v}" for k, v in sorted(motivi.items(), key=lambda x: -x[1])))
    n, pg, m, e = save(results, a.out, prev)
    log(f"\nFatto: {n} interpelli attivi, {pg} pagine interpelli, {m} pagine di candidature libere, {e} scuole con albo esterno "
        f"(Nuvola, Spaggiari...), {errors} siti non raggiungibili. File in {a.out}/")


if __name__ == "__main__":
    main()
