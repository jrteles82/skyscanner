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
    GoogleFlightsScraper,
    RouteQuery,
    build_queries,
    classify_price,
    format_brl,
    sync_playwright,
)

app = Flask(__name__, static_folder="static", static_url_path="/static")


SCAN_INTERVAL_SECONDS = int(os.getenv("SKYSCANNER_FULL_SCAN_EVERY_SECONDS", str(3 * 60 * 60)))
AUTO_SCAN_ENABLED = os.getenv("SKYSCANNER_AUTO_SCAN", "1") == "1"
_scan_lock = threading.Lock()
_scan_last_run_at = None


def run_full_scan(on_row=None):
    global _scan_last_run_at
    routes = build_queries()
    db = Database(get_db_path())
    parsed = []

    with _scan_lock:
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

                if on_row:
                    on_row(idx, len(routes), row)

            browser.close()

    _scan_last_run_at = datetime.now().isoformat()
    return parsed


def _auto_scan_loop():
    while True:
        try:
            run_full_scan()
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


@app.route("/consulta", methods=["GET"])
def consulta():
    try:
        route = _to_route(request.args)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    db = Database(get_db_path())

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
    return jsonify({"status": "ok", "resultados": parsed, "last_run_at": _scan_last_run_at})


@app.route("/cron-stream", methods=["GET"])
def cron_stream():
    def event_stream():
        total = len(build_queries())
        yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

        def on_row(idx, total_rows, row):
            payload = {"type": "row", "index": idx, "total": total_rows, "item": row}
            nonlocal_buffer.append(f"data: {json.dumps(payload)}\n\n")

        nonlocal_buffer = []
        try:
            run_full_scan(on_row=on_row)
            for chunk in nonlocal_buffer:
                yield chunk
                time.sleep(0.05)
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    start_auto_scan_if_needed()
    app.run(debug=True)
