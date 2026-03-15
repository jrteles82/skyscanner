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
                price_band TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_results_route
            ON results (origin, destination, outbound_date, inbound_date, created_at)
            """
        )
        self.conn.commit()

    def save(self, result: FlightResult, price_band: str) -> None:
        self.conn.execute(
            """
            INSERT INTO results (
                created_at, site, origin, destination, outbound_date, inbound_date,
                price, currency, url, notes, price_band
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    return f"https://www.google.com/travel/flights?q={quote(q)}&hl=pt-BR&curr=BRL"


def describe_trip(route: RouteQuery) -> str:
    if route.trip_type == "oneway":
        return f"{route.origin}->{route.destination} | {route.outbound_date} | ida simples"
    return f"{route.origin}->{route.destination} | {route.outbound_date}/{route.inbound_date} | ida e volta"


class GoogleFlightsScraper:
    def __init__(self, browser: Browser):
        self.browser = browser

    def search(self, route: RouteQuery) -> FlightResult:
        page = self.browser.new_page(locale="pt-BR")
        page.set_default_timeout(int(CONFIG["timeout_ms"]))
        url = build_google_flights_url(route)
        notes = []
        try:
            page.goto(url, wait_until="domcontentloaded")
            self._accept_cookies_if_present(page)
            time.sleep(CONFIG["settle_seconds"])

            # Tentativas de capturar o menor preço visível em pt-BR.
            text_chunks = []
            selectors = [
                "body",
                "main",
                "[role='main']",
            ]
            for selector in selectors:
                try:
                    text = page.locator(selector).inner_text(timeout=3000)
                    if text:
                        text_chunks.append(text)
                except Exception:
                    pass

            combined = "\n".join(text_chunks)
            price = parse_price_brl(combined)
            if price is None:
                notes.append("Preço não identificado automaticamente; ajuste de seletor pode ser necessário.")

            return FlightResult(
                site="google_flights",
                origin=route.origin,
                destination=route.destination,
                outbound_date=route.outbound_date,
                inbound_date=route.inbound_date,
                trip_type=route.trip_type,
                price=price,
                currency="BRL",
                url=page.url,
                notes=" ".join(notes),
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

    def _accept_cookies_if_present(self, page: Page) -> None:
        labels = [
            "Aceitar tudo",
            "Aceito",
            "I agree",
            "Accept all",
        ]
        for label in labels:
            try:
                page.get_by_role("button", name=label).click(timeout=2000)
                time.sleep(1)
                return
            except Exception:
                pass


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
