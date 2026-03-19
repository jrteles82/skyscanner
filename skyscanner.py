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
    "outbound_dates": ["2026-06-04", "2026-06-05"],
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

    def _wait_briefly_for_results(self, page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        try:
            page.locator("text=Menores preços").first.wait_for(timeout=5000)
        except Exception:
            pass
        time.sleep(CONFIG["settle_seconds"])

    def _extract_summary_price(self, page) -> float | None:
        patterns = [
            r"Menores preços\s+a partir de\s+R\$\s*([\d\.]+(?:,\d{2})?)",
            r"Menores preços.*?R\$\s*([\d\.]+(?:,\d{2})?)",
        ]
        for sel in ["body", "main", "[role='main']"]:
            try:
                txt = page.locator(sel).first.inner_text(timeout=3000)
                if not txt:
                    continue
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
                    self._wait_briefly_for_results(page)
                    return True
            except Exception:
                pass
        return False

    def _is_probable_flight_card(self, text: str) -> bool:
        low = text.lower()
        if not text or "R$" not in text:
            return False
        if any(x in low for x in ["menores preços", "histórico", "gráfico", "monitorar", "explorar"]):
            return False
        return any(x in low for x in ["parada", "escalas", "co2", "emissões", "voo", "aeroporto"])

    def _extract_visible_flight_cards(self, page) -> list[dict]:
        cards = []
        selectors = ["[role='listitem']", "li", "div[jscontroller]", "div[role='button']"]

        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = min(loc.count(), 140)
                for i in range(count):
                    card = loc.nth(i)
                    try:
                        txt = card.inner_text(timeout=1200).strip()
                    except Exception:
                        continue

                    if not self._is_probable_flight_card(txt):
                        continue

                    nums = re.findall(r"R\$\s*([\d\.]+(?:,\d{2})?)", txt)
                    if not nums:
                        continue
                    parsed_prices = []
                    for raw in nums:
                        try:
                            parsed_prices.append(float(raw.replace('.', '').replace(',', '.')))
                        except Exception:
                            pass
                    if not parsed_prices:
                        continue

                    cards.append({
                        "selector": sel,
                        "index": i,
                        "price": min(parsed_prices),
                        "prices": parsed_prices,
                        "text": txt[:400],
                        "loc": card,
                    })
            except Exception:
                pass
            if cards:
                break
        return cards

    def _sort_candidate_cards(self, cards: list[dict], summary_price: float | None) -> list[dict]:
        def _score(item: dict):
            price = item.get("price")
            if price is None:
                return (10**12, 10**12)
            if summary_price is None:
                return (0, price)
            return (abs(price - summary_price), price)

        return sorted(cards, key=_score)

    def _try_click(self, target) -> bool:
        strategies = [
            lambda: target.click(timeout=3500),
            lambda: target.click(timeout=3500, force=True),
        ]
        for fn in strategies:
            try:
                fn()
                return True
            except Exception:
                pass
        return False

    def _wait_for_booking_page(self, page) -> bool:
        if "/travel/flights/booking" in (page.url or ""):
            return True
        try:
            page.wait_for_url(re.compile(r".*/travel/flights/booking.*"), timeout=12000)
            return True
        except Exception:
            return "/travel/flights/booking" in (page.url or "")

    def _open_booking_from_card(self, page, card) -> bool:
        try:
            card.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        card_clicked = False
        try:
            card.click(timeout=4000)
            card_clicked = True
            time.sleep(1.5)
        except Exception:
            pass

        if self._wait_for_booking_page(page):
            return True

        action_labels = [
            "Selecionar voo", "Ver voos", "Selecionar", "Reservar", "Opções de reserva",
            "Continuar", "Ver opção", "Escolher"
        ]

        targets = [card, page]
        for target in targets:
            for label in action_labels:
                for role in ["button", "link"]:
                    try:
                        loc = target.get_by_role(role, name=re.compile(label, re.I))
                        if loc.count() > 0 and self._try_click(loc.first):
                            time.sleep(1.8)
                            if self._wait_for_booking_page(page):
                                return True
                    except Exception:
                        pass

        if card_clicked:
            try:
                card.dblclick(timeout=2500)
                time.sleep(1.5)
            except Exception:
                pass

        return self._wait_for_booking_page(page)

    def _collect_booking_text_blocks(self, page) -> list[str]:
        blocks = []
        selectors = [
            "[role='main'] [role='listitem']",
            "[role='main'] li",
            "[role='main'] div",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = min(loc.count(), 180)
                for i in range(count):
                    try:
                        txt = loc.nth(i).inner_text(timeout=800).strip()
                    except Exception:
                        continue
                    if txt and "R$" in txt:
                        blocks.append(txt)
                if blocks:
                    break
            except Exception:
                pass
        if not blocks:
            try:
                body = page.locator("body").inner_text(timeout=5000)
                if body:
                    blocks = [body]
            except Exception:
                pass
        return blocks

    def _extract_vendor_options_from_text(self, text: str) -> list[dict]:
        options = []
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        known_vendors = [
            "maxmilhas", "zupper", "decolar", "booking", "gol", "latam", "azul",
            "123 milhas", "123milhas", "viajanet", "voeazul", "smiles", "kayak",
            "mytrip", "trip.com", "edreams", "kiwi", "cvc", "submarino viagens",
        ]

        for idx, line in enumerate(lines):
            low = line.lower()
            prices = re.findall(r"R\$\s*([\d\.]+(?:,\d{2})?)", line)
            if not prices:
                continue
            try:
                price = float(prices[-1].replace('.', '').replace(',', '.'))
            except Exception:
                continue

            vendor = ""
            context = " ".join(lines[max(0, idx - 1): min(len(lines), idx + 2)])
            for name in known_vendors:
                if name in context.lower():
                    vendor = name
                    break

            if not vendor:
                m = re.search(r"(?:Reserve com a|Reservar com|Comprar com|Emitido por|Vendido por)\s+([^\n\r]+)", context, re.I)
                if m:
                    vendor = m.group(1).strip(" :-")

            if vendor:
                options.append({"vendor": vendor.strip(), "price": price})

        dedup = []
        seen = set()
        for item in options:
            key = (item["vendor"].lower(), item["price"])
            if key not in seen:
                seen.add(key)
                dedup.append(item)
        return dedup

    def _extract_booking_options(self, page) -> tuple[str, float | None, list[dict]]:
        blocks = self._collect_booking_text_blocks(page)
        options = []
        for block in blocks:
            options.extend(self._extract_vendor_options_from_text(block))

        if not options:
            try:
                body = page.locator("body").inner_text(timeout=7000)
            except Exception:
                return "", None, []
            for pattern in [
                r"Reserve com a\s+([^\n\r]+?)\s+R\$\s*([\d\.]+(?:,\d{2})?)",
                r"Reservar com\s+([^\n\r]+?)\s+R\$\s*([\d\.]+(?:,\d{2})?)",
                r"Vendido por\s+([^\n\r]+?)\s+R\$\s*([\d\.]+(?:,\d{2})?)",
            ]:
                for vendor, raw_price in re.findall(pattern, body, flags=re.IGNORECASE):
                    try:
                        price = float(raw_price.replace(".", "").replace(",", "."))
                    except Exception:
                        continue
                    options.append({"vendor": vendor.strip(), "price": price})

        cleaned = []
        seen = set()
        for item in options:
            vendor = (item.get("vendor") or "").strip()
            price = item.get("price")
            if not vendor or price is None:
                continue
            key = (vendor.lower(), price)
            if key not in seen:
                seen.add(key)
                cleaned.append({"vendor": vendor, "price": price})

        if not cleaned:
            return "", None, []
        best = sorted(cleaned, key=lambda x: x["price"])[0]
        return best["vendor"], best["price"], cleaned

    def search(self, route: RouteQuery) -> FlightResult:
        context = getattr(self.browser, "new_context", None)
        ctx = None
        if callable(context):
            ctx = self.browser.new_context(
                locale="pt-BR",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            page = ctx.new_page()
        else:
            page = self.browser.new_page()
        page.set_default_timeout(int(CONFIG["timeout_ms"]))
        url = build_google_flights_url(route)
        notes = []
        try:
            page.goto(url, wait_until="domcontentloaded")
            self._accept_cookies_if_present(page)
            self._wait_briefly_for_results(page)

            summary_price = self._extract_summary_price(page)
            notes.append(f"summary_price={format_brl(summary_price)}" if summary_price is not None else "summary_price=N/D")

            clicked_lowest = self._click_lowest_prices_tab(page)
            notes.append(f"clicou_menores_precos={'sim' if clicked_lowest else 'nao'}")

            cards = self._extract_visible_flight_cards(page)
            notes.append(f"cards_encontrados={len(cards)}")

            best_vendor = ""
            best_vendor_price = None
            booking_options = []
            final_price = None
            booking_opened = False

            ranked_cards = self._sort_candidate_cards(cards, summary_price)
            max_attempts = min(len(ranked_cards), 4)
            for idx, item in enumerate(ranked_cards[:max_attempts], start=1):
                price = item.get("price")
                notes.append(f"tentativa_card_{idx}={format_brl(price)}")
                if self._open_booking_from_card(page, item["loc"]):
                    booking_opened = True
                    notes.append(f"booking_aberto_no_card={idx}")
                    best_vendor, best_vendor_price, booking_options = self._extract_booking_options(page)
                    if best_vendor:
                        final_price = best_vendor_price if best_vendor_price is not None else price
                        break
                    notes.append(f"booking_sem_vendor_no_card={idx}")
                    try:
                        page.go_back(wait_until="domcontentloaded")
                        self._wait_briefly_for_results(page)
                    except Exception:
                        break
                else:
                    notes.append(f"falha_abrir_booking_card={idx}")

            if not booking_opened and ranked_cards:
                fallback = ranked_cards[0]
                final_price = fallback.get("price")
                notes.append(f"fallback_primeira_lista={format_brl(final_price)}")

            if best_vendor:
                notes.append(f"melhor_vendedor={best_vendor} ({format_brl(best_vendor_price)})")
                notes.append(f"opcoes_reserva={len(booking_options)}")

            if final_price is None and best_vendor_price is not None:
                final_price = best_vendor_price

            if final_price is None and ranked_cards:
                final_price = ranked_cards[0].get("price")

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
            try:
                page.close()
            except Exception:
                pass
            if ctx is not None:
                try:
                    ctx.close()
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
