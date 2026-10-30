import json
import secrets
import hashlib
import hmac
from http.cookies import SimpleCookie
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
PROMOTIONS_ADMIN_PASSWORD = os.getenv("PROMOTIONS_ADMIN_PASSWORD", "").strip()
PORT = int(os.getenv("PORT", "8080"))
SERVICES = {"Наращивание", "Окрашивание", "Маникюр", "Брови"}

def init_db():
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS website_promotions (
                id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', image_url TEXT NOT NULL DEFAULT '', old_price TEXT NOT NULL DEFAULT '', new_price TEXT NOT NULL DEFAULT '', start_date DATE, end_date DATE, published BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
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
            db.execute("""CREATE TABLE IF NOT EXISTS website_promotions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', image_url TEXT NOT NULL DEFAULT '', old_price TEXT NOT NULL DEFAULT '', new_price TEXT NOT NULL DEFAULT '', start_date TEXT, end_date TEXT, published INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
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

def promotion_rows(admin=False):
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as db:
            if admin:
                rows = db.execute("SELECT id,title,description,image_url,old_price,new_price,start_date,end_date,published FROM website_promotions ORDER BY id DESC").fetchall()
            else:
                rows = db.execute("SELECT id,title,description,image_url,old_price,new_price,start_date,end_date,published FROM website_promotions WHERE published=TRUE AND (start_date IS NULL OR start_date<=CURRENT_DATE) AND (end_date IS NULL OR end_date>=CURRENT_DATE) ORDER BY id DESC").fetchall()
    else:
        with sqlite3.connect(DB_PATH) as db:
            db.row_factory = sqlite3.Row
            if admin:
                rows = db.execute("SELECT id,title,description,image_url,old_price,new_price,start_date,end_date,published FROM website_promotions ORDER BY id DESC").fetchall()
            else:
                today = date.today().isoformat()
                rows = db.execute("SELECT id,title,description,image_url,old_price,new_price,start_date,end_date,published FROM website_promotions WHERE published=1 AND (start_date IS NULL OR start_date<=?) AND (end_date IS NULL OR end_date>=?) ORDER BY id DESC", (today,today)).fetchall()
    keys = ["id","title","description","image_url","old_price","new_price","start_date","end_date","published"]
    return [dict(zip(keys, row)) for row in rows]

def admin_cookie_ok(handler):
    if not PROMOTIONS_ADMIN_PASSWORD:
        return False
    cookie = SimpleCookie()
    try: cookie.load(handler.headers.get("Cookie", ""))
    except Exception: return False
    morsel = cookie.get("salon_admin")
    if not morsel: return False
    expected = hmac.new(PROMOTIONS_ADMIN_PASSWORD.encode(), b"lepetok-promotions-admin", hashlib.sha256).hexdigest()
    return hmac.compare_digest(morsel.value, expected)

def save_promotion(data, promotion_id=None):
    title = str(data.get("title", "")).strip()[:120]
    description = str(data.get("description", "")).strip()[:1500]
    image_url = str(data.get("image_url", "")).strip()[:2800000]
    old_price = str(data.get("old_price", "")).strip()[:40]
    new_price = str(data.get("new_price", "")).strip()[:40]
    start_date = str(data.get("start_date", "")).strip() or None
    end_date = str(data.get("end_date", "")).strip() or None
    published = bool(data.get("published", False))
    if len(title) < 2: raise ValueError("??????? ???????? ????? (??????? 2 ???????).")
    for value in (start_date, end_date):
        if value:
            try: date.fromisoformat(value)
            except ValueError: raise ValueError("????????? ???? ?????.")
    if start_date and end_date and end_date < start_date: raise ValueError("???? ????????? ?? ????? ???? ?????? ???? ??????.")
    if image_url and not (image_url.startswith(("https://", "http://")) or re.fullmatch(r"data:image/(?:jpeg|png|webp|gif);base64,[A-Za-z0-9+/=]+", image_url)):
        raise ValueError("Укажите ссылку https:// или загрузите изображение допустимого формата.")
    if image_url.startswith("data:image/") and len(image_url) > 2800000:
        raise ValueError("Изображение слишком большое. Максимальный размер файла — 2 МБ.")
    vals=(title,description,image_url,old_price,new_price,start_date,end_date,published)
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as db:
            if promotion_id:
                cur=db.execute("UPDATE website_promotions SET title=%s,description=%s,image_url=%s,old_price=%s,new_price=%s,start_date=%s,end_date=%s,published=%s WHERE id=%s RETURNING id", vals+(promotion_id,))
            else:
                cur=db.execute("INSERT INTO website_promotions(title,description,image_url,old_price,new_price,start_date,end_date,published) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id", vals)
            row=cur.fetchone()
            if not row: raise ValueError("????? ?? ???????.")
            return row[0]
    with sqlite3.connect(DB_PATH) as db:
        if promotion_id:
            cur=db.execute("UPDATE website_promotions SET title=?,description=?,image_url=?,old_price=?,new_price=?,start_date=?,end_date=?,published=? WHERE id=?", vals+(promotion_id,))
            if cur.rowcount == 0: raise ValueError("????? ?? ???????.")
            return promotion_id
        cur=db.execute("INSERT INTO website_promotions(title,description,image_url,old_price,new_price,start_date,end_date,published) VALUES(?,?,?,?,?,?,?,?)", vals)
        return cur.lastrowid

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
        if self.path == "/api/promotions":
            return self.send_json(200, {"ok": True, "promotions": promotion_rows(False)})
        if self.path == "/api/admin/promotions":
            if not admin_cookie_ok(self): return self.send_json(401, {"ok": False, "error": "??????? ? ?????? ??????????."})
            return self.send_json(200, {"ok": True, "promotions": promotion_rows(True)})
        if self.path in ("/api/health", "/health"):
            return self.send_json(200, {"ok": True, "service": "salon-booking"})
        request_path = urllib.parse.urlsplit(self.path).path
        if request_path in ("/", "/index.html"): request_path = "index.html"
        elif request_path == "/admin": request_path = "admin.html"
        path = (ROOT / request_path.lstrip("/")).resolve()
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
        if self.path == "/api/admin/login":
            if not PROMOTIONS_ADMIN_PASSWORD:
                return self.send_json(503, {"ok": False, "error": "????????????? ??? ?? ???????? ?????? ? Render."})
            try:
                length=int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 4000: raise ValueError()
                data=json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                return self.send_json(400, {"ok": False, "error": "?? ??????? ????????? ?????."})
            if not hmac.compare_digest(str(data.get("password", "")), PROMOTIONS_ADMIN_PASSWORD):
                return self.send_json(401, {"ok": False, "error": "???????? ??????."})
            token=hmac.new(PROMOTIONS_ADMIN_PASSWORD.encode(), b"lepetok-promotions-admin", hashlib.sha256).hexdigest()
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", f"salon_admin={token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=28800")
            self.end_headers(); self.wfile.write(json.dumps({"ok":True},ensure_ascii=False).encode()); return
        if self.path == "/api/admin/logout":
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Set-Cookie", "salon_admin=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0"); self.end_headers(); self.wfile.write(b'{"ok":true}'); return
        if self.path == "/api/admin/promotions":
            if not admin_cookie_ok(self): return self.send_json(401, {"ok":False,"error":"??????? ? ?????? ??????????."})
            try:
                length=int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 4000000: raise ValueError()
                data=json.loads(self.rfile.read(length).decode("utf-8"))
                pid=save_promotion(data)
                return self.send_json(201,{"ok":True,"id":pid})
            except ValueError as exc: return self.send_json(400,{"ok":False,"error":str(exc)})
            except Exception as exc:
                print("Promotion save failed:",exc,flush=True); return self.send_json(500,{"ok":False,"error":"?? ??????? ????????? ?????."})
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

    def do_PUT(self):
        if not self.path.startswith("/api/admin/promotions/") or not admin_cookie_ok(self): return self.send_json(401,{"ok":False,"error":"??? ???????."})
        try:
            pid=int(self.path.rsplit("/",1)[1]); length=int(self.headers.get("Content-Length","0"))
            if length < 2 or length > 4000000: raise ValueError()
            data=json.loads(self.rfile.read(length).decode("utf-8")); save_promotion(data,pid)
            return self.send_json(200,{"ok":True,"id":pid})
        except ValueError as exc: return self.send_json(400,{"ok":False,"error":str(exc)})
        except Exception as exc:
            print("Promotion update failed:",exc,flush=True); return self.send_json(500,{"ok":False,"error":"?? ??????? ???????? ?????."})

    def do_DELETE(self):
        if not self.path.startswith("/api/admin/promotions/") or not admin_cookie_ok(self): return self.send_json(401,{"ok":False,"error":"??? ???????."})
        try:
            pid=int(self.path.rsplit("/",1)[1])
            if DATABASE_URL:
                import psycopg
                with psycopg.connect(DATABASE_URL) as db: cur=db.execute("DELETE FROM website_promotions WHERE id=%s",(pid,))
            else:
                with sqlite3.connect(DB_PATH) as db: cur=db.execute("DELETE FROM website_promotions WHERE id=?",(pid,))
            if cur.rowcount == 0: return self.send_json(404,{"ok":False,"error":"????? ?? ???????."})
            return self.send_json(200,{"ok":True})
        except Exception as exc:
            print("Promotion delete failed:",exc,flush=True); return self.send_json(500,{"ok":False,"error":"?? ??????? ??????? ?????."})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

if __name__ == "__main__":
    init_db()
    print(f"Salon website listening on 0.0.0.0:{PORT}; database={DB_PATH}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
