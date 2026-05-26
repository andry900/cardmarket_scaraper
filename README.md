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

Modalita consigliata (browser visibile, utile per challenge Cloudflare):

```bash
python scraper.py --manual-challenge
```

Modalita headless:

```bash
python scraper.py --headless
```

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

## Nota su Cloudflare

CardMarket protegge le pagine con challenge anti-bot. Se compare "Just a moment...":

- avvia senza `--headless`;
- usa `--manual-challenge`;
- risolvi il challenge nella finestra browser e premi INVIO nel terminale.
