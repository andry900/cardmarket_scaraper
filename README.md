# CardMarket Pokemon Price Extractor

Progetto Python per estrarre il prezzo minimo da pagine CardMarket Pokemon, con filtri su commenti e condizioni speciali.

## Requisiti

- Python 3.10+
- Accesso web a CardMarket

## Setup

```bash
cd cardmarket_scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Configurazione prodotti

Modifica `products.json`.

Esempio:

```json
[
  {
    "name": "Giratina V (LOR 186) - PSA 9",
    "url": "https://www.cardmarket.com/it/Pokemon/Products/Singles/Lost-Origin/Giratina-V-V3-LOR186?language=5&minCondition=2",
    "required_any": ["psa9", "psa 9"]
  }
]
```

- `required_any` (opzionale): almeno una parola deve essere presente nell'offerta/commento.
- Per Giratina PSA 9 usa `"psa9"` e `"psa 9"` come nel file esempio.

## Esecuzione

Da qualsiasi cartella/terminale (comando unico consigliato):

```bash
/Users/i526499/projects/test/cardmarket_scraper/run_cardmarket.sh
```

Questo wrapper usa automaticamente:

- venv: `/Users/i526499/projects/test/.venv/bin/python`
- modalita: `--browser-mode connect-cdp` (piu robusta contro challenge)
- browser: Brave (`/Applications/Brave Browser.app/Contents/MacOS/Brave Browser`)
- profilo persistente: `$HOME/.cm-debug-profile`
- auto-avvio Brave con `--remote-debugging-port=9222` se non e gia attivo

Puoi aggiungere opzioni extra in coda, ad esempio test rapido su 2 prodotti:

```bash
/Users/i526499/projects/test/cardmarket_scraper/run_cardmarket.sh --limit 2
```

Modalita consigliata (browser visibile, utile per challenge Cloudflare):

```bash
python scraper.py --manual-challenge
```

Con `--manual-challenge` il flusso ora e:

1. compare un solo prompt `premi INVIO`;
2. dopo INVIO, lo script aspetta fino a un timeout configurabile;
3. se il challenge resta attivo, passa al tentativo successivo (non resta in loop infinito).

Puoi cambiare attesa con:

```bash
python scraper.py --manual-challenge --manual-challenge-timeout-seconds 240
```

Modalita consigliata se Cloudflare blocca sempre Playwright standard:

```bash
python scraper.py --browser-mode persistent-chrome --manual-challenge --limit 2
```

Questa modalita usa Chrome reale con profilo persistente in `.browser_profile`.
In pratica puoi risolvere il challenge una volta e riutilizzare sessione/cookie anche ai run successivi.

Se usi Brave (consigliato nel tuo caso):

```bash
python scraper.py --browser-mode persistent-chrome --browser-executable-path "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" --manual-challenge --limit 2
```

Modalita headless:

```bash
python scraper.py --headless
```

Collegamento a Chrome gia aperto (stessa sessione del browser manuale):

1. Avvia Chrome con porta DevTools:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.cm-debug-profile"
```

2. Esegui lo scraper collegandoti a quella istanza:

```bash
python scraper.py --browser-mode connect-cdp --cdp-url http://127.0.0.1:9222 --manual-challenge --limit 2
```

Con Brave (stesso flusso CDP):

1. Avvia Brave con DevTools remoto:

```bash
/Applications/Brave\ Browser.app/Contents/MacOS/Brave\ Browser --remote-debugging-port=9222 --user-data-dir="$HOME/.cm-debug-profile"
```

2. In un altro terminale, collega lo scraper e riusa il tab aperto:

```bash
python scraper.py --browser-mode connect-cdp --cdp-url http://127.0.0.1:9222 --use-existing-page --manual-challenge --limit 2
```

Nota: esegui i due comandi separati, senza testo intermedio nella shell.

Con parole vietate personalizzate:

```bash
python scraper.py --manual-challenge --banned foro graffio buco piega
```

Output JSON personalizzato:

```bash
python scraper.py --manual-challenge --output-json risultati_cardmarket.json
```

## Cosa fa il filtro

Per ogni offerta:

1. trova il prezzo in euro;
2. raccoglie testo riga + commenti (title/tooltip/aria-label);
3. esclude offerte con parole vietate (default: `foro`, `graffio`, `buco`);
4. applica eventuale `required_any` (es. solo PSA 9);
5. ritorna il prezzo minimo tra le offerte valide.

## Nota su eBay (senza login)

Lo scraper eBay lavora in modalita guest: non richiede login.
Se eBay mostra popup o pagina di accesso, lo script prova automaticamente a chiuderla/bypassarla e ritenta.
Se eBay continua a forzare login, quel prodotto viene saltato su eBay e resta valido il prezzo CardMarket.

## Nota su Cloudflare

CardMarket protegge le pagine con challenge anti-bot. Se compare "Just a moment...":

- avvia senza `--headless`;
- usa `--manual-challenge`;
- risolvi il challenge nella finestra browser e premi INVIO nel terminale.

Se il checkbox non e cliccabile nella finestra automatizzata, usa `--browser-mode persistent-chrome`
oppure `--browser-mode connect-cdp` per lavorare con una sessione Chrome reale.
