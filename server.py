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

def init_services():
    """Create the editable price catalog and import the current HTML catalog once."""
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as db:
            db.execute("CREATE TABLE IF NOT EXISTS website_service_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS website_services (
                id BIGSERIAL PRIMARY KEY,
                category TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '✦',
                name TEXT NOT NULL,
                price TEXT NOT NULL DEFAULT '',
                duration_minutes INTEGER,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            seeded = db.execute("SELECT value FROM website_service_meta WHERE key='catalog_initialized'").fetchone()
            count = db.execute("SELECT COUNT(*) FROM website_services").fetchone()[0]
            if not seeded:
                if count == 0:
                    _seed_services(db, "%s")
                db.execute("INSERT INTO website_service_meta(key,value) VALUES('catalog_initialized','1')")
    else:
        with sqlite3.connect(DB_PATH) as db:
            db.execute("CREATE TABLE IF NOT EXISTS website_service_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS website_services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '✦',
                name TEXT NOT NULL,
                price TEXT NOT NULL DEFAULT '',
                duration_minutes INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            seeded = db.execute("SELECT value FROM website_service_meta WHERE key='catalog_initialized'").fetchone()
            count = db.execute("SELECT COUNT(*) FROM website_services").fetchone()[0]
            if not seeded:
                if count == 0:
                    _seed_services(db, "?")
                db.execute("INSERT INTO website_service_meta(key,value) VALUES('catalog_initialized','1')")

def _seed_services(db, placeholder):
    """Read the existing visible catalog so first deployment preserves all current prices."""
    import html
    import re
    source = (ROOT / "index.html").read_text(encoding="utf-8")
    cards = re.findall(r'<article class="service catalog-card">(.*?)</article>', source, re.S)
    rows = []
    for card in cards:
        heading = re.search(r'<h3>(.*?)</h3>', card, re.S)
        icon_match = re.search(r'<div class="icon">(.*?)</div>', card, re.S)
        if not heading:
            continue
        category = html.unescape(re.sub(r'<.*?>', '', heading.group(1))).strip()
        icon = html.unescape(re.sub(r'<.*?>', '', icon_match.group(1))).strip() if icon_match else "✦"
        for item in re.findall(r'<li>\s*<span>(.*?)</span>\s*<b>(.*?)</b>\s*</li>', card, re.S):
            name = html.unescape(re.sub(r'<.*?>', '', item[0])).strip()
            price = html.unescape(re.sub(r'<.*?>', '', item[1])).strip()
            if name:
                rows.append((category, icon or "✦", name, price, None, len(rows)))
    if not rows:
        rows = [("Услуги", "✦", "Новая услуга", "Уточняйте", None, 0)]
    sql = "INSERT INTO website_services(category,icon,name,price,duration_minutes,sort_order) VALUES(" + ",".join([placeholder] * 6) + ")"
    for row in rows:
        db.execute(sql, row)

def service_rows(admin=False):
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as db:
            where = "" if admin else " WHERE active=TRUE"
            rows = db.execute("SELECT id,category,icon,name,price,duration_minutes,active,sort_order FROM website_services" + where + " ORDER BY sort_order,id").fetchall()
    else:
        with sqlite3.connect(DB_PATH) as db:
            db.row_factory = sqlite3.Row
            where = "" if admin else " WHERE active=1"
            rows = db.execute("SELECT id,category,icon,name,price,duration_minutes,active,sort_order FROM website_services" + where + " ORDER BY sort_order,id").fetchall()
    keys = ["id","category","icon","name","price","duration_minutes","active","sort_order"]
    return [dict(zip(keys, row)) for row in rows]

def save_service(data, service_id=None):
    category = re.sub(r"\s+", " ", str(data.get("category", "")).strip())[:100]
    icon = str(data.get("icon", "✦")).strip()[:12] or "✦"
    name = re.sub(r"\s+", " ", str(data.get("name", "")).strip())[:140]
    price = str(data.get("price", "")).strip()[:60]
    duration = data.get("duration_minutes")
    active = bool(data.get("active", True))
    try:
        duration = int(duration) if duration not in (None, "") else None
    except (TypeError, ValueError):
        raise ValueError("Длительность должна быть числом минут.")
    if duration is not None and not 1 <= duration <= 1440:
        raise ValueError("Длительность должна быть от 1 до 1440 минут.")
    if len(category) < 2 or len(name) < 2 or not price:
        raise ValueError("Укажите категорию, название услуги и цену.")
    sort_order = int(data.get("sort_order") or 0)
    vals = (category, icon, name, price, duration, active, sort_order)
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as db:
            if service_id:
                cur = db.execute("UPDATE website_services SET category=%s,icon=%s,name=%s,price=%s,duration_minutes=%s,active=%s,sort_order=%s WHERE id=%s RETURNING id", vals + (service_id,))
                if not cur.fetchone(): raise ValueError("Услуга не найдена.")
                return service_id
            cur = db.execute("INSERT INTO website_services(category,icon,name,price,duration_minutes,active,sort_order) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id", vals)
            return cur.fetchone()[0]
    with sqlite3.connect(DB_PATH) as db:
        if service_id:
            cur = db.execute("UPDATE website_services SET category=?,icon=?,name=?,price=?,duration_minutes=?,active=?,sort_order=? WHERE id=?", vals + (service_id,))
            if cur.rowcount == 0: raise ValueError("Услуга не найдена.")
            return service_id
        cur = db.execute("INSERT INTO website_services(category,icon,name,price,duration_minutes,active,sort_order) VALUES(?,?,?,?,?,?,?)", vals)
        return cur.lastrowid

def delete_service(service_id):
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as db:
            cur = db.execute("DELETE FROM website_services WHERE id=%s", (service_id,))
    else:
        with sqlite3.connect(DB_PATH) as db:
            cur = db.execute("DELETE FROM website_services WHERE id=?", (service_id,))
    if cur.rowcount == 0:
        raise ValueError("Услуга не найдена.")

def valid_booking_service(value):
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as db:
            row = db.execute("SELECT 1 FROM website_services WHERE active=TRUE AND category=%s LIMIT 1", (value,)).fetchone()
    else:
        with sqlite3.connect(DB_PATH) as db:
            row = db.execute("SELECT 1 FROM website_services WHERE active=1 AND category=? LIMIT 1", (value,)).fetchone()
    return bool(row)

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
        data = json.dumps(payload, ensure_ascii=False, default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value)).encode()
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
        if self.path == "/api/services":
            return self.send_json(200, {"ok": True, "services": service_rows(False)})
        if self.path == "/api/admin/services":
            if not admin_cookie_ok(self): return self.send_json(401, {"ok": False, "error": "Требуется вход администратора."})
            return self.send_json(200, {"ok": True, "services": service_rows(True)})
        if self.path == "/api/promotions":
            return self.send_json(200, {"ok": True, "promotions": promotion_rows(False)})
        if self.path == "/api/admin/promotions":
            if not admin_cookie_ok(self): return self.send_json(401, {"ok": False, "error": "??????? ? ?????? ??????????."})
            return self.send_json(200, {"ok": True, "promotions": promotion_rows(True)})
        if self.path == "/api/admin/bookings":
            if not admin_cookie_ok(self): return self.send_json(401, {"ok": False, "error": "Unauthorized"})
            if DATABASE_URL:
                import psycopg
                with psycopg.connect(DATABASE_URL) as db:
                    rows = db.execute("SELECT id,name,phone,service,booking_date,preferred_time,contact_method,comment,created_at,status FROM website_bookings ORDER BY booking_date DESC, preferred_time DESC, id DESC LIMIT 500").fetchall()
            else:
                with sqlite3.connect(DB_PATH) as db:
                    db.row_factory = sqlite3.Row
                    rows = db.execute("SELECT id,name,phone,service,booking_date,preferred_time,contact_method,comment,created_at,status FROM website_bookings ORDER BY booking_date DESC, preferred_time DESC, id DESC LIMIT 500").fetchall()
            keys = ["id","name","phone","service","booking_date","preferred_time","contact_method","comment","created_at","status"]
            return self.send_json(200, {"ok": True, "bookings": [dict(zip(keys, row)) for row in rows]})
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
        if self.path == "/api/admin/services":
            if not admin_cookie_ok(self): return self.send_json(401, {"ok": False, "error": "Admin login required."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 12000: raise ValueError("Invalid request size.")
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                service_id = save_service(data)
                return self.send_json(201, {"ok": True, "id": service_id})
            except ValueError as exc: return self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                print("Service save failed:", exc, flush=True)
                return self.send_json(500, {"ok": False, "error": "Could not save service."})
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
        if len(name) < 2 or len(phone) < 5 or not valid_booking_service(service):
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
        if self.path.startswith("/api/admin/services/"):
            if not admin_cookie_ok(self): return self.send_json(401, {"ok": False, "error": "Требуется вход администратора."})
            try:
                service_id = int(self.path.rsplit("/", 1)[1])
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 12000: raise ValueError("Некорректный размер запроса.")
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                save_service(data, service_id)
                return self.send_json(200, {"ok": True, "id": service_id})
            except ValueError as exc: return self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                print("Service update failed:", exc, flush=True)
                return self.send_json(500, {"ok": False, "error": "Не удалось обновить услугу."})
        if not self.path.startswith("/api/admin/promotions/") or not admin_cookie_ok(self): return self.send_json(401,{"ok":False,"error":"??? ???????."})
        try:
            pid=int(self.path.rsplit("/",1)[1]); length=int(self.headers.get("Content-Length","0"))
            if length < 2 or length > 4000000: raise ValueError()
            data=json.loads(self.rfile.read(length).decode("utf-8")); save_promotion(data,pid)
            return self.send_json(200,{"ok":True,"id":pid})
        except ValueError as exc: return self.send_json(400,{"ok":False,"error":str(exc)})
        except Exception as exc:
            print("Promotion update failed:",exc,flush=True); return self.send_json(500,{"ok":False,"error":"?? ??????? ???????? ?????."})

    def do_PATCH(self):
        if not self.path.startswith("/api/admin/bookings/") or not admin_cookie_ok(self):
            return self.send_json(401, {"ok": False, "error": "Unauthorized"})
        try:
            booking_id = int(self.path.rsplit("/", 1)[1])
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > 4000: raise ValueError("Invalid request")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            status = str(data.get("status", ""))
            if status not in {"new", "confirmed", "completed", "cancelled"}:
                raise ValueError("Invalid booking status")
            if DATABASE_URL:
                import psycopg
                with psycopg.connect(DATABASE_URL) as db:
                    cur = db.execute("UPDATE website_bookings SET status=%s WHERE id=%s", (status, booking_id))
            else:
                with sqlite3.connect(DB_PATH) as db:
                    cur = db.execute("UPDATE website_bookings SET status=? WHERE id=?", (status, booking_id))
            if cur.rowcount == 0: return self.send_json(404, {"ok": False, "error": "Booking not found"})
            return self.send_json(200, {"ok": True, "id": booking_id, "status": status})
        except ValueError as exc:
            return self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            print("Booking status update failed:", exc, flush=True)
            return self.send_json(500, {"ok": False, "error": "Could not update booking"})

    def do_DELETE(self):
        if self.path.startswith("/api/admin/services/"):
            if not admin_cookie_ok(self): return self.send_json(401, {"ok": False, "error": "Требуется вход администратора."})
            try:
                service_id = int(self.path.rsplit("/", 1)[1])
                delete_service(service_id)
                return self.send_json(200, {"ok": True})
            except ValueError as exc: return self.send_json(404, {"ok": False, "error": str(exc)})
            except Exception as exc:
                print("Service delete failed:", exc, flush=True)
                return self.send_json(500, {"ok": False, "error": "Не удалось удалить услугу."})
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
    init_services()
    print(f"Salon website listening on 0.0.0.0:{PORT}; database={DB_PATH}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
