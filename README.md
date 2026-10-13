# Interpelli Friuli Venezia Giulia

Raccoglie ogni giorno gli avvisi di interpello dei siti e degli albi pubblici delle scuole
e li mostra in una pagina web che si può installare sul telefono. Opzionale: manda le novità su Telegram.

## Come è fatto
- `crawler_fvg.py` – visita le scuole e cerca gli interpelli (rispetta robots.txt, pause tra le richieste).
- `finalize.py` – segna gli avvisi nuovi e li manda a Telegram.
- `docs/` – l'app web (pubblicata con GitHub Pages) e i dati in `docs/data/interpelli.json`.
- `.github/workflows/aggiorna.yml` – lo fa partire da solo: completo ogni notte, albi Nuvola 4 volte al giorno.
- `data/scuole_fvg.csv` – l'elenco delle scuole (dall'anagrafe del Ministero).

## Impostazioni (GitHub → Settings → Secrets and variables → Actions)
| Nome | Dove | A cosa serve |
|---|---|---|
| `CONTATTO_EMAIL` | Secrets | la tua email, scritta nell'User-Agent del programma così i siti possono scriverti |
| `TELEGRAM_TOKEN` | Secrets | il token del bot (da @BotFather) – facoltativo |
| `TELEGRAM_CHAT` | Secrets | il canale, per esempio `@interpelli_fvg` – facoltativo |
| `APP_URL` | Variables | l'indirizzo dell'app, aggiunto ai messaggi – facoltativo |

## Provare a mano
Actions → «Aggiorna interpelli» → Run workflow. Modalità: `completo`, `nuvola` oppure `test-telegram`.

## Avvertenze
- L'app può sbagliare o non vedere tutto: gli avvisi vanno sempre controllati sul sito della scuola.
- Alcuni albi (Spaggiari) non si possono leggere in automatico: per quelle scuole l'app dà solo il link.
- La lettura degli albi Nuvola rispetta il loro robots.txt (una richiesta ogni 4 secondi). Prima di un uso
  pubblico ampio conviene chiedere conferma a Madisoft.
- Il programma non deve salvare nomi di persone: gli esiti e le nomine vengono scartati.
