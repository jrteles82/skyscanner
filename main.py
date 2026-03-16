from __future__ import annotations

from flask import Flask, Response, jsonify, request, stream_with_context
from pathlib import Path
import json
import os
import time
import requests
import threading
from datetime import datetime

from skyscanner import (
    CONFIG,
    Database,
    FlightResult,
    GoogleFlightsScraper,
    RouteQuery,
    build_queries,
    classify_price,
    format_brl,
    parse_price_brl,
    sync_playwright,
)

app = Flask(__name__, static_folder="static", static_url_path="/static")


SCAN_INTERVAL_SECONDS = int(os.getenv("SKYSCANNER_FULL_SCAN_EVERY_SECONDS", str(3 * 60 * 60)))
AUTO_SCAN_ENABLED = os.getenv("SKYSCANNER_AUTO_SCAN", "1") == "1"
_scan_lock = threading.Lock()
_scan_last_run_at = None


def notify_full_scan(parsed: list[dict], trigger: str = "manual") -> None:
    def _price_num(row):
        v = row.get("price")
        return v if isinstance(v, (int, float)) and v is not None else 10**12

    if not parsed:
        msg = (
            "────────── ✈️ CONSULTA COMPLETA ✈️ ──────────\n"
            "Sem dados nesta execução."
        )
        try:
            send_telegram_message(msg)
        except Exception:
            pass
        return

    idas = [r for r in parsed if str(r.get("origin", "")).upper() == "PVH" and str(r.get("destination", "")).upper() != "PVH"]
    voltas = [r for r in parsed if str(r.get("destination", "")).upper() == "PVH"]

    idas_ok = sorted([r for r in idas if r.get("price") is not None], key=_price_num)
    voltas_ok = sorted([r for r in voltas if r.get("price") is not None], key=_price_num)

    lines = [
        "────────── ✈️ CONSULTA COMPLETA ✈️ ──────────",
        f"Execução: {trigger}",
        "",
        "IDAS (PVH -> destino):",
    ]

    if idas_ok:
        for i, r in enumerate(idas_ok[:3], start=1):
            medal = "🥇 " if i == 1 else ""
            data_txt = f"{r.get('outbound_date')}" + (f" / {r.get('inbound_date')}" if r.get('inbound_date') else "")
            lines.append(f"{medal}{r.get('origin')}→{r.get('destination')} | {data_txt} | {r.get('price_fmt')} | {r.get('site')}")
    else:
        lines.append("N/D")

    lines += ["", "VOLTAS (destino -> PVH):"]
    if voltas_ok:
        for i, r in enumerate(voltas_ok[:3], start=1):
            medal = "🥇 " if i == 1 else ""
            data_txt = f"{r.get('outbound_date')}" + (f" / {r.get('inbound_date')}" if r.get('inbound_date') else "")
            lines.append(f"{medal}{r.get('origin')}→{r.get('destination')} | {data_txt} | {r.get('price_fmt')} | {r.get('site')}")
    else:
        lines.append("N/D")

    total_ok = len([r for r in parsed if r.get("price") is not None])
    lines += ["", f"Resumo: {total_ok}/{len(parsed)} rotas com preço válido."]

    msg = "\n".join(lines)
    try:
        send_telegram_message(msg)
    except Exception:
        pass


def run_full_scan(on_row=None):
    global _scan_last_run_at
    routes = build_queries()
    db = Database(get_db_path())
    parsed = []

    with _scan_lock:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=bool(CONFIG.get("headless", True)))
            scraper = GoogleFlightsScraper(browser)

            idx = 0
            total = len(routes) * 3
            for route in routes:
                # Google
                result = scraper.search(route)
                min_price, avg_price, _last_price = db.stats_for(route)
                band = classify_price(result.price, min_price, avg_price)
                db.save(result, band)
                row = {
                    "origin": result.origin,
                    "destination": result.destination,
                    "outbound_date": result.outbound_date,
                    "inbound_date": result.inbound_date,
                    "trip_type": result.trip_type,
                    "price": result.price,
                    "price_fmt": format_brl(result.price),
                    "site": result.site,
                    "notes": result.notes,
                    "price_band": band,
                }
                parsed.append(row)
                idx += 1
                if on_row:
                    on_row(idx, total, row)

                # Kayak
                k = search_kayak(route)
                min_price, avg_price, _last_price = db.stats_for(route)
                band = classify_price(k.price, min_price, avg_price)
                db.save(k, band)
                row_k = {
                    "origin": k.origin,
                    "destination": k.destination,
                    "outbound_date": k.outbound_date,
                    "inbound_date": k.inbound_date,
                    "trip_type": k.trip_type,
                    "price": k.price,
                    "price_fmt": format_brl(k.price),
                    "site": k.site,
                    "notes": k.notes,
                    "price_band": band,
                }
                parsed.append(row_k)
                idx += 1
                if on_row:
                    on_row(idx, total, row_k)

                # MaxMilhas
                m = search_maxmilhas(route)
                min_price, avg_price, _last_price = db.stats_for(route)
                band = classify_price(m.price, min_price, avg_price)
                db.save(m, band)
                row_m = {
                    "origin": m.origin,
                    "destination": m.destination,
                    "outbound_date": m.outbound_date,
                    "inbound_date": m.inbound_date,
                    "trip_type": m.trip_type,
                    "price": m.price,
                    "price_fmt": format_brl(m.price),
                    "site": m.site,
                    "notes": m.notes,
                    "price_band": band,
                }
                parsed.append(row_m)
                idx += 1
                if on_row:
                    on_row(idx, total, row_m)

            browser.close()

    _scan_last_run_at = datetime.now().isoformat()
    return parsed


def _auto_scan_loop():
    while True:
        try:
            parsed = run_full_scan()
            notify_full_scan(parsed, trigger="agendada")
            print(f"[auto-scan] consulta completa executada em {_scan_last_run_at}")
        except Exception as e:
            print(f"[auto-scan] erro: {e}")
        time.sleep(SCAN_INTERVAL_SECONDS)


def start_auto_scan_if_needed():
    if not AUTO_SCAN_ENABLED:
        print("[auto-scan] desativado por SKYSCANNER_AUTO_SCAN=0")
        return

    # Evita thread duplicada com reloader do Flask
    is_reloader_main = os.getenv("WERKZEUG_RUN_MAIN") == "true"
    is_debug = os.getenv("FLASK_DEBUG") == "1"
    if is_debug and not is_reloader_main:
        return

    t = threading.Thread(target=_auto_scan_loop, daemon=True)
    t.start()
    print(f"[auto-scan] ligado: intervalo {SCAN_INTERVAL_SECONDS}s")


def get_db_path() -> str:
    configured = str(CONFIG.get("db_path", "flight_tracker_browser.db"))
    # Em Vercel/Lambda, /var/task é read-only; use /tmp (gravável)
    if os.getenv("VERCEL") or configured.startswith("/var/task"):
        return "/tmp/flight_tracker_browser.db"
    return configured


def send_telegram_message(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or CONFIG.get("telegram_bot_token")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or CONFIG.get("telegram_chat_id")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=20).raise_for_status()




def is_skyscanner_blocked(result: FlightResult) -> bool:
    url = (result.url or "").lower()
    notes = (result.notes or "").lower()
    return ("captcha" in url) or ("captcha" in notes) or (result.price is None and "skyscanner" in result.site)


def _to_route(query_args) -> RouteQuery:
    origin = query_args.get("origin", CONFIG.get("origin", "PVH")).upper()
    destination = query_args.get("destination", "JPA").upper()
    outbound_date = query_args.get("outbound_date", "")
    inbound_date = query_args.get("inbound_date", "")
    trip_type = "roundtrip" if inbound_date else "oneway"

    if not outbound_date:
        raise ValueError("Parâmetro obrigatório: outbound_date (YYYY-MM-DD)")

    return RouteQuery(
        origin=origin,
        destination=destination,
        outbound_date=outbound_date,
        inbound_date=inbound_date,
        trip_type=trip_type,
    )


@app.route("/", methods=["GET"])
def index():
    return app.send_static_file("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "skyscanner-monitor"})


@app.route("/rotas", methods=["GET"])
def rotas():
    routes = build_queries()
    return jsonify(
        {
            "count": len(routes),
            "rotas": [
                {
                    "origin": r.origin,
                    "destination": r.destination,
                    "outbound_date": r.outbound_date,
                    "inbound_date": r.inbound_date,
                    "trip_type": r.trip_type,
                }
                for r in routes
            ],
        }
    )




def _to_skyscanner_date(date_iso: str) -> str:
    # 2026-06-04 -> 260604
    y, m, d = date_iso.split("-")
    return f"{y[2:]}{m}{d}"


def skyscanner_url(route: RouteQuery) -> str:
    o = route.origin.lower()
    d = route.destination.lower()
    out = _to_skyscanner_date(route.outbound_date)
    if route.inbound_date:
        inn = _to_skyscanner_date(route.inbound_date)
        return f"https://www.skyscanner.com.br/transporte/voos/{o}/{d}/{out}/{inn}/?adultsv2=1&cabinclass=economy&currency=BRL&locale=pt-BR"
    return f"https://www.skyscanner.com.br/transporte/voos/{o}/{d}/{out}/?adultsv2=1&cabinclass=economy&currency=BRL&locale=pt-BR"


def kayak_url(route: RouteQuery) -> str:
    o = route.origin.upper()
    d = route.destination.upper()
    out = route.outbound_date
    if route.inbound_date:
        return f"https://www.kayak.com.br/flights/{o}-{d}/{out}/{route.inbound_date}?sort=bestflight_a"
    return f"https://www.kayak.com.br/flights/{o}-{d}/{out}?sort=bestflight_a"


def search_kayak(route: RouteQuery) -> FlightResult:
    url = kayak_url(route)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=bool(CONFIG.get("headless", True)))
        page = browser.new_page(locale="pt-BR")
        page.set_default_timeout(int(CONFIG.get("timeout_ms", 45000)))
        notes = []
        try:
            page.goto(url, wait_until="domcontentloaded")
            time.sleep(8)
            try:
                page.mouse.move(150, 220)
                page.mouse.wheel(0, 400)
                time.sleep(1)
            except Exception:
                pass

            text = page.locator("body").inner_text(timeout=5000)
            price = parse_price_brl(text)
            if price is None:
                notes.append("Preço não identificado automaticamente no Kayak")

            return FlightResult(
                site="kayak",
                origin=route.origin,
                destination=route.destination,
                outbound_date=route.outbound_date,
                inbound_date=route.inbound_date,
                trip_type=route.trip_type,
                price=price,
                currency="BRL",
                url=page.url,
                notes=" | ".join(notes),
            )
        except Exception as e:
            return FlightResult(
                site="kayak",
                origin=route.origin,
                destination=route.destination,
                outbound_date=route.outbound_date,
                inbound_date=route.inbound_date,
                trip_type=route.trip_type,
                price=None,
                currency="BRL",
                url=url,
                notes=f"erro Kayak: {e}",
            )
        finally:
            page.close()
            browser.close()


def is_kayak_blocked(result: FlightResult) -> bool:
    url = (result.url or "").lower()
    notes = (result.notes or "").lower()
    return ("captcha" in url) or ("access denied" in notes) or (result.price is None and "kayak" in result.site)


def maxmilhas_url(route: RouteQuery) -> str:
    # URL genérica de busca por voos no domínio MaxMilhas (pode redirecionar)
    o = route.origin.upper()
    d = route.destination.upper()
    out = route.outbound_date
    if route.inbound_date:
        return f"https://www.maxmilhas.com.br/busca-passagens-aereas/{o}/{d}/{out}/{route.inbound_date}"
    return f"https://www.maxmilhas.com.br/busca-passagens-aereas/{o}/{d}/{out}"


def search_maxmilhas(route: RouteQuery) -> FlightResult:
    url = maxmilhas_url(route)
    timeout_ms = int(CONFIG.get("timeout_ms", 45000))
    notes = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=bool(CONFIG.get("headless", True)))
        page = browser.new_page(locale="pt-BR")
        page.set_default_timeout(timeout_ms)
        try:
            for attempt, backoff in [(1, 8), (2, 20), (3, 45)]:
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    time.sleep(7)
                    text = page.locator("body").inner_text(timeout=5000)
                    price = parse_price_brl(text)
                    if price is not None:
                        return FlightResult(
                            site="maxmilhas",
                            origin=route.origin,
                            destination=route.destination,
                            outbound_date=route.outbound_date,
                            inbound_date=route.inbound_date,
                            trip_type=route.trip_type,
                            price=price,
                            currency="BRL",
                            url=page.url,
                            notes=f"tentativa={attempt}",
                        )
                    notes.append(f"tentativa {attempt}: sem tarifa extraível")
                except Exception as e:
                    notes.append(f"tentativa {attempt}: erro {e}")
                if attempt < 3:
                    time.sleep(backoff)

            return FlightResult(
                site="maxmilhas",
                origin=route.origin,
                destination=route.destination,
                outbound_date=route.outbound_date,
                inbound_date=route.inbound_date,
                trip_type=route.trip_type,
                price=None,
                currency="BRL",
                url=page.url if page else url,
                notes=" | ".join(notes) if notes else "sem tarifa extraível",
            )
        finally:
            page.close()
            browser.close()


def is_maxmilhas_blocked(result: FlightResult) -> bool:
    txt = ((result.notes or "") + " " + (result.url or "")).lower()
    return ("captcha" in txt) or ("bloque" in txt) or (result.price is None and "maxmilhas" in result.site)


def search_skyscanner(route: RouteQuery) -> FlightResult:
    """Tentativa agressiva-controlada para Skyscanner.

    Ordem:
    1) headless padrão
    2) não-headless com contexto persistente
    3) headless com proxy (se SKYSCANNER_PROXY_SERVER estiver definido)
    """
    url = skyscanner_url(route)
    timeout_ms = int(CONFIG.get("timeout_ms", 45000))
    proxy_server = os.getenv("SKYSCANNER_PROXY_SERVER", "").strip()

    attempts = [
        {"label": "headless", "headless": True, "persistent": False, "proxy": False},
        {"label": "headed-persistent", "headless": False, "persistent": True, "proxy": False},
    ]
    if proxy_server:
        attempts.append({"label": "headless-proxy", "headless": True, "persistent": False, "proxy": True})

    errors = []

    for cfg in attempts:
        try:
            with sync_playwright() as p:
                proxy_cfg = {"server": proxy_server} if cfg["proxy"] else None

                if cfg["persistent"]:
                    user_data_dir = os.getenv("SKYSCANNER_USER_DATA_DIR", "/tmp/skyscanner-profile")
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=user_data_dir,
                        headless=cfg["headless"],
                        locale="pt-BR",
                        proxy=proxy_cfg,
                    )
                    page = context.new_page()
                    close_ctx = True
                    close_browser = False
                else:
                    browser = p.chromium.launch(headless=cfg["headless"], proxy=proxy_cfg)
                    context = browser.new_context(locale="pt-BR")
                    page = context.new_page()
                    close_ctx = True
                    close_browser = True

                page.set_default_timeout(timeout_ms)
                page.goto(url, wait_until="domcontentloaded")
                time.sleep(9)

                # tentativa de interação mínima para reduzir falso bloqueio
                try:
                    page.mouse.move(120, 180)
                    time.sleep(0.8)
                    page.mouse.wheel(0, 450)
                    time.sleep(1.2)
                except Exception:
                    pass

                text = page.locator("body").inner_text(timeout=6000)
                price = parse_price_brl(text)
                final_url = page.url

                notes = []
                if price is None:
                    notes.append(f"tentativa={cfg['label']}")
                    notes.append("Preço não identificado automaticamente no Skyscanner")
                else:
                    notes.append(f"tentativa={cfg['label']}")

                result = FlightResult(
                    site="skyscanner",
                    origin=route.origin,
                    destination=route.destination,
                    outbound_date=route.outbound_date,
                    inbound_date=route.inbound_date,
                    trip_type=route.trip_type,
                    price=price,
                    currency="BRL",
                    url=final_url,
                    notes=" | ".join(notes),
                )

                if close_ctx:
                    context.close()
                if close_browser:
                    browser.close()

                if price is not None and "captcha" not in (final_url or "").lower():
                    return result

                errors.append(f"{cfg['label']}: bloqueado/sem preço")
        except Exception as e:
            errors.append(f"{cfg['label']}: {e}")

    return FlightResult(
        site="skyscanner",
        origin=route.origin,
        destination=route.destination,
        outbound_date=route.outbound_date,
        inbound_date=route.inbound_date,
        trip_type=route.trip_type,
        price=None,
        currency="BRL",
        url=url,
        notes="erro Skyscanner: " + " || ".join(errors),
    )


@app.route("/consulta", methods=["GET"])
def consulta():
    try:
        route = _to_route(request.args)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    db = Database(get_db_path())

    fonte = request.args.get("fonte", default="google", type=str).lower().strip()

    if fonte == "skyscanner":
        result = search_skyscanner(route)
        if is_skyscanner_blocked(result):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=bool(CONFIG.get("headless", True)))
                scraper = GoogleFlightsScraper(browser)
                fallback = scraper.search(route)
                browser.close()
            fallback.notes = (fallback.notes + " | " if fallback.notes else "") + "fallback: skyscanner bloqueado por captcha"
            result = fallback
    elif fonte == "kayak":
        result = search_kayak(route)
        if is_kayak_blocked(result):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=bool(CONFIG.get("headless", True)))
                scraper = GoogleFlightsScraper(browser)
                fallback = scraper.search(route)
                browser.close()
            fallback.notes = (fallback.notes + " | " if fallback.notes else "") + "fallback: kayak bloqueado/sem preço"
            result = fallback
    elif fonte == "maxmilhas":
        result = search_maxmilhas(route)
        if is_maxmilhas_blocked(result):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=bool(CONFIG.get("headless", True)))
                scraper = GoogleFlightsScraper(browser)
                fallback = scraper.search(route)
                browser.close()
            fallback.notes = (fallback.notes + " | " if fallback.notes else "") + "fallback: maxmilhas sem tarifa"
            result = fallback
    else:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=bool(CONFIG.get("headless", True)))
            scraper = GoogleFlightsScraper(browser)
            result = scraper.search(route)
            browser.close()

    min_price, avg_price, last_price = db.stats_for(route)
    band = classify_price(result.price, min_price, avg_price)
    db.save(result, band)

    try:
        resumo = (
            "────────── ✈️ CONSULTA RÁPIDA ✈️ ──────────\n"
            f"Rota: {route.origin} → {route.destination}\n"
            f"Data: {route.outbound_date}"
            + (f" / {route.inbound_date}" if route.inbound_date else "")
            + "\n"
            + f"Preço: {format_brl(result.price)}\n"
            + f"Classificação: {band}\n"
            + f"Fonte: {result.site}"
        )
        send_telegram_message(resumo)
    except Exception:
        pass

    return jsonify(
        {
            "rota": {
                "origin": route.origin,
                "destination": route.destination,
                "outbound_date": route.outbound_date,
                "inbound_date": route.inbound_date,
                "trip_type": route.trip_type,
            },
            "resultado": {
                "price": result.price,
                "price_fmt": format_brl(result.price),
                "price_band": band,
                "site": result.site,
                "currency": result.currency,
                "url": result.url,
                "notes": result.notes,
            },
            "historico": {
                "min_price": min_price,
                "avg_price": avg_price,
                "last_price": last_price,
            },
        }
    )






@app.route("/consulta-maxmilhas", methods=["GET"])
def consulta_maxmilhas():
    args = request.args.to_dict(flat=True)
    args["fonte"] = "maxmilhas"
    with app.test_request_context(query_string=args):
        return consulta()


@app.route("/consulta-skyscanner", methods=["GET"])
def consulta_skyscanner():
    args = request.args.to_dict(flat=True)
    args["fonte"] = "skyscanner"
    with app.test_request_context(query_string=args):
        return consulta()


@app.route("/historico", methods=["GET"])
def historico():
    limit = request.args.get("limit", default=20, type=int)
    limit = max(1, min(limit, 200))

    db_path = Path(get_db_path())
    if not db_path.exists():
        return jsonify({"total": 0, "items": []})

    db = Database(str(db_path))
    rows = db.conn.execute(
        """
        SELECT created_at, site, origin, destination, outbound_date, inbound_date,
               price, currency, price_band, notes, url
        FROM results
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    items = [dict(r) for r in rows]
    return jsonify({"total": len(items), "items": items})


@app.route("/cron", methods=["GET"])
def cron():
    parsed = run_full_scan()
    notify_full_scan(parsed, trigger="manual")
    return jsonify({"status": "ok", "resultados": parsed, "last_run_at": _scan_last_run_at})


@app.route("/cron-stream", methods=["GET"])
def cron_stream():
    def event_stream():
        routes = build_queries()
        total = len(routes)
        yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

        # evita concorrência com auto-scan/execuções manuais
        if not _scan_lock.acquire(blocking=False):
            yield f"data: {json.dumps({'type': 'error', 'message': 'Já existe uma varredura em andamento. Tente novamente em instantes.'})}\n\n"
            return

        try:
            parsed = []
            db = Database(get_db_path())
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=bool(CONFIG.get("headless", True)))
                scraper = GoogleFlightsScraper(browser)

                for idx, route in enumerate(routes, start=1):
                    result = scraper.search(route)
                    min_price, avg_price, _last_price = db.stats_for(route)
                    band = classify_price(result.price, min_price, avg_price)
                    db.save(result, band)

                    row = {
                        "origin": result.origin,
                        "destination": result.destination,
                        "outbound_date": result.outbound_date,
                        "inbound_date": result.inbound_date,
                        "trip_type": result.trip_type,
                        "price": result.price,
                        "price_fmt": format_brl(result.price),
                        "site": result.site,
                        "notes": result.notes,
                        "price_band": band,
                    }
                    parsed.append(row)
                    payload = {"type": "row", "index": idx, "total": total, "item": row}
                    yield f"data: {json.dumps(payload)}\n\n"
                    time.sleep(0.05)

                browser.close()

            notify_full_scan(parsed, trigger="completa")
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            if _scan_lock.locked():
                _scan_lock.release()

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    start_auto_scan_if_needed()
    app.run(debug=True)
