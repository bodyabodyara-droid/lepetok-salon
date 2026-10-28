import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT.parent / ".env")
except ImportError:
    pass
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = Path(os.getenv("BOOKINGS_DB", str(ROOT / "bookings.sqlite3")))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
PORT = int(os.getenv("PORT", "8080"))
SERVICES = {"Наращивание", "Окрашивание", "Маникюр", "Брови"}

def init_db():
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS website_bookings (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL, phone TEXT NOT NULL, service TEXT NOT NULL,
                booking_date DATE NOT NULL, preferred_time TEXT NOT NULL DEFAULT '',
                contact_method TEXT NOT NULL DEFAULT 'Телефон', comment TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, status TEXT NOT NULL DEFAULT 'new'
            )""")
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS website_bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, phone TEXT NOT NULL, service TEXT NOT NULL,
                booking_date TEXT NOT NULL, preferred_time TEXT NOT NULL DEFAULT '',
                contact_method TEXT NOT NULL DEFAULT 'Телефон', comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, status TEXT NOT NULL DEFAULT 'new'
            )""")

def notify_admin(text):
    if not BOT_TOKEN or not ADMIN_ID or not ADMIN_ID.isdigit():
        return False
    payload = urllib.parse.urlencode({"chat_id": ADMIN_ID, "text": text}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=payload, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status == 200
    except Exception as exc:
        print("Telegram notification failed:", exc, flush=True)
        return False

class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/api/health", "/health"):
            return self.send_json(200, {"ok": True, "service": "salon-booking"})
        path = (ROOT / ("index.html" if self.path in ("/", "/index.html") else self.path.lstrip("/"))).resolve()
        if ROOT not in path.parents and path != ROOT / "index.html":
            return self.send_json(403, {"ok": False, "error": "Forbidden"})
        if not path.is_file() or path.suffix.lower() not in {".html", ".css", ".js", ".svg", ".png", ".jpg", ".webp"}:
            return self.send_json(404, {"ok": False, "error": "Not found"})
        content_type = {".html":"text/html; charset=utf-8",".css":"text/css; charset=utf-8",".js":"application/javascript; charset=utf-8",".svg":"image/svg+xml",".png":"image/png",".jpg":"image/jpeg",".webp":"image/webp"}.get(path.suffix.lower(), "application/octet-stream")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/api/bookings":
            return self.send_json(404, {"ok": False, "error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > 12000:
                return self.send_json(400, {"ok": False, "error": "Некорректный размер заявки."})
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self.send_json(400, {"ok": False, "error": "Не удалось прочитать форму."})
        name = re.sub(r"\s+", " ", str(data.get("name", "")).strip())[:80]
        phone = re.sub(r"\s+", " ", str(data.get("phone", "")).strip())[:40]
        service = str(data.get("service", "")).strip()
        booking_date = str(data.get("date", "")).strip()
        preferred_time = str(data.get("time", "")).strip()[:40]
        contact = str(data.get("contact", "Телефон")).strip()[:30]
        comment = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(data.get("comment", "")).strip())[:1000]
        if len(name) < 2 or len(phone) < 5 or service not in SERVICES:
            return self.send_json(400, {"ok": False, "error": "Проверьте имя, телефон и выбранную услугу."})
        try:
            selected_date = date.fromisoformat(booking_date)
            if selected_date < date.today():
                return self.send_json(400, {"ok": False, "error": "Выберите сегодняшнюю или будущую дату."})
        except ValueError:
            return self.send_json(400, {"ok": False, "error": "Выберите корректную дату."})
        if DATABASE_URL:
            import psycopg
            with psycopg.connect(DATABASE_URL) as db:
                cur = db.execute(
                    "INSERT INTO website_bookings(name,phone,service,booking_date,preferred_time,contact_method,comment) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (name, phone, service, booking_date, preferred_time, contact, comment)
                )
                booking_id = cur.fetchone()[0]
        else:
            with sqlite3.connect(DB_PATH) as db:
                cur = db.execute(
                    "INSERT INTO website_bookings(name,phone,service,booking_date,preferred_time,contact_method,comment) VALUES(?,?,?,?,?,?,?)",
                    (name, phone, service, booking_date, preferred_time, contact, comment)
                )
                booking_id = cur.lastrowid
        message = (
            f"🌸 Новая заявка с сайта №{booking_id}\n"
            f"Имя: {name}\nТелефон: {phone}\nУслуга: {service}\n"
            f"Дата: {booking_date}\nВремя: {preferred_time or 'не указано'}\n"
            f"Связь: {contact}\nКомментарий: {comment or '—'}"
        )
        notified = notify_admin(message)
        print(f"Booking {booking_id} saved. Telegram notified: {notified}", flush=True)
        return self.send_json(201, {"ok": True, "booking_id": booking_id, "notified": notified,
            "message": "Заявка сохранена. " + ("Уведомление отправлено мастеру." if notified else "Для подтверждения записи мастер свяжется с вами.")})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

if __name__ == "__main__":
    init_db()
    print(f"Salon website listening on 0.0.0.0:{PORT}; database={DB_PATH}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
