from __future__ import annotations

from flask import Flask, Response, jsonify, request, stream_with_context, session, redirect, url_for, render_template_string, g
from pathlib import Path
import json
import os
import re
import time
import random
import requests
import threading
import sqlite3
from functools import wraps
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

from skyscanner import (
    CONFIG,
    Database,
    FlightResult,
    GoogleFlightsScraper,
    RouteQuery,
    build_config_queries,
    build_db_queries,
    classify_price,
    format_brl,
    parse_price_brl,
    sync_playwright,
)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = os.getenv("SKYSCANNER_SECRET_KEY", "dev-change-this-secret")


DEFAULT_SCAN_INTERVAL = int(CONFIG.get("full_scan_seconds", 3 * 60 * 60))
DEFAULT_SCHEDULE_MINUTES = max(1, int(CONFIG.get("schedule_minutes", DEFAULT_SCAN_INTERVAL // 60)))
SCAN_INTERVAL_SECONDS = int(os.getenv("SKYSCANNER_FULL_SCAN_EVERY_SECONDS", str(DEFAULT_SCAN_INTERVAL)))
AUTO_SCAN_ENABLED = os.getenv("SKYSCANNER_AUTO_SCAN", "1") == "1"
USER_SCAN_POLL_SECONDS = int(os.getenv("SKYSCANNER_USER_SCAN_POLL_SECONDS", "60"))
_scan_lock = threading.Lock()
_scan_last_run_at = None
_user_scheduler_started = False
_auth_tables_initialized = False

AIRPORT_OPTIONS = [
    ("PVH", "PVH — Porto Velho (RO)"),
    ("RIO", "RIO — Rio de Janeiro (RJ)"),
    ("SAO", "SAO — São Paulo (SP)"),
    ("BSB", "BSB — Brasília (DF)"),
    ("CGB", "CGB — Cuiabá (MT)"),
    ("GYN", "GYN — Goiânia (GO)"),
    ("MCZ", "MCZ — Maceió (AL)"),
    ("AJU", "AJU — Aracaju (SE)"),
    ("SSA", "SSA — Salvador (BA)"),
    ("FOR", "FOR — Fortaleza (CE)"),
    ("SLZ", "SLZ — São Luís (MA)"),
    ("CGR", "CGR — Campo Grande (MS)"),
    ("BHZ", "BHZ — Belo Horizonte (MG)"),
    ("BEL", "BEL — Belém (PA)"),
    ("JPA", "JPA — João Pessoa (PB)"),
    ("CWB", "CWB — Curitiba (PR)"),
    ("REC", "REC — Recife (PE)"),
    ("THE", "THE — Teresina (PI)"),
    ("NAT", "NAT — Natal (RN)"),
    ("POA", "POA — Porto Alegre (RS)"),
    ("FLN", "FLN — Florianópolis (SC)"),
    ("VIX", "VIX — Vitória (ES)"),
    ("MAO", "MAO — Manaus (AM)"),
    ("RBR", "RBR — Rio Branco (AC)"),
    ("BVB", "BVB — Boa Vista (RR)"),
    ("MCP", "MCP — Macapá (AP)"),
    ("PMW", "PMW — Palmas (TO)"),
]


def build_full_scan_message(parsed: list[dict], trigger: str = "manual") -> str:
    def _price_num(row):
        v = row.get("price")
        return v if isinstance(v, (int, float)) and v is not None else 10**12

    def _route_line(row, medal=""):
        data_txt = f"{row.get('outbound_date')}" + (f" / {row.get('inbound_date')}" if row.get('inbound_date') else "")
        vendor = (row.get("best_vendor") or "").strip()
        line = f"{medal}{row.get('origin')}→{row.get('destination')} | {data_txt} | {row.get('price_fmt')}"
        if vendor:
            line += f" | vendedor: {vendor}"
        return line

    if not parsed:
        return (
            "────────── ✈️ CONSULTA COMPLETA ✈️ ──────────\n"
            "Sem dados nesta execução."
        )
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
        for i, r in enumerate(idas_ok, start=1):
            medal = "🥇 " if i == 1 else ""
            lines.append(_route_line(r, medal))
    else:
        lines.append("N/D")

    lines += ["", "VOLTAS (destino -> PVH):"]
    if voltas_ok:
        for i, r in enumerate(voltas_ok, start=1):
            medal = "🥇 " if i == 1 else ""
            lines.append(_route_line(r, medal))
    else:
        lines.append("N/D")

    total_ok = len([r for r in parsed if r.get("price") is not None])
    lines += ["", f"Resumo: {total_ok}/{len(parsed)} rotas com preço válido."]
    return "\n".join(lines)


def notify_full_scan(parsed: list[dict], trigger: str = "manual", send_fn=None) -> None:
    msg = build_full_scan_message(parsed, trigger=trigger)
    sender = send_fn or send_telegram_message
    try:
        sender(msg)
    except Exception:
        pass


def _build_user_routes(conn, user_id: int) -> list[RouteQuery]:
    rows = conn.execute(
        """
        SELECT origin, destination, outbound_date, inbound_date
        FROM user_routes
        WHERE user_id = ? AND active = 1
        ORDER BY id DESC
        """,
        (user_id,),
    ).fetchall()
    routes = []
    for r in rows:
        inbound = (r["inbound_date"] or "").strip()
        routes.append(
            RouteQuery(
                origin=(r["origin"] or "").upper(),
                destination=(r["destination"] or "").upper(),
                outbound_date=r["outbound_date"],
                inbound_date=inbound,
                trip_type="roundtrip" if inbound else "oneway",
            )
        )
    return routes


def _ensure_parent_dir(file_path: str) -> str:
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def run_scan_for_routes(routes: list[RouteQuery], on_row=None):
    db = Database(get_db_path())
    parsed = []

    with _scan_lock:
        with sync_playwright() as p:
            user_data_dir = os.getenv("SKYSCANNER_USER_DATA_DIR", "/tmp/skyscanner-profile")
            browser = p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=bool(CONFIG.get("headless", True)),
                locale="pt-BR",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            scraper = GoogleFlightsScraper(browser)

            idx = 0
            total = len(routes)
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
                    "best_vendor": getattr(result, "best_vendor", ""),
                    "best_vendor_price": getattr(result, "best_vendor_price", None),
                    "final_price_source": extract_final_price_source(result.notes),
                }
                parsed.append(row)
                idx += 1
                if on_row:
                    on_row(idx, total, row)
            browser.close()

    return parsed


def run_full_scan(on_row=None):
    global _scan_last_run_at
    parsed = run_scan_for_routes(build_db_queries(get_db_path()), on_row=on_row)
    _scan_last_run_at = datetime.now().isoformat()
    return parsed


def _create_user_run(conn, user_id: int, trigger: str = "manual-user") -> int:
    cur = conn.execute(
        "INSERT INTO user_runs (user_id, started_at, status, summary, trigger) VALUES (?, ?, ?, ?, ?)",
        (user_id, datetime.now().isoformat(), "running", "", trigger),
    )
    conn.commit()
    return int(cur.lastrowid)


def _finish_user_run(conn, run_id: int, status: str, summary: str) -> None:
    conn.execute(
        "UPDATE user_runs SET finished_at = ?, status = ?, summary = ? WHERE id = ?",
        (datetime.now().isoformat(), status, summary, run_id),
    )
    conn.commit()


def _touch_user_cron_run(conn, user_id: int) -> None:
    conn.execute(
        "UPDATE user_cron SET last_run_at = ?, updated_at = COALESCE(updated_at, ?) WHERE user_id = ?",
        (datetime.now().isoformat(), datetime.now().isoformat(), user_id),
    )
    conn.commit()


def run_user_scan(user_id: int, trigger: str = "manual-user", notify: bool = True):
    conn = sqlite3.connect(auth_db_path())
    conn.row_factory = sqlite3.Row
    run_id = _create_user_run(conn, user_id, trigger=trigger)
    try:
        routes = _build_user_routes(conn, user_id)
        if not routes:
            routes = build_db_queries(get_db_path())
        parsed = run_scan_for_routes(routes)
        msg = build_full_scan_message(parsed, trigger=trigger)
        if notify:
            send_user_telegram_message(user_id, msg)
        total_ok = len([r for r in parsed if r.get("price") is not None])
        summary = f"ok: {total_ok}/{len(parsed)} com preço"
        _finish_user_run(conn, run_id, "ok", summary)
        if trigger.startswith("agendada"):
            _touch_user_cron_run(conn, user_id)
        return {"status": "ok", "summary": summary, "parsed": parsed}
    except Exception as e:
        _finish_user_run(conn, run_id, "error", str(e)[:500])
        raise
    finally:
        conn.close()




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

    is_reloader_main = os.getenv("WERKZEUG_RUN_MAIN") == "true"
    is_debug = os.getenv("FLASK_DEBUG") == "1"
    if is_debug and not is_reloader_main:
        return

    t = threading.Thread(target=_auto_scan_loop, daemon=True)
    t.start()
    start_user_scan_scheduler_if_needed()
    print(f"[auto-scan] ligado: intervalo {SCAN_INTERVAL_SECONDS}s + cron por usuário")


def _should_run_user_now(conn, user_id: int, schedule_minutes: int) -> bool:
    row = conn.execute(
        "SELECT last_run_at, updated_at FROM user_cron WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    interval_seconds = max(60, schedule_minutes * 60)
    if row and row["last_run_at"]:
        try:
            last_started = datetime.fromisoformat(row["last_run_at"])
            return (datetime.now() - last_started).total_seconds() >= interval_seconds
        except Exception:
            pass

    fallback = conn.execute(
        "SELECT started_at FROM user_runs WHERE user_id = ? AND trigger LIKE 'agendada%' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if not fallback or not fallback["started_at"]:
        return True
    try:
        last_started = datetime.fromisoformat(fallback["started_at"])
    except Exception:
        return True
    return (datetime.now() - last_started).total_seconds() >= interval_seconds


def _user_scan_scheduler_loop():
    while True:
        conn = sqlite3.connect(auth_db_path())
        conn.row_factory = sqlite3.Row
        try:
            users = conn.execute(
                """
                SELECT u.id AS user_id,
                       COALESCE(c.enabled, 1) AS enabled,
                       c.schedule_minutes,
                       c.every_hours
                FROM users u
                LEFT JOIN user_cron c ON c.user_id = u.id
                """
            ).fetchall()
            for u in users:
                if int(u["enabled"] or 0) != 1:
                    continue
                schedule_minutes = u["schedule_minutes"]
                if schedule_minutes is None:
                    fallback_hours = u["every_hours"]
                    if fallback_hours and fallback_hours > 0:
                        schedule_minutes = int(fallback_hours) * 60
                    else:
                        schedule_minutes = DEFAULT_SCHEDULE_MINUTES
                schedule_minutes = max(1, int(schedule_minutes))
                if not _should_run_user_now(conn, int(u["user_id"]), schedule_minutes):
                    continue
                try:
                    run_user_scan(int(u["user_id"]), trigger="agendada-usuario")
                    print(f"[user-scan] execução usuário={u['user_id']} concluída")
                except Exception as e:
                    print(f"[user-scan] erro usuário={u['user_id']}: {e}")
        finally:
            conn.close()

        time.sleep(max(30, USER_SCAN_POLL_SECONDS))


def start_user_scan_scheduler_if_needed():
    global _user_scheduler_started
    if _user_scheduler_started:
        return

    is_reloader_main = os.getenv("WERKZEUG_RUN_MAIN") == "true"
    is_debug = os.getenv("FLASK_DEBUG") == "1"
    if is_debug and not is_reloader_main:
        return

    t = threading.Thread(target=_user_scan_scheduler_loop, daemon=True)
    t.start()
    _user_scheduler_started = True
    print(f"[user-scan] scheduler ligado: verificação a cada {USER_SCAN_POLL_SECONDS}s")


def get_db_path() -> str:
    env_path = os.getenv("SKYSCANNER_DB_PATH")
    configured = str(env_path or CONFIG.get("db_path", "flight_tracker_browser.db"))
    # Em Vercel/Lambda, /var/task é read-only; use /tmp (gravável)
    if os.getenv("VERCEL") or configured.startswith("/var/task"):
        configured = "/tmp/flight_tracker_browser.db"
    return _ensure_parent_dir(configured)


def send_telegram_message_to(text: str, token: str | None = None, chat_id: str | None = None) -> None:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN") or CONFIG.get("telegram_bot_token")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID") or CONFIG.get("telegram_chat_id")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=20).raise_for_status()


def send_telegram_message(text: str) -> None:
    send_telegram_message_to(text)


def send_user_telegram_message(user_id: int, text: str) -> None:
    conn = sqlite3.connect(auth_db_path())
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT bot_token, chat_id FROM user_telegram WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return
        token = (row["bot_token"] or "").strip()
        chat_id = (row["chat_id"] or "").strip()
        if not token or not chat_id:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=20).raise_for_status()
    finally:
        conn.close()


def extract_final_price_source(notes: str | None) -> str:
    txt = (notes or "")
    m = re.search(r"final_price_source=([^|]+)", txt)
    if not m:
        return ""
    return (m.group(1) or "").strip()



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
    if session.get("user_id"):
        return redirect(url_for("painel"))
    return redirect(url_for("auth_login"))


@app.route("/app", methods=["GET"])
def app_front():
    return app.send_static_file("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "voobot-monitor"})


@app.route("/rotas", methods=["GET"])
def rotas():
    routes = build_db_queries(get_db_path())
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
        vendor_line = ""
        if getattr(result, "best_vendor", ""):
            vendor_price = format_brl(getattr(result, "best_vendor_price", None))
            vendor_line = f"\nOnde comprar mais barato: {result.best_vendor} ({vendor_price})"

        resumo = (
            "────────── ✈️ CONSULTA RÁPIDA ✈️ ──────────\n"
            f"Rota: {route.origin} → {route.destination}\n"
            f"Data: {route.outbound_date}\n"
            + (f" / {route.inbound_date}" if route.inbound_date else "")
            + "\n"
            + f"Preço: {format_brl(result.price)}\n"
            + f"Classificação: {band}\n"
            + f"Fonte: {result.site}\n"
            + vendor_line
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
                "best_vendor": getattr(result, "best_vendor", ""),
                "best_vendor_price": getattr(result, "best_vendor_price", None),
                "final_price_source": extract_final_price_source(result.notes),
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
               price, currency, price_band, notes, url,
               best_vendor, best_vendor_price, booking_options_json
        FROM results
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    items = [dict(r) for r in rows]
    for item in items:
        item["final_price_source"] = extract_final_price_source(item.get("notes"))
    return jsonify({"total": len(items), "items": items})


@app.route("/cron", methods=["GET"])
def cron():
    parsed = run_full_scan()
    notify_full_scan(parsed, trigger="manual")
    return jsonify({"status": "ok", "resultados": parsed, "last_run_at": _scan_last_run_at})


@app.route("/cron-stream", methods=["GET"])
def cron_stream():
    def event_stream():
        routes = build_db_queries(get_db_path())
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
                        "best_vendor": getattr(result, "best_vendor", ""),
                        "best_vendor_price": getattr(result, "best_vendor_price", None),
                        "final_price_source": extract_final_price_source(result.notes),
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




@app.route("/app-page", methods=["GET"])
def app_page():
    if not session.get("user_id"):
        return redirect(url_for("auth_login"))
    return render_template_string(
        """
        <!doctype html>
        <html lang='pt-BR'>
        <head>
          <meta charset='utf-8'>
          <meta name='viewport' content='width=device-width, initial-scale=1'>
          <title>App Consultas</title>
          <link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css' rel='stylesheet'>
        </head>
        <body class='bg-light'>
          <nav class='navbar navbar-dark bg-dark'>
            <div class='container-fluid'>
              <span class='navbar-brand mb-0 h1'>App Consultas</span>
              <a class='btn btn-outline-light btn-sm' href='{{ url_for("painel") }}'>Voltar ao Painel</a>
            </div>
          </nav>
          <div class='container-fluid p-0'>
            <iframe src='{{ url_for("app_front") }}' style='width:100%;height:92vh;border:0;'></iframe>
          </div>
        </body>
        </html>
        """
    )

def auth_db_path() -> str:
    return get_db_path()


def get_auth_db():
    global _auth_tables_initialized
    if not _auth_tables_initialized:
        init_auth_tables()
    if "auth_db" not in g:
        conn = sqlite3.connect(auth_db_path())
        conn.row_factory = sqlite3.Row
        g.auth_db = conn
    return g.auth_db


def _current_iso_ts() -> str:
    return datetime.now().isoformat()

def _ensure_user_routes_defaults(conn, user_id: int) -> None:
    exists = conn.execute("SELECT 1 FROM user_routes WHERE user_id = ? LIMIT 1", (user_id,)).fetchone()
    if exists:
        return
    now = _current_iso_ts()
    for route in build_config_queries():
        conn.execute("INSERT INTO user_routes (user_id, origin, destination, outbound_date, inbound_date, active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (user_id, route.origin, route.destination, route.outbound_date, route.inbound_date or "", now),
        )
    conn.commit()

def _ensure_user_telegram_defaults(conn, user_id: int) -> None:
    exists = conn.execute("SELECT 1 FROM user_telegram WHERE user_id = ? LIMIT 1", (user_id,)).fetchone()
    if exists:
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN") or CONFIG.get("telegram_bot_token")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or CONFIG.get("telegram_chat_id")
    if not token and not chat_id:
        return
    conn.execute("INSERT INTO user_telegram (user_id, bot_token, chat_id, updated_at) VALUES (?, ?, ?, ?)",
        (user_id, token or "", chat_id or "", _current_iso_ts()),
    )
    conn.commit()

def _ensure_user_cron_defaults(conn, user_id: int) -> None:
    exists = conn.execute("SELECT 1 FROM user_cron WHERE user_id = ? LIMIT 1", (user_id,)).fetchone()
    if exists:
        return
    schedule_minutes = DEFAULT_SCHEDULE_MINUTES
    every_hours = max(1, min(24, round(schedule_minutes / 60)))
    now = _current_iso_ts()
    conn.execute("INSERT INTO user_cron (user_id, enabled, every_hours, schedule_minutes, updated_at, last_run_at) VALUES (?, 1, ?, ?, ?, ?)",
        (user_id, every_hours, schedule_minutes, now, now),
    )
    conn.commit()

def ensure_user_defaults(conn, user_id: int) -> None:
    _ensure_user_routes_defaults(conn, user_id)
    _ensure_user_telegram_defaults(conn, user_id)
    _ensure_user_cron_defaults(conn, user_id)






def init_auth_tables():
    global _auth_tables_initialized
    db = sqlite3.connect(auth_db_path())
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            outbound_date TEXT NOT NULL,
            inbound_date TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_telegram (
            user_id INTEGER PRIMARY KEY,
            bot_token TEXT,
            chat_id TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_cron (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER DEFAULT 1,
            every_hours INTEGER DEFAULT 3,
            schedule_minutes INTEGER DEFAULT 60,
            updated_at TEXT NOT NULL,
            last_run_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            summary TEXT,
            trigger TEXT DEFAULT 'manual-user',
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    for ddl in [
        "ALTER TABLE user_cron ADD COLUMN last_run_at TEXT",
        "ALTER TABLE user_cron ADD COLUMN schedule_minutes INTEGER",
        "ALTER TABLE user_runs ADD COLUMN trigger TEXT DEFAULT 'manual-user'",
    ]:
        try:
            cur.execute(ddl)
        except sqlite3.OperationalError:
            pass

    try:
        cur.execute("UPDATE user_cron SET schedule_minutes = COALESCE(schedule_minutes, every_hours * 60, ?) WHERE schedule_minutes IS NULL", (DEFAULT_SCHEDULE_MINUTES,))
    except sqlite3.OperationalError:
        pass

    db.commit()
    db.close()
    _auth_tables_initialized = True


init_auth_tables()


@app.teardown_appcontext
def close_auth_db(_exc):
    db = g.pop("auth_db", None)
    if db is not None:
        db.close()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth_login"))
        user = current_user()
        if user is None:
            session.clear()
            return redirect(url_for("auth_login"))
        return fn(*args, **kwargs)

    return wrapper


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_auth_db()
    user = db.execute("SELECT id, email FROM users WHERE id = ?", (uid,)).fetchone()
    if user is None:
        session.pop("user_id", None)
    return user


@app.route("/auth/register", methods=["GET", "POST"])
def auth_register():
    error = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or len(password) < 6:
            error = "Informe email válido e senha com pelo menos 6 caracteres."
        else:
            db = get_auth_db()
            try:
                db.execute(
                    "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                    (email, generate_password_hash(password), datetime.now().isoformat()),
                )
                db.commit()
                return redirect(url_for("auth_login"))
            except sqlite3.IntegrityError:
                error = "Esse email já está cadastrado."

    return render_template_string(
        """
        <!doctype html>
        <html lang='pt-BR'>
        <head>
          <meta charset='utf-8'>
          <meta name='viewport' content='width=device-width, initial-scale=1'>
          <title>Cadastro | VooBot Admin</title>
          <link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css' rel='stylesheet'>
        </head>
        <body class='bg-light d-flex align-items-center' style='min-height:100vh;'>
          <div class='container'>
            <div class='row justify-content-center'>
              <div class='col-md-5'>
                <div class='card shadow-sm'>
                  <div class='card-header bg-primary text-white'>Cadastro</div>
                  <div class='card-body'>
                    <form method='post'>
                      <div class='mb-3'><input class='form-control' name='email' type='email' placeholder='Email' required></div>
                      <div class='mb-3'><input class='form-control' name='password' type='password' placeholder='Senha (mín 6)' required></div>
                      <button class='btn btn-primary w-100' type='submit'>Cadastrar</button>
                    </form>
                    {% if error %}<div class='alert alert-danger mt-3 mb-0'>{{error}}</div>{% endif %}
                    <div class='mt-3 text-center'><a href='{{ url_for("auth_login") }}'>Já tenho login</a></div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </body>
        </html>
        """,
        error=error,
    )


@app.route("/auth/login", methods=["GET", "POST"])
def auth_login():
    error = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = get_auth_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            error = "Login inválido."
        else:
            session["user_id"] = user["id"]
            return redirect(url_for("painel"))

    return render_template_string(
        """
        <!doctype html>
        <html lang='pt-BR'>
        <head>
          <meta charset='utf-8'>
          <meta name='viewport' content='width=device-width, initial-scale=1'>
          <title>Login | VooBot Admin</title>
          <link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css' rel='stylesheet'>
        </head>
        <body class='bg-light d-flex align-items-center' style='min-height:100vh;'>
          <div class='container'>
            <div class='row justify-content-center'>
              <div class='col-md-5'>
                <div class='card shadow-sm'>
                  <div class='card-header bg-dark text-white'>VooBot Admin</div>
                  <div class='card-body'>
                    <form method='post'>
                      <div class='mb-3'><input class='form-control' name='email' type='email' placeholder='Email' required></div>
                      <div class='mb-3'><input class='form-control' name='password' type='password' placeholder='Senha' required></div>
                      <button class='btn btn-dark w-100' type='submit'>Entrar</button>
                    </form>
                    {% if error %}<div class='alert alert-danger mt-3 mb-0'>{{error}}</div>{% endif %}
                    <div class='mt-3 text-center'><a href='{{ url_for("auth_register") }}'>Criar conta</a></div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </body>
        </html>
        """,
        error=error,
    )


@app.route("/auth/logout")
def auth_logout():
    session.clear()
    return redirect(url_for("auth_login"))


@app.route("/painel", methods=["GET"])
@login_required
def painel():
    db = get_auth_db()
    user = current_user()
    if user is None:
        session.clear()
        return redirect(url_for("auth_login"))
    ensure_user_defaults(db, user["id"])
    routes = db.execute(
        "SELECT id, origin, destination, outbound_date, inbound_date, active FROM user_routes WHERE user_id = ? ORDER BY id DESC",
        (user["id"],),
    ).fetchall()
    tg = db.execute("SELECT bot_token, chat_id FROM user_telegram WHERE user_id = ?", (user["id"],)).fetchone()
    cron = db.execute("SELECT enabled, every_hours, schedule_minutes FROM user_cron WHERE user_id = ?", (user["id"],)).fetchone()
    cron_minutes = DEFAULT_SCHEDULE_MINUTES
    if cron is not None:
        schedule_minutes = cron["schedule_minutes"]
        if schedule_minutes is not None:
            cron_minutes = int(schedule_minutes)
        elif cron["every_hours"] is not None:
            cron_minutes = max(1, int(cron["every_hours"]) * 60)
    last_run = db.execute("SELECT started_at, finished_at, status, summary FROM user_runs WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    default_tg_bot = os.getenv("TELEGRAM_BOT_TOKEN") or CONFIG.get("telegram_bot_token", "")
    default_tg_chat = os.getenv("TELEGRAM_CHAT_ID") or CONFIG.get("telegram_chat_id", "")

    return render_template_string(
        """
        <!doctype html>
        <html lang='pt-BR'>
        <head>
          <meta charset='utf-8'>
          <meta name='viewport' content='width=device-width, initial-scale=1'>
          <title>Painel Admin | VooBot</title>
          <link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css' rel='stylesheet'>
          <link href='https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css' rel='stylesheet'>
          <style>
            body { background:#f4f6f9; }
            .sidebar { min-height: 100vh; background: #343a40; }
            .sidebar a { color: #c2c7d0; text-decoration: none; display:block; padding:.65rem 1rem; }
            .sidebar a:hover { background:#495057; color:#fff; }
            .brand { color:#fff; font-weight:700; padding:1rem; border-bottom:1px solid #495057; }
            .topbar { background:#fff; border-bottom:1px solid #dee2e6; }
            .kpi { border-left:4px solid #0d6efd; }
            body.dark-mode { background:#1f2d3d; color:#dee2e6; }
            body.dark-mode .card, body.dark-mode .topbar { background:#2c3b4b; color:#dee2e6; border-color:#3d4b5a; }
            body.dark-mode .text-muted { color:#adb5bd !important; }
            body.sidebar-collapsed .sidebar { width: 72px; }
            body.sidebar-collapsed .sidebar .brand, body.sidebar-collapsed .sidebar a { text-align:center; }
            body.sidebar-collapsed .sidebar a { font-size:0; }
            body.sidebar-collapsed .sidebar a i { font-size:1rem; margin:0 !important; }
          </style>
        </head>
        <body class='bg-light'>
          <div class='container-fluid'>
            <div class='row'>
              <aside class='col-md-3 col-lg-2 p-0 sidebar'>
                <div class='brand'><i class='bi bi-activity'></i> VooBot Admin</div>
                <a href='#rotas'><i class='bi bi-signpost-split me-2'></i>Rotas</a>
                <a href='#consultas'><i class='bi bi-window me-2'></i>Consultas</a>
                <a href='#telegram'><i class='bi bi-telegram me-2'></i>Telegram</a>
                <a href='#cron'><i class='bi bi-clock-history me-2'></i>Cron</a>
                <a href='{{ url_for("auth_logout") }}'><i class='bi bi-box-arrow-right me-2'></i>Sair</a>
              </aside>
              <main class='col-md-9 col-lg-10 p-0'>
                <div class='topbar d-flex justify-content-between align-items-center px-4 py-3'>
                  <div><strong>Painel</strong> <span class='text-muted'>/ Dashboard</span></div>
                  <div class='d-flex align-items-center gap-2'>
                    <button class='btn btn-sm btn-outline-secondary' type='button' onclick='toggleSidebar()'><i class='bi bi-list'></i></button>
                    <button class='btn btn-sm btn-outline-secondary' type='button' onclick='toggleTheme()'><i class='bi bi-moon-stars'></i></button>
                    <div class='text-muted small'>{{user['email']}}</div>
                  </div>
                </div>
                <div class='p-4'>
                <div class='row g-3 mb-3'>
                  <div class='col-md-4'><div class='card kpi'><div class='card-body'><div class='text-muted'>Rotas</div><div class='h4 mb-0'>{{ routes|length }}</div></div></div></div>
                  <div class='col-md-4'><div class='card kpi'><div class='card-body'><div class='text-muted'>Cron</div><div class='h6 mb-0'>{% if not cron or cron['enabled'] %}Ativo{% else %}Inativo{% endif %} ({{ cron_minutes }} min)</div></div></div></div>
                  <div class='col-md-4'><div class='card kpi'><div class='card-body'><div class='text-muted'>Última execução</div><div class='small mb-0'>{% if last_run %}{{last_run['status']}}{% else %}sem execução{% endif %}</div></div></div></div>
                </div>

                <div class='card mb-3 shadow-sm dashboard-section' id='rotas'>
                  <div class='card-header'><i class='bi bi-signpost-split me-2'></i>Rotas configuradas</div>
                  <div class='card-body'>
                    <form method='post' action='{{ url_for("add_route") }}' class='row g-2 mb-3 align-items-end'>
                      <div class='col-md-2'>
                        <label class='form-label small text-uppercase mb-1'>Origem</label>
                        <select class='form-select form-select-sm' name='origin' required>
                          {% for code, label in airport_options %}
                            <option value='{{ code }}' {% if code == 'PVH' %}selected{% endif %}>{{ label }}</option>
                          {% endfor %}
                        </select>
                      </div>
                      <div class='col-md-2'>
                        <label class='form-label small text-uppercase mb-1'>Destino</label>
                        <select class='form-select form-select-sm' name='destination' required>
                          {% for code, label in airport_options %}
                            <option value='{{ code }}' {% if code == 'JPA' %}selected{% endif %}>{{ label }}</option>
                          {% endfor %}
                        </select>
                      </div>
                      <div class='col-md-3'>
                        <label class='form-label small text-uppercase mb-1'>Ida</label>
                        <input class='form-control form-control-sm' name='outbound_date' type='date' required>
                      </div>
                      <div class='col-md-3'>
                        <label class='form-label small text-uppercase mb-1'>Volta</label>
                        <input class='form-control form-control-sm' name='inbound_date' type='date'>
                      </div>
                      <div class='col-md-2 d-grid'>
                        <button class='btn btn-primary btn-sm' type='submit'>Adicionar</button>
                      </div>
                    </form>
                    <div class='small text-muted mb-3'>As datas padrão globais de ida foram reduzidas para 04 e 05 de junho.</div>
                    <div class='table-responsive border rounded'>
                      <table class='table table-hover table-striped mb-0 align-middle'>
                        <thead class='table-light'>
                          <tr>
                            <th>Origem</th>
                            <th>Destino</th>
                            <th>Data de Ida</th>
                            <th>Data de Volta</th>
                            <th class='text-end'>Ações</th>
                          </tr>
                        </thead>
                        <tbody>
                          {% for r in routes %}
                            <tr>
                              <form method='post' action='{{ url_for("update_route", route_id=r["id"]) }}'>
                                <td>
                                  <select class='form-select form-select-sm' name='origin' required>
                                    {% for code, label in airport_options %}
                                      <option value='{{ code }}' {% if code == (r["origin"] or "").upper() %}selected{% endif %}>{{ label }}</option>
                                    {% endfor %}
                                  </select>
                                </td>
                                <td>
                                  <select class='form-select form-select-sm' name='destination' required>
                                    {% for code, label in airport_options %}
                                      <option value='{{ code }}' {% if code == (r["destination"] or "").upper() %}selected{% endif %}>{{ label }}</option>
                                    {% endfor %}
                                  </select>
                                </td>
                                <td><input class='form-control form-control-sm' name='outbound_date' type='date' value='{{r["outbound_date"]}}' required></td>
                                <td><input class='form-control form-control-sm' name='inbound_date' type='date' value='{{r["inbound_date"] if r["inbound_date"] else ""}}'></td>
                                <td class='text-end text-nowrap'>
                                  <button class='btn btn-sm btn-outline-primary' type='submit'><i class='bi bi-save'></i> Salvar</button>
                                  <a class='btn btn-sm btn-outline-danger' href='{{ url_for("delete_route", route_id=r["id"]) }}'>
                                    <i class='bi bi-trash'></i> Excluir
                                  </a>
                                </td>
                              </form>
                            </tr>
                          {% else %}
                            <tr><td colspan='5' class='text-center text-muted py-3'>Nenhuma rota cadastrada.</td></tr>
                          {% endfor %}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </div>

                <div class='card mb-3 shadow-sm dashboard-section d-none' id='consultas'>
                  <div class='card-header d-flex justify-content-between align-items-center'>
                    <div>
                      <i class='bi bi-window me-2'></i>App Consultas
                      <small class='text-muted d-block'>Executa buscas com o cron integrando histórico, rotas e consultas manuais.</small>
                    </div>
                    <button class='btn btn-sm btn-outline-secondary' type='button' onclick='document.getElementById('btn-consultar').scrollIntoView({behavior: "smooth"});'>Ir para consulta</button>
                  </div>
                  <div class='card-body'>
                    <section class='mb-4'>
                      <div class='row g-2 align-items-end'>
                        <div class='col-md-3'>
                          <label class='form-label small text-uppercase'>Origem</label>
                          <select id='origin' class='form-select form-select-sm'>
                            <option value='PVH' selected>PVH — Porto Velho (RO)</option>
                            <option value='RIO'>RIO — Rio de Janeiro (RJ)</option>
                            <option value='SAO'>SAO — São Paulo (SP)</option>
                            <option value='BSB'>BSB — Brasília (DF)</option>
                            <option value='CGB'>CGB — Cuiabá (MT)</option>
                            <option value='GYN'>GYN — Goiânia (GO)</option>
                            <option value='MCZ'>MCZ — Maceió (AL)</option>
                            <option value='AJU'>AJU — Aracaju (SE)</option>
                            <option value='SSA'>SSA — Salvador (BA)</option>
                            <option value='FOR'>FOR — Fortaleza (CE)</option>
                            <option value='SLZ'>SLZ — São Luís (MA)</option>
                            <option value='CGR'>CGR — Campo Grande (MS)</option>
                            <option value='BHZ'>BHZ — Belo Horizonte (MG)</option>
                            <option value='BEL'>BEL — Belém (PA)</option>
                            <option value='JPA'>JPA — João Pessoa (PB)</option>
                            <option value='CWB'>CWB — Curitiba (PR)</option>
                            <option value='REC'>REC — Recife (PE)</option>
                            <option value='THE'>THE — Teresina (PI)</option>
                            <option value='NAT'>NAT — Natal (RN)</option>
                            <option value='POA'>POA — Porto Alegre (RS)</option>
                            <option value='FLN'>FLN — Florianópolis (SC)</option>
                            <option value='VIX'>VIX — Vitória (ES)</option>
                            <option value='MAO'>MAO — Manaus (AM)</option>
                            <option value='RBR'>RBR — Rio Branco (AC)</option>
                            <option value='BVB'>BVB — Boa Vista (RR)</option>
                            <option value='MCP'>MCP — Macapá (AP)</option>
                            <option value='PMW'>PMW — Palmas (TO)</option>
                          </select>
                        </div>
                        <div class='col-md-3'>
                          <label class='form-label small text-uppercase'>Destino</label>
                          <select id='destination' class='form-select form-select-sm'>
                            <option value='JPA' selected>JPA — João Pessoa (PB)</option>
                            <option value='REC'>REC — Recife (PE)</option>
                            <option value='NAT'>NAT — Natal (RN)</option>
                            <option value='SLZ'>SLZ — São Luís (MA)</option>
                            <option value='THE'>THE — Teresina (PI)</option>
                            <option value='FOR'>FOR — Fortaleza (CE)</option>
                            <option value='MCZ'>MCZ — Maceió (AL)</option>
                            <option value='AJU'>AJU — Aracaju (SE)</option>
                            <option value='SSA'>SSA — Salvador (BA)</option>
                            <option value='PVH'>PVH — Porto Velho (RO)</option>
                            <option value='RIO'>RIO — Rio de Janeiro (RJ)</option>
                            <option value='SAO'>SAO — São Paulo (SP)</option>
                            <option value='BSB'>BSB — Brasília (DF)</option>
                            <option value='CGB'>CGB — Cuiabá (MT)</option>
                            <option value='GYN'>GYN — Goiânia (GO)</option>
                            <option value='CGR'>CGR — Campo Grande (MS)</option>
                            <option value='BHZ'>BHZ — Belo Horizonte (MG)</option>
                            <option value='BEL'>BEL — Belém (PA)</option>
                            <option value='CWB'>CWB — Curitiba (PR)</option>
                            <option value='POA'>POA — Porto Alegre (RS)</option>
                            <option value='FLN'>FLN — Florianópolis (SC)</option>
                            <option value='VIX'>VIX — Vitória (ES)</option>
                            <option value='MAO'>MAO — Manaus (AM)</option>
                            <option value='RBR'>RBR — Rio Branco (AC)</option>
                            <option value='BVB'>BVB — Boa Vista (RR)</option>
                            <option value='MCP'>MCP — Macapá (AP)</option>
                            <option value='PMW'>PMW — Palmas (TO)</option>
                          </select>
                        </div>
                        <div class='col-md-2'>
                          <label class='form-label small text-uppercase'>Ida</label>
                          <input id='outbound_date' type='date' class='form-control form-control-sm' value='2026-06-05' />
                        </div>
                        <div class='col-md-2'>
                          <label class='form-label small text-uppercase'>Volta</label>
                          <input id='inbound_date' type='date' class='form-control form-control-sm' value='' />
                        </div>
                        <div class='col-12 col-md-1 d-grid'>
                          <button id='btn-consultar' class='btn btn-primary btn-sm' onclick='consultar()'>Consultar</button>
                        </div>
                      </div>
                      <small class='text-muted d-block mt-2'>Se preencher volta, consulta como ida e volta.</small>
                    </section>
                    <section class='mb-4'>
                      <h6 class='text-uppercase text-muted mb-3'>Resultados da consulta</h6>
                      <div class='table-responsive'>
                        <table class='table table-striped table-hover align-middle text-center mb-0' id='consulta-table'>
                          <thead class='table-light'>
                            <tr>
                              <th>Rota</th>
                              <th>Data voo</th>
                              <th>Preço</th>
                              <th>Onde comprar mais barato</th>
                              <th>Fonte</th>
                              <th>Origem preço</th>
                              <th>Data/Hora</th>
                            </tr>
                          </thead>
                          <tbody id='consulta-body'>
                            <tr>
                              <td colspan='7' class='text-center text-muted'>Faça uma consulta para ver resultados.</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                    </section>
                    <section class='mb-4'>
                      <div class='d-flex justify-content-between align-items-center mb-2'>
                        <h6 class='text-uppercase text-muted m-0'>Buscar todos (cron)</h6>
                        <button id='btn-cron' class='btn btn-warning btn-sm' type='button' onclick='executarCron()'>Executar busca completa</button>
                      </div>
                      <div id='cron-loading' class='text-muted mb-2' style='display:none;'>Buscando rotas... isso pode levar alguns minutos.</div>
                      <div class='table-responsive'>
                        <table class='table table-striped table-hover align-middle text-center mb-0' id='cron-table'>
                          <thead class='table-light'>
                            <tr>
                              <th>Rota</th>
                              <th>Data voo</th>
                              <th>Preço</th>
                              <th>Onde comprar mais barato</th>
                              <th>Fonte</th>
                              <th>Origem preço</th>
                              <th>Data/Hora</th>
                            </tr>
                          </thead>
                          <tbody id='cron-body'>
                            <tr><td colspan='7' class='text-center text-muted'>Clique em “Executar busca completa”.</td></tr>
                          </tbody>
                        </table>
                      </div>
                    </section>
                    <section class='mb-4'>
                      <div class='d-flex justify-content-between align-items-center mb-2'>
                        <h6 class='text-uppercase text-muted m-0'>Histórico</h6>
                        <div class='d-flex gap-2'>
                          <input id='historico-limit' type='number' class='form-control form-control-sm' value='20' min='1' max='200' style='width: 90px;' />
                          <button class='btn btn-outline-success btn-sm' type='button' onclick='historico()'>Atualizar</button>
                        </div>
                      </div>
                      <div id='historico-loading' class='text-muted mb-2' style='display:none;'>Carregando histórico...</div>
                      <div class='table-responsive'>
                        <table class='table table-striped table-hover align-middle text-center mb-0' id='historico-table'>
                          <thead class='table-light'>
                            <tr>
                              <th>Rota</th>
                              <th>Data voo</th>
                              <th>Preço</th>
                              <th>Onde comprar mais barato</th>
                              <th>Fonte</th>
                              <th>Origem preço</th>
                              <th>Data/Hora</th>
                            </tr>
                          </thead>
                          <tbody id='historico-body'>
                            <tr>
                              <td colspan='7' class='text-center text-muted'>Clique em “Atualizar” para carregar.</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                    </section>
                    <section>
                      <div class='d-flex justify-content-between align-items-center mb-2'>
                        <h6 class='text-uppercase text-muted m-0'>Rotas configuradas</h6>
                        <button class='btn btn-outline-secondary btn-sm' type='button' onclick='rotas()'>Atualizar</button>
                      </div>
                      <div id='rotas-loading' class='text-muted mb-2' style='display:none;'>Carregando rotas...</div>
                      <div class='table-responsive'>
                        <table class='table table-striped table-hover align-middle text-center mb-0' id='rotas-table'>
                          <thead class='table-light'>
                            <tr>
                              <th>Origem</th>
                              <th>Destino</th>
                              <th>Ida</th>
                              <th>Volta</th>
                              <th>Tipo</th>
                            </tr>
                          </thead>
                          <tbody id='rotas-body'>
                            <tr>
                              <td colspan='5' class='text-center text-muted'>Clique em “Atualizar” para carregar.</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                    </section>
                  </div>
                </div>

                <div class='card mb-3 shadow-sm dashboard-section d-none' id='telegram'>
                  <div class='card-header'><i class='bi bi-telegram me-2'></i>Telegram do usuário</div>
                  <div class='card-body'>
                    <form method='post' action='{{ url_for("save_telegram") }}' class='row g-2'>
                      <div class='col-md-6'><input class='form-control' name='bot_token' placeholder='Bot token' value='{{ tg["bot_token"] if tg and tg["bot_token"] else default_tg_bot }}'></div>
                      <div class='col-md-4'><input class='form-control' name='chat_id' placeholder='Chat ID' value='{{ tg["chat_id"] if tg and tg["chat_id"] else default_tg_chat }}'></div>
                      <div class='col-md-2 d-grid'><button class='btn btn-success' type='submit'>Salvar</button></div>
                    </form>
                  </div>
                </div>

                <div class='card shadow-sm dashboard-section d-none' id='cron'>
                  <div class='card-header'><i class='bi bi-clock-history me-2'></i>Cron do usuário</div>
                  <div class='card-body'>
                    <form method='post' action='{{ url_for("save_cron") }}' class='row g-2 align-items-center'>
                      <div class='col-md-2 form-check ms-2'>
                        <input class='form-check-input' type='checkbox' name='enabled' id='enabled' {% if not cron or cron['enabled'] %}checked{% endif %}>
                        <label class='form-check-label' for='enabled'>Ativo</label>
                      </div>
                      <div class='col-md-3'><input class='form-control' name='schedule_minutes' type='number' min='1' max='1440' step='1' value='{{ cron_minutes }}'></div>
                      <div class='col-md-2 d-grid'><button class='btn btn-primary' type='submit'>Salvar</button></div>
                    </form>
                    <form method='post' action='{{ url_for("run_now_user") }}' class='mt-3'>
                      <button class='btn btn-warning' type='submit'>Executar agora</button>
                    </form>
                    <div class='mt-3'><strong>Última execução:</strong><br>
                      {% if last_run %}
                        {{last_run['started_at']}} → {{last_run['finished_at']}} | {{last_run['status']}} | {{last_run['summary']}}
                      {% else %}
                        sem execução
                      {% endif %}
                    </div>
                  </div>
                </div>


                </div>
              </main>
            </div>
          </div>
        <script src='{{ url_for("static", filename="consulta-app.js") }}'></script>
        <script>
          function showSection(hash) {
            document.querySelectorAll('.dashboard-section').forEach(el => el.classList.add('d-none'));
            var target = document.getElementById(hash);
            if (target) {
              target.classList.remove('d-none');
              localStorage.setItem('adminActiveTab', hash);
            } else {
              document.getElementById('rotas').classList.remove('d-none');
              localStorage.setItem('adminActiveTab', 'rotas');
            }
            document.querySelectorAll('.sidebar a').forEach(el => el.classList.remove('fw-bold', 'text-white'));
            var activeLink = document.querySelector('.sidebar a[href="#' + hash + '"]');
            if (activeLink) activeLink.classList.add('fw-bold', 'text-white');
          }
          window.addEventListener('hashchange', () => {
            let hash = window.location.hash.substring(1);
            if(hash) {
              showSection(hash);
            }
          });
          window.addEventListener('load', () => {
            let hash = window.location.hash.substring(1) || localStorage.getItem('adminActiveTab') || 'rotas';
            showSection(hash);
          });
          function toggleTheme() {
            document.body.classList.toggle('dark-mode');
            localStorage.setItem('adminThemeDark', document.body.classList.contains('dark-mode') ? '1' : '0');
          }
          function toggleSidebar() {
            document.body.classList.toggle('sidebar-collapsed');
            localStorage.setItem('adminSidebarCollapsed', document.body.classList.contains('sidebar-collapsed') ? '1' : '0');
          }
          (function restoreUiState() {
            if (localStorage.getItem('adminThemeDark') === '1') document.body.classList.add('dark-mode');
            if (localStorage.getItem('adminSidebarCollapsed') === '1') document.body.classList.add('sidebar-collapsed');
          })();
        </script>
        </body>
        </html>
        """,
        user=user,
        routes=routes,
        tg=tg,
        cron=cron,
        cron_minutes=cron_minutes,
        last_run=last_run,
        default_tg_bot=default_tg_bot,
        default_tg_chat=default_tg_chat,
        airport_options=AIRPORT_OPTIONS,
    )


@app.route("/painel/route/add", methods=["POST"])
@login_required
def add_route():
    db = get_auth_db()
    user = current_user()
    db.execute(
        "INSERT INTO user_routes (user_id, origin, destination, outbound_date, inbound_date, active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
        (
            user["id"],
            request.form.get("origin", "").strip().upper(),
            request.form.get("destination", "").strip().upper(),
            request.form.get("outbound_date", "").strip(),
            request.form.get("inbound_date", "").strip(),
            datetime.now().isoformat(),
        ),
    )
    db.commit()
    return redirect(url_for("painel"))


@app.route("/painel/route/delete/<int:route_id>", methods=["GET"])
@login_required
def delete_route(route_id: int):
    db = get_auth_db()
    user = current_user()
    db.execute("DELETE FROM user_routes WHERE id = ? AND user_id = ?", (route_id, user["id"]))
    db.commit()
    return redirect(url_for("painel"))

@app.route("/painel/route/update/<int:route_id>", methods=["POST"])
@login_required
def update_route(route_id: int):
    db = get_auth_db()
    user = current_user()
    db.execute(
        """
        UPDATE user_routes
        SET origin = ?, destination = ?, outbound_date = ?, inbound_date = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            request.form.get("origin", "").strip().upper(),
            request.form.get("destination", "").strip().upper(),
            request.form.get("outbound_date", "").strip(),
            request.form.get("inbound_date", "").strip(),
            route_id,
            user["id"],
        ),
    )
    db.commit()
    return redirect(url_for("painel", _anchor="rotas"))


@app.route("/painel/telegram", methods=["POST"])
@login_required
def save_telegram():
    db = get_auth_db()
    user = current_user()
    db.execute(
        """
        INSERT INTO user_telegram (user_id, bot_token, chat_id, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
          bot_token = excluded.bot_token,
          chat_id = excluded.chat_id,
          updated_at = excluded.updated_at
        """,
        (
            user["id"],
            request.form.get("bot_token", "").strip(),
            request.form.get("chat_id", "").strip(),
            datetime.now().isoformat(),
        ),
    )
    db.commit()
    return redirect(url_for("painel"))


@app.route("/painel/run-now", methods=["POST"])
@login_required
def run_now_user():
    user = current_user()
    run_user_scan(int(user["id"]), trigger="painel-manual", notify=True)
    return redirect(url_for("painel", _anchor="cron"))


@app.route("/painel/cron", methods=["POST"])
@login_required
def save_cron():
    db = get_auth_db()
    user = current_user()
    enabled = 1 if request.form.get("enabled") else 0
    schedule_minutes = max(1, min(1440, int(request.form.get("schedule_minutes", DEFAULT_SCHEDULE_MINUTES))))
    hours_from_minutes = (schedule_minutes + 59) // 60
    every_hours = max(1, min(24, hours_from_minutes))
    db.execute(
        """
        INSERT INTO user_cron (user_id, enabled, every_hours, schedule_minutes, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
          enabled = excluded.enabled,
          every_hours = excluded.every_hours,
          schedule_minutes = excluded.schedule_minutes,
          updated_at = excluded.updated_at
        """,
        (user["id"], enabled, every_hours, schedule_minutes, datetime.now().isoformat()),
    )
    db.commit()
    return redirect(url_for("painel", _anchor="cron"))


if __name__ == "__main__":
    init_auth_tables()
    start_auto_scan_if_needed()
    debug_mode = os.getenv("FLASK_DEBUG", "0").strip().lower() in ("1", "true", "yes")
    app.run(debug=debug_mode)
