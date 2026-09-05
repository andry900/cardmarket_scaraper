#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import re
import time
from urllib.parse import parse_qs, unquote_plus, urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright._impl._errors import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

EURO_PATTERN = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€|€\s*(\d{1,3}(?:\.\d{3})*,\d{2})")
DEFAULT_BANNED = [
    "foro", "fori", "graffi", "buco", "hole", "scratch",
    "damaged", "danneggiato", "rotto", "broke", "crepa", "rovinat", "difett", "dmg", "difect", "wear",
    "dent", "ding", "shock on", "shock al", "transport damage", "damage from transport",
    "damage in shipping", "damaged in transit", "ammaccato", "ammaccatura",
]
DEFAULT_SPECULATIVE = [
    "possibile",
    "papabile",
    "could be",
    "possible",
    "maybe",
    "perhaps",
    "potential",
    "potenziale",
    "probabile",
    "probably",
    "candidate",
    "candidato",
    "candidata",
    "grading candidate",
    "candidato grading",
    "for grading",
    "da gradare",
    "to grade",
]
COMMENT_ATTRS = ["title", "data-bs-original-title", "aria-label", "data-original-title"]
NEGATING_PREFIXES = ["no", "non", "senza", "nessun", "nessuna", "nessuno", "privo di", "priva di", "privi di", "prive di", "senza alcun", "senza alcuna", "senza alcuno", "without", "free of", "free from", "no signs of"]

# eBay price extraction: matches "EUR 45,99" or "45,99 EUR" anywhere in row text.
_EBAY_PRICE_RE = re.compile(r"EUR\s*([0-9][0-9\.\,]*)|([0-9][0-9\.\,]*)\s*EUR", re.IGNORECASE)

_EBAY_IT_MARKERS = [
    " ita ", " italiano ", " italian ", " lingua italiana ", " in italiano ", " edizione italiana ",
]
_EBAY_JP_MARKERS = [
    " jap ", " japan ", " japanese ", " giapponese ", " jpn ", " jp ", " japonais ",
]
_GENERIC_QUERY_TOKENS = {
    "pokemon", "booster", "box", "bundle", "mini", "tin", "collection", "elite", "trainer", "etb",
    "special", "illustration", "card", "cards", "set", "single", "singles", "psa", "bgs", "cgc",
    "ita", "italiano", "italiana", "italian", "jap", "japan", "japanese", "giapponese", "jpn", "jp",
    "v", "ex", "sv", "tg", "lor", "sit",
}


@dataclass
class OfferResult:
    price: float
    price_display: str
    row_text: str
    comment_text: str
    effective_price: float | None = None


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
    return re.sub(r"[^\w]+", "", normalize_text(value))


def has_keyword(text: str, keyword: str) -> bool:
    text_norm = normalize_text(text)
    kw_norm = normalize_text(keyword)
    if " " not in kw_norm:
        return re.search(rf"\b{re.escape(kw_norm)}\w*\b", text_norm) is not None
    if kw_norm in text_norm:
        return True
    return compact_text(kw_norm) in compact_text(text)


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


def _extract_ebay_query_hint_tokens(url: str) -> set[str]:
    """Extract meaningful keyword hints from _nkw query text."""
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        nkw_raw = query.get("_nkw", [""])[0]
        nkw_text = normalize_text(unquote_plus(nkw_raw))
        tokens = re.findall(r"[a-z0-9]{3,}", nkw_text)
    except Exception:
        return set()

    hints: set[str] = set()
    for token in tokens:
        if token in _GENERIC_QUERY_TOKENS:
            continue
        if token.isdigit():
            continue
        hints.add(token)
    return hints


def extract_card_number(product_url: str) -> str | None:
    """Extract the CardMarket card number from a product slug such as EVS180."""
    slug = urlparse(product_url).path.rsplit("/", maxsplit=1)[-1]
    matches = re.findall(r"[a-zA-Z]{2,}(\d{1,4})\b", slug)
    return matches[-1] if matches else None


def ebay_item_matches_card_number(item_text: str, card_number: str | None) -> bool:
    """Return True when no card number is required or the listing contains it."""
    if not card_number:
        return True
    return re.search(rf"(?<!\d){re.escape(card_number)}(?!\d)", item_text) is not None


def infer_ebay_expected_language(url: str, configured_language: str | None = None) -> str:
    """Infer expected listing language: any, it, jp."""
    if configured_language:
        lang = normalize_text(configured_language)
        if lang in {"it", "italian", "italiano"}:
            return "it"
        if lang in {"jp", "jpn", "japanese", "giapponese", "jap"}:
            return "jp"
        return "any"

    low = normalize_text(unquote_plus(url))
    if any(marker.strip() in low for marker in ["jap", "japan", "japanese", "giapponese", "jpn", "jp"]):
        return "jp"
    if any(marker.strip() in low for marker in ["ita", "italiano", "italiana", "italian"]):
        return "it"
    return "any"


def _contains_any_marker(text: str, markers: list[str]) -> bool:
    hay = f" {normalize_text(text)} "
    return any(marker in hay for marker in markers)


def ebay_item_matches_language(
    item_text: str,
    *,
    expected_language: str,
    query_hint_tokens: set[str],
) -> bool:
    """Return True if an eBay item text is compatible with expected language."""
    if expected_language == "any":
        return True

    text = normalize_text(item_text)
    text_spaced = f" {text} "
    has_it = _contains_any_marker(text, _EBAY_IT_MARKERS)
    has_jp = _contains_any_marker(text, _EBAY_JP_MARKERS)

    has_hint_token = any(token in text_spaced for token in query_hint_tokens)

    if expected_language == "it":
        if has_jp:
            return False
        return has_it or has_hint_token

    if expected_language == "jp":
        if has_it:
            return False
        return has_jp or has_hint_token

    return True


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
    discount_any: list[str],
    discount_percent: float,
    manual_challenge: bool,
    manual_challenge_timeout_seconds: int,
    max_retries: int = 3,
) -> ProductResult:
    def is_challenge(p: Any) -> bool:
        title = p.title().strip().lower()
        body = normalize_text(p.inner_text("body"))
        content_lc = p.content().lower()
        return (
            "just a moment" in title
            or "enable javascript and cookies to continue" in body
            or "challenge-platform" in content_lc
            or "esecuzione della verifica di sicurezza" in body
            or "verifica di sicurezza" in body
            or "controllo di sicurezza" in body
            or "cloudflare" in body and "verifica" in body
        )

    def wait_until_challenge_cleared(timeout_seconds: int) -> bool:
        deadline = time.time() + max(1, timeout_seconds)
        while time.time() < deadline:
            if not is_challenge(page):
                return True
            page.wait_for_timeout(1500)
        return not is_challenge(page)

    for attempt in range(1, max_retries + 1):
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(3000)

        if not is_challenge(page):
            break

        if manual_challenge:
            print(f"[INFO] Challenge Cloudflare (tentativo {attempt}/{max_retries}) per: {name}")
            print("[INFO] Risolvi il challenge nel browser aperto, poi premi INVIO qui.")
            input()
            print(
                f"[INFO] Verifico risoluzione challenge (attendo fino a {manual_challenge_timeout_seconds}s)..."
            )
            if wait_until_challenge_cleared(manual_challenge_timeout_seconds):
                break
            print("[WARN] Challenge ancora attiva dopo il timeout manuale.")
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
    normalized_discount = [normalize_text(x) for x in discount_any]

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
            effective_price = price
            if normalized_discount and any(has_non_negated_keyword(full_text, word) for word in normalized_discount):
                effective_price = round(price * (1 - discount_percent / 100), 2)
            candidate = OfferResult(
                price=price,
                price_display=price_display,
                row_text=row_text_raw,
                comment_text=comment_text,
                effective_price=effective_price,
            )
            if best_offer is None or effective_price < (best_offer.effective_price or best_offer.price):
                best_offer = candidate

        processed_count = row_count

        # Products without discounts can stop at the first valid offer because
        # Cardmarket listings are ordered by price ascending.
        if best_offer is not None and not normalized_discount:
            break

        # Discounted grading labels may make a later listing cheaper after the
        # discount, so continue loading results for those products.
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


def _ebay_is_login_page(page: Any) -> bool:
    url = normalize_text(page.url)
    title = normalize_text(_safe_page_title(page))
    body = normalize_text(_safe_page_body_text(page))
    return (
        "signin.ebay" in url
        or "/signin" in url
        or "account/signin" in url
        or "accedi" in title and "ebay" in title
        or "sign in" in title and "ebay" in title
        or "registrati o accedi" in body
        or "accedi o registrati" in body
        or "continua con l accesso" in body
        or "continue with sign in" in body
    )


def _ebay_try_dismiss_login(page: Any) -> None:
    selectors = [
        "button:has-text('No grazie')",
        "button:has-text('Non ora')",
        "button:has-text('Continua senza')",
        "button:has-text('Continue without')",
        "button:has-text('Maybe later')",
        "button:has-text('Resta disconnesso')",
        "button[aria-label='Chiudi']",
        "button[aria-label='Close']",
        "#passkeys-cancel-btn",
    ]
    for selector in selectors:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                el.first.click(timeout=2000)
                page.wait_for_timeout(1000)
        except Exception:
            continue


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
        login_page = _ebay_is_login_page(page)

        if login_page:
            print(f"[EBAY] Prompt login rilevato per '{name}', provo modalita guest...")
            _ebay_try_dismiss_login(page)
            _wait_for_ebay_results(page, timeout_ms)
            denied = _ebay_is_access_denied(page)
            server_error = _ebay_is_server_error_page(page)
            login_page = _ebay_is_login_page(page)

        if not denied and not server_error and not login_page:
            return True
        if attempt < max_denied_retries:
            if denied:
                reason = "Access Denied"
            elif server_error:
                reason = "errore pagina eBay"
            else:
                reason = "pagina login eBay"
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
    expected_language: str | None = None,
    card_number: str | None = None,
    banned_words: list[str] | None = None,
    max_items: int = 3,
    timeout_ms: int = 120000,
) -> tuple[float | None, list[float]]:
    """Scrape Italian eBay sold listings for a product.

    Navigates to the provided sold-listings URL, retrying automatically on
    Access Denied (Akamai block resolved by reload, no user action needed).
    Extracts prices from the first ``max_items`` rows that pass the quality
    filter and returns ``(average_price, prices_used)``.

    Items are accepted only inside this range around *cardmarket_price*:
    - not below 25 % (>= 75 % of CardMarket)
    - not above 40 % (<= 140 % of CardMarket)
    """
    resolved_language = infer_ebay_expected_language(url, expected_language)
    hint_tokens = _extract_ebay_query_hint_tokens(url)
    normalized_banned = [normalize_text(w) for w in (banned_words if banned_words is not None else DEFAULT_BANNED)]

    def collect_prices(active_page: Any) -> tuple[float | None, list[float]]:
        items = _ebay_item_locator(active_page)
        count = items.count()
        print(f"[EBAY] Trovati {count} elementi nella pagina.")

        min_acceptable = cardmarket_price * 0.75
        max_acceptable = cardmarket_price * 1.40

        prices: list[float] = []
        excluded_language = 0
        excluded_card_number = 0
        for i in range(count):
            if len(prices) >= max_items:
                break
            try:
                item_text = items.nth(i).inner_text()
            except Exception:
                continue
            if not ebay_item_matches_language(
                item_text,
                expected_language=resolved_language,
                query_hint_tokens=hint_tokens,
            ):
                excluded_language += 1
                continue
            if not ebay_item_matches_card_number(item_text, card_number):
                excluded_card_number += 1
                continue
            item_text_norm = normalize_text(item_text)
            if any(
                has_keyword(item_text_norm, w) and not keyword_is_negated(item_text_norm, w)
                for w in normalized_banned
            ):
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

        if excluded_language:
            print(f"[EBAY] Esclusi {excluded_language} annunci per mismatch lingua ({resolved_language}).")
        if excluded_card_number:
            print(f"[EBAY] Esclusi {excluded_card_number} annunci senza numero carta {card_number}.")

        if not prices:
            return None, []
        avg = sum(prices) / len(prices)
        return avg, prices

    # First attempt on the current page (usually more trusted in CDP sessions).
    if _navigate_ebay_with_retries(
        page,
        url=url,
        timeout_ms=timeout_ms,
        max_denied_retries=3,
        name=name,
    ):
        avg, prices = collect_prices(page)
        if avg is not None:
            return avg, prices

    # Fallback on a fresh tab if current page failed or yielded no data.
    print(f"[EBAY] Nessun risultato utile sul tab corrente per '{name}', provo su nuovo tab...")
    ebay_page = page.context.new_page()
    try:
        if not _navigate_ebay_with_retries(
            ebay_page,
            url=url,
            timeout_ms=timeout_ms,
            max_denied_retries=3,
            name=name,
        ):
            print(f"[EBAY] Blocco/errore eBay dopo 3 tentativi per: {name}")
            return None, []

        return collect_prices(ebay_page)
    finally:
        try:
            ebay_page.close()
        except Exception:
            pass


def load_products(config_path: Path) -> list[dict[str, Any]]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Il file di configurazione deve contenere una lista JSON.")
    return data


def load_existing_results(output_path: Path) -> dict[str, dict[str, Any]]:
    if not output_path.exists():
        return {}
    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
        return {r["url"]: r for r in data.get("results", [])}
    except Exception:
        return {}


def create_browser_context(p: Any, args: argparse.Namespace) -> tuple[Any, Any, bool]:
    """Create a Playwright context using the requested browser strategy.

    Returns (browser, context, context_is_browser).
    If context_is_browser is True, the context itself owns the browser process and
    closing context is enough.
    """
    mode = args.browser_mode

    if mode == "persistent-chrome":
        if args.headless:
            raise ValueError("La modalita persistent-chrome richiede browser visibile (senza --headless).")

        profile_dir = str(Path(args.user_data_dir).resolve())
        launch_kwargs: dict[str, Any] = {
            "headless": False,
            "locale": "it-IT",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
            "ignore_default_args": ["--enable-automation"],
        }

        # If user passes Brave path (or any Chromium browser), prefer it.
        if args.browser_executable_path.strip():
            launch_kwargs["executable_path"] = args.browser_executable_path.strip()
        else:
            launch_kwargs["channel"] = "chrome"

        context = p.chromium.launch_persistent_context(profile_dir, **launch_kwargs)
        return context.browser, context, True

    if mode == "connect-cdp":
        if not args.cdp_url.strip():
            raise ValueError("Per --browser-mode connect-cdp devi impostare --cdp-url.")

        try:
            browser = p.chromium.connect_over_cdp(args.cdp_url.strip())
        except PlaywrightError as exc:
            raise ValueError(
                "Impossibile connettersi a CDP. Avvia Brave/Chrome con "
                "--remote-debugging-port=9222 e verifica che la porta sia in ascolto. "
                f"Dettaglio: {exc}"
            ) from exc
        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = browser.new_context(locale="it-IT")
        return browser, context, False

    launch_options: dict[str, Any] = {"headless": args.headless}
    if args.browser_executable_path.strip():
        launch_options["executable_path"] = args.browser_executable_path.strip()
    browser = p.chromium.launch(**launch_options)
    context = browser.new_context(locale="it-IT")
    return browser, context, False


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
        "--manual-challenge-timeout-seconds",
        type=int,
        default=180,
        help="Timeout attesa risoluzione challenge manuale dopo INVIO (default: 180s).",
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
    parser.add_argument(
        "--source",
        choices=["all", "cardmarket", "ebay"],
        default="all",
        help="Fonte da cui estrarre i prezzi: 'all' (default), 'cardmarket' (solo CardMarket), 'ebay' (solo eBay usando prezzi CardMarket dal JSON precedente).",
    )
    parser.add_argument(
        "--browser-mode",
        choices=["playwright", "persistent-chrome", "connect-cdp"],
        default="playwright",
        help=(
            "Strategia browser: 'playwright' (default), 'persistent-chrome' (Chrome reale con profilo persistente), "
            "'connect-cdp' (si collega a Chrome gia aperto via DevTools)."
        ),
    )
    parser.add_argument(
        "--user-data-dir",
        default=".browser_profile",
        help="Cartella profilo per --browser-mode persistent-chrome (default: .browser_profile).",
    )
    parser.add_argument(
        "--cdp-url",
        default="http://127.0.0.1:9222",
        help="URL DevTools per --browser-mode connect-cdp (default: http://127.0.0.1:9222).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limita il numero di prodotti processati (0 = tutti). Utile per test rapidi.",
    )
    parser.add_argument(
        "--use-existing-page",
        action="store_true",
        help="Con --browser-mode connect-cdp riusa il primo tab gia aperto invece di crearne uno nuovo.",
    )

    args = parser.parse_args()
    products = load_products(Path(args.config))
    if args.limit > 0:
        products = products[:args.limit]

    source = args.source
    results: list[ProductResult] = []

    # For --source ebay we need CardMarket prices from a previous run.
    existing_results: dict[str, dict[str, Any]] = {}
    if source == "ebay":
        existing_results = load_existing_results(Path(args.output_json))
        if not existing_results:
            print("[WARN] --source ebay: nessun risultato CardMarket precedente trovato in output-json. "
                  "Esegui prima con --source cardmarket (o all) per popolare i prezzi di riferimento.")

    with sync_playwright() as p:
        browser, context, context_is_browser = create_browser_context(p, args)
        if args.browser_mode == "connect-cdp" and args.use_existing_page:
            existing_pages = [pg for pg in context.pages if not pg.url.startswith("about:blank")]
            if existing_pages:
                page = existing_pages[0]
                print(f"[INFO] Riuso tab esistente: {page.url}")
            else:
                page = context.new_page()
                print("[INFO] Nessun tab esistente trovato, apro un nuovo tab.")
        else:
            page = context.new_page()

        # Reduce basic automation fingerprint in JS-exposed fields.
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        for idx, product in enumerate(products):
            name = str(product.get("name") or product.get("url"))
            url = str(product["url"])
            required_any = [str(x) for x in product.get("required_any", [])]
            discount_any = [str(x) for x in product.get("discount_any", [])]
            discount_percent = float(product.get("discount_percent", 0))
            quantity = max(1, int(product.get("quantity", 1)))
            ebay_url_product = str(product.get("ebay_url", "")).strip()
            ebay_language_product = str(product.get("ebay_language", "")).strip() or None

            # ── CardMarket scraping ────────────────────────────────────────────
            if source in ("all", "cardmarket"):
                if idx > 0:
                    min_delay = max(0.0, args.request_delay_seconds)
                    max_delay = max(min_delay, args.request_delay_max_seconds)
                    if max_delay > 0:
                        delay = random.uniform(min_delay, max_delay)
                        print(f"\n[INFO] Attendo {delay:.1f}s prima della prossima richiesta...")
                        time.sleep(delay)

                try:
                    result = scrape_product(
                        page,
                        name=name,
                        url=url,
                        quantity=quantity,
                        banned_words=args.banned,
                        speculative_words=args.speculative,
                        required_any=required_any,
                        discount_any=discount_any,
                        discount_percent=discount_percent,
                        manual_challenge=args.manual_challenge,
                        manual_challenge_timeout_seconds=args.manual_challenge_timeout_seconds,
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
            else:
                # --source ebay: rebuild a ProductResult from the previous run
                prev = existing_results.get(url)
                prev_offer = prev.get("best_offer") if prev else None
                if prev_offer:
                    best = OfferResult(
                        price=float(prev_offer["price"]),
                        price_display=str(prev_offer.get("price_display", "")),
                        row_text=str(prev_offer.get("row_text", "")),
                        comment_text=str(prev_offer.get("comment_text", "")),
                        effective_price=prev_offer.get("effective_price"),
                    )
                    result = ProductResult(
                        name=name,
                        url=url,
                        quantity=quantity,
                        best_offer=best,
                        scanned_rows=int(prev.get("scanned_rows", 0)),
                        eligible_rows=int(prev.get("eligible_rows", 0)),
                        excluded_banned=int(prev.get("excluded_banned", 0)),
                        excluded_speculative=int(prev.get("excluded_speculative", 0)),
                        excluded_required=int(prev.get("excluded_required", 0)),
                        note=prev.get("note"),
                    )
                    print(f"[EBAY] Uso prezzo CardMarket precedente per '{name}': {best.price_display}")
                else:
                    print(f"[EBAY] Nessun prezzo CardMarket precedente per '{name}', salto eBay.")
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
                        note="Nessun prezzo CardMarket precedente disponibile per la ricerca eBay.",
                    )

            results.append(result)

            # ── eBay sold comparison ───────────────────────────────────────────
            if source in ("all", "ebay"):
                if result.best_offer and ebay_url_product:
                    if source == "ebay" and idx > 0:
                        min_delay = max(0.0, args.request_delay_seconds)
                        max_delay = max(min_delay, args.request_delay_max_seconds)
                        if max_delay > 0:
                            delay = random.uniform(min_delay, max_delay)
                            print(f"\n[INFO] Attendo {delay:.1f}s prima della prossima richiesta...")
                            time.sleep(delay)
                    elif source == "all":
                        time.sleep(3)

                    print(f"[EBAY] Cerco venduti italiani su eBay per: {name}")
                    ebay_avg, ebay_prices = scrape_ebay_sold(
                        page,
                        name=name,
                        url=ebay_url_product,
                        cardmarket_price=result.best_offer.effective_price or result.best_offer.price,
                        expected_language=ebay_language_product,
                        card_number=extract_card_number(url),
                        banned_words=args.banned,
                    )
                    result.ebay_url = ebay_url_product
                    result.ebay_avg_price = ebay_avg
                    result.ebay_prices_used = ebay_prices

                    if ebay_avg is not None:
                        prices_str = ", ".join(format_euro(p) for p in ebay_prices)
                        print(f"[EBAY] Prezzi usati: [{prices_str}] -> media {format_euro(ebay_avg)}")
                        effective_price = result.best_offer.effective_price or result.best_offer.price
                        if ebay_avg < effective_price:
                            result.final_price = ebay_avg
                            result.final_source = "ebay"
                            print(f"[EBAY] eBay ({format_euro(ebay_avg)}) < CardMarket ({format_euro(effective_price)}) -> uso eBay")
                        else:
                            result.final_price = effective_price
                            result.final_source = "cardmarket"
                            print(f"[EBAY] eBay ({format_euro(ebay_avg)}) >= CardMarket ({format_euro(effective_price)}) -> uso CardMarket")
                    else:
                        result.final_price = effective_price
                        result.final_source = "cardmarket"
                        print("[EBAY] Nessun venduto eBay trovato -> uso CardMarket")
                elif result.best_offer:
                    result.final_price = result.best_offer.effective_price or result.best_offer.price
                    result.final_source = "cardmarket"
            elif result.best_offer:
                # --source cardmarket: no eBay lookup
                result.final_price = result.best_offer.effective_price or result.best_offer.price
                result.final_source = "cardmarket"

            if result.best_offer is not None:
                final_price = result.final_price if result.final_price is not None else result.best_offer.price
                line_total = final_price * result.quantity
                source_label = f" [{result.final_source}]" if result.final_source else ""
                print(f"[PREZZO] {result.name}: {format_euro(final_price)}{source_label} x {result.quantity} = {format_euro(line_total)}")
            else:
                print(f"[PREZZO] {result.name}: nessun prezzo valido trovato")

        context.close()
        if not context_is_browser:
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
                "effective_price": result.best_offer.effective_price or result.best_offer.price,
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
