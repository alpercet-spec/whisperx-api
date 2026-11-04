import hashlib
import hmac
import os
import secrets
import subprocess
import tempfile
import threading
import time

import whisperx
from whisperx.diarize import DiarizationPipeline
from fastapi import Cookie, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

HF_TOKEN = os.environ["HF_TOKEN"]
API_KEY = os.environ.get("API_KEY", "")
PANEL_USER = os.environ.get("PANEL_USER", "admin")
PANEL_PASS = os.environ.get("PANEL_PASS", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))
MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
DEVICE = "cpu"
COMPUTE = os.environ.get("COMPUTE_TYPE", "int8")
THREADS = int(os.environ.get("THREADS", "6"))
DEFAULT_LANG = os.environ.get("LANGUAGE", "tr")

app = FastAPI(title="WhisperX API")
lock = threading.Lock()

print("Model yükleniyor...", flush=True)
asr_model = whisperx.load_model(MODEL_NAME, DEVICE, compute_type=COMPUTE, threads=THREADS)
align_model, align_meta = whisperx.load_align_model(language_code=DEFAULT_LANG, device=DEVICE)
DIARIZE_MODEL = os.environ.get("DIARIZE_MODEL", "pyannote/speaker-diarization-community-1")
try:
    diarize_model = DiarizationPipeline(model_name=DIARIZE_MODEL, token=HF_TOKEN, device=DEVICE)
except TypeError:
    diarize_model = DiarizationPipeline(model_name=DIARIZE_MODEL, use_auth_token=HF_TOKEN, device=DEVICE)
print("Hazır.", flush=True)


def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    exp = str(int(time.time()) + SESSION_HOURS * 3600)
    return f"{exp}.{_sign(exp)}"


def session_ok(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    return hmac.compare_digest(sig, _sign(exp)) and int(exp) > time.time()


def check_key(authorization: str | None, session: str | None = None):
    if API_KEY and authorization == f"Bearer {API_KEY}":
        return
    if PANEL_PASS and session_ok(session):
        return
    raise HTTPException(401, "Yetkisiz")


LOGIN_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Giriş</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:15px system-ui,sans-serif;background:#f4f5f7;color:#1a1d21}
@media(prefers-color-scheme:dark){body{background:#0f1115;color:#e7e9ee}}
form{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px;width:320px;display:grid;gap:12px}
@media(prefers-color-scheme:dark){form{background:#171a21;border-color:#2a2f3a}}
h1{font-size:18px;margin:0 0 6px}input{padding:10px;border:1px solid #cbd5e1;border-radius:8px;font-size:15px;background:transparent;color:inherit}
button{padding:10px;border:0;border-radius:8px;background:#0f766e;color:#fff;font-weight:600;cursor:pointer}.err{color:#dc2626;font-size:13px}</style></head>
<body><form method="post" action="/login"><h1>WhisperX Panel</h1>
<input name="username" placeholder="Kullanıcı adı" autocomplete="username" required>
<input name="password" type="password" placeholder="Şifre" autocomplete="current-password" required>
<button>Giriş yap</button>__ERR__</form></body></html>"""


@app.get("/", response_class=HTMLResponse)
def panel(wx_session: str | None = Cookie(None)):
    if not PANEL_PASS:
        return "<p style='font-family:sans-serif;padding:20px'>PANEL_PASS ortam değişkeni ayarlanmadı; panel kapalı.</p>"
    if not session_ok(wx_session):
        return LOGIN_HTML.replace("__ERR__", "")
    with open(os.path.join(os.path.dirname(__file__), "panel.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return LOGIN_HTML.replace("__ERR__", "")


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    time.sleep(0.5)
    if PANEL_PASS and hmac.compare_digest(username, PANEL_USER) and hmac.compare_digest(password, PANEL_PASS):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("wx_session", make_session(), httponly=True, samesite="lax", secure=True, max_age=SESSION_HOURS * 3600)
        return resp
    return HTMLResponse(LOGIN_HTML.replace("__ERR__", "<div class='err'>Kullanıcı adı veya şifre hatalı.</div>"), status_code=401)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("wx_session")
    return resp


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME}



def transcribe_path(raw_path: str, language: str = DEFAULT_LANG, min_speakers=None, max_speakers=None):
    """Ses dosyasını (herhangi format) ön işleyip konuşmacı ayrımlı segmentlere çevirir."""
    path = raw_path + ".wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", raw_path, "-ac", "1", "-ar", "16000",
             "-af", "highpass=f=200,lowpass=f=3400,loudnorm=I=-16:TP=-1.5:LRA=11", path],
            check=True,
        )
        with lock:
            audio = whisperx.load_audio(path)
            result = asr_model.transcribe(audio, language=language, batch_size=8)
            if language == DEFAULT_LANG:
                am, meta = align_model, align_meta
            else:
                am, meta = whisperx.load_align_model(language_code=language, device=DEVICE)
            result = whisperx.align(result["segments"], am, meta, audio, DEVICE)
            diar = diarize_model(audio, min_speakers=min_speakers, max_speakers=max_speakers)
            result = whisperx.assign_word_speakers(diar, result)
    finally:
        if os.path.exists(path):
            os.remove(path)
    return [{"start": round(x["start"], 2), "end": round(x["end"], 2),
             "speaker": x.get("speaker", "UNKNOWN"), "text": x["text"].strip()} for x in result["segments"]]


@app.post("/transcribe")
def transcribe(
    file: UploadFile = File(...),
    language: str = Form(DEFAULT_LANG),
    min_speakers: int | None = Form(None),
    max_speakers: int | None = Form(None),
    authorization: str | None = Header(None),
    wx_session: str | None = Cookie(None),
):
    check_key(authorization, wx_session)

    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename or "a.wav")[1]) as tmp:
        tmp.write(file.file.read())
        raw_path = tmp.name
    try:
        segments = transcribe_path(raw_path, language, min_speakers, max_speakers)
    finally:
        if os.path.exists(raw_path):
            os.remove(raw_path)
    dialogue = "\n".join(f"[{s['speaker']}] {s['text']}" for s in segments)

    return {
        "language": language,
        "text": " ".join(s["text"] for s in segments),
        "dialogue": dialogue,
        "segments": segments,
    }


# ---------------- Analiz (Claude) ----------------
import json
import urllib.error
import urllib.request

from pydantic import BaseModel

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANALYZE_MODEL = os.environ.get("ANALYZE_MODEL", "claude-sonnet-5-5")

def _load_prompt():
    try:
        with open(os.path.join(os.path.dirname(__file__), "analiz_prompt.md"), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "Görüşmeyi analiz et ve JSON döndür.\nGÖRÜŞME DÖKÜMÜ:\n{dokum}"
ANALYZE_PROMPT_TPL = _load_prompt()
def _load_prompt_t1():
    try:
        with open(os.path.join(os.path.dirname(__file__), "analiz_prompt_t1.md"), encoding="utf-8") as f: return f.read()
    except FileNotFoundError: return ANALYZE_PROMPT_TPL
ANALYZE_PROMPT_T1 = _load_prompt_t1()
ANALYZE_MODEL_T1 = os.environ.get("ANALYZE_MODEL_T1", "claude-haiku-5-5")
IN_PRICE_T1 = float(os.environ.get("ANALYZE_T1_IN_PRICE_PER_M", "1")); OUT_PRICE_T1 = float(os.environ.get("ANALYZE_T1_OUT_PRICE_PER_M", "5"))
ANALYZE_STATE = {"durum": "bekliyor", "secilen": 0, "yapilan": 0, "hata": 0, "son_hata": None, "input_tokens": 0, "output_tokens": 0, "tahmini_usd": 0.0}
IN_PRICE = float(os.environ.get("ANALYZE_IN_PRICE_PER_M", "3")); OUT_PRICE = float(os.environ.get("ANALYZE_OUT_PRICE_PER_M", "15"))

def analyze_dialogue(dialogue: str, yon=None, dahili=None, tarih=None, tier: int = 2):
    """Claude'a gönderir; (json_sonuc, usage) döner. tier=1: hızlı/ucuz sınıflandırma, tier=2: detaylı çıkarım."""
    tpl = ANALYZE_PROMPT_T1 if tier == 1 else ANALYZE_PROMPT_TPL
    model = ANALYZE_MODEL_T1 if tier == 1 else ANALYZE_MODEL
    prompt = (tpl.replace("{yon}", str(yon or "")).replace("{dahili}", str(dahili or ""))
              .replace("{kayit_tarihi}", str(tarih or "")).replace("{dokum}", dialogue[:30000]))
    payload = {"model": model, "max_tokens": 3000 if tier == 1 else 8000, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    text = "".join(b.get("text", "") for b in data.get("content", [])).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()
    try: out = json.loads(text)
    except json.JSONDecodeError:
        out = {"ham": text, "kesildi": data.get("stop_reason") == "max_tokens"}
    return out, data.get("usage", {})



class AnalyzeIn(BaseModel):
    dialogue: str


@app.post("/analyze")
def analyze(body: AnalyzeIn, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session)
    if not ANTHROPIC_API_KEY: raise HTTPException(500, "ANTHROPIC_API_KEY ayarlanmadı")
    try:
        out, _ = analyze_dialogue(body.dialogue)
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"Claude API hatası: {e.read().decode()[:300]}")
    return out


# ---------------- Arşiv içe aktarma (Ortak Drive -> MySQL) ----------------
import io as _io, tarfile, threading as _th
IMPORT_STATE = {"durum": "bekliyor", "okunan": 0, "yazilan": 0, "hata": None}

DDL = """CREATE TABLE IF NOT EXISTS kayit_dokumleri (
  id INT AUTO_INCREMENT PRIMARY KEY, drive_id VARCHAR(64) NOT NULL UNIQUE, dosya_adi VARCHAR(255) NOT NULL,
  klasor_yolu VARCHAR(64), kayit_tarihi DATE, yon ENUM('gelen','giden') NULL, telefon VARCHAR(32), dahili VARCHAR(32),
  boyut_kb INT, durum ENUM('bekliyor','isleniyor','tamam','hata','atlandi') DEFAULT 'bekliyor', hata TEXT,
  dokum MEDIUMTEXT, segmentler JSON, analiz JSON, genel_puan TINYINT, temsilci_sp VARCHAR(12), sonuc VARCHAR(64), islem_sn INT,
  olusturma TIMESTAMP DEFAULT CURRENT_TIMESTAMP, guncelleme TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX (durum), INDEX (kayit_tarihi), INDEX (dahili), INDEX (telefon)
) CHARACTER SET utf8mb4"""

UPSERT = """INSERT INTO kayit_dokumleri (drive_id,dosya_adi,klasor_yolu,kayit_tarihi,yon,telefon,dahili,boyut_kb,durum,hata,dokum,segmentler,islem_sn)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE durum=VALUES(durum),hata=VALUES(hata),dokum=VALUES(dokum),segmentler=VALUES(segmentler),islem_sn=VALUES(islem_sn)"""

def _db():
    import pymysql
    return pymysql.connect(host=os.environ["DB_HOST"], user=os.environ["DB_USER"], password=os.environ["DB_PASS"],
                           database=os.environ["DB_NAME"], charset="utf8mb4", autocommit=False)

def _drive_download(name):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    c = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    d = build("drive", "v3", credentials=c, cache_discovery=False)
    r = d.files().list(q=f"name='{name}' and trashed=false", fields="files(id,size)", supportsAllDrives=True,
                       includeItemsFromAllDrives=True, corpora="allDrives").execute()["files"]
    if not r: raise RuntimeError(f"Drive'da bulunamadı: {name}")
    buf = _io.BytesIO(); dl = MediaIoBaseDownload(buf, d.files().get_media(fileId=r[0]["id"], supportsAllDrives=True))
    done = False
    while not done: _, done = dl.next_chunk()
    buf.seek(0); return buf

def _import_worker(name):
    try:
        IMPORT_STATE.update(durum="indiriliyor", okunan=0, yazilan=0, hata=None)
        buf = _drive_download(name)
        IMPORT_STATE["durum"] = "yaziliyor"
        conn = _db(); cur = conn.cursor(); cur.execute(DDL); conn.commit()
        cur.execute("ALTER TABLE kayit_dokumleri MODIFY telefon VARCHAR(32), MODIFY dahili VARCHAR(32), MODIFY klasor_yolu VARCHAR(64)"); conn.commit()
        batch = []
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            for m in tar:
                if not (m.isfile() and m.name.endswith(".json") and "out/" in m.name): continue
                j = json.loads(tar.extractfile(m).read().decode("utf-8")); IMPORT_STATE["okunan"] += 1
                tel = (j.get("telefon") or None); dah = (j.get("dahili") or None)
                batch.append((j["drive_id"], j["dosya_adi"][:255], j.get("klasor_yolu"), j.get("kayit_tarihi"), j.get("yon"), tel[:32] if tel else None,
                              dah[:32] if dah else None, j.get("boyut_kb"), j["durum"], j.get("hata"), j.get("dokum"),
                              json.dumps(j.get("segmentler"), ensure_ascii=False) if j.get("segmentler") is not None else None, j.get("islem_sn")))
                if len(batch) >= 500:
                    cur.executemany(UPSERT, batch); conn.commit(); IMPORT_STATE["yazilan"] += len(batch); batch = []
        if batch: cur.executemany(UPSERT, batch); conn.commit(); IMPORT_STATE["yazilan"] += len(batch)
        cur.execute("SELECT durum, COUNT(*) FROM kayit_dokumleri GROUP BY durum"); IMPORT_STATE["tablo"] = dict(cur.fetchall())
        conn.close(); IMPORT_STATE["durum"] = "tamam"
    except Exception as e:
        IMPORT_STATE.update(durum="hata", hata=str(e)[:500])

@app.post("/admin/import_drive")
def import_drive(name: str = "results.tgz", authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session)
    if IMPORT_STATE["durum"] in ("indiriliyor", "yaziliyor"): return IMPORT_STATE
    _th.Thread(target=_import_worker, args=(name,), daemon=True).start()
    return {"baslatildi": True}

@app.get("/admin/import_status")
def import_status(authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session); return IMPORT_STATE

@app.get("/admin/db_stats")
def db_stats(authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session)
    conn = _db(); cur = conn.cursor()
    cur.execute("SELECT durum, COUNT(*) FROM kayit_dokumleri GROUP BY durum"); r = dict(cur.fetchall())
    cur.execute("SELECT dahili, COUNT(*) FROM kayit_dokumleri WHERE durum='tamam' GROUP BY dahili"); r["dahili"] = dict(cur.fetchall())
    conn.close(); return r


# ---------------- Gece görevi: Drive'daki yeni kayıtları işle ----------------
import datetime as _dt, re as _re, zoneinfo as _zi

DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "1mhWZk0fHPT97qPORXIPukkGufWTyOR00")   # aylincetinkaya.bulutsantralim.com
DAILY_HOUR = int(os.environ.get("DAILY_HOUR", "2"))
DAILY_DAYS_BACK = int(os.environ.get("DAILY_DAYS_BACK", "3"))
DAILY_MIN_KB = int(os.environ.get("DAILY_MIN_KB", "50"))
TZ = _zi.ZoneInfo("Europe/Istanbul")
DAILY_STATE = {"durum": "bekliyor", "son_calisma": None, "sonraki": None, "bulunan": 0, "yeni": 0, "islenen": 0, "atlanan": 0, "hata": 0, "son_hata": None, "simdiki": None}

def _drive_service(scope="https://www.googleapis.com/auth/drive.readonly"):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    c = service_account.Credentials.from_service_account_info(info, scopes=[scope])
    return build("drive", "v3", credentials=c, cache_discovery=False)

def _child(d, parent, name, folder=True):
    mt = "=" if folder else "!="
    q = f"'{parent}' in parents and name='{name}' and trashed=false and mimeType {mt} 'application/vnd.google-apps.folder'"
    r = d.files().list(q=q, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()["files"]
    return r[0]["id"] if r else None

def _list_files(d, parent):
    q = f"'{parent}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'"
    out, token = [], None
    while True:
        r = d.files().list(q=q, fields="nextPageToken, files(id,name,size)", pageSize=1000, supportsAllDrives=True,
                           includeItemsFromAllDrives=True, pageToken=token).execute()
        out += r.get("files", []); token = r.get("nextPageToken")
        if not token: return out

def _parse_name(name):
    parts = name.rsplit(".", 1)[0].split("_")
    if len(parts) < 3: return None, None, None
    a, b = parts[1], parts[2]
    return ("giden", b, a) if len(a) <= 4 else ("gelen", a, b)

def daily_job(days_back=None):
    days_back = days_back or DAILY_DAYS_BACK
    DAILY_STATE.update(durum="calisiyor", bulunan=0, yeni=0, islenen=0, atlanan=0, hata=0, son_hata=None, son_calisma=_dt.datetime.now(TZ).isoformat(timespec="minutes"))
    try:
        d = _drive_service()
        from googleapiclient.http import MediaIoBaseDownload
        conn = _db(); cur = conn.cursor(); cur.execute(DDL); conn.commit()
        today = _dt.datetime.now(TZ).date()
        for back in range(days_back):
            day = today - _dt.timedelta(days=back)
            y, m, g = f"{day.year}", f"{day.month:02d}", f"{day.day:02d}"
            fy = _child(d, DRIVE_ROOT, y); fm = _child(d, fy, m) if fy else None
            fg = None
            if fm:
                fg = _child(d, fm, g) or _child(d, fm, str(day.day))
            if not fg: continue
            files = [f for f in _list_files(d, fg) if _re.search(r"\.(mp3|wav|m4a|ogg)$", f["name"], _re.I)]
            DAILY_STATE["bulunan"] += len(files)
            if not files: continue
            cur.execute("SELECT drive_id FROM kayit_dokumleri WHERE drive_id IN (%s)" % ",".join(["%s"] * len(files)), [f["id"] for f in files])
            have = {r[0] for r in cur.fetchall()}
            for f in files:
                if f["id"] in have: continue
                DAILY_STATE["yeni"] += 1; DAILY_STATE["simdiki"] = f["name"]
                kb = int(f.get("size", 0)) // 1024
                yon, tel, dah = _parse_name(f["name"])
                base = (f["id"], f["name"][:255], f"{y}/{m}/{g}", day.isoformat(), yon, tel[:32] if tel else None, dah[:32] if dah else None, kb)
                if kb < DAILY_MIN_KB:
                    cur.execute(UPSERT, base + ("atlandi", None, None, None, None)); conn.commit(); DAILY_STATE["atlanan"] += 1; continue
                raw = None; t0 = time.time()
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(f["name"])[1]) as tmp:
                        raw = tmp.name
                        dl = MediaIoBaseDownload(tmp, d.files().get_media(fileId=f["id"], supportsAllDrives=True))
                        done = False
                        while not done: _, done = dl.next_chunk()
                    segs = transcribe_path(raw, DEFAULT_LANG, 2, 2)
                    dok = "\n".join(f"[{s['speaker']}] {s['text']}" for s in segs)
                    cur.execute(UPSERT, base + ("tamam", None, dok, json.dumps(segs, ensure_ascii=False), round(time.time() - t0)))
                    DAILY_STATE["islenen"] += 1
                except Exception as e:
                    cur.execute(UPSERT, base + ("hata", str(e)[:2000], None, None, None)); DAILY_STATE["hata"] += 1; DAILY_STATE["son_hata"] = str(e)[:300]
                finally:
                    if raw and os.path.exists(raw): os.remove(raw)
                conn.commit()
        conn.close(); DAILY_STATE["durum"] = "tamam"
    except Exception as e:
        DAILY_STATE.update(durum="hata", son_hata=str(e)[:500])
    DAILY_STATE["simdiki"] = None

def _scheduler():
    while True:
        now = _dt.datetime.now(TZ)
        nxt = now.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now: nxt += _dt.timedelta(days=1)
        DAILY_STATE["sonraki"] = nxt.isoformat(timespec="minutes")
        time.sleep((nxt - now).total_seconds())
        if DAILY_STATE["durum"] != "calisiyor": daily_job()

if os.environ.get("GOOGLE_SA_JSON") and os.environ.get("DB_HOST"):
    _th.Thread(target=_scheduler, daemon=True).start()
    print(f"Gece görevi aktif: her gün {DAILY_HOUR:02d}:00 (Europe/Istanbul), son {DAILY_DAYS_BACK} gün taranır.", flush=True)

@app.post("/admin/run_daily")
def run_daily(days_back: int = DAILY_DAYS_BACK, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session)
    if DAILY_STATE["durum"] == "calisiyor": return DAILY_STATE
    _th.Thread(target=daily_job, args=(days_back,), daemon=True).start()
    return {"baslatildi": True, "days_back": days_back}

@app.get("/admin/daily_status")
def daily_status(authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session); return DAILY_STATE



# ---------------- Toplu analiz (tablo üzerinden) ----------------
def _analyze_batch_worker(limit, where, tier=2):
    ANALYZE_STATE.update(durum="calisiyor", tier=tier, secilen=0, yapilan=0, hata=0, son_hata=None, input_tokens=0, output_tokens=0, tahmini_usd=0.0)
    ip, op = (IN_PRICE_T1, OUT_PRICE_T1) if tier == 1 else (IN_PRICE, OUT_PRICE)
    try:
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='kayit_dokumleri'")
        cols = {r[0] for r in cur.fetchall()}
        for name, ddl in (("analiz_t1", "JSON NULL"), ("puan_t1", "TINYINT NULL"), ("aday", "VARCHAR(16) NULL")):
            if name not in cols: cur.execute(f"ALTER TABLE kayit_dokumleri ADD COLUMN {name} {ddl}")
        conn.commit()
        col = "analiz_t1" if tier == 1 else "analiz"
        cur.execute(f"SELECT drive_id, dokum, yon, dahili, kayit_tarihi FROM kayit_dokumleri WHERE durum='tamam' AND {col} IS NULL AND CHAR_LENGTH(dokum) > 200 {where} ORDER BY kayit_tarihi DESC, id LIMIT %s", (limit,))
        rows = cur.fetchall(); ANALYZE_STATE["secilen"] = len(rows)
        for drive_id, dokum, yon, dahili, tarih in rows:
            try:
                out, usage = analyze_dialogue(dokum, yon, dahili, tarih, tier=tier)
                if tier == 1:
                    cur.execute("UPDATE kayit_dokumleri SET analiz_t1=%s, puan_t1=%s, aday=%s, temsilci_sp=COALESCE(temsilci_sp,%s), sonuc=COALESCE(sonuc,%s) WHERE drive_id=%s",
                                (json.dumps(out, ensure_ascii=False), out.get("genel_puan"), out.get("aday"), out.get("temsilci_speaker"), (out.get("sonuc") or None), drive_id))
                else:
                    k = out.get("kalite", {}) if isinstance(out, dict) else {}
                    cur.execute("UPDATE kayit_dokumleri SET analiz=%s, genel_puan=%s, temsilci_sp=%s, sonuc=%s WHERE drive_id=%s",
                                (json.dumps(out, ensure_ascii=False), k.get("genel_puan"), out.get("temsilci_speaker"), (out.get("sonuc") or None), drive_id))
                conn.commit(); ANALYZE_STATE["yapilan"] += 1
                ANALYZE_STATE["input_tokens"] += usage.get("input_tokens", 0); ANALYZE_STATE["output_tokens"] += usage.get("output_tokens", 0)
                ANALYZE_STATE["tahmini_usd"] = round(ANALYZE_STATE["input_tokens"] / 1e6 * ip + ANALYZE_STATE["output_tokens"] / 1e6 * op, 3)
            except Exception as e:
                ANALYZE_STATE["hata"] += 1; ANALYZE_STATE["son_hata"] = str(e)[:300]
        conn.close(); ANALYZE_STATE["durum"] = "tamam"
    except Exception as e:
        ANALYZE_STATE.update(durum="hata", son_hata=str(e)[:500])

@app.post("/admin/analyze_batch")
def analyze_batch(limit: int = 30, since: str | None = None, tier: int = 2, only_candidates: bool = False, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session)
    if not ANTHROPIC_API_KEY: raise HTTPException(500, "ANTHROPIC_API_KEY ayarlanmadı")
    if ANALYZE_STATE["durum"] == "calisiyor": return ANALYZE_STATE
    where = f"AND kayit_tarihi >= '{since}'" if since and _re.fullmatch(r"\d{4}-\d{2}-\d{2}", since) else ""
    if tier == 2 and only_candidates: where += " AND aday IN ('iyi','kotu')"
    _th.Thread(target=_analyze_batch_worker, args=(limit, where, tier), daemon=True).start()
    return {"baslatildi": True, "limit": limit, "tier": tier}

@app.get("/admin/analyze_status")
def analyze_status(authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session); return ANALYZE_STATE

@app.get("/admin/analyze_sample")
def analyze_sample(n: int = 5, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session)
    conn = _db(); cur = conn.cursor()
    cur.execute("SELECT drive_id, kayit_tarihi, yon, dahili, genel_puan, dokum, analiz, analiz_t1, puan_t1, aday FROM kayit_dokumleri WHERE analiz IS NOT NULL OR analiz_t1 IS NOT NULL ORDER BY guncelleme DESC LIMIT %s", (n,))
    rows = [{"drive_id": r[0], "tarih": str(r[1]), "yon": r[2], "dahili": r[3], "puan": r[4], "dokum": r[5], "analiz": json.loads(r[6]) if r[6] else None,
             "analiz_t1": json.loads(r[7]) if r[7] else None, "puan_t1": r[8], "aday": r[9]} for r in cur.fetchall()]
    conn.close(); return rows

@app.post("/admin/analyze_reset")
def analyze_reset(only_broken: bool = True, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    """Analizi sıfırla: only_broken=True -> sadece 'ham' (bozuk JSON) olanlar; False -> tüm analizler."""
    check_key(authorization, wx_session)
    conn = _db(); cur = conn.cursor()
    if only_broken:
        cur.execute("UPDATE kayit_dokumleri SET analiz=NULL, genel_puan=NULL, temsilci_sp=NULL, sonuc=NULL WHERE analiz IS NOT NULL AND JSON_EXTRACT(analiz,'$.ham') IS NOT NULL")
    else:
        cur.execute("UPDATE kayit_dokumleri SET analiz=NULL, genel_puan=NULL, temsilci_sp=NULL, sonuc=NULL WHERE analiz IS NOT NULL")
    n = cur.rowcount; conn.commit(); conn.close()
    return {"sifirlanan": n}
