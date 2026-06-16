#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright._impl._errors import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

EURO_PATTERN = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€|€\s*(\d{1,3}(?:\.\d{3})*,\d{2})")
DEFAULT_BANNED = ["foro", "fori", "graffi", "buco", "hole", "scratch", "damaged", "danneggiato", "rotto", "broke", "crepa", "rovinat", "difett", "dmg", "difect", "wear"]
DEFAULT_SPECULATIVE = ["possibile", "papabile", "could be", "possible"]
COMMENT_ATTRS = ["title", "data-bs-original-title", "aria-label", "data-original-title"]
NEGATING_PREFIXES = ["no", "non", "senza", "nessun", "nessuna", "nessuno", "privo di", "priva di", "privi di", "prive di", "senza alcun", "senza alcuna", "senza alcuno", "without", "free of", "free from", "no signs of"]

# eBay price extraction: matches "EUR 45,99" or "45,99 EUR" anywhere in row text.
_EBAY_PRICE_RE = re.compile(r"EUR\s*([0-9][0-9\.\,]*)|([0-9][0-9\.\,]*)\s*EUR", re.IGNORECASE)


@dataclass
class OfferResult:
    price: float
    price_display: str
    row_text: str
    comment_text: str


@dataclass
class ProductResult:
    name: str
    url: str
    quantity: int
    best_offer: OfferResult | None
    scanned_rows: int
    eligible_rows: int
    excluded_banned: int
    excluded_speculative: int
    excluded_required: int
    note: str | None = None
    ebay_url: str | None = None
    ebay_avg_price: float | None = None
    ebay_prices_used: list[float] | None = None
    final_price: float | None = None
    final_source: str | None = None


def normalize_text(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"\s+", " ", value)
    return value


def compact_text(value: str) -> str:
    return normalize_text(value).replace(" ", "")


def has_keyword(text: str, keyword: str) -> bool:
    text_norm = normalize_text(text)
    kw_norm = normalize_text(keyword)
    if kw_norm in text_norm:
        return True
    return kw_norm.replace(" ", "") in compact_text(text)


def keyword_is_negated(text: str, keyword: str) -> bool:
    text_norm = normalize_text(text)
    kw_norm = normalize_text(keyword)
    kw_pattern = re.escape(kw_norm).replace(r"\ ", r"\s+") + r"\w*"

    for prefix in NEGATING_PREFIXES:
        prefix_pattern = re.escape(prefix).replace(r"\ ", r"\s+")
        pattern = rf"\b{prefix_pattern}\b(?:\s+\w+){{0,2}}\s+{kw_pattern}\b"
        if re.search(pattern, text_norm):
            return True

    post_patterns = [
        rf"\b{kw_pattern}\b(?:\s|-)+(?:free|less)\b",
        rf"\b{kw_pattern}\b(?:\s+)?(?:without|free of|free from)\b",
    ]
    for pattern in post_patterns:
        if re.search(pattern, text_norm):
            return True

    return False


def has_non_negated_keyword(text: str, keyword: str) -> bool:
    return has_keyword(text, keyword) and not keyword_is_negated(text, keyword)


def has_uncertain_question_mark(text: str) -> bool:
    # Ignore '?' that appears inside URLs like '?timestamp=...'.
    text_no_urls = re.sub(r"https?://\S+", " ", text)
    return "?" in text_no_urls


def parse_euro(value: str) -> float:
    return float(value.replace(".", "").replace(",", "."))


def format_euro(value: float) -> str:
    # Italian-like format: 1234.56 -> 1.234,56 €
    base = f"{value:,.2f}"
    base = base.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{base} €"


def _formula_price(value: float) -> str:
    if value % 1 == 0:
        return str(int(value))
    return f"{value:.2f}".replace(".", ",")


def extract_ebay_price(text: str) -> float | None:
    """Return the first EUR price found in eBay item text (item price, before shipping)."""
    match = _EBAY_PRICE_RE.search(text)
    if not match:
        return None
    amount_str = (match.group(1) or match.group(2) or "").strip()
    if not amount_str:
        return None
    try:
        return parse_euro(amount_str)
    except ValueError:
        return None


def extract_price(row_text: str) -> tuple[float, str] | None:
    match = EURO_PATTERN.search(row_text)
    if not match:
        return None

    amount = match.group(1) or match.group(2)
    if not amount:
        return None

    return parse_euro(amount), f"{amount} €"


def extract_comment_blob(row_locator: Any) -> str:
    chunks: list[str] = []

    for attr in COMMENT_ATTRS:
        candidates = row_locator.locator(f"[{attr}]")
        count = candidates.count()
        for i in range(min(count, 25)):
            value = candidates.nth(i).get_attribute(attr)
            if value:
                chunks.append(value)

    return normalize_text(" ".join(chunks))


def scrape_product(
    page: Any,
    *,
    name: str,
    url: str,
    quantity: int,
    banned_words: list[str],
    speculative_words: list[str],
    required_any: list[str],
    manual_challenge: bool,
    max_retries: int = 3,
) -> ProductResult:
    def is_challenge(p: Any) -> bool:
        title = p.title().strip().lower()
        body = normalize_text(p.inner_text("body"))
        return (
            "just a moment" in title
            or "enable javascript and cookies to continue" in body
            or "challenge-platform" in p.content().lower()
        )

    for attempt in range(1, max_retries + 1):
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(3000)

        if not is_challenge(page):
            break

        if manual_challenge:
            print(f"[INFO] Challenge Cloudflare (tentativo {attempt}/{max_retries}) per: {name}")
            print("[INFO] Risolvi il challenge nel browser aperto, poi premi INVIO qui.")
            input()
            page.wait_for_timeout(3000)
            if not is_challenge(page):
                break
        else:
            wait_sec = 15 * attempt
            print(f"[INFO] Challenge rilevata per: {name} (tentativo {attempt}/{max_retries}), riprovo tra {wait_sec}s...")
            time.sleep(wait_sec)
    else:
        return ProductResult(
            name=name,
            url=url,
            quantity=quantity,
            best_offer=None,
            scanned_rows=0,
            eligible_rows=0,
            excluded_banned=0,
            excluded_speculative=0,
            excluded_required=0,
            note=f"Challenge Cloudflare non risolta dopo {max_retries} tentativi.",
        )

    best_offer: OfferResult | None = None
    scanned_rows = 0
    eligible_rows = 0
    excluded_banned = 0
    excluded_speculative = 0
    excluded_required = 0

    normalized_banned = [normalize_text(x) for x in banned_words]
    normalized_speculative = [normalize_text(x) for x in speculative_words]
    normalized_required = [normalize_text(x) for x in required_any]

    def get_rows():
        r = page.locator("div.row.g-0.article-row")
        if r.count() == 0:
            r = page.locator("div[class*='article-row']")
        return r

    max_load_more = 20
    processed_count = 0

    for load_attempt in range(max_load_more + 1):
        rows = get_rows()
        row_count = rows.count()

        # Process only newly loaded rows
        for i in range(processed_count, row_count):
            row = rows.nth(i)
            row_text_raw = row.inner_text().strip()
            if not row_text_raw:
                continue

            row_text = normalize_text(row_text_raw)
            if "€" not in row_text_raw:
                continue

            scanned_rows += 1

            price_info = extract_price(row_text_raw)
            if not price_info:
                continue

            price, price_display = price_info
            comment_text = extract_comment_blob(row)
            full_text = f"{row_text} {comment_text}".strip()

            banned_hit = False
            for word in normalized_banned:
                if has_keyword(full_text, word):
                    if keyword_is_negated(full_text, word):
                        continue
                    banned_hit = True
                    break

            if banned_hit:
                excluded_banned += 1
                continue

            if any(has_keyword(full_text, word) for word in normalized_speculative) or has_uncertain_question_mark(full_text):
                excluded_speculative += 1
                continue

            if normalized_required and not any(has_non_negated_keyword(full_text, token) for token in normalized_required):
                excluded_required += 1
                continue

            eligible_rows += 1
            candidate = OfferResult(
                price=price,
                price_display=price_display,
                row_text=row_text_raw,
                comment_text=comment_text,
            )
            if best_offer is None or candidate.price < best_offer.price:
                best_offer = candidate

        processed_count = row_count

        # Listings are ordered by price ascending, so if we already found
        # a valid offer in the currently loaded rows we can stop here.
        if best_offer is not None:
            break

        # Keep loading more results to ensure we capture the true minimum price.
        more_btn = page.locator("button:has-text('MOSTRA PIÙ RISULTATI'), a:has-text('MOSTRA PIÙ RISULTATI')")
        if more_btn.count() == 0:
            break
        try:
            more_btn.first.scroll_into_view_if_needed()
            more_btn.first.click()
            page.wait_for_timeout(2000)
            print(f"[INFO] Carico altri risultati per: {name} (passo {load_attempt + 1})")
        except Exception:
            break

    return ProductResult(
        name=name,
        url=url,
        quantity=quantity,
        best_offer=best_offer,
        scanned_rows=scanned_rows,
        eligible_rows=eligible_rows,
        excluded_banned=excluded_banned,
        excluded_speculative=excluded_speculative,
        excluded_required=excluded_required,
    )


# ── eBay page helpers (ported from FPop) ─────────────────────────────────────

def _safe_page_title(page: Any, retries: int = 3) -> str:
    for _ in range(max(1, retries)):
        try:
            return page.title()
        except PlaywrightError as exc:
            if "Execution context was destroyed" not in str(exc):
                return ""
            page.wait_for_timeout(200)
    return ""


def _safe_page_body_text(page: Any, retries: int = 3) -> str:
    for _ in range(max(1, retries)):
        try:
            return page.inner_text("body")
        except PlaywrightError as exc:
            if "Execution context was destroyed" not in str(exc):
                return ""
            page.wait_for_timeout(200)
    return ""


def _ebay_is_access_denied(page: Any) -> bool:
    title = normalize_text(_safe_page_title(page))
    body = normalize_text(_safe_page_body_text(page))
    return (
        "access denied" in title
        or "you don't have permission" in body
        or "errors.edgesuite.net" in body
    )


def _ebay_is_server_error_page(page: Any) -> bool:
    title = normalize_text(_safe_page_title(page))
    body = normalize_text(_safe_page_body_text(page))
    return (
        title == "sorry"
        or title.startswith("sorry ")
        or "sorry something went wrong on our end" in body
        or "sorry something went wrong on our end" in title
        or "something went wrong on our end" in body
        or "something went wrong on our end" in title
        or "we are working on it" in body
        or "si e verificato un problema" in body
    )


def _ebay_is_transient_wait_page(page: Any) -> bool:
    body = normalize_text(_safe_page_body_text(page))
    return (
        "ci scusiamo per l'attesa" in body
        or "ci scusiamo per l attesa" in body
        or "sorry for the wait" in body
        or "sorry for the delay" in body
        or "controllo del browser prima dell'accesso a ebay" in body
        or "controllo del browser prima dell accesso a ebay" in body
        or "a breve verrai reindirizzato ai contenuti richiesti" in body
        or "attendi qualche istante" in body
        or "browser check before accessing ebay" in body
    )


def _ebay_item_locator(page: Any) -> Any:
    primary = page.locator("li.s-item")
    if primary.count() > 0:
        return primary
    fallback = page.locator("ul.srp-results > li")
    if fallback.count() > 0:
        return fallback
    return page.locator("li[data-view*='iid:']")


def _wait_for_ebay_results(page: Any, timeout_ms: int) -> None:
    wait_step_ms = 1000
    max_wait_ms = min(timeout_ms, 15000)
    elapsed_ms = 0
    while elapsed_ms < max_wait_ms:
        denied = _ebay_is_access_denied(page)
        server_error = _ebay_is_server_error_page(page)
        waiting = _ebay_is_transient_wait_page(page)
        if (denied or server_error) and not waiting:
            return
        if not denied and not server_error and not waiting:
            if _ebay_item_locator(page).count() > 0:
                return
        page.wait_for_timeout(wait_step_ms)
        elapsed_ms += wait_step_ms


def _navigate_ebay_with_retries(
    page: Any,
    *,
    url: str,
    timeout_ms: int,
    max_denied_retries: int,
    name: str,
) -> bool:
    """Navigate to an eBay URL, retrying automatically on block/error pages.

    Access Denied and transient eBay error pages can resolve on reload
    without any user interaction.
    """
    for attempt in range(1, max_denied_retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            print(f"[EBAY] Timeout navigazione (tentativo {attempt}/{max_denied_retries}).")
            if attempt == max_denied_retries:
                return False
            continue
        _wait_for_ebay_results(page, timeout_ms)
        denied = _ebay_is_access_denied(page)
        server_error = _ebay_is_server_error_page(page)
        if not denied and not server_error:
            return True
        if attempt < max_denied_retries:
            reason = "Access Denied" if denied else "errore pagina eBay"
            print(
                f"[EBAY] {reason} per '{name}' (tentativo {attempt}/{max_denied_retries}), riprovo..."
            )
    return False


# ── eBay sold scraping ────────────────────────────────────────────────────────

def scrape_ebay_sold(
    page: Any,
    *,
    name: str,
    url: str,
    cardmarket_price: float,
    max_items: int = 3,
    timeout_ms: int = 120000,
) -> tuple[float | None, list[float]]:
    """Scrape Italian eBay sold listings for a product.

    Navigates to the provided sold-listings URL, retrying automatically on
    Access Denied (Akamai block resolved by reload, no user action needed).
    Extracts prices from the first ``max_items`` rows that pass the quality
    filter and returns ``(average_price, prices_used)``.

    Items are accepted only inside this range around *cardmarket_price*:
    - not below 20 % (>= 80 % of CardMarket)
    - not above 40 % (<= 140 % of CardMarket)
    """
    if not _navigate_ebay_with_retries(
        page,
        url=url,
        timeout_ms=timeout_ms,
        max_denied_retries=3,
        name=name,
    ):
        print(f"[EBAY] Blocco/errore eBay dopo 3 tentativi per: {name}")
        return None, []

    items = _ebay_item_locator(page)
    count = items.count()
    print(f"[EBAY] Trovati {count} elementi nella pagina.")

    min_acceptable = cardmarket_price * 0.80
    max_acceptable = cardmarket_price * 1.40

    prices: list[float] = []
    for i in range(count):
        if len(prices) >= max_items:
            break
        try:
            item_text = items.nth(i).inner_text()
        except Exception:
            continue
        price = extract_ebay_price(item_text)
        if price is None:
            continue
        if price < min_acceptable:
            print(f"[EBAY] Scarto {format_euro(price)} (< soglia {format_euro(min_acceptable)})")
            continue
        if price > max_acceptable:
            print(f"[EBAY] Scarto {format_euro(price)} (> soglia {format_euro(max_acceptable)})")
            continue
        prices.append(price)

    if not prices:
        return None, []

    avg = sum(prices) / len(prices)
    return avg, prices


def load_products(config_path: Path) -> list[dict[str, Any]]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Il file di configurazione deve contenere una lista JSON.")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Estrae il prezzo minimo CardMarket filtrando commenti indesiderati e condizioni speciali."
    )
    parser.add_argument(
        "--config",
        default="products.json",
        help="Percorso al file JSON con i prodotti da analizzare.",
    )
    parser.add_argument(
        "--banned",
        nargs="*",
        default=DEFAULT_BANNED,
        help="Parole da escludere se presenti nei commenti/testo offerta.",
    )
    parser.add_argument(
        "--speculative",
        nargs="*",
        default=DEFAULT_SPECULATIVE,
        help="Parole/frasi da escludere per annunci non certificati (es. papabile, possible).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Esegue il browser in headless. Se Cloudflare blocca, usa senza questo flag.",
    )
    parser.add_argument(
        "--manual-challenge",
        action="store_true",
        help="Se compare challenge Cloudflare, attende la risoluzione manuale nel browser invece di ritentare automaticamente.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Numero massimo di tentativi automatici in caso di challenge Cloudflare.",
    )
    parser.add_argument(
        "--output-json",
        default="results.json",
        help="File JSON di output con i risultati.",
    )
    parser.add_argument(
        "--browser-executable-path",
        default="",
        help="Percorso eseguibile browser (es. Brave). Se vuoto usa Chromium di Playwright.",
    )
    parser.add_argument(
        "--request-delay-seconds",
        type=float,
        default=7.0,
        help="Secondi minimi di attesa tra una richiesta pagina e la successiva.",
    )
    parser.add_argument(
        "--request-delay-max-seconds",
        type=float,
        default=11.0,
        help="Secondi massimi di attesa tra una richiesta pagina e la successiva.",
    )

    args = parser.parse_args()
    products = load_products(Path(args.config))

    results: list[ProductResult] = []

    with sync_playwright() as p:
        launch_options: dict[str, Any] = {"headless": args.headless}
        if args.browser_executable_path.strip():
            launch_options["executable_path"] = args.browser_executable_path.strip()
        browser = p.chromium.launch(**launch_options)
        context = browser.new_context(locale="it-IT")
        page = context.new_page()

        for idx, product in enumerate(products):
            if idx > 0:
                min_delay = max(0.0, args.request_delay_seconds)
                max_delay = max(min_delay, args.request_delay_max_seconds)
                if max_delay > 0:
                    delay = random.uniform(min_delay, max_delay)
                    print(f"\n[INFO] Attendo {delay:.1f}s prima della prossima richiesta...")
                    time.sleep(delay)

            name = str(product.get("name") or product.get("url"))
            url = str(product["url"])
            required_any = [str(x) for x in product.get("required_any", [])]
            quantity = max(1, int(product.get("quantity", 1)))

            try:
                result = scrape_product(
                    page,
                    name=name,
                    url=url,
                    quantity=quantity,
                    banned_words=args.banned,
                    speculative_words=args.speculative,
                    required_any=required_any,
                    manual_challenge=args.manual_challenge,
                    max_retries=args.max_retries,
                )
            except PlaywrightTimeoutError:
                result = ProductResult(
                    name=name,
                    url=url,
                    quantity=quantity,
                    best_offer=None,
                    scanned_rows=0,
                    eligible_rows=0,
                    excluded_banned=0,
                    excluded_speculative=0,
                    excluded_required=0,
                    note="Timeout durante il caricamento pagina.",
                )

            results.append(result)

            # ── eBay sold comparison ───────────────────────────────────────────
            ebay_url_product = str(product.get("ebay_url", "")).strip()
            if result.best_offer and ebay_url_product:
                time.sleep(3)
                print(f"[EBAY] Cerco venduti italiani su eBay per: {name}")
                ebay_avg, ebay_prices = scrape_ebay_sold(
                    page,
                    name=name,
                    url=ebay_url_product,
                    cardmarket_price=result.best_offer.price,
                )
                result.ebay_url = ebay_url_product
                result.ebay_avg_price = ebay_avg
                result.ebay_prices_used = ebay_prices

                if ebay_avg is not None:
                    prices_str = ", ".join(format_euro(p) for p in ebay_prices)
                    print(f"[EBAY] Prezzi usati: [{prices_str}] -> media {format_euro(ebay_avg)}")
                    if ebay_avg < result.best_offer.price:
                        result.final_price = ebay_avg
                        result.final_source = "ebay"
                        print(f"[EBAY] eBay ({format_euro(ebay_avg)}) < CardMarket ({result.best_offer.price_display}) -> uso eBay")
                    else:
                        result.final_price = result.best_offer.price
                        result.final_source = "cardmarket"
                        print(f"[EBAY] eBay ({format_euro(ebay_avg)}) >= CardMarket ({result.best_offer.price_display}) -> uso CardMarket")
                else:
                    result.final_price = result.best_offer.price
                    result.final_source = "cardmarket"
                    print("[EBAY] Nessun venduto eBay trovato -> uso CardMarket")
            elif result.best_offer:
                result.final_price = result.best_offer.price
                result.final_source = "cardmarket"

            if result.best_offer is not None:
                final_price = result.final_price if result.final_price is not None else result.best_offer.price
                line_total = final_price * result.quantity
                source_label = f" [{result.final_source}]" if result.final_source else ""
                print(f"[PREZZO] {result.name}: {format_euro(final_price)}{source_label} x {result.quantity} = {format_euro(line_total)}")
            else:
                print(f"[PREZZO] {result.name}: nessun prezzo valido trovato")

        context.close()
        browser.close()

    payload: list[dict[str, Any]] = []
    values_list: list[tuple[str, int, float, float, str]] = []

    for result in results:
        print(f"\n=== {result.name} ===")
        print(f"URL: {result.url}")
        print(f"Quantita: {result.quantity}")

        row_data: dict[str, Any] = {
            "name": result.name,
            "url": result.url,
            "quantity": result.quantity,
            "scanned_rows": result.scanned_rows,
            "eligible_rows": result.eligible_rows,
            "excluded_banned": result.excluded_banned,
            "excluded_speculative": result.excluded_speculative,
            "excluded_required": result.excluded_required,
            "best_offer": None,
            "note": result.note,
        }

        if result.best_offer is None:
            print("Nessuna offerta valida trovata con i filtri richiesti.")
            if result.note:
                print(f"Nota: {result.note}")
        else:
            final_price = result.final_price if result.final_price is not None else result.best_offer.price
            final_source = result.final_source or "cardmarket"
            line_total = final_price * result.quantity

            print(f"Prezzo minimo CardMarket: {result.best_offer.price_display}")
            if result.ebay_avg_price is not None:
                print(f"Media eBay venduti ({len(result.ebay_prices_used or [])} articoli): {format_euro(result.ebay_avg_price)}")
            print(f"Prezzo finale usato [{final_source}]: {format_euro(final_price)}")
            print(f"Subtotale ({result.quantity}x): {format_euro(line_total)}")
            print(f"Estratto offerta: {result.best_offer.row_text[:220]}")
            if result.best_offer.comment_text:
                print(f"Commento rilevato: {result.best_offer.comment_text[:220]}")
            values_list.append(
                (result.name, result.quantity, final_price, line_total, format_euro(final_price))
            )
            row_data["best_offer"] = {
                "price": result.best_offer.price,
                "price_display": result.best_offer.price_display,
                "row_text": result.best_offer.row_text,
                "comment_text": result.best_offer.comment_text,
            }
            if result.ebay_url:
                row_data["ebay_url"] = result.ebay_url
                row_data["ebay_avg_price"] = result.ebay_avg_price
                row_data["ebay_avg_price_display"] = format_euro(result.ebay_avg_price) if result.ebay_avg_price is not None else None
                row_data["ebay_prices_used"] = result.ebay_prices_used or []
            row_data["final_price"] = final_price
            row_data["final_price_display"] = format_euro(final_price)
            row_data["final_source"] = final_source
            row_data["line_total"] = line_total
            row_data["line_total_display"] = format_euro(line_total)

        payload.append(row_data)

    total_sum = sum(item[3] for item in values_list)
    total_examined_objects = sum(result.quantity for result in results)
    total_with_valid_price = sum(result.quantity for result in results if result.best_offer is not None)

    print("\n=== ELENCO VALORI ===")
    if not values_list:
        print("Nessun valore valido disponibile.")
    else:
        for idx, (name, quantity, _unit_price, line_total, price_display) in enumerate(values_list, start=1):
            print(f"{idx}. {name}: {price_display} x {quantity} = {format_euro(line_total)}")
        print(f"Somma totale: {format_euro(total_sum)}")
        formula_terms = []
        for _name, quantity, unit_price, _line_total, _price_display in values_list:
            p = _formula_price(unit_price)
            formula_terms.append(f"{p}*{quantity}" if quantity > 1 else p)
        print(f"Formula Google Sheet: ={'+'  .join(formula_terms)}")

    print(f"Numero oggetti esaminati (con quantita): {total_examined_objects}")
    print(f"Numero oggetti con prezzo valido: {total_with_valid_price}")

    output_data: dict[str, Any] = {
        "results": payload,
        "values": [
            {
                "name": name,
                "quantity": quantity,
                "unit_price": unit_price,
                "price_display": price_display,
                "line_total": line_total,
                "line_total_display": format_euro(line_total),
            }
            for name, quantity, unit_price, line_total, price_display in values_list
        ],
        "sum_total": total_sum,
        "sum_total_display": format_euro(total_sum),
        "objects_examined_total_quantity": total_examined_objects,
        "objects_with_valid_price_total_quantity": total_with_valid_price,
    }

    output_path = Path(args.output_json)
    output_path.write_text(json.dumps(output_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nRisultati salvati in: {output_path}")


if __name__ == "__main__":
    main()
