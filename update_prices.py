#!/usr/bin/env python3
"""
Aggiorna results.json con prezzi CardMarket manuali e ri-scrapa eBay.

Flusso:
  1. Legge cardmarket_prices.json  (prezzi CardMarket impostati a mano)
  2. Aggiorna best_offer.price in results.json
  3. Ri-scrapa eBay con la nuova soglia di prezzo
  4. Ricalcola final_price, final_source, line_total, values, sum_total
  5. Salva results.json

Uso:
  python update_prices.py
  python update_prices.py --prices-file miei_prezzi.json --results-file results.json
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

# Importa le funzioni di scraping eBay da scraper.py
from scraper import (
    format_euro,
    _formula_price,
    scrape_ebay_sold,
)


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_price_map(prices: list[dict]) -> dict[str, float]:
    """Restituisce {url: price} dalle entry di cardmarket_prices.json."""
    return {entry["url"]: float(entry["price"]) for entry in prices}


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggiorna prezzi CardMarket e ri-scrapa eBay")
    parser.add_argument("--prices-file", default="cardmarket_prices.json",
                        help="File JSON con i prezzi CardMarket manuali (default: cardmarket_prices.json)")
    parser.add_argument("--results-file", default="results.json",
                        help="File results.json da aggiornare (default: results.json)")
    parser.add_argument("--no-ebay", action="store_true",
                        help="Aggiorna solo i prezzi CardMarket senza ri-scrapar eBay")
    parser.add_argument("--headless", action="store_true", default=True,
                        help="Browser headless (default: True)")
    parser.add_argument("--delay", type=float, default=5.0,
                        help="Secondi di attesa tra richieste eBay (default: 5)")
    args = parser.parse_args()

    prices_path = Path(args.prices_file)
    results_path = Path(args.results_file)

    if not prices_path.exists():
        print(f"[ERRORE] File prezzi non trovato: {prices_path}")
        return
    if not results_path.exists():
        print(f"[ERRORE] File risultati non trovato: {results_path}")
        return

    price_map = build_price_map(load_json(prices_path))
    data = load_json(results_path)
    results: list[dict] = data["results"]

    # ── Aggiorna prezzi CardMarket ────────────────────────────────────────────
    updated_count = 0
    for item in results:
        url = item["url"]
        if url not in price_map:
            print(f"[WARN] Nessun prezzo manuale per: {item['name']} — salto")
            continue
        new_price = price_map[url]
        old_price = item.get("best_offer", {}).get("price") if item.get("best_offer") else None
        if item.get("best_offer") is None:
            print(f"[WARN] {item['name']}: best_offer mancante, salto")
            continue
        item["best_offer"]["price"] = new_price
        item["best_offer"]["price_display"] = format_euro(new_price)
        if old_price != new_price:
            print(f"[CM] {item['name']}: {format_euro(old_price) if old_price else '?'} → {format_euro(new_price)}")
            updated_count += 1
        else:
            print(f"[CM] {item['name']}: invariato ({format_euro(new_price)})")

    print(f"\n[CM] {updated_count} prezzi aggiornati su {len(results)} item.")

    # ── Ri-scrapa eBay ────────────────────────────────────────────────────────
    if not args.no_ebay:
        print("\n[EBAY] Avvio scraping eBay con i nuovi prezzi CardMarket come soglia...\n")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=args.headless)
            context = browser.new_context(locale="it-IT")
            page = context.new_page()

            for idx, item in enumerate(results):
                name = item["name"]
                ebay_url = item.get("ebay_url", "")
                best_offer = item.get("best_offer")

                if not best_offer or not ebay_url:
                    print(f"[EBAY] {name}: nessun best_offer o ebay_url — salto")
                    continue

                if idx > 0:
                    delay = random.uniform(args.delay, args.delay * 2)
                    print(f"[INFO] Attendo {delay:.1f}s...")
                    time.sleep(delay)

                cardmarket_price = best_offer["price"]
                print(f"[EBAY] Scraping: {name} (soglia CM: {format_euro(cardmarket_price)})")
                ebay_avg, ebay_prices = scrape_ebay_sold(
                    page,
                    name=name,
                    url=ebay_url,
                    cardmarket_price=cardmarket_price,
                )

                item["ebay_avg_price"] = ebay_avg
                item["ebay_avg_price_display"] = format_euro(ebay_avg) if ebay_avg is not None else None
                item["ebay_prices_used"] = ebay_prices

                if ebay_avg is not None:
                    prices_str = ", ".join(format_euro(p) for p in ebay_prices)
                    print(f"[EBAY] Prezzi: [{prices_str}] → media {format_euro(ebay_avg)}")

            context.close()
            browser.close()

    # ── Ricalcola final_price, line_total ─────────────────────────────────────
    values_list = []
    for item in results:
        best_offer = item.get("best_offer")
        if best_offer is None:
            continue
        cm_price = best_offer["price"]
        ebay_avg = item.get("ebay_avg_price")
        quantity = item.get("quantity", 1)

        if ebay_avg is not None and ebay_avg < cm_price:
            final_price = ebay_avg
            final_source = "ebay"
        else:
            final_price = cm_price
            final_source = "cardmarket"

        line_total = round(final_price * quantity, 2)
        item["final_price"] = final_price
        item["final_price_display"] = format_euro(final_price)
        item["final_source"] = final_source
        item["line_total"] = line_total
        item["line_total_display"] = format_euro(line_total)

        values_list.append({
            "name": item["name"],
            "quantity": quantity,
            "unit_price": final_price,
            "price_display": format_euro(final_price),
            "line_total": line_total,
            "line_total_display": format_euro(line_total),
        })

        print(f"[FINALE] {item['name']}: {format_euro(final_price)} [{final_source}] x {quantity} = {format_euro(line_total)}")

    sum_total = round(sum(v["line_total"] for v in values_list), 2)
    data["values"] = values_list
    data["sum_total"] = sum_total
    data["sum_total_display"] = format_euro(sum_total)

    save_json(results_path, data)

    print("\n=== ELENCO VALORI ===")
    for idx, v in enumerate(values_list, start=1):
        print(f"{idx}. {v['name']}: {v['price_display']} x {v['quantity']} = {v['line_total_display']}")
    print(f"Somma totale: {format_euro(sum_total)}")

    formula_terms = []
    for v in values_list:
        p = _formula_price(v["unit_price"])
        formula_terms.append(f"{p}*{v['quantity']}" if v["quantity"] > 1 else p)
    print(f"Formula Google Sheet: ={'+'  .join(formula_terms)}")

    print(f"\n[OK] {results_path} aggiornato. Totale: {format_euro(sum_total)}")


if __name__ == "__main__":
    main()
