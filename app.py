"""
Sistem Pemantauan Sampah Cerdas – Kampus ITERA
Backend Flask (satu file) : auth, CCTV worker + AI detection, WhatsApp alert,
riwayat, laporan (PDF/Excel), pengaturan, dan API real-time untuk dashboard.

Jalankan:  python app.py      lalu buka http://localhost:5000
Login awal: admin / admin123   (ubah di Pengaturan)
"""
import io
import json
import os
import random
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

import numpy as np
import requests
from PIL import Image as PILImage, ImageDraw, ImageFont

try:  # OpenCV opsional: tanpa cv2, web tetap jalan untuk mode Simulasi & eksternal
    import cv2
except ImportError:
    cv2 = None
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "sampah.db")
SNAP_DIR = os.path.join(BASE_DIR, "data", "snapshots")
os.makedirs(SNAP_DIR, exist_ok=True)



# ---------------------------------------------------------------------------
# Utilitas gambar: pakai OpenCV jika ada, jika tidak pakai Pillow.
# Semua frame tetap berupa array numpy BGR (format OpenCV).
# ---------------------------------------------------------------------------
def _pil_to_bgr(im):
    return np.asarray(im.convert("RGB"))[..., ::-1].copy()


def img_read(path):
    if cv2 is not None:
        return cv2.imread(path)
    try:
        return _pil_to_bgr(PILImage.open(path))
    except Exception:
        return None


def img_decode(data):
    if not data:
        return None
    if cv2 is not None:
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    try:
        return _pil_to_bgr(PILImage.open(io.BytesIO(data)))
    except Exception:
        return None


def img_resize(im, w, h):
    if cv2 is not None:
        return cv2.resize(im, (w, h), interpolation=cv2.INTER_AREA if w < im.shape[1] else cv2.INTER_CUBIC)
    return _pil_to_bgr(PILImage.fromarray(im[..., ::-1]).resize((w, h), PILImage.LANCZOS))


def img_encode(im, quality=85):
    if cv2 is not None:
        ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None
    out = io.BytesIO()
    PILImage.fromarray(im[..., ::-1]).save(out, "JPEG", quality=quality)
    return out.getvalue()


def img_write(path, im, quality=85):
    data = img_encode(im, quality)
    if data:
        with open(path, "wb") as f:
            f.write(data)


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "ganti-secret-key-ini-" + "itera"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

LABEL = {"penuh": "Penuh", "kosong": "Kosong", "berserakan": "Berserakan",
         "sebagian": "Terisi sebagian", "bersih": "Bersih", "offline": "CCTV Offline", "plastik": "Plastik di bak"}

DEFAULT_SETTINGS = {
    "WA":"9GBA-Z22Z",
    "confidence_threshold": "0.35",   # batas minimum keyakinan model (sama dengan notebook)
    "full_threshold": "50",           # % level (luas bak tertutup plastik) => penuh
    "empty_threshold": "10",          # % level => kosong
    "detection_interval": "2",        # detik antar inferensi
    "alert_cooldown": "300",          # detik jeda alert berulang
    "stable_frames": "3",             # jumlah inferensi konsisten sebelum dicatat
    "roi": "0.15,0.55,1.0,1.0",       # x1,y1,x2,y2 (0-1) area pantauan sampah berserakan
    "model_path": "models/best.pt",   # model YOLO (ultralytics). Jika tidak ada => mode simulasi
    "sim_mode": "auto",               # auto (berputar) | manual (satu skenario), diatur di menu Simulasi
    "sim_cycle": "20",                # detik per skenario saat mode auto
    "sim_active": "",                 # id skenario saat mode manual
    "notify_enabled": "1",
    "notify_full": "1",
    "notify_scattered": "1",
    "notify_empty": "0",
    "notify_offline": "0",
    "wa_provider": "simulasi",        # simulasi | cloud (Meta WhatsApp Cloud API) | fonnte
    "wa_cloud_token": "",
    "wa_phone_number_id": "",
    "wa_api_version": "v20.0",
    "wa_verify_token": "itera-verify",
    "fonnte_token": "",
    "base_url": "http://localhost:5000",
    "ingest_api_key": "",
}


# ---------------------------------------------------------------- database
def db_connect():
    con = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def get_db():
    if "db" not in g:
        g.db = db_connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cameras (
  id INTEGER PRIMARY KEY AUTOINCREMENT, camera_name TEXT NOT NULL, location TEXT NOT NULL,
  stream_url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'offline', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS detection_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id INTEGER NOT NULL REFERENCES cameras(id),
  detection_type TEXT NOT NULL, fullness_percentage REAL, confidence_score REAL,
  snapshot_path TEXT, detected_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notification_recipients (
  id INTEGER PRIMARY KEY AUTOINCREMENT, recipient_name TEXT NOT NULL, phone_number TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notification_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, detection_id INTEGER REFERENCES detection_logs(id) ON DELETE SET NULL,
  recipient_id INTEGER REFERENCES notification_recipients(id) ON DELETE SET NULL,
  message TEXT NOT NULL, delivery_status TEXT NOT NULL, provider_message_id TEXT,
  error TEXT, sent_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS system_settings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, setting_key TEXT UNIQUE NOT NULL, setting_value TEXT,
  updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cleaning_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, detection_id INTEGER NOT NULL REFERENCES detection_logs(id) ON DELETE CASCADE,
  assigned_to INTEGER REFERENCES notification_recipients(id) ON DELETE SET NULL,
  action_status TEXT NOT NULL DEFAULT 'menunggu', token TEXT UNIQUE, confirmed_at TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS fullness_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id INTEGER NOT NULL, fullness REAL NOT NULL,
  sampled_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_det_time ON detection_logs(detected_at);
CREATE INDEX IF NOT EXISTS idx_samp_time ON fullness_samples(sampled_at);
CREATE INDEX IF NOT EXISTS idx_notif_time ON notification_logs(sent_at);
CREATE TABLE IF NOT EXISTS sim_images (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, file_path TEXT NOT NULL, condition TEXT NOT NULL,
  fullness REAL, boxes TEXT, is_enabled INTEGER NOT NULL DEFAULT 1, is_builtin INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 100, created_at TEXT NOT NULL);
"""


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    con = db_connect()
    con.executescript(SCHEMA)
    ts = now_str()
    if not con.execute("SELECT 1 FROM users").fetchone():
        con.execute("INSERT INTO users(name,username,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                    ("Admin ITERA", "admin", generate_password_hash("admin123"), "admin", ts))
    if not con.execute("SELECT 1 FROM cameras").fetchone():
        con.execute("INSERT INTO cameras(camera_name,location,stream_url,status,created_at) VALUES(?,?,?,?,?)",
                    ("CCTV-01", "Area Gedung A ITERA", "demo", "offline", ts))
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO system_settings(setting_key,setting_value,updated_at) VALUES(?,?,?)",
                    (k, v, ts))
    for i, (name, path, cond, full, boxes) in enumerate(BUILTIN_SCENARIOS):
        if not con.execute("SELECT 1 FROM sim_images WHERE file_path=?", (path,)).fetchone():
            con.execute("INSERT INTO sim_images(name,file_path,condition,fullness,boxes,is_enabled,is_builtin,"
                        "sort_order,created_at) VALUES(?,?,?,?,?,?,1,?,?)",
                        (name, path, cond, full, json.dumps(boxes), 0 if cond == "offline" else 1, i, ts))
    if not con.execute("SELECT 1 FROM system_settings WHERE setting_key='skema_v2'").fetchone():
        # Sekali jalan saat beralih ke model bak + plastik: ambang mengikuti notebook (hanya jika masih nilai lama),
        # skenario bak ITERA (dideteksi model) jadi putaran utama, skenario tong umum dimatikan (bisa dinyalakan lagi).
        for k, lama, baru in (("confidence_threshold", "0.50", "0.35"), ("full_threshold", "80", "50"),
                              ("empty_threshold", "15", "10")):
            con.execute("UPDATE system_settings SET setting_value=? WHERE setting_key=? AND setting_value=?",
                        (baru, k, lama))
        con.execute("UPDATE sim_images SET is_enabled=0, sort_order=sort_order+20 WHERE is_builtin=1 AND condition!='ai'")
        con.execute("INSERT INTO system_settings(setting_key,setting_value,updated_at) VALUES('skema_v2','1',?)", (ts,))
    if not con.execute("SELECT setting_value FROM system_settings WHERE setting_key='ingest_api_key'").fetchone()[0]:
        con.execute("UPDATE system_settings SET setting_value=? WHERE setting_key='ingest_api_key'",
                    (secrets.token_urlsafe(24),))
    con.commit()
    con.close()


_settings_cache = {"t": 0, "v": {}}
_settings_lock = threading.Lock()


def settings(force=False):
    with _settings_lock:
        if force or time.time() - _settings_cache["t"] > 2:
            con = db_connect()
            rows = con.execute("SELECT setting_key, setting_value FROM system_settings").fetchall()
            con.close()
            v = dict(DEFAULT_SETTINGS)
            v.update({r["setting_key"]: r["setting_value"] for r in rows})
            _settings_cache.update(t=time.time(), v=v)
        return dict(_settings_cache["v"])


def sget(key, cast=str):
    val = settings().get(key, DEFAULT_SETTINGS.get(key))
    try:
        return cast(val)
    except (TypeError, ValueError):
        return cast(DEFAULT_SETTINGS[key])


def sbool(key):
    return str(settings().get(key, "0")) in ("1", "true", "on")


def save_settings(values: dict):
    con = db_connect()
    ts = now_str()
    for k, v in values.items():
        con.execute("INSERT INTO system_settings(setting_key,setting_value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at",
                    (k, str(v), ts))
    con.commit()
    con.close()
    settings(force=True)


def get_camera(con=None):
    own = con is None
    con = con or db_connect()
    row = con.execute("SELECT * FROM cameras ORDER BY id LIMIT 1").fetchone()
    if own:
        con.close()
    return dict(row) if row else None


# ---------------------------------------------------------------- WhatsApp
def normalize_phone(p):
    digits = "".join(ch for ch in p if ch.isdigit())
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    return digits


def build_message(kind, cam, fullness=None, when=None, confirm_url=None):
    t = (when or datetime.now()).strftime("%H:%M WIB")
    tgl = (when or datetime.now()).strftime("%d/%m/%Y")
    lokasi = f"- Lokasi: {cam['location']}\n- CCTV: {cam['camera_name']}\n"
    if kind == "penuh":
        msg = ("⚠️ ALERT SAMPAH – KAMPUS ITERA\n\nTerdeteksi kondisi tempat sampah penuh.\n\n" + lokasi +
               f"- Kondisi: Penuh\n- Kepenuhan: {fullness:.0f}%\n- Waktu: {t} ({tgl})\n\n"
               "Mohon petugas segera melakukan pengecekan dan pengangkutan sampah.")
    elif kind == "berserakan":
        msg = ("⚠️ PERINGATAN SAMPAH BERSERAKAN\n\nSistem mendeteksi sampah di luar tempat sampah.\n\n" + lokasi +
               f"- Kondisi: Berserakan\n- Waktu: {t} ({tgl})\n\nMohon dilakukan pembersihan area.")
    elif kind == "kosong":
        msg = ("ℹ️ INFORMASI KONDISI TEMPAT SAMPAH\n\nTerdeteksi tempat sampah dalam kondisi kosong.\n\n" + lokasi +
               f"- Kondisi: Kosong\n- Waktu: {t} ({tgl})")
    elif kind == "offline":
        msg = ("🔌 PERINGATAN CCTV OFFLINE\n\nKamera tidak dapat diakses oleh sistem.\n\n" + lokasi +
               f"- Waktu: {t} ({tgl})\n\nMohon admin memeriksa koneksi kamera.")
    else:
        msg = "Pesan uji dari Sistem Pemantauan Sampah Cerdas ITERA. Integrasi WhatsApp berfungsi."
    if confirm_url:
        msg += f"\n\nSetelah ditangani, konfirmasi di:\n{confirm_url}"
    return msg + "\n\nSistem Pemantauan Sampah Cerdas ITERA."


def wa_send(phone, text):
    """Kirim pesan. Return (status, provider_message_id, error)."""
    provider = sget("wa_provider")
    to = normalize_phone(phone)
    try:
        if provider == "cloud":
            token, pnid = sget("wa_cloud_token"), sget("wa_phone_number_id")
            if not token or not pnid:
                return "failed", None, "Token / Phone Number ID WhatsApp Cloud API belum diisi"
            r = requests.post(
                f"https://graph.facebook.com/{sget('wa_api_version')}/{pnid}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"messaging_product": "whatsapp", "to": to, "type": "text",
                      "text": {"preview_url": True, "body": text}}, timeout=15)
            data = r.json() if r.content else {}
            if r.ok and data.get("messages"):
                return "sent", data["messages"][0].get("id"), None
            return "failed", None, (data.get("error") or {}).get("message", f"HTTP {r.status_code}")
        if provider == "fonnte":
            token = sget("fonnte_token")
            if not token:
                return "failed", None, "Token Fonnte belum diisi"
            r = requests.post("https://api.fonnte.com/send", headers={"Authorization": token},
                              data={"target": to, "message": text, "countryCode": "62"}, timeout=15)
            data = r.json() if r.content else {}
            if r.ok and data.get("status"):
                mid = data.get("id")
                return "sent", str(mid[0] if isinstance(mid, list) and mid else mid or ""), None
            return "failed", None, data.get("reason", f"HTTP {r.status_code}")
        # simulasi: tidak benar-benar mengirim, dicatat sebagai terkirim
        return "sent", "sim-" + secrets.token_hex(6), None
    except requests.RequestException as e:
        return "failed", None, f"Koneksi ke API gagal: {e.__class__.__name__}"


def notify(kind, detection_id=None, fullness=None, force=False, only_recipient=None):
    """Kirim notifikasi ke semua penerima aktif. Dipanggil dari worker (thread)."""
    if not force:
        if not sbool("notify_enabled"):
            return 0
        flag = {"penuh": "notify_full", "berserakan": "notify_scattered",
                "kosong": "notify_empty", "offline": "notify_offline"}.get(kind)
        if flag and not sbool(flag):
            return 0
    con = db_connect()
    cam = get_camera(con)
    q = "SELECT * FROM notification_recipients WHERE is_active=1"
    args = ()
    if only_recipient:
        q, args = "SELECT * FROM notification_recipients WHERE id=?", (only_recipient,)
    recips = con.execute(q, args).fetchall()
    token_row = None
    if detection_id:
        token_row = con.execute("SELECT token FROM cleaning_actions WHERE detection_id=?", (detection_id,)).fetchone()
    confirm_url = f"{sget('base_url').rstrip('/')}/konfirmasi/{token_row['token']}" if token_row else None
    text = build_message(kind, cam, fullness, datetime.now(), confirm_url)
    n = 0
    for rc in recips:
        status, mid, err = wa_send(rc["phone_number"], text)
        con.execute("INSERT INTO notification_logs(detection_id,recipient_id,message,delivery_status,"
                    "provider_message_id,error,sent_at) VALUES(?,?,?,?,?,?,?)",
                    (detection_id, rc["id"], text, status, mid, err, now_str()))
        if token_row and status == "sent":
            con.execute("UPDATE cleaning_actions SET assigned_to=COALESCE(assigned_to,?) WHERE detection_id=?",
                        (rc["id"], detection_id))
        n += 1
    con.commit()
    con.close()
    return n


# ---------------------------------------------------------------- AI detection
class Detector:
    """Membungkus model YOLO (ultralytics). Jika model tidak tersedia -> None (pakai simulasi)."""

    def __init__(self):
        self.model = None
        self.path = None
        self.error = None

    def ensure(self):
        path = os.path.join(BASE_DIR, sget("model_path"))
        if self.path == path:
            return self.model
        self.path, self.model, self.error = path, None, None
        if not os.path.exists(path):
            self.error = "File model tidak ditemukan, sistem memakai mode simulasi"
            return None
        try:
            from ultralytics import YOLO  # noqa
            self.model = YOLO(path)
        except Exception as e:  # pragma: no cover
            self.error = f"Gagal memuat model: {e}"
        return self.model

    @staticmethod
    def _cls_kind(name):
        n = name.lower()
        if any(k in n for k in ("berserakan", "scatter", "litter", "luar", "outside")):
            return "berserakan"
        if any(k in n for k in ("penuh", "full", "overflow")):
            return "penuh"
        if any(k in n for k in ("kosong", "empty")):
            return "kosong"
        if any(k in n for k in ("sebagian", "half", "partial", "bin", "tong", "tempat")):
            return "sebagian"
        return None

    def is_bak_model(self):
        """Model hasil notebook (YOLOv8-seg) dengan kelas box_sampah & sampah_plastik."""
        return self.model is not None and {"box_sampah", "sampah_plastik"} <= set(self.model.names.values())

    def infer(self, frame):
        """Return (boxes, fullness). box: {kind, conf, box:[x1,y1,x2,y2] (0-1)}."""
        if self.is_bak_model():
            return self._infer_bak(frame)
        conf_th = sget("confidence_threshold", float)
        res = self.model.predict(frame, conf=conf_th, verbose=False)[0]
        h, w = frame.shape[:2]
        out = []
        names = res.names
        for b in res.boxes:
            kind = self._cls_kind(names[int(b.cls)])
            if not kind:
                continue
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            out.append({"kind": kind, "conf": float(b.conf), "box": [x1 / w, y1 / h, x2 / w, y2 / h]})
        fullness, _ = estimate_fullness(out)
        return out, fullness

    def _infer_bak(self, frame):
        """Sama dengan notebook: mask bak (convex hull) -> plastik di dalam/luar -> level = % bak tertutup plastik."""
        conf_th = sget("confidence_threshold", float)
        res = self.model.predict(frame, conf=conf_th, iou=0.5, imgsz=640, retina_masks=True, verbose=False)[0]
        h, w = frame.shape[:2]
        names = res.names
        bak, plastik = None, []
        for i, b in enumerate(res.boxes):
            kelas, conf = names[int(b.cls)], float(b.conf)
            xyxy = [float(v) for v in b.xyxy[0].tolist()]
            if kelas == "box_sampah" and res.masks is not None:
                luas = (xyxy[2] - xyxy[0]) * (xyxy[3] - xyxy[1])
                if bak is None or conf * luas > bak[1] * bak[3]:
                    bak = (i, conf, xyxy, luas)
            elif kelas == "sampah_plastik":
                plastik.append({"conf": conf, "xyxy": xyxy})
        if bak is None:  # tanpa bak, level tidak bisa dihitung
            return [], None
        import cv2 as _cv2  # tersedia karena ultralytics memerlukannya
        m = (res.masks.data[bak[0]].cpu().numpy() > 0.5).astype(np.uint8)
        if m.shape != (h, w):
            m = _cv2.resize(m, (w, h), interpolation=_cv2.INTER_NEAREST)
        kontur, _ = _cv2.findContours(m, _cv2.RETR_EXTERNAL, _cv2.CHAIN_APPROX_SIMPLE)
        if not kontur:
            return [], None
        hull = _cv2.convexHull(max(kontur, key=_cv2.contourArea))
        mask = np.zeros((h, w), np.uint8)
        _cv2.fillPoly(mask, [hull], 1)
        tutup = np.zeros_like(mask)
        out = []
        for d in plastik:
            x1, y1, x2, y2 = (int(v) for v in d["xyxy"])
            bagian = mask[y1:y2, x1:x2]
            dalam = bagian.size and bagian.mean() >= 0.5
            if dalam:
                tutup[y1:y2, x1:x2] = 1
            out.append({"kind": "plastik" if dalam else "berserakan", "conf": round(d["conf"], 3),
                        "box": [x1 / w, y1 / h, x2 / w, y2 / h]})
        level = round(float((tutup & mask).sum() / max(1, mask.sum()) * 100), 1)
        full_th, empty_th = sget("full_threshold", float), sget("empty_threshold", float)
        status = "penuh" if level >= full_th else "kosong" if level <= empty_th else "sebagian"
        x1, y1, x2, y2 = bak[2]
        poly = [[round(float(px) / w, 4), round(float(py) / h, 4)] for px, py in hull.reshape(-1, 2)]
        out.insert(0, {"kind": status, "conf": round(bak[1], 3), "box": [x1 / w, y1 / h, x2 / w, y2 / h],
                       "poly": poly, "level": level, "bak": True})
        return out, level


def estimate_fullness(boxes):
    """Estimasi % kepenuhan dari kelas + confidence.
    Catatan penelitian: ganti dengan model regresi / segmentasi tinggi tumpukan bila tersedia."""
    full_th, empty_th = sget("full_threshold", float), sget("empty_threshold", float)
    bins = [b for b in boxes if b["kind"] in ("penuh", "kosong", "sebagian")]
    if not bins:
        return None, None
    b = max(bins, key=lambda x: x["conf"])
    if b["kind"] == "penuh":
        pct = full_th + (100 - full_th) * b["conf"]
    elif b["kind"] == "kosong":
        pct = empty_th * (1 - b["conf"])
    else:
        pct = empty_th + (full_th - empty_th) * 0.5
    return round(min(100, max(0, pct)), 1), b


def in_roi(box, roi):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return roi[0] <= cx <= roi[2] and roi[1] <= cy <= roi[3]


def parse_roi():
    try:
        v = [float(x) for x in sget("roi").split(",")]
        if len(v) == 4:
            return v
    except ValueError:
        pass
    return [0, 0, 1, 1]


SIM_CONDITIONS = {  # kondisi skenario -> (kepenuhan bawaan, kotak bawaan untuk foto unggahan)
    "penuh": (92, [{"kind": "penuh", "box": [0.30, 0.15, 0.70, 0.92]}]),
    "sebagian": (35, [{"kind": "sebagian", "box": [0.30, 0.15, 0.70, 0.92]}]),
    "kosong": (6, [{"kind": "kosong", "box": [0.30, 0.15, 0.70, 0.92]}]),
    "berserakan": (None, [{"kind": "berserakan", "box": [0.15, 0.62, 0.85, 0.96]}]),
    "penuh_berserakan": (94, [{"kind": "penuh", "box": [0.32, 0.10, 0.66, 0.82]},
                              {"kind": "berserakan", "box": [0.15, 0.74, 0.90, 0.97]}]),
    "offline": (None, []),
    "ai": (None, []),   # kotak & level dihitung oleh model AI (models/best.pt) dari foto
}
SIM_LABEL = {"penuh": "Penuh", "sebagian": "Terisi sebagian", "kosong": "Kosong", "berserakan": "Berserakan",
             "penuh_berserakan": "Penuh & berserakan", "offline": "CCTV offline", "ai": "Deteksi AI"}

BUILTIN_SCENARIOS = [  # (nama, file, kondisi, kepenuhan, kotak)
    # Foto bak bata ITERA: kotak & level dihitung langsung oleh model hasil training (kondisi "ai")
    ("Bak bata ITERA, kosong", "static/img/sim/bak-kosong.jpg", "ai", None, []),
    ("Bak bata ITERA, terisi sebagian", "static/img/sim/bak-sebagian.jpg", "ai", None, []),
    ("Bak terisi, plastik tercecer", "static/img/sim/bak-sebagian-berserakan.jpg", "ai", None, []),
    ("Bak terisi, banyak plastik di luar", "static/img/sim/bak-berserakan-banyak.jpg", "ai", None, []),
    ("Bak bata ITERA, penuh", "static/img/sim/bak-penuh.jpg", "ai", None, []),
    ("Bak penuh & berserakan", "static/img/sim/bak-penuh-berserakan.jpg", "ai", None, []),
    ("Penuh & berserakan, siang", "static/img/sim/penuh-siang.jpg", "penuh_berserakan", 94,
     [{"kind": "penuh", "box": [0.355, 0.20, 0.595, 0.885]},
      {"kind": "berserakan", "box": [0.345, 0.74, 0.72, 0.93]}, {"kind": "berserakan", "box": [0.765, 0.79, 0.83, 0.85]}]),
    ("Tong kosong, siang", "static/img/sim/kosong-siang.jpg", "kosong", 6,
     [{"kind": "kosong", "box": [0.30, 0.15, 0.72, 0.97]}]),
    ("Sampah berserakan di jalur pejalan kaki", "static/img/sim/berserakan-jalur.jpg", "berserakan", None,
     [{"kind": "berserakan", "box": [0.18, 0.49, 0.44, 0.67]}, {"kind": "berserakan", "box": [0.32, 0.62, 0.74, 0.90]}]),
    ("Penuh & berserakan, senja (cahaya redup)", "static/img/sim/penuh-senja.jpg", "penuh_berserakan", 88,
     [{"kind": "penuh", "box": [0.405, 0.20, 0.645, 0.885]},
      {"kind": "berserakan", "box": [0.28, 0.74, 0.655, 0.93]}, {"kind": "berserakan", "box": [0.17, 0.79, 0.235, 0.85]}]),
    ("Tong kosong, mendung", "static/img/sim/kosong-mendung.jpg", "kosong", 9,
     [{"kind": "kosong", "box": [0.28, 0.15, 0.70, 0.97]}]),
    ("Sampah berserakan, cahaya redup", "static/img/sim/berserakan-redup.jpg", "berserakan", None,
     [{"kind": "berserakan", "box": [0.56, 0.49, 0.82, 0.67]}, {"kind": "berserakan", "box": [0.26, 0.62, 0.68, 0.90]}]),
    ("CCTV terputus (uji offline)", "static/img/sim/offline.jpg", "offline", None, []),
]


def sim_scenarios(only_enabled=False):
    con = db_connect()
    q = "SELECT * FROM sim_images" + (" WHERE is_enabled=1" if only_enabled else "") + " ORDER BY sort_order, id"
    rows = [dict(r) for r in con.execute(q).fetchall()]
    con.close()
    for r in rows:
        r["boxes"] = json.loads(r["boxes"] or "[]")
    return rows


class Simulator:
    """Mode simulasi: memutar daftar foto skenario (menu Simulasi) seolah-olah gambar CCTV,
    lengkap dengan hasil 'deteksi' sehingga alur alert, riwayat, dan WhatsApp bisa diuji tanpa kamera."""

    def __init__(self, detector=None):
        self.t0 = time.time()
        self.cache = None          # (id skenario, frame dasar, boxes, fullness, waktu)
        self.images = {}
        self.detector = detector
        self.ai_cache = {}         # hasil model per foto (foto diam -> cukup dideteksi sekali)
        self.ai_error = None
        self.rows = []
        self.rows_t = 0
        self.current = None
        self.next_in = None

    def restart_cycle(self):
        self.t0 = time.time()
        self.cache = None
        self.rows_t = 0

    def _rows(self):
        if time.time() - self.rows_t > 1.5:
            self.rows, self.rows_t = sim_scenarios(), time.time()
        return self.rows

    def active(self):
        rows = self._rows()
        if sget("sim_mode") == "manual":
            sel = next((r for r in rows if str(r["id"]) == sget("sim_active")), None)
            self.next_in = None
            return sel or (rows[0] if rows else None)
        playlist = [r for r in rows if r["is_enabled"]] or rows
        if not playlist:
            return None
        cycle = max(5.0, sget("sim_cycle", float))
        elapsed = time.time() - self.t0
        self.next_in = int(cycle - elapsed % cycle) + 1
        return playlist[int(elapsed // cycle) % len(playlist)]

    def _image(self, row):
        key = (row["id"], row["file_path"])
        if key not in self.images:
            im = img_read(os.path.join(BASE_DIR, row["file_path"]))
            if im is None:
                im = np.full((441, 960, 3), 40, np.uint8)
            im = pad_landscape(im)
            h, w = im.shape[:2]
            if w != 960:
                im = img_resize(im, 960, int(960 * h / w))
            self.images[key] = im
        return self.images[key]

    def _ai(self, row, base):
        """Jalankan model pada foto skenario (sekali per foto). None jika model belum tersedia."""
        key = (row["id"], row["file_path"])
        if key not in self.ai_cache:
            model = self.detector.ensure() if self.detector else None
            if model is None:
                self.ai_error = (self.detector.error if self.detector else None) or "Model AI belum tersedia"
                return None
            try:
                self.ai_cache[key] = self.detector.infer(base)
                self.ai_error = None
            except Exception as e:  # jaga agar simulasi tetap jalan
                self.ai_error = f"Deteksi AI gagal: {e}"
                return None
        return self.ai_cache[key]

    def step(self):
        """Return (frame, boxes, fullness), atau None jika skenario = CCTV offline.
        Hasil hanya berubah tiap interval deteksi agar gambar dan label selalu cocok."""
        row = self.active()
        self.current = row
        if row is None or row["condition"] == "offline":
            self.cache = None
            return None
        interval = max(0.3, sget("detection_interval", float))
        if self.cache is None or self.cache[0] != row["id"] or time.time() - self.cache[4] >= interval:
            base = self._image(row)
            if row["condition"] == "ai":
                hasil = self._ai(row, base)
                boxes, fullness = (hasil if hasil else ([], None))
                self.cache = (row["id"], base, boxes, fullness, time.time())
                _, base, boxes, fullness, _ = self.cache
                noise = np.random.normal(0, 3, base.shape).astype(np.int16)
                return np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8), boxes, fullness
            boxes = [{"kind": b["kind"], "box": b["box"],
                      "conf": round(random.uniform(0.84, 0.96) if b["kind"] != "berserakan"
                                    else random.uniform(0.74, 0.92), 2)}
                     for b in row["boxes"]]
            fullness = row["fullness"]
            if fullness is not None:
                fullness = round(max(0, min(100, fullness + random.uniform(-1.5, 1.5))), 1)
            self.cache = (row["id"], base, boxes, fullness, time.time())
        _, base, boxes, fullness, _ = self.cache
        noise = np.random.normal(0, 3, base.shape).astype(np.int16)
        frame = np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        return frame, boxes, fullness

    def status(self):
        r = self.current
        return {"mode": sget("sim_mode"), "next_in": self.next_in, "cycle": sget("sim_cycle", float),
                "ai_error": self.ai_error if r and r["condition"] == "ai" else None,
                "active": {"id": r["id"], "name": r["name"], "condition": r["condition"]} if r else None}


class Monitor:
    """Thread tunggal: membaca CCTV, menjalankan AI, menstabilkan hasil, mencatat & mengirim alert."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jpeg = None
        self.frame_size = (960, 441)
        self.online = False
        self.mode = "simulasi"
        self.boxes = []
        self.fullness = None
        self.bin_state = None       # penuh | sebagian | kosong
        self.area_state = None      # berserakan | bersih
        self.last_update = None
        self.last_confidence = None
        self.version = 0
        self.history = {"bin": [], "area": []}
        self.last_alert = {}
        self.last_sample = 0
        self.offline_logged = False
        self.detector = Detector()
        self.sim = Simulator(self.detector)
        self.cap = None
        self.cap_url = None
        self.restart_flag = False
        self.last_ingest = 0

    # ---- kamera
    def _open(self, url):
        if self.cap is not None:
            self.cap.release()
        src = int(url) if url.strip().isdigit() else url
        self.cap = cv2.VideoCapture(src)
        self.cap_url = url

    def _read_camera(self, url):
        if cv2 is None:  # kamera/RTSP/video butuh OpenCV
            return None
        if self.cap is None or self.cap_url != url or self.restart_flag or not self.cap.isOpened():
            self.restart_flag = False
            self._open(url)
        ok, frame = self.cap.read() if self.cap.isOpened() else (False, None)
        if not ok and isinstance(url, str) and os.path.isfile(url):  # file video: ulang dari awal
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        if not ok:
            self.cap.release()
            self.cap = None
            return None
        return frame

    def run(self):
        last_infer = 0
        while True:
            try:
                cam = get_camera()
                url = (cam or {}).get("stream_url", "demo").strip()
                interval = max(0.3, sget("detection_interval", float))
                if url.lower() == "eksternal":  # hasil datang dari detector_client.py via /api/detections
                    self.mode = "eksternal"
                    self._set_online(time.time() - self.last_ingest < 30, cam)
                    time.sleep(1)
                    continue
                if url.lower() in ("demo", "simulasi", ""):
                    res = self.sim.step()
                    cur = self.sim.current
                    self.mode = "simulasi + AI" if cur and cur["condition"] == "ai" and not self.sim.ai_error else "simulasi"
                    if res is None:  # skenario "CCTV offline"
                        self._set_online(False, cam)
                        time.sleep(0.5)
                        continue
                    frame, boxes, fullness = res
                    self._set_online(True, cam)
                    self._publish(frame)
                    if time.time() - last_infer >= interval:
                        last_infer = time.time()
                        self._process(cam, frame, boxes, fullness)
                    time.sleep(0.25)
                    continue
                frame = self._read_camera(url)
                if cv2 is None:
                    self.mode = "kamera (butuh OpenCV)"
                if frame is None:
                    self._set_online(False, cam)
                    time.sleep(3)
                    continue
                self._set_online(True, cam)
                self._publish(frame)
                if time.time() - last_infer >= interval:
                    last_infer = time.time()
                    model = self.detector.ensure()
                    if model is not None:
                        self.mode = "yolo"
                        boxes, fullness = self.detector.infer(frame)
                        self._process(cam, frame, boxes, fullness)
                    else:
                        self.mode = "kamera (tanpa model)"
                time.sleep(0.03)
            except Exception as e:  # jaga agar thread tidak mati
                print("[monitor] error:", e)
                time.sleep(2)

    def _publish(self, frame):
        h, w = frame.shape[:2]
        if w > 1280:
            frame = img_resize(frame, 1280, int(1280 * h / w))
            h, w = frame.shape[:2]
        jpg = img_encode(frame, 80)
        if jpg:
            with self.lock:
                self.jpeg = jpg
                self.frame_size = (w, h)

    def _set_online(self, online, cam):
        if online == self.online and (online or self.offline_logged):
            return
        self.online = online
        con = db_connect()
        con.execute("UPDATE cameras SET status=? WHERE id=?", ("online" if online else "offline", cam["id"]))
        if not online and not self.offline_logged:
            cur = con.execute("INSERT INTO detection_logs(camera_id,detection_type,detected_at) VALUES(?,?,?)",
                              (cam["id"], "offline", now_str()))
            con.commit()
            self.offline_logged = True
            con.close()
            self._alert("offline", cur.lastrowid, None)
        else:
            if online:
                self.offline_logged = False
            con.commit()
            con.close()
        with self.lock:
            if not online:
                self.boxes = []
            self.version += 1

    # ---- stabilisasi + pencatatan
    def _stable(self, key, value):
        n = max(1, sget("stable_frames", int))
        h = self.history[key]
        h.append(value)
        del h[:-n]
        return value if len(h) == n and all(x == value for x in h) else None

    def _process(self, cam, frame, boxes, fullness):
        roi = parse_roi()
        conf_th = sget("confidence_threshold", float)
        boxes = [b for b in boxes if b["conf"] >= conf_th]
        litter = [b for b in boxes if b["kind"] == "berserakan" and in_roi(b["box"], roi)]
        full_th, empty_th = sget("full_threshold", float), sget("empty_threshold", float)
        bin_now = None
        if fullness is not None:
            bin_now = "penuh" if fullness >= full_th else "kosong" if fullness <= empty_th else "sebagian"
        area_now = "berserakan" if litter else "bersih"
        stable_bin = self._stable("bin", bin_now) if bin_now else None
        stable_area = self._stable("area", area_now)
        conf = max([b["conf"] for b in boxes], default=None)
        with self.lock:
            self.boxes = boxes
            self.fullness = fullness
            self.last_confidence = conf
            self.last_update = datetime.now()
            self.version += 1
        if fullness is not None and time.time() - self.last_sample >= 30:
            self.last_sample = time.time()
            con = db_connect()
            con.execute("INSERT INTO fullness_samples(camera_id,fullness,sampled_at) VALUES(?,?,?)",
                        (cam["id"], fullness, now_str()))
            con.commit()
            con.close()
        if stable_bin and stable_bin != self.bin_state:
            prev, self.bin_state = self.bin_state, stable_bin
            if stable_bin in ("penuh", "kosong"):
                bconf = max([b["conf"] for b in boxes if b["kind"] != "berserakan"], default=conf)
                self._record(cam, stable_bin, fullness, bconf, frame, boxes)
            if prev == "penuh" and stable_bin != "penuh":
                self._auto_resolve("penuh")
        if stable_area and stable_area != self.area_state:
            prev, self.area_state = self.area_state, stable_area
            if stable_area == "berserakan":
                lconf = max([b["conf"] for b in litter], default=conf)
                self._record(cam, "berserakan", fullness, lconf, frame, boxes)
            elif prev == "berserakan":
                self._auto_resolve("berserakan")
        with self.lock:
            self.version += 1

    def _record(self, cam, kind, fullness, conf, frame, boxes):
        snap = draw_boxes(frame.copy(), boxes, parse_roi())
        fname = f"{datetime.now():%Y%m%d_%H%M%S}_{kind}.jpg"
        img_write(os.path.join(SNAP_DIR, fname), snap, 85)
        con = db_connect()
        cur = con.execute("INSERT INTO detection_logs(camera_id,detection_type,fullness_percentage,confidence_score,"
                          "snapshot_path,detected_at) VALUES(?,?,?,?,?,?)",
                          (cam["id"], kind, fullness, conf, fname, now_str()))
        det_id = cur.lastrowid
        if kind in ("penuh", "berserakan"):
            con.execute("INSERT INTO cleaning_actions(detection_id,action_status,token) VALUES(?,?,?)",
                        (det_id, "menunggu", secrets.token_urlsafe(12)))
        con.commit()
        con.close()
        self._alert(kind, det_id, fullness)

    def _alert(self, kind, det_id, fullness):
        cooldown = sget("alert_cooldown", float)
        if time.time() - self.last_alert.get(kind, 0) < cooldown:
            return
        self.last_alert[kind] = time.time()
        threading.Thread(target=notify, args=(kind, det_id, fullness), daemon=True).start()

    def _auto_resolve(self, kind):
        con = db_connect()
        con.execute("UPDATE cleaning_actions SET action_status='selesai', confirmed_at=?, "
                    "notes=COALESCE(notes,'Terverifikasi otomatis oleh sistem') "
                    "WHERE action_status='menunggu' AND detection_id IN "
                    "(SELECT id FROM detection_logs WHERE detection_type=?)", (now_str(), kind))
        con.commit()
        con.close()

    def snapshot_state(self):
        with self.lock:
            return {
                "online": self.online, "mode": self.mode, "boxes": self.boxes,
                "fullness": self.fullness, "bin_state": self.bin_state, "area_state": self.area_state,
                "confidence": self.last_confidence, "frame_size": self.frame_size, "version": self.version,
                "last_update": self.last_update.strftime("%Y-%m-%d %H:%M:%S") if self.last_update else None,
            }


def box_text(b):
    if b.get("bak"):
        return f"Bak {LABEL.get(b['kind'], b['kind']).lower()} - level {b['level']:.0f}%"
    return f"Sampah {LABEL.get(b['kind'], b['kind'])} ({b['conf'] * 100:.0f}%)"


def pad_landscape(im):
    """Foto tegak (portrait) dijadikan 4:3 mendatar dengan latar buram, agar pas di tampilan CCTV."""
    h, w = im.shape[:2]
    if w >= h:
        return im
    from PIL import ImageFilter
    W = int(h * 4 / 3)
    src = PILImage.fromarray(im[..., ::-1])
    bg = src.resize((W, h)).filter(ImageFilter.GaussianBlur(30)).point(lambda v: int(v * 0.55))
    bg.paste(src, ((W - w) // 2, 0))
    return np.asarray(bg)[..., ::-1].copy()


def _font(size=14):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow lama
        return ImageFont.load_default()


def _draw_boxes_pil(frame, boxes, roi=None):
    """Versi Pillow dari draw_boxes (dipakai jika OpenCV tidak terpasang)."""
    h, w = frame.shape[:2]
    im = PILImage.fromarray(frame[..., ::-1].copy())
    d, font = ImageDraw.Draw(im), _font()
    if roi:
        d.rectangle([int(roi[0] * w), int(roi[1] * h), int(roi[2] * w) - 1, int(roi[3] * h) - 1],
                    outline=(230, 160, 60), width=1)
    for b in boxes:
        x1, y1, x2, y2 = [int(v * (w if i % 2 == 0 else h)) for i, v in enumerate(b["box"])]
        col = (200, 40, 40) if b["kind"] in ("penuh", "berserakan") else (60, 160, 80)
        if b["kind"] == "plastik":
            d.rectangle([x1, y1, x2, y2], outline=(60, 190, 80), width=1)
            continue
        if b.get("poly"):
            d.polygon([(px * w, py * h) for px, py in b["poly"]], outline=(0, 200, 230), width=2)
        else:
            d.rectangle([x1, y1, x2, y2], outline=col, width=2)
        txt = box_text(b)
        l, t, r, bt = d.textbbox((0, 0), txt, font=font)
        top = max(0, y1 - (bt - t) - 8)
        d.rectangle([x1, top, x1 + (r - l) + 8, y1], fill=col)
        d.text((x1 + 4, top + 4 - t), txt, fill=(255, 255, 255), font=font)
    ts = datetime.now().strftime("%d/%m/%Y %H:%M:%S WIB")
    d.text((10, h - 24), ts, fill=(255, 255, 255), font=font)
    frame[:] = np.asarray(im)[..., ::-1]
    return frame


def draw_boxes(frame, boxes, roi=None):
    if cv2 is None:
        return _draw_boxes_pil(frame, boxes, roi)
    h, w = frame.shape[:2]
    if roi:
        x1, y1, x2, y2 = int(roi[0] * w), int(roi[1] * h), int(roi[2] * w) - 1, int(roi[3] * h) - 1
        cv2.rectangle(frame, (x1, y1), (x2, y2), (60, 160, 230), 1)
    for b in boxes:
        x1, y1, x2, y2 = [int(v * (w if i % 2 == 0 else h)) for i, v in enumerate(b["box"])]
        col = (40, 40, 200) if b["kind"] in ("penuh", "berserakan") else (80, 160, 60)
        if b["kind"] == "plastik":      # plastik di dalam bak: kotak tipis tanpa label
            cv2.rectangle(frame, (x1, y1), (x2, y2), (80, 190, 60), 1)
            continue
        if b.get("poly"):
            pts = np.array([[px * w, py * h] for px, py in b["poly"]], np.int32)
            cv2.polylines(frame, [pts], True, (230, 200, 0), 2)
        else:
            cv2.rectangle(frame, (x1, y1), (x2, y2), col, 2)
        txt = box_text(b)
        (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(frame, (x1, max(0, y1 - th - 8)), (x1 + tw + 8, y1), col, -1)
        cv2.putText(frame, txt, (x1 + 4, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    ts = datetime.now().strftime("%d/%m/%Y %H:%M:%S WIB")
    cv2.putText(frame, ts, (10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return frame


monitor = Monitor()


# ---------------------------------------------------------------- auth
def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not session.get("uid"):
            if request.path.startswith("/api/"):
                return jsonify(error="Sesi berakhir, silakan login kembali"), 401
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def check_csrf():
    if request.method == "POST":
        tok = request.form.get("_csrf") or request.headers.get("X-CSRF")
        if not tok or tok != session.get("csrf"):
            abort(400, "Token form tidak valid, muat ulang halaman")


@app.before_request
def _before():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(16)
    if session.get("uid") and request.endpoint not in ("static", "video_feed"):
        g.user = get_db().execute("SELECT id,name,username,role FROM users WHERE id=?", (session["uid"],)).fetchone()
        if not g.user:
            session.clear()


@app.context_processor
def _ctx():
    return {"csrf": session.get("csrf"), "user": g.get("user"), "LABEL": LABEL,
            "camera": get_camera(), "S": settings()}


@app.template_filter("wib")
def fmt_wib(s, fmt="%d %b %Y %H:%M"):
    if not s:
        return "-"
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").strftime(fmt)
    except ValueError:
        return s


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("uid"):
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        check_csrf()
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM users WHERE username=? OR lower(name)=lower(?)", (u, u)).fetchone()
        if row and check_password_hash(row["password_hash"], p):
            csrf = session.get("csrf")
            session.clear()
            session.permanent = bool(request.form.get("remember"))
            session["uid"] = row["id"]
            session["csrf"] = csrf
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        error = "Username atau password salah."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    session.clear()
    flash("Anda sudah keluar.", "ok")
    return redirect(url_for("login"))


# ---------------------------------------------------------------- pages
@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html", page="dashboard")


@app.route("/cctv")
@login_required
def cctv():
    return render_template("cctv.html", page="cctv")


def history_query(args, limit=None, offset=0):
    where, params = ["1=1"], []
    if args.get("dari"):
        where.append("d.detected_at >= ?"); params.append(args["dari"] + " 00:00:00")
    if args.get("sampai"):
        where.append("d.detected_at <= ?"); params.append(args["sampai"] + " 23:59:59")
    if args.get("kategori"):
        where.append("d.detection_type = ?"); params.append(args["kategori"])
    if args.get("status"):
        where.append("COALESCE(c.action_status,'-') = ?"); params.append(args["status"])
    if args.get("q"):
        q = f"%{args['q']}%"
        where.append("(d.detection_type LIKE ? OR d.detected_at LIKE ? OR cam.location LIKE ? OR c.notes LIKE ?)")
        params += [q, q, q, q]
    base = (" FROM detection_logs d JOIN cameras cam ON cam.id=d.camera_id "
            "LEFT JOIN cleaning_actions c ON c.detection_id=d.id WHERE " + " AND ".join(where))
    sql = ("SELECT d.*, cam.camera_name, cam.location, c.action_status, c.confirmed_at, c.notes, c.id AS action_id, "
           "(SELECT delivery_status FROM notification_logs n WHERE n.detection_id=d.id ORDER BY n.id DESC LIMIT 1) AS wa_status"
           + base + " ORDER BY d.detected_at DESC, d.id DESC")
    if limit:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    con = get_db()
    total = con.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
    return con.execute(sql, params).fetchall(), total


@app.route("/riwayat")
@login_required
def riwayat():
    page_no = max(1, request.args.get("hal", 1, type=int))
    per = 15
    rows, total = history_query(request.args, per, (page_no - 1) * per)
    return render_template("riwayat.html", page="riwayat", rows=rows, total=total, hal=page_no,
                           pages=max(1, -(-total // per)), args=request.args)


@app.route("/riwayat/<int:det_id>")
@login_required
def riwayat_detail(det_id):
    con = get_db()
    d = con.execute("SELECT d.*, cam.camera_name, cam.location FROM detection_logs d JOIN cameras cam ON cam.id=d.camera_id "
                    "WHERE d.id=?", (det_id,)).fetchone()
    if not d:
        abort(404)
    action = con.execute("SELECT c.*, r.recipient_name FROM cleaning_actions c LEFT JOIN notification_recipients r "
                         "ON r.id=c.assigned_to WHERE detection_id=?", (det_id,)).fetchone()
    notifs = con.execute("SELECT n.*, r.recipient_name, r.phone_number FROM notification_logs n LEFT JOIN "
                         "notification_recipients r ON r.id=n.recipient_id WHERE detection_id=? ORDER BY n.id",
                         (det_id,)).fetchall()
    return render_template("riwayat_detail.html", page="riwayat", d=d, action=action, notifs=notifs)


@app.route("/riwayat/<int:det_id>/tangani", methods=["POST"])
@login_required
def riwayat_tangani(det_id):
    check_csrf()
    con = get_db()
    con.execute("UPDATE cleaning_actions SET action_status='selesai', confirmed_at=?, notes=? WHERE detection_id=?",
                (now_str(), request.form.get("notes") or f"Ditandai selesai oleh {g.user['name']}", det_id))
    con.commit()
    flash("Kejadian ditandai sudah ditangani.", "ok")
    return redirect(request.referrer or url_for("riwayat"))


@app.route("/snapshot/<path:name>")
@login_required
def snapshot(name):
    path = os.path.join(SNAP_DIR, os.path.basename(name))
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg")


@app.route("/notifikasi", methods=["GET", "POST"])
@login_required
def notifikasi():
    con = get_db()
    if request.method == "POST":
        check_csrf()
        act = request.form.get("action")
        if act == "add":
            name, phone = request.form.get("name", "").strip(), request.form.get("phone", "").strip()
            if not name or len(normalize_phone(phone)) < 9:
                flash("Isi nama dan nomor WhatsApp yang valid (contoh 0812xxxxxxx).", "err")
            else:
                con.execute("INSERT INTO notification_recipients(recipient_name,phone_number,is_active,created_at) "
                            "VALUES(?,?,1,?)", (name, phone, now_str()))
                flash(f"{name} ditambahkan sebagai penerima.", "ok")
        elif act == "edit":
            con.execute("UPDATE notification_recipients SET recipient_name=?, phone_number=? WHERE id=?",
                        (request.form["name"].strip(), request.form["phone"].strip(), request.form["id"]))
            flash("Data penerima disimpan.", "ok")
        elif act == "toggle":
            con.execute("UPDATE notification_recipients SET is_active=1-is_active WHERE id=?", (request.form["id"],))
        elif act == "delete":
            con.execute("DELETE FROM notification_recipients WHERE id=?", (request.form["id"],))
            flash("Penerima dihapus.", "ok")
        elif act == "test":
            con.commit()
            n = notify("test", force=True, only_recipient=request.form.get("id") or None)
            flash(f"Pesan uji dikirim ke {n} penerima. Lihat status di riwayat pengiriman." if n
                  else "Belum ada penerima aktif untuk dikirimi pesan uji.", "ok" if n else "err")
        elif act == "prefs":
            save_settings({k: "1" if request.form.get(k) else "0" for k in
                           ("notify_enabled", "notify_full", "notify_scattered", "notify_empty", "notify_offline")})
            flash("Aturan notifikasi disimpan.", "ok")
        con.commit()
        return redirect(url_for("notifikasi"))
    recips = con.execute("SELECT r.*, (SELECT MAX(sent_at) FROM notification_logs n WHERE n.recipient_id=r.id) AS last_sent "
                         "FROM notification_recipients r ORDER BY r.id").fetchall()
    logs = con.execute("SELECT n.*, r.recipient_name, d.detection_type FROM notification_logs n LEFT JOIN "
                       "notification_recipients r ON r.id=n.recipient_id LEFT JOIN detection_logs d ON d.id=n.detection_id "
                       "ORDER BY n.id DESC LIMIT 50").fetchall()
    stats = con.execute("SELECT delivery_status, COUNT(*) c FROM notification_logs GROUP BY delivery_status").fetchall()
    return render_template("notifikasi.html", page="notifikasi", recips=recips, logs=logs,
                           stats={r["delivery_status"]: r["c"] for r in stats})


def report_range(args):
    periode = args.get("periode", "mingguan")
    today = datetime.now().date()
    if periode == "harian":
        start, end = today, today
    elif periode == "bulanan":
        start, end = today.replace(day=1), today
    elif periode == "kustom" and args.get("dari") and args.get("sampai"):
        start = datetime.strptime(args["dari"], "%Y-%m-%d").date()
        end = datetime.strptime(args["sampai"], "%Y-%m-%d").date()
    else:
        periode, start, end = "mingguan", today - timedelta(days=6), today
    return periode, start, end


def report_data(start, end):
    con = get_db()
    s, e = f"{start} 00:00:00", f"{end} 23:59:59"
    counts = {r["detection_type"]: r["c"] for r in con.execute(
        "SELECT detection_type, COUNT(*) c FROM detection_logs WHERE detected_at BETWEEN ? AND ? GROUP BY 1", (s, e))}
    per_day = con.execute(
        "SELECT substr(detected_at,1,10) d, detection_type t, COUNT(*) c FROM detection_logs "
        "WHERE detected_at BETWEEN ? AND ? GROUP BY 1,2", (s, e)).fetchall()
    per_hour = con.execute(
        "SELECT CAST(substr(detected_at,12,2) AS INTEGER) h, COUNT(*) c FROM detection_logs "
        "WHERE detected_at BETWEEN ? AND ? AND detection_type IN ('penuh','berserakan') GROUP BY 1", (s, e)).fetchall()
    fill = con.execute(
        "SELECT substr(sampled_at,1,10) d, ROUND(AVG(fullness),1) avg, ROUND(MAX(fullness),1) mx FROM fullness_samples "
        "WHERE sampled_at BETWEEN ? AND ? GROUP BY 1 ORDER BY 1", (s, e)).fetchall()
    notif = {r["delivery_status"]: r["c"] for r in con.execute(
        "SELECT delivery_status, COUNT(*) c FROM notification_logs WHERE sent_at BETWEEN ? AND ? GROUP BY 1", (s, e))}
    handled = con.execute(
        "SELECT c.action_status, COUNT(*) n FROM cleaning_actions c JOIN detection_logs d ON d.id=c.detection_id "
        "WHERE d.detected_at BETWEEN ? AND ? GROUP BY 1", (s, e)).fetchall()
    handled = {r["action_status"]: r["n"] for r in handled}
    resp = con.execute(
        "SELECT AVG((julianday(c.confirmed_at)-julianday(d.detected_at))*1440) FROM cleaning_actions c "
        "JOIN detection_logs d ON d.id=c.detection_id WHERE c.confirmed_at IS NOT NULL AND d.detected_at BETWEEN ? AND ?",
        (s, e)).fetchone()[0]
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    series = {t: [0] * len(days) for t in ("penuh", "berserakan", "kosong")}
    for r in per_day:
        if r["t"] in series and r["d"] in days:
            series[r["t"]][days.index(r["d"])] = r["c"]
    fill_map = {r["d"]: r for r in fill}
    hours = [0] * 24
    for r in per_hour:
        hours[r["h"]] = r["c"]
    return {
        "counts": {k: counts.get(k, 0) for k in ("penuh", "kosong", "berserakan", "offline")},
        "days": days, "series": series,
        "fill_avg": [fill_map[d]["avg"] if d in fill_map else None for d in days],
        "fill_max": [fill_map[d]["mx"] if d in fill_map else None for d in days],
        "hours": hours,
        "notif": {"sent": notif.get("sent", 0) + notif.get("delivered", 0) + notif.get("read", 0),
                  "failed": notif.get("failed", 0)},
        "handled": {"selesai": handled.get("selesai", 0), "menunggu": handled.get("menunggu", 0)},
        "avg_response_min": round(resp, 1) if resp else None,
    }


@app.route("/laporan")
@login_required
def laporan():
    periode, start, end = report_range(request.args)
    return render_template("laporan.html", page="laporan", periode=periode, start=start, end=end,
                           data=report_data(start, end))


@app.route("/laporan/export.<fmt>")
@login_required
def laporan_export(fmt):
    periode, start, end = report_range(request.args)
    data = report_data(start, end)
    rows, _ = history_query({"dari": str(start), "sampai": str(end)})
    fname = f"laporan-sampah-itera_{start}_{end}"
    if fmt == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        wb = Workbook()
        ws = wb.active
        ws.title = "Ringkasan"
        ws.append(["Laporan Sistem Pemantauan Sampah Cerdas ITERA"])
        ws["A1"].font = Font(bold=True, size=14)
        ws.append([f"Periode {periode}: {start:%d/%m/%Y} – {end:%d/%m/%Y}"])
        ws.append([])
        for k, v in [("Deteksi sampah penuh", data["counts"]["penuh"]),
                     ("Deteksi sampah kosong", data["counts"]["kosong"]),
                     ("Deteksi sampah berserakan", data["counts"]["berserakan"]),
                     ("CCTV offline", data["counts"]["offline"]),
                     ("Notifikasi terkirim", data["notif"]["sent"]), ("Notifikasi gagal", data["notif"]["failed"]),
                     ("Kejadian sudah ditangani", data["handled"]["selesai"]),
                     ("Kejadian menunggu", data["handled"]["menunggu"]),
                     ("Rata-rata waktu respons (menit)", data["avg_response_min"] or "-")]:
            ws.append([k, v])
        ws.column_dimensions["A"].width = 34
        ws2 = wb.create_sheet("Harian")
        ws2.append(["Tanggal", "Penuh", "Berserakan", "Kosong", "Rata-rata kepenuhan (%)", "Maks kepenuhan (%)"])
        for i, d in enumerate(data["days"]):
            ws2.append([d, data["series"]["penuh"][i], data["series"]["berserakan"][i], data["series"]["kosong"][i],
                        data["fill_avg"][i], data["fill_max"][i]])
        ws3 = wb.create_sheet("Riwayat Deteksi")
        ws3.append(["Waktu", "Kondisi", "Kepenuhan (%)", "Confidence", "Lokasi", "CCTV", "Penanganan", "Status WA"])
        for r in rows:
            ws3.append([r["detected_at"], LABEL.get(r["detection_type"], r["detection_type"]), r["fullness_percentage"],
                        round(r["confidence_score"] * 100, 1) if r["confidence_score"] else None, r["location"],
                        r["camera_name"], r["action_status"] or "-", r["wa_status"] or "-"])
        for w in (ws2, ws3):
            for c in w[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="A3161E")
            for col in "ABCDEFGH":
                w.column_dimensions[col].width = 20
        bio = io.BytesIO()
        wb.save(bio)
        bio.seek(0)
        return send_file(bio, as_attachment=True, download_name=fname + ".xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if fmt == "pdf":
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        bio = io.BytesIO()
        doc = SimpleDocTemplate(bio, pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm, topMargin=1.5 * cm,
                                title="Laporan Pemantauan Sampah ITERA")
        st = getSampleStyleSheet()
        red = colors.HexColor("#A3161E")
        el = []
        logo = os.path.join(BASE_DIR, "static", "img", "logo-mark.png")
        head = Table([[Image(logo, 1.3 * cm, 1.3 * cm),
                       Paragraph("<b>Laporan Pemantauan Sampah Cerdas</b><br/>Institut Teknologi Sumatera", st["Title"].clone("t", fontSize=14, alignment=0, leading=18))]],
                     colWidths=[1.8 * cm, None])
        el += [head, Spacer(1, 6),
               Paragraph(f"Periode {periode}: {start:%d/%m/%Y} – {end:%d/%m/%Y} &nbsp;|&nbsp; Dicetak {datetime.now():%d/%m/%Y %H:%M} WIB", st["Normal"]),
               Spacer(1, 12)]
        summary = [["Indikator", "Jumlah"],
                   ["Deteksi sampah penuh", data["counts"]["penuh"]],
                   ["Deteksi sampah berserakan", data["counts"]["berserakan"]],
                   ["Deteksi sampah kosong", data["counts"]["kosong"]],
                   ["CCTV offline", data["counts"]["offline"]],
                   ["Notifikasi WhatsApp terkirim", data["notif"]["sent"]],
                   ["Notifikasi WhatsApp gagal", data["notif"]["failed"]],
                   ["Kejadian sudah ditangani", data["handled"]["selesai"]],
                   ["Kejadian menunggu penanganan", data["handled"]["menunggu"]],
                   ["Rata-rata waktu respons", f"{data['avg_response_min']} menit" if data["avg_response_min"] else "-"]]
        ts = TableStyle([("BACKGROUND", (0, 0), (-1, 0), red), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                         ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
                         ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9D4CC")),
                         ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F5F2")]),
                         ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4)])
        el += [Paragraph("<b>Ringkasan</b>", st["Heading3"]), Table(summary, colWidths=[9 * cm, 4 * cm], style=ts),
               Spacer(1, 12), Paragraph("<b>Rekap harian</b>", st["Heading3"])]
        daily = [["Tanggal", "Penuh", "Berserakan", "Kosong", "Rata-rata %", "Maks %"]]
        for i, d in enumerate(data["days"]):
            daily.append([d, data["series"]["penuh"][i], data["series"]["berserakan"][i], data["series"]["kosong"][i],
                          data["fill_avg"][i] if data["fill_avg"][i] is not None else "-",
                          data["fill_max"][i] if data["fill_max"][i] is not None else "-"])
        el += [Table(daily, repeatRows=1, style=ts), Spacer(1, 12), Paragraph("<b>Riwayat deteksi</b>", st["Heading3"])]
        det = [["Waktu", "Kondisi", "Kepenuhan", "Conf.", "Penanganan", "WA"]]
        for r in rows[:300]:
            det.append([r["detected_at"], LABEL.get(r["detection_type"], r["detection_type"]),
                        f"{r['fullness_percentage']:.0f}%" if r["fullness_percentage"] is not None else "-",
                        f"{r['confidence_score'] * 100:.0f}%" if r["confidence_score"] else "-",
                        r["action_status"] or "-", r["wa_status"] or "-"])
        el.append(Table(det, repeatRows=1, style=ts))
        doc.build(el)
        bio.seek(0)
        return send_file(bio, as_attachment=True, download_name=fname + ".pdf", mimetype="application/pdf")
    abort(404)


@app.route("/pengaturan", methods=["GET", "POST"])
@login_required
def pengaturan():
    con = get_db()
    if request.method == "POST":
        check_csrf()
        sec = request.form.get("section")
        if sec == "kamera":
            con.execute("UPDATE cameras SET camera_name=?, location=?, stream_url=? WHERE id=?",
                        (request.form["camera_name"].strip(), request.form["location"].strip(),
                         request.form["stream_url"].strip() or "demo", get_camera(con)["id"]))
            con.commit()
            monitor.restart_flag = True
            flash("Pengaturan kamera disimpan. Sistem menyambung ulang ke CCTV.", "ok")
        elif sec == "ai":
            vals = {}
            try:
                vals["confidence_threshold"] = f"{min(0.99, max(0.05, float(request.form['confidence_threshold']))):.2f}"
                full, empty = float(request.form["full_threshold"]), float(request.form["empty_threshold"])
                if not 0 <= empty < full <= 100:
                    raise ValueError
                vals.update(full_threshold=f"{full:g}", empty_threshold=f"{empty:g}")
                vals["detection_interval"] = f"{max(0.5, float(request.form['detection_interval'])):g}"
                vals["alert_cooldown"] = str(max(0, int(float(request.form["alert_cooldown"]) * 60)))
                vals["stable_frames"] = str(max(1, min(20, int(request.form["stable_frames"]))))
                roi = [min(1, max(0, float(request.form[f"roi_{k}"]) / 100)) for k in ("x1", "y1", "x2", "y2")]
                if roi[0] >= roi[2] or roi[1] >= roi[3]:
                    raise ValueError
                vals["roi"] = ",".join(f"{v:.3f}" for v in roi)
                vals["model_path"] = request.form.get("model_path", "models/best.pt").strip()
            except (ValueError, KeyError):
                flash("Nilai parameter tidak valid. Ambang kosong harus lebih kecil dari ambang penuh, "
                      "dan ROI kiri/atas harus lebih kecil dari kanan/bawah.", "err")
                return redirect(url_for("pengaturan") + "#ai")
            save_settings(vals)
            flash("Parameter deteksi disimpan dan langsung dipakai.", "ok")
        elif sec == "whatsapp":
            save_settings({k: request.form.get(k, "").strip() for k in
                           ("wa_provider", "wa_cloud_token", "wa_phone_number_id", "wa_api_version",
                            "wa_verify_token", "fonnte_token", "base_url")})
            flash("Konfigurasi WhatsApp disimpan.", "ok")
        elif sec == "akun":
            u = con.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()
            if not check_password_hash(u["password_hash"], request.form.get("old", "")):
                flash("Password lama tidak cocok.", "err")
            elif len(request.form.get("new", "")) < 6:
                flash("Password baru minimal 6 karakter.", "err")
            else:
                con.execute("UPDATE users SET password_hash=?, name=? WHERE id=?",
                            (generate_password_hash(request.form["new"]),
                             request.form.get("name", u["name"]).strip() or u["name"], u["id"]))
                con.commit()
                flash("Akun diperbarui.", "ok")
        elif sec == "apikey":
            save_settings({"ingest_api_key": secrets.token_urlsafe(24)})
            flash("API key baru dibuat. Perbarui juga di skrip deteksi eksternal.", "ok")
        return redirect(url_for("pengaturan") + "#" + (sec or ""))
    return render_template("pengaturan.html", page="pengaturan", detector=monitor.detector, roi=parse_roi())


@app.route("/simulasi", methods=["GET", "POST"])
@login_required
def simulasi():
    con = get_db()
    if request.method == "POST":
        check_csrf()
        act = request.form.get("action")
        sid = request.form.get("id", "")
        if act == "show":
            save_settings({"sim_mode": "manual", "sim_active": sid})
            monitor.sim.restart_cycle()
            flash("Skenario ditampilkan di CCTV. Putaran otomatis dijeda.", "ok")
        elif act == "auto":
            cyc = request.form.get("sim_cycle", sget("sim_cycle"))
            try:
                cyc = str(int(max(5, min(600, float(cyc)))))
            except ValueError:
                cyc = sget("sim_cycle")
            save_settings({"sim_mode": "auto", "sim_cycle": cyc})
            monitor.sim.restart_cycle()
            flash(f"Simulasi berputar otomatis, ganti skenario tiap {cyc} detik.", "ok")
        elif act == "toggle":
            con.execute("UPDATE sim_images SET is_enabled=1-is_enabled WHERE id=?", (sid,))
        elif act == "delete":
            row = con.execute("SELECT * FROM sim_images WHERE id=? AND is_builtin=0", (sid,)).fetchone()
            if row:
                con.execute("DELETE FROM sim_images WHERE id=?", (sid,))
                fp = os.path.join(BASE_DIR, row["file_path"])
                if row["file_path"].startswith("data/sim/") and os.path.isfile(fp):
                    os.remove(fp)
                flash(f"Skenario \"{row['name']}\" dihapus.", "ok")
        elif act == "upload":
            f = request.files.get("image")
            cond = request.form.get("condition", "penuh")
            data = f.read() if f else b""
            img = img_decode(data)
            if cond not in SIM_CONDITIONS or img is None:
                flash("Pilih file gambar JPG/PNG yang valid dan kondisinya.", "err")
            else:
                h, w = img.shape[:2]
                if w > 1280:
                    img = img_resize(img, 1280, int(1280 * h / w))
                os.makedirs(os.path.join(BASE_DIR, "data", "sim"), exist_ok=True)
                rel = f"data/sim/{secrets.token_hex(8)}.jpg"
                img_write(os.path.join(BASE_DIR, rel), img, 88)
                default_full, boxes = SIM_CONDITIONS[cond]
                try:
                    full = float(request.form["fullness"]) if request.form.get("fullness") else default_full
                except ValueError:
                    full = default_full
                if cond in ("berserakan", "offline", "ai"):
                    full = None
                name = (request.form.get("name") or "").strip()[:80] or f"Foto unggahan ({SIM_LABEL[cond]})"
                con.execute("INSERT INTO sim_images(name,file_path,condition,fullness,boxes,is_enabled,is_builtin,"
                            "sort_order,created_at) VALUES(?,?,?,?,?,1,0,100,?)",
                            (name, rel, cond, full, json.dumps(boxes), now_str()))
                flash(f"\"{name}\" ditambahkan ke putaran simulasi.", "ok")
        con.commit()
        monitor.sim.rows_t = 0
        return redirect(url_for("simulasi"))
    return render_template("simulasi.html", page="simulasi", scenarios=sim_scenarios(), SIM_LABEL=SIM_LABEL,
                           sim=monitor.sim.status(), streaming_demo=get_camera()["stream_url"].lower() in ("demo", "simulasi", ""))


@app.route("/sim-img/<int:sid>")
@login_required
def sim_img(sid):
    row = get_db().execute("SELECT file_path FROM sim_images WHERE id=?", (sid,)).fetchone()
    path = os.path.join(BASE_DIR, row["file_path"]) if row else ""
    if not row or not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg", max_age=3600)


@app.route("/simulasi/aktifkan", methods=["POST"])
@login_required
def simulasi_aktifkan():
    check_csrf()
    con = get_db()
    con.execute("UPDATE cameras SET stream_url='demo' WHERE id=?", (get_camera(con)["id"],))
    con.commit()
    monitor.restart_flag = True
    flash("Sumber CCTV dialihkan ke mode simulasi.", "ok")
    return redirect(url_for("simulasi"))


@app.route("/konfirmasi/<token>", methods=["GET", "POST"])
def konfirmasi(token):
    """Halaman publik untuk petugas (link dari WhatsApp)."""
    con = get_db()
    a = con.execute("SELECT c.*, d.detection_type, d.detected_at, d.fullness_percentage, cam.location, cam.camera_name "
                    "FROM cleaning_actions c JOIN detection_logs d ON d.id=c.detection_id JOIN cameras cam ON cam.id=d.camera_id "
                    "WHERE c.token=?", (token,)).fetchone()
    if not a:
        abort(404)
    if request.method == "POST" and a["action_status"] != "selesai":
        con.execute("UPDATE cleaning_actions SET action_status='selesai', confirmed_at=?, notes=? WHERE id=?",
                    (now_str(), (request.form.get("notes") or "Dikonfirmasi petugas via WhatsApp").strip()[:300], a["id"]))
        con.commit()
        return redirect(url_for("konfirmasi", token=token))
    return render_template("konfirmasi.html", a=a)


# ---------------------------------------------------------------- API
@app.route("/video_feed")
@login_required
def video_feed():
    def gen():
        last = None
        while True:
            with monitor.lock:
                frame = monitor.jpeg
            if frame is not None and frame is not last:
                last = frame
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            time.sleep(0.1)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/frame.jpg")
@login_required
def api_frame():
    with monitor.lock:
        frame = monitor.jpeg
    if frame is None:
        abort(503)
    return Response(frame, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@app.route("/api/state")
@login_required
def api_state():
    con = get_db()
    st = monitor.snapshot_state()
    today = datetime.now().strftime("%Y-%m-%d")
    counts = {r["detection_type"]: r["c"] for r in con.execute(
        "SELECT detection_type, COUNT(*) c FROM detection_logs WHERE detected_at >= ? GROUP BY 1", (today + " 00:00:00",))}
    trend = con.execute("SELECT sampled_at, fullness FROM fullness_samples WHERE sampled_at >= ? ORDER BY sampled_at",
                        ((datetime.now() - timedelta(hours=int(request.args.get("jam", 6)))).strftime("%Y-%m-%d %H:%M:%S"),)).fetchall()
    alerts = con.execute(
        "SELECT d.id, d.detection_type, d.fullness_percentage, d.detected_at, c.action_status, "
        "(SELECT delivery_status FROM notification_logs n WHERE n.detection_id=d.id ORDER BY n.id DESC LIMIT 1) AS wa_status "
        "FROM detection_logs d LEFT JOIN cleaning_actions c ON c.detection_id=d.id ORDER BY d.id DESC LIMIT 6").fetchall()
    last_sent = con.execute("SELECT MAX(sent_at) FROM notification_logs WHERE delivery_status!='failed'").fetchone()[0]
    recips = con.execute("SELECT id, recipient_name, phone_number FROM notification_recipients WHERE is_active=1").fetchall()
    pending = con.execute("SELECT COUNT(*) FROM cleaning_actions WHERE action_status='menunggu'").fetchone()[0]
    cam = get_camera(con)
    st.update({
        "camera": {"name": cam["camera_name"], "location": cam["location"]},
        "today": {k: counts.get(k, 0) for k in ("penuh", "berserakan", "kosong")},
        "trend": [{"t": r["sampled_at"], "v": r["fullness"]} for r in trend],
        "alerts": [dict(r) for r in alerts],
        "notify_enabled": sbool("notify_enabled"),
        "last_sent": last_sent,
        "recipients": [dict(r) for r in recips],
        "pending": pending,
        "sim": monitor.sim.status() if monitor.mode.startswith("simulasi") else None,
        "thresholds": {"full": sget("full_threshold", float), "empty": sget("empty_threshold", float)},
        "roi": parse_roi(),
        "server_time": now_str(),
    })
    return jsonify(st)


@app.route("/api/notify-toggle", methods=["POST"])
@login_required
def api_notify_toggle():
    check_csrf()
    on = bool((request.get_json(silent=True) or {}).get("enabled"))
    save_settings({"notify_enabled": "1" if on else "0"})
    return jsonify(enabled=on)


@app.route("/api/detections", methods=["POST"])
def api_ingest():
    """Endpoint untuk skrip AI eksternal (mis. YOLO di PC lain / Jetson).
    Header: X-API-Key. Body JSON: {"boxes":[{"kind":"penuh","conf":0.92,"box":[x1,y1,x2,y2]}], "fullness": 92}
    (opsional "image": JPEG base64 untuk snapshot)."""
    if request.headers.get("X-API-Key") != sget("ingest_api_key"):
        return jsonify(error="API key tidak valid"), 401
    body = request.get_json(silent=True) or {}
    boxes = [b for b in body.get("boxes", []) if isinstance(b, dict) and b.get("kind") in LABEL]
    fullness = body.get("fullness")
    if fullness is None:
        fullness, _ = estimate_fullness(boxes)
    frame = None
    if body.get("image"):
        import base64
        frame = img_decode(base64.b64decode(body["image"]))
        if frame is not None:
            monitor._publish(frame)
    if frame is None:
        with monitor.lock:
            jpg = monitor.jpeg
        frame = img_decode(jpg) if jpg else None
        if frame is None:
            frame = np.zeros((360, 640, 3), np.uint8)
    monitor.mode = "eksternal"
    monitor.last_ingest = time.time()
    monitor._set_online(True, get_camera())
    monitor._process(get_camera(), frame, boxes, float(fullness) if fullness is not None else None)
    return jsonify(ok=True, state=monitor.snapshot_state())


@app.route("/webhook/whatsapp", methods=["GET", "POST"])
def wa_webhook():
    """Webhook Meta WhatsApp Cloud API untuk status delivered/read/failed."""
    if request.method == "GET":
        if request.args.get("hub.verify_token") == sget("wa_verify_token"):
            return request.args.get("hub.challenge", "")
        abort(403)
    con = get_db()
    data = request.get_json(silent=True) or {}
    for entry in data.get("entry", []):
        for ch in entry.get("changes", []):
            for s in ch.get("value", {}).get("statuses", []):
                con.execute("UPDATE notification_logs SET delivery_status=? WHERE provider_message_id=?",
                            (s.get("status"), s.get("id")))
    con.commit()
    return "ok"


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, msg="Halaman tidak ditemukan."), 404


@app.errorhandler(400)
def bad_req(e):
    return render_template("error.html", code=400, msg=getattr(e, "description", "Permintaan tidak valid.")), 400


# ---------------------------------------------------------------- CLI: data contoh
@app.cli.command("seed-demo")
def seed_demo():
    """Isi data contoh 14 hari terakhir (untuk mencoba laporan). flask --app app seed-demo"""
    con = db_connect()
    cam = get_camera(con)
    if not con.execute("SELECT 1 FROM notification_recipients").fetchone():
        con.execute("INSERT INTO notification_recipients(recipient_name,phone_number,is_active,created_at) VALUES(?,?,1,?)",
                    ("Petugas Kebersihan", "+62 812-3456-7890", now_str()))
    rid = con.execute("SELECT id FROM notification_recipients LIMIT 1").fetchone()[0]
    for day in range(14, 0, -1):
        base = datetime.now().replace(hour=7, minute=0, second=0) - timedelta(days=day)
        level = 5.0
        for step in range(0, 11 * 12):
            t = base + timedelta(minutes=5 * step)
            level = min(100, level + random.uniform(0, 1.6))
            if level >= 97 and random.random() < 0.3:
                level = 4
            con.execute("INSERT INTO fullness_samples(camera_id,fullness,sampled_at) VALUES(?,?,?)",
                        (cam["id"], round(level, 1), t.strftime("%Y-%m-%d %H:%M:%S")))
        for kind, n in (("penuh", random.randint(1, 4)), ("berserakan", random.randint(0, 3)), ("kosong", random.randint(2, 6))):
            for _ in range(n):
                t = base + timedelta(minutes=random.randint(30, 600))
                cur = con.execute("INSERT INTO detection_logs(camera_id,detection_type,fullness_percentage,confidence_score,"
                                  "detected_at) VALUES(?,?,?,?,?)",
                                  (cam["id"], kind, random.uniform(82, 99) if kind == "penuh" else random.uniform(2, 12)
                                   if kind == "kosong" else random.uniform(40, 90), random.uniform(0.72, 0.96),
                                   t.strftime("%Y-%m-%d %H:%M:%S")))
                if kind != "kosong":
                    done = random.random() < 0.85
                    con.execute("INSERT INTO cleaning_actions(detection_id,assigned_to,action_status,token,confirmed_at,notes) "
                                "VALUES(?,?,?,?,?,?)", (cur.lastrowid, rid, "selesai" if done else "menunggu",
                                                        secrets.token_urlsafe(12),
                                                        (t + timedelta(minutes=random.randint(10, 90))).strftime("%Y-%m-%d %H:%M:%S") if done else None,
                                                        "Data contoh" if done else None))
                    con.execute("INSERT INTO notification_logs(detection_id,recipient_id,message,delivery_status,"
                                "provider_message_id,sent_at) VALUES(?,?,?,?,?,?)",
                                (cur.lastrowid, rid, build_message(kind, cam, 90, t), random.choice(["sent", "delivered", "delivered", "failed"]),
                                 "sim", t.strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()
    con.close()
    print("Data contoh 14 hari berhasil ditambahkan.")


init_db()

if __name__ == "__main__":
    threading.Thread(target=monitor.run, daemon=True, name="monitor").start()
    print("Dashboard: http://localhost:5000  (login awal admin / admin123)")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True, use_reloader=False)
