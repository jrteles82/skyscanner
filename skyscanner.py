#!/usr/bin/env python3
"""
Monitor local de passagens usando automação do navegador.

Fonte alvo: Google Flights (sem API key)
Tecnologia: Playwright + SQLite + Telegram opcional

O que faz:
- abre o navegador como um usuário normal
- pesquisa várias combinações de datas e destinos
- extrai preço visível da página
- salva histórico em SQLite
- classifica preço por comparação com histórico
- envia alerta por Telegram opcionalmente
- roda uma vez ou em loop a cada 3 horas

Instalação:
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers .venv/bin/playwright install chromium

Uso:
    export TELEGRAM_BOT_TOKEN='...'
    export TELEGRAM_CHAT_ID='...'
    .venv/bin/python skyscanner.py run-once
    .venv/bin/python skyscanner.py daemon

Observações:
- Como o site pode mudar, os seletores podem precisar de ajustes.
- O script tenta ser conservador, com poucas consultas e pausas.
- Use apenas para suas consultas pessoais e respeite os termos do serviço do site.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import quote

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).with_name(".playwright-browsers")))

import requests

try:
    from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
except ImportError as exc:
    raise SystemExit(
        "Playwright não está instalado neste ambiente.\n"
        "Use um virtualenv local:\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/pip install -r requirements.txt\n"
        "  .venv/bin/playwright install chromium\n"
        "  .venv/bin/python skyscanner.py run-once"
    ) from exc


CONFIG = {
    "origin": "PVH",
    "destinations_br": ["JPA", "REC", "NAT"], "destinations_sa": [],
    "enable_south_america": False,
    "outbound_dates": ["2026-06-04", "2026-06-05", "2026-06-06"],
    "inbound_dates": ["2026-06-15", "2026-06-16"],
    "check_every_hours": 3,
    "headless": True,
    "timeout_ms": 45000,
    "settle_seconds": 10,
    "request_pause_seconds": 4,
    "db_path": str(Path(__file__).with_name("flight_tracker_browser.db")),
    "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "8651349481:AAHRdUKl7Dx-GJ76Yy_kQiJ4jA6TCaQ8r4g"),
    "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", "1748352987"),
    "price_alert_brl": 1800.0,
    "drop_alert_percent": 8.0,
    "target_site": "google_flights",
}


@dataclass
class RouteQuery:
    origin: str
    destination: str
    outbound_date: str
    inbound_date: str = ""
    trip_type: str = "oneway"


@dataclass
class FlightResult:
    site: str
    origin: str
    destination: str
    outbound_date: str
    price: Optional[float]
    inbound_date: str = ""
    trip_type: str = "oneway"
    currency: str = "BRL"
    url: str = ""
    notes: str = ""
    best_vendor: str = ""
    best_vendor_price: Optional[float] = None
    booking_options_json: str = ""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_brl(value: Optional[float]) -> str:
    if value is None:
        return "sem preço"
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                site TEXT NOT NULL,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                outbound_date TEXT NOT NULL,
                inbound_date TEXT NOT NULL,
                price REAL,
                currency TEXT,
                url TEXT,
                notes TEXT,
                price_band TEXT,
                best_vendor TEXT,
                best_vendor_price REAL,
                booking_options_json TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_results_route
            ON results (origin, destination, outbound_date, inbound_date, created_at)
            """
        )

        # Migrações leves para bases já existentes
        for ddl in [
            "ALTER TABLE results ADD COLUMN best_vendor TEXT",
            "ALTER TABLE results ADD COLUMN best_vendor_price REAL",
            "ALTER TABLE results ADD COLUMN booking_options_json TEXT",
        ]:
            try:
                cur.execute(ddl)
            except sqlite3.OperationalError:
                pass

        self.conn.commit()

    def save(self, result: FlightResult, price_band: str) -> None:
        self.conn.execute(
            """
            INSERT INTO results (
                created_at, site, origin, destination, outbound_date, inbound_date,
                price, currency, url, notes, price_band,
                best_vendor, best_vendor_price, booking_options_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                result.site,
                result.origin,
                result.destination,
                result.outbound_date,
                result.inbound_date,
                result.price,
                result.currency,
                result.url,
                result.notes,
                price_band,
                result.best_vendor,
                result.best_vendor_price,
                result.booking_options_json,
            ),
        )
        self.conn.commit()

    def stats_for(self, route: RouteQuery) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        row = self.conn.execute(
            """
            SELECT MIN(price) AS min_price, AVG(price) AS avg_price
            FROM results
            WHERE origin = ? AND destination = ? AND outbound_date = ? AND inbound_date = ? AND price IS NOT NULL
            """,
            (route.origin, route.destination, route.outbound_date, route.inbound_date),
        ).fetchone()
        last = self.conn.execute(
            """
            SELECT price FROM results
            WHERE origin = ? AND destination = ? AND outbound_date = ? AND inbound_date = ? AND price IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (route.origin, route.destination, route.outbound_date, route.inbound_date),
        ).fetchone()
        min_price = float(row["min_price"]) if row and row["min_price"] is not None else None
        avg_price = float(row["avg_price"]) if row and row["avg_price"] is not None else None
        last_price = float(last["price"]) if last and last["price"] is not None else None
        return min_price, avg_price, last_price


def build_queries() -> List[RouteQuery]:
    destinations = list(CONFIG["destinations_br"])
    if CONFIG["enable_south_america"]:
        destinations.extend(CONFIG["destinations_sa"])

    queries: List[RouteQuery] = []
    seen = set()
    for dest in destinations:
        for outbound in CONFIG["outbound_dates"]:
            key = (CONFIG["origin"], dest, outbound, "", "oneway")
            if key not in seen:
                seen.add(key)
                queries.append(
                    RouteQuery(
                        origin=CONFIG["origin"],
                        destination=dest,
                        outbound_date=outbound,
                        trip_type="oneway",
                    )
                )
        for inbound in CONFIG["inbound_dates"]:
            key = (dest, CONFIG["origin"], inbound, "", "oneway")
            if key not in seen:
                seen.add(key)
                queries.append(
                    RouteQuery(
                        origin=dest,
                        destination=CONFIG["origin"],
                        outbound_date=inbound,
                        trip_type="oneway",
                    )
                )
    return queries


def classify_price(price: Optional[float], min_price: Optional[float], avg_price: Optional[float]) -> str:
    if price is None:
        return "sem_preco"
    if min_price is None and avg_price is None:
        return "novo"
    if min_price is not None and price <= min_price:
        return "excelente"
    if avg_price is not None and price <= avg_price * 0.92:
        return "bom"
    if avg_price is not None and price >= avg_price * 1.15:
        return "caro"
    return "normal"


def should_alert(price: Optional[float], min_price: Optional[float], last_price: Optional[float]) -> Tuple[bool, str]:
    if price is None:
        return False, "sem preço"
    if price <= float(CONFIG["price_alert_brl"]):
        return True, f"abaixo do teto configurado ({format_brl(CONFIG['price_alert_brl'])})"
    if min_price is not None and price <= min_price:
        return True, "novo menor preço"
    if last_price is not None and last_price > 0:
        drop = ((last_price - price) / last_price) * 100.0
        if drop >= float(CONFIG["drop_alert_percent"]):
            return True, f"queda de {drop:.1f}%"
    return False, "sem gatilho"


def send_telegram_message(text: str) -> None:
    token = CONFIG["telegram_bot_token"]
    chat_id = CONFIG["telegram_chat_id"]
    if not token or not chat_id:
        print("[alerta] Telegram não configurado")
        print(text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=30)
    resp.raise_for_status()


def parse_price_brl(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = text.replace("\xa0", " ")
    patterns = [
        r"R\$\s*([\d\.]+,\d{2})",
        r"R\$\s*([\d\.]+)",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, cleaned)
        if matches:
            raw = matches[0].replace(".", "").replace(",", ".")
            try:
                return float(raw)
            except ValueError:
                pass
    return None


def build_google_flights_url(route: RouteQuery) -> str:
    if route.trip_type == "oneway":
        q = f"{route.origin} to {route.destination} {route.outbound_date} one way"
    else:
        q = f"{route.origin} to {route.destination} {route.outbound_date} return {route.inbound_date}"
    return f"https://www.google.com/travel/flights?q={quote(q)}&hl=pt-BR&gl=BR&curr=BRL"


def describe_trip(route: RouteQuery) -> str:
    if route.trip_type == "oneway":
        return f"{route.origin}->{route.destination} | {route.outbound_date} | ida simples"
    return f"{route.origin}->{route.destination} | {route.outbound_date}/{route.inbound_date} | ida e volta"


class GoogleFlightsScraper:
    def __init__(self, browser):
        self.browser = browser

    def _accept_cookies_if_present(self, page) -> None:
        labels = ["Aceitar tudo", "Aceito", "I agree", "Accept all"]
        for label in labels:
            try:
                page.get_by_role("button", name=label).click(timeout=2000)
                time.sleep(1)
                return
            except Exception:
                pass

    def _extract_summary_price(self, page) -> float | None:
        patterns = [
            r"Menores preços\s+a partir de\s+R\$\s*([\d\.]+(?:,\d{2})?)",
            r"Menores preços.*?R\$\s*([\d\.]+(?:,\d{2})?)",
        ]
        for sel in ["body", "main", "[role='main']"]:
            try:
                txt = page.locator(sel).first.inner_text(timeout=3000)
                if not txt: continue
                for pattern in patterns:
                    m = re.search(pattern, txt, flags=re.IGNORECASE | re.DOTALL)
                    if m:
                        try:
                            return float(m.group(1).replace(".", "").replace(",", "."))
                        except ValueError:
                            pass
            except Exception:
                pass
        return None

    def _click_lowest_prices_tab(self, page) -> bool:
        candidates = [
            lambda: page.get_by_text("Menores preços", exact=False),
            lambda: page.get_by_role("button", name=re.compile(r"Menores preços", re.I)),
            lambda: page.get_by_role("tab", name=re.compile(r"Menores preços", re.I)),
        ]
        for factory in candidates:
            try:
                loc = factory()
                if loc.count() > 0:
                    loc.first.click(timeout=4000)
                    time.sleep(2.5)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    time.sleep(2.0)
                    return True
            except Exception:
                pass
        return False

    def _extract_visible_flight_cards(self, page) -> list[dict]:
        cards = []
        selectors = ["[role='listitem']", "li", "div[jscontroller]", "div[role='button']"]
        
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = min(loc.count(), 120)
                for i in range(count):
                    card = loc.nth(i)
                    try:
                        txt = card.inner_text(timeout=1200).strip()
                    except Exception:
                        continue
                    
                    if not txt or "R$" not in txt: continue
                    low = txt.lower()
                    if any(x in low for x in ["menores preços", "histórico", "gráfico", "monitorar"]):
                        continue
                    
                    has_shape = any(x in low for x in ["parada", "escalas", "co2", "emissões"])
                    if not has_shape: continue
                    
                    nums = re.findall(r"R\$\s*([\d\.]+(?:,\d{2})?)", txt)
                    if not nums: continue
                    try:
                        price = float(nums[-1].replace('.', '').replace(',', '.'))
                        cards.append({"selector": sel, "index": i, "price": price, "loc": card})
                    except Exception:
                        pass
            except Exception:
                pass
            if cards: break
        return cards

    def _open_card_and_extract_vendor(self, page, card) -> tuple[str, float | None, list[dict]]:
        try:
            card.click(timeout=4000)
            time.sleep(2.5)
        except Exception:
            pass
            
        try:
            btns = card.locator("button")
            if btns.count() > 0:
                btns.last.click(timeout=3000)
                time.sleep(2.0)
        except Exception:
            pass
            
        action_labels = ["Selecionar voo", "Ver voos", "Selecionar", "Reservar", "Opções de reserva"]
        for label in action_labels:
            try:
                loc = page.get_by_role("button", name=label)
                if loc.count() > 0:
                    loc.first.click(timeout=3000)
                    time.sleep(2.5)
                    break
            except Exception:
                pass
            try:
                loc = page.get_by_role("link", name=label)
                if loc.count() > 0:
                    loc.first.click(timeout=3000)
                    time.sleep(2.5)
                    break
            except Exception:
                pass

        try:
            body = page.locator("body").inner_text(timeout=7000)
        except Exception:
            return "", None, []

        options = []
        patterns = [
            r"Reserve com a\s+([^\n\r]+?)\s+R\$\s*([\d\.]+(?:,\d{2})?)",
            r"Reservar com\s+([^\n\r]+?)\s+R\$\s*([\d\.]+(?:,\d{2})?)",
        ]
        for pattern in patterns:
            for vendor, raw_price in re.findall(pattern, body, flags=re.IGNORECASE):
                vendor = (vendor or "").strip()
                try:
                    price = float(raw_price.replace(".", "").replace(",", "."))
                except Exception:
                    continue
                if vendor:
                    options.append({"vendor": vendor, "price": price})
        
        cleaned = []
        seen = set()
        for item in options:
            key = (item["vendor"].lower(), item["price"])
            if key not in seen:
                seen.add(key)
                cleaned.append(item)
                
        if not cleaned:
            return "", None, []
            
        best = sorted(cleaned, key=lambda x: x["price"])[0]
        return best["vendor"], best["price"], cleaned

    def _open_best_flight_details_if_possible(self, page) -> None:
        for role, label in [("button", "Selecionar voo"), ("button", "Ver voos"), ("link", "Selecionar voo"), ("link", "Ver voos")]:
            try:
                locator = page.get_by_role(role, name=label)
                if locator.count() > 0:
                    locator.first.click(timeout=2500)
                    time.sleep(2)
                    return
            except Exception:
                pass

    def search(self, route: RouteQuery) -> FlightResult:
        context = getattr(self.browser, "new_context", None)
        if callable(context):
            ctx = self.browser.new_context(
                locale="pt-BR",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            page = ctx.new_page()
        else:
            # It's a persistent context, so we just create a new page
            page = self.browser.new_page()
        page.set_default_timeout(int(CONFIG["timeout_ms"]))
        url = build_google_flights_url(route)
        notes = []
        try:
            page.goto(url, wait_until="domcontentloaded")
            self._accept_cookies_if_present(page)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            time.sleep(CONFIG["settle_seconds"])

            summary_price = self._extract_summary_price(page)
            if summary_price is not None:
                notes.append(f"summary_price={format_brl(summary_price)}")
            else:
                notes.append("summary_price=N/D")

            clicked_lowest = self._click_lowest_prices_tab(page)
            notes.append(f"clicou_menores_precos={'sim' if clicked_lowest else 'nao'}")

            best_vendor = ""
            best_vendor_price = None
            booking_options = []
            final_price = None

            cards = self._extract_visible_flight_cards(page)
            if summary_price is not None and cards:
                # Find matching price
                for item in cards:
                    if abs(item["price"] - summary_price) < 0.01:
                        final_price = item["price"]
                        notes.append(f"card_preco_encontrado={format_brl(final_price)}")
                        best_vendor, best_vendor_price, booking_options = self._open_card_and_extract_vendor(page, item["loc"])
                        break
            
            if final_price is None and cards:
                cheapest = sorted(cards, key=lambda x: x["price"])[0]
                final_price = cheapest["price"]
                notes.append(f"fallback_list_min_price={format_brl(final_price)}")
                best_vendor, best_vendor_price, booking_options = self._open_card_and_extract_vendor(page, cheapest["loc"])

            if best_vendor:
                notes.append(f"melhor_vendedor={best_vendor} ({format_brl(best_vendor_price)})")

            if not best_vendor:
                self._open_best_flight_details_if_possible(page)
                v2, p2, options2 = self._open_card_and_extract_vendor(page, page.locator("body"))
                if v2:
                    best_vendor = v2
                    best_vendor_price = p2
                    booking_options = options2
                    notes.append(f"fallback_global_melhor_vendedor={best_vendor} ({format_brl(best_vendor_price)})")

            if best_vendor_price is not None and final_price is None:
                final_price = best_vendor_price

            if final_price is None:
                notes.append("Preço não identificado automaticamente.")

            return FlightResult(
                site="google_flights",
                origin=route.origin,
                destination=route.destination,
                outbound_date=route.outbound_date,
                inbound_date=route.inbound_date,
                trip_type=route.trip_type,
                price=final_price,
                currency="BRL",
                url=page.url,
                notes=" | ".join(notes),
                best_vendor=best_vendor,
                best_vendor_price=best_vendor_price,
                booking_options_json=json.dumps(booking_options, ensure_ascii=False) if booking_options else "",
            )
        except PlaywrightTimeoutError:
            return FlightResult(
                site="google_flights",
                origin=route.origin,
                destination=route.destination,
                outbound_date=route.outbound_date,
                inbound_date=route.inbound_date,
                trip_type=route.trip_type,
                price=None,
                currency="BRL",
                url=page.url if page else url,
                notes="timeout na página",
            )
        finally:
            page.close()

class Monitor:
    def __init__(self) -> None:
        self.db = Database(CONFIG["db_path"])

    def run_once(self) -> List[FlightResult]:
        routes = build_queries()
        results: List[FlightResult] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=bool(CONFIG["headless"]))
            scraper = GoogleFlightsScraper(browser)

            for route in routes:
                result = scraper.search(route)
                min_price, avg_price, last_price = self.db.stats_for(route)
                band = classify_price(result.price, min_price, avg_price)
                self.db.save(result, band)
                results.append(result)
                print(
                    f"[coleta] {describe_trip(route)} | {format_brl(result.price)} | {band}"
                )

                do_alert, reason = should_alert(result.price, min_price, last_price)
                if do_alert:
                    msg = (
                        f"✈️ Alerta de passagem\n"
                        f"Rota: {route.origin} → {route.destination}\n"
                        f"Data: {route.outbound_date}\n"
                        f"Tipo: {'ida simples' if route.trip_type == 'oneway' else 'ida e volta'}\n"
                        f"Preço: {format_brl(result.price)}\n"
                        f"Motivo: {reason}\n"
                        f"Site: {result.site}"
                    )
                    try:
                        send_telegram_message(msg)
                    except Exception as exc:
                        print(f"[erro] telegram: {exc}")

                time.sleep(CONFIG["request_pause_seconds"])

            browser.close()

        return results

    def daemon(self) -> None:
        interval = int(CONFIG["check_every_hours"]) * 3600
        while True:
            started = time.time()
            try:
                self.run_once()
            except Exception as exc:
                print(f"[erro] execução: {exc}")
            elapsed = time.time() - started
            sleep_for = max(60, interval - int(elapsed))
            print(f"[daemon] próxima execução em {sleep_for // 60} min")
            time.sleep(sleep_for)


def print_summary(results: Iterable[FlightResult]) -> None:
    sorted_results = sorted(
        [r for r in results if r.price is not None],
        key=lambda x: x.price if x.price is not None else 10**12,
    )
    print("\n=== MELHORES RESULTADOS ===")
    for item in sorted_results[:10]:
        print(
            f"{describe_trip(RouteQuery(item.origin, item.destination, item.outbound_date, item.inbound_date, item.trip_type))} | "
            f"{format_brl(item.price)} | {item.site}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor local de passagens via navegador")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run-once")
    sub.add_parser("daemon")
    sub.add_parser("show-config")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    monitor = Monitor()

    if args.command == "run-once":
        results = monitor.run_once()
        print_summary(results)
        return 0

    if args.command == "daemon":
        monitor.daemon()
        return 0

    if args.command == "show-config":
        for k, v in CONFIG.items():
            print(f"{k} = {v}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
