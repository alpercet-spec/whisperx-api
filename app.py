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

DIARIZE_MODEL = os.environ.get("DIARIZE_MODEL", "pyannote/speaker-diarization-community-1")
asr_model = align_model = align_meta = diarize_model = None
MODELS_READY = False
_model_lock = threading.Lock()

def _ensure_models():
    """Modelleri ilk ihtiyaçta (tembel) yükler; konteyner açılışı böylece saniyeler sürer."""
    global asr_model, align_model, align_meta, diarize_model, MODELS_READY
    if MODELS_READY: return
    with _model_lock:
        if MODELS_READY: return
        print("Model yükleniyor...", flush=True)
        asr_model = whisperx.load_model(MODEL_NAME, DEVICE, compute_type=COMPUTE, threads=THREADS)
        align_model, align_meta = whisperx.load_align_model(language_code=DEFAULT_LANG, device=DEVICE)
        try:
            diarize_model = DiarizationPipeline(model_name=DIARIZE_MODEL, token=HF_TOKEN, device=DEVICE)
        except TypeError:
            diarize_model = DiarizationPipeline(model_name=DIARIZE_MODEL, use_auth_token=HF_TOKEN, device=DEVICE)
        MODELS_READY = True
        print("Hazır.", flush=True)

PANEL_DIR = os.environ.get("PANEL_DIR", "/panel")
PANEL_FILES = ("panel.html", "kb.html", "kb_mobil.html", "kayitlar.html")

def panel_file(name: str) -> str:
    """Panel dosyasını önce /panel (mount) klasöründen, yoksa imajdan okur."""
    for base in (PANEL_DIR, os.path.dirname(__file__)):
        try:
            with open(os.path.join(base, name), encoding="utf-8") as f: return f.read()
        except FileNotFoundError: continue
    return "<p>panel dosyası bulunamadı</p>"


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
    return panel_file("panel.html")


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
    return {"status": "ok", "model": MODEL_NAME, "model_loaded": MODELS_READY}


@app.post("/admin/panel_file")
async def panel_file_upload(request: Request, name: str, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    """Panel HTML dosyasını dağıtım yapmadan günceller (PANEL_DIR'e yazar)."""
    check_key(authorization, wx_session)
    if name not in PANEL_FILES: raise HTTPException(400, "izin verilmeyen dosya")
    body = await request.body()
    os.makedirs(PANEL_DIR, exist_ok=True)
    with open(os.path.join(PANEL_DIR, name), "wb") as f: f.write(body)
    return {"yazildi": name, "byte": len(body)}



def transcribe_path(raw_path: str, language: str = DEFAULT_LANG, min_speakers=None, max_speakers=None):
    """Ses dosyasını (herhangi format) ön işleyip konuşmacı ayrımlı segmentlere çevirir."""
    path = raw_path + ".wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", raw_path, "-ac", "1", "-ar", "16000",
             "-af", "highpass=f=200,lowpass=f=3400,loudnorm=I=-16:TP=-1.5:LRA=11", path],
            check=True,
        )
        _ensure_models()
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
    # Dökümden sonra: son günlerin analiz edilmemiş görüşmelerini Claude ile analiz et (DAILY_ANALYZE=1)
    if os.environ.get("DAILY_ANALYZE", "1") == "1" and ANTHROPIC_API_KEY and ANALYZE_STATE["durum"] != "calisiyor":
        try:
            since = (_dt.datetime.now(TZ) - _dt.timedelta(days=days_back)).strftime("%Y-%m-%d")
            DAILY_STATE["simdiki"] = f"analiz ({since} sonrası)"
            yon_f = os.environ.get("DAILY_ANALYZE_YON", "gelen")
            yon_sql = f" AND yon='{yon_f}'" if yon_f in ("gelen", "giden") else ""
            _analyze_batch_worker(int(os.environ.get("DAILY_ANALYZE_LIMIT", "400")), f"AND kayit_tarihi >= '{since}'" + yon_sql, 2, 4)
            DAILY_STATE["analiz"] = {"yapilan": ANALYZE_STATE["yapilan"], "hata": ANALYZE_STATE["hata"], "usd": ANALYZE_STATE["tahmini_usd"]}
        except Exception as e:
            DAILY_STATE["son_hata"] = "analiz: " + str(e)[:300]
        DAILY_STATE["simdiki"] = None
    try:
        conn = _db(); cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS gece_log (id INT AUTO_INCREMENT PRIMARY KEY, calisma DATETIME, durum VARCHAR(16), bulunan INT, yeni INT, islenen INT, atlanan INT, hata INT, son_hata TEXT, analiz_yapilan INT, analiz_usd DECIMAL(8,3))")
        a = DAILY_STATE.get("analiz") or {}
        cur.execute("INSERT INTO gece_log (calisma,durum,bulunan,yeni,islenen,atlanan,hata,son_hata,analiz_yapilan,analiz_usd) VALUES (NOW(),%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (DAILY_STATE["durum"], DAILY_STATE["bulunan"], DAILY_STATE["yeni"], DAILY_STATE["islenen"], DAILY_STATE["atlanan"], DAILY_STATE["hata"], DAILY_STATE["son_hata"], a.get("yapilan"), a.get("usd")))
        conn.commit(); conn.close()
    except Exception: pass

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
def _analyze_one(conn_factory, tier, row):
    """Tek kayıt: Claude çağrısı + DB yazımı. Thread havuzunda çalışır."""
    drive_id, dokum, yon, dahili, tarih = row
    for attempt in range(2):
        try:
            out, usage = analyze_dialogue(dokum, yon, dahili, tarih, tier=tier)
            if isinstance(out, dict) and out.get("ham") and attempt == 0: continue  # bozuk JSON -> bir kez daha dene
            conn = conn_factory(); cur = conn.cursor()
            if tier == 1:
                cur.execute("UPDATE kayit_dokumleri SET analiz_t1=%s, puan_t1=%s, aday=%s, temsilci_sp=COALESCE(temsilci_sp,%s), sonuc=COALESCE(sonuc,%s) WHERE drive_id=%s",
                            (json.dumps(out, ensure_ascii=False), out.get("genel_puan"), out.get("aday"), out.get("temsilci_speaker"), (out.get("sonuc") or None), drive_id))
            else:
                k = out.get("kalite", {}) if isinstance(out, dict) else {}
                cur.execute("UPDATE kayit_dokumleri SET analiz=%s, genel_puan=%s, temsilci_sp=%s, sonuc=%s WHERE drive_id=%s",
                            (json.dumps(out, ensure_ascii=False), k.get("genel_puan"), out.get("temsilci_speaker"), (out.get("sonuc") or None), drive_id))
                if not out.get("ham"):
                    try: extract_rows(cur, drive_id, tarih, yon, dahili, out)
                    except Exception: pass
            conn.commit(); conn.close()
            return usage, None
        except Exception as e:
            err = str(e)[:300]
            if attempt == 0 and ("429" in err or "529" in err or "overloaded" in err.lower()): time.sleep(5); continue
            return None, err
    return None, "bozuk JSON (2 deneme)"

def _analyze_batch_worker(limit, where, tier=2, workers=4):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ANALYZE_STATE.update(durum="calisiyor", tier=tier, secilen=0, yapilan=0, hata=0, son_hata=None, input_tokens=0, output_tokens=0, tahmini_usd=0.0, baslangic=time.time(), sure_sn=0, workers=workers)
    ip, op = (IN_PRICE_T1, OUT_PRICE_T1) if tier == 1 else (IN_PRICE, OUT_PRICE)
    try:
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='kayit_dokumleri'")
        cols = {r[0] for r in cur.fetchall()}
        for name, ddl in (("analiz_t1", "JSON NULL"), ("puan_t1", "TINYINT NULL"), ("aday", "VARCHAR(16) NULL")):
            if name not in cols: cur.execute(f"ALTER TABLE kayit_dokumleri ADD COLUMN {name} {ddl}")
        for ddl in EXTRACT_DDL: cur.execute(ddl)
        conn.commit()
        col = "analiz_t1" if tier == 1 else "analiz"
        cur.execute(f"SELECT drive_id, dokum, yon, dahili, kayit_tarihi FROM kayit_dokumleri WHERE durum='tamam' AND {col} IS NULL AND CHAR_LENGTH(dokum) > 200 {where} ORDER BY kayit_tarihi DESC, id LIMIT %s", (limit,))
        rows = cur.fetchall(); conn.close(); ANALYZE_STATE["secilen"] = len(rows)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_analyze_one, _db, tier, r) for r in rows]
            for f in as_completed(futs):
                usage, err = f.result()
                if err: ANALYZE_STATE["hata"] += 1; ANALYZE_STATE["son_hata"] = err
                else:
                    ANALYZE_STATE["yapilan"] += 1
                    ANALYZE_STATE["input_tokens"] += usage.get("input_tokens", 0); ANALYZE_STATE["output_tokens"] += usage.get("output_tokens", 0)
                    ANALYZE_STATE["tahmini_usd"] = round(ANALYZE_STATE["input_tokens"] / 1e6 * ip + ANALYZE_STATE["output_tokens"] / 1e6 * op, 3)
                ANALYZE_STATE["sure_sn"] = round(time.time() - ANALYZE_STATE["baslangic"])
        ANALYZE_STATE["durum"] = "tamam"
    except Exception as e:
        ANALYZE_STATE.update(durum="hata", son_hata=str(e)[:500])

@app.post("/admin/analyze_batch")
def analyze_batch(limit: int = 30, since: str | None = None, tier: int = 2, only_candidates: bool = False, yon: str | None = None, workers: int = 4, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session)
    if not ANTHROPIC_API_KEY: raise HTTPException(500, "ANTHROPIC_API_KEY ayarlanmadı")
    if ANALYZE_STATE["durum"] == "calisiyor": return ANALYZE_STATE
    where = f"AND kayit_tarihi >= '{since}'" if since and _re.fullmatch(r"\d{4}-\d{2}-\d{2}", since) else ""
    if yon in ("gelen", "giden"): where += f" AND yon='{yon}'"
    if tier == 2 and only_candidates: where += " AND aday IN ('iyi','kotu')"
    _th.Thread(target=_analyze_batch_worker, args=(limit, where, tier, max(1, min(workers, 8))), daemon=True).start()
    return {"baslatildi": True, "limit": limit, "tier": tier, "workers": workers}

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


# ---------------- Çıkarım tabloları: analiz JSON -> yapısal satırlar ----------------
EXTRACT_DDL = [
"""CREATE TABLE IF NOT EXISTS gorusme_ozet (
  drive_id VARCHAR(64) PRIMARY KEY, kayit_tarihi DATE, yon VARCHAR(8), dahili VARCHAR(32), temsilci VARCHAR(64),
  gorusme_turu VARCHAR(48), sonuc VARCHAR(48), genel_puan TINYINT, etiket VARCHAR(16), kirmizi_bayrak TEXT, kacirilan_firsat TEXT,
  p_karsilama TINYINT, p_ihtiyac TINYINT, p_sorular TINYINT, p_bilgi TINYINT, p_yonlendirme TINYINT, p_nezaket TINYINT, p_kapanis TINYINT,
  ozet TEXT, INDEX(kayit_tarihi), INDEX(temsilci), INDEX(gorusme_turu), INDEX(etiket)) CHARACTER SET utf8mb4""",
"""CREATE TABLE IF NOT EXISTS bilgi_adaylari (
  id INT AUTO_INCREMENT PRIMARY KEY, drive_id VARCHAR(64), kayit_tarihi DATE, temsilci VARCHAR(64), konu VARCHAR(32),
  bilgi VARCHAR(500), kaynak_cumle VARCHAR(500), guven VARCHAR(8), INDEX(drive_id), INDEX(konu), INDEX(guven)) CHARACTER SET utf8mb4""",
"""CREATE TABLE IF NOT EXISTS kural_adaylari (
  id INT AUTO_INCREMENT PRIMARY KEY, drive_id VARCHAR(64), kayit_tarihi DATE, temsilci VARCHAR(64), kural VARCHAR(500), INDEX(drive_id)) CHARACTER SET utf8mb4""",
"""CREATE TABLE IF NOT EXISTS hizmet_eslestirme (
  id INT AUTO_INCREMENT PRIMARY KEY, drive_id VARCHAR(64), kayit_tarihi DATE, musteri_ifadesi VARCHAR(255), temsilci_adlandirmasi VARCHAR(255),
  normalize_ad VARCHAR(48), nitelik VARCHAR(32), bolge_miktar VARCHAR(64), baglam VARCHAR(32), fiyat_soruldu TINYINT, INDEX(normalize_ad), INDEX(drive_id)) CHARACTER SET utf8mb4""",
"""CREATE TABLE IF NOT EXISTS soru_cevap (
  id INT AUTO_INCREMENT PRIMARY KEY, drive_id VARCHAR(64), kayit_tarihi DATE, gorusme_turu VARCHAR(48), musteri_sorusu VARCHAR(500), temsilci_cevabi VARCHAR(1000), bilgi_icerir TINYINT, INDEX(drive_id)) CHARACTER SET utf8mb4""",
"""CREATE TABLE IF NOT EXISTS kaliplar (
  id INT AUTO_INCREMENT PRIMARY KEY, drive_id VARCHAR(64), temsilci VARCHAR(64), tur VARCHAR(24), cumle VARCHAR(500), INDEX(tur), INDEX(temsilci), INDEX(drive_id)) CHARACTER SET utf8mb4""",
"""CREATE TABLE IF NOT EXISTS standart_sorular (
  id INT AUTO_INCREMENT PRIMARY KEY, drive_id VARCHAR(64), temsilci VARCHAR(64), soru VARCHAR(300), INDEX(drive_id)) CHARACTER SET utf8mb4""",
]
EXTRACT_STATE = {"durum": "bekliyor", "islenen": 0, "hata": 0}

def _s(v, n):  # güvenli kısaltma
    return (str(v)[:n] if v is not None else None)

def extract_rows(cur, drive_id, tarih, yon, dahili, a):
    tem = _s(a.get("temsilci_adi"), 64)
    for t in ("gorusme_ozet", "bilgi_adaylari", "kural_adaylari", "hizmet_eslestirme", "soru_cevap", "kaliplar", "standart_sorular"):
        cur.execute(f"DELETE FROM {t} WHERE drive_id=%s", (drive_id,))
    k = a.get("kalite") or {}; p = k.get("puanlar") or {}; e = a.get("egitim_ornegi") or {}
    cur.execute("""INSERT INTO gorusme_ozet VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (drive_id, tarih, yon, dahili, tem, _s(a.get("gorusme_turu"), 48), _s(a.get("sonuc"), 48), k.get("genel_puan"), _s(e.get("etiket"), 16),
         _s(k.get("kirmizi_bayrak"), 2000), _s(k.get("kacirilan_firsat"), 2000),
         p.get("karsilama_tanitim"), p.get("ihtiyaci_anlama"), p.get("standart_sorular"), p.get("bilgi_dogrulugu_netligi"),
         p.get("randevuya_yonlendirme"), p.get("nezaket_ton"), p.get("kapanis_teyit"), _s(a.get("ozet"), 2000)))
    for b in a.get("olgusal_bilgiler") or []:
        if isinstance(b, dict) and b.get("bilgi"):
            cur.execute("INSERT INTO bilgi_adaylari (drive_id,kayit_tarihi,temsilci,konu,bilgi,kaynak_cumle,guven) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (drive_id, tarih, tem, _s(b.get("konu"), 32), _s(b.get("bilgi"), 500), _s(b.get("kaynak_cumle"), 500), _s(b.get("guven"), 8)))
    for r in a.get("kurallar_ve_politikalar") or []:
        if isinstance(r, str) and r.strip():
            cur.execute("INSERT INTO kural_adaylari (drive_id,kayit_tarihi,temsilci,kural) VALUES (%s,%s,%s,%s)", (drive_id, tarih, tem, _s(r, 500)))
    for h in a.get("sorulan_hizmetler") or []:
        if isinstance(h, dict):
            n = h.get("nitelikler") or {}
            cur.execute("INSERT INTO hizmet_eslestirme (drive_id,kayit_tarihi,musteri_ifadesi,temsilci_adlandirmasi,normalize_ad,nitelik,bolge_miktar,baglam,fiyat_soruldu) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (drive_id, tarih, _s(h.get("musteri_ifadesi"), 255), _s(h.get("temsilci_adlandirmasi"), 255), _s(h.get("normalize_ad"), 48),
                         _s(n.get("ilk_mi_yenileme_mi") if isinstance(n, dict) else None, 32), _s(n.get("bolge_miktar") if isinstance(n, dict) else None, 64),
                         _s(h.get("baglam"), 32), 1 if h.get("fiyat_soruldu") else 0))
    for q in a.get("soru_cevap") or []:
        if isinstance(q, dict) and q.get("musteri_sorusu"):
            cur.execute("INSERT INTO soru_cevap (drive_id,kayit_tarihi,gorusme_turu,musteri_sorusu,temsilci_cevabi,bilgi_icerir) VALUES (%s,%s,%s,%s,%s,%s)",
                        (drive_id, tarih, _s(a.get("gorusme_turu"), 48), _s(q.get("musteri_sorusu"), 500), _s(q.get("temsilci_cevabi"), 1000), 1 if q.get("bilgi_icerir") else 0))
    for c in a.get("temsilci_kaliplari") or []:
        if isinstance(c, dict) and c.get("cumle"):
            cur.execute("INSERT INTO kaliplar (drive_id,temsilci,tur,cumle) VALUES (%s,%s,%s,%s)", (drive_id, tem, _s(c.get("tur"), 24), _s(c.get("cumle"), 500)))
    for q in a.get("standart_sorular") or []:
        if isinstance(q, str) and q.strip():
            cur.execute("INSERT INTO standart_sorular (drive_id,temsilci,soru) VALUES (%s,%s,%s)", (drive_id, tem, _s(q, 300)))

def _extract_worker(only_new=True):
    EXTRACT_STATE.update(durum="calisiyor", islenen=0, hata=0)
    try:
        conn = _db(); cur = conn.cursor()
        for ddl in EXTRACT_DDL: cur.execute(ddl)
        conn.commit()
        q = "SELECT k.drive_id, k.kayit_tarihi, k.yon, k.dahili, k.analiz FROM kayit_dokumleri k WHERE k.analiz IS NOT NULL AND JSON_EXTRACT(k.analiz,'$.ham') IS NULL"
        if only_new: q += " AND NOT EXISTS (SELECT 1 FROM gorusme_ozet g WHERE g.drive_id=k.drive_id)"
        cur.execute(q); rows = cur.fetchall()
        for drive_id, tarih, yon, dahili, aj in rows:
            try:
                extract_rows(cur, drive_id, tarih, yon, dahili, json.loads(aj)); conn.commit(); EXTRACT_STATE["islenen"] += 1
            except Exception as e:
                conn.rollback(); EXTRACT_STATE["hata"] += 1; EXTRACT_STATE["son_hata"] = str(e)[:300]
        conn.close(); EXTRACT_STATE["durum"] = "tamam"
    except Exception as e:
        EXTRACT_STATE.update(durum="hata", son_hata=str(e)[:500])

@app.post("/admin/extract")
def extract(only_new: bool = True, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session)
    if EXTRACT_STATE["durum"] == "calisiyor": return EXTRACT_STATE
    _th.Thread(target=_extract_worker, args=(only_new,), daemon=True).start(); return {"baslatildi": True}

@app.get("/admin/extract_status")
def extract_status(authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session); return EXTRACT_STATE

@app.get("/admin/bilgi_ozet")
def bilgi_ozet(authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session)
    conn = _db(); cur = conn.cursor(); out = {}
    for t in ("gorusme_ozet", "bilgi_adaylari", "kural_adaylari", "hizmet_eslestirme", "soru_cevap", "kaliplar", "standart_sorular"):
        cur.execute(f"SELECT COUNT(*) FROM {t}"); out[t] = cur.fetchone()[0]
    cur.execute("SELECT konu, COUNT(*) FROM bilgi_adaylari GROUP BY konu ORDER BY 2 DESC"); out["bilgi_konu"] = dict(cur.fetchall())
    cur.execute("SELECT normalize_ad, COUNT(*) FROM hizmet_eslestirme GROUP BY normalize_ad ORDER BY 2 DESC LIMIT 25"); out["hizmet"] = dict(cur.fetchall())
    cur.execute("SELECT musteri_ifadesi, normalize_ad, COUNT(*) c FROM hizmet_eslestirme GROUP BY 1,2 ORDER BY c DESC LIMIT 25"); out["eslestirme_top"] = [list(r) for r in cur.fetchall()]
    cur.execute("SELECT tur, cumle, COUNT(*) c FROM kaliplar GROUP BY 1,2 HAVING c>=2 ORDER BY c DESC LIMIT 25"); out["kalip_top"] = [list(r) for r in cur.fetchall()]
    cur.execute("SELECT soru, COUNT(*) c FROM standart_sorular GROUP BY 1 HAVING c>=2 ORDER BY c DESC LIMIT 20"); out["soru_top"] = [list(r) for r in cur.fetchall()]
    cur.execute("SELECT temsilci, COUNT(*), ROUND(AVG(genel_puan),1) FROM gorusme_ozet GROUP BY temsilci ORDER BY 2 DESC"); out["temsilci"] = [list(r) for r in cur.fetchall()]
    cur.execute("SELECT etiket, COUNT(*) FROM gorusme_ozet GROUP BY etiket"); out["etiket"] = dict(cur.fetchall())
    conn.close(); return out


# ---------------- Bilgi tabanı 2. katman (kb.py) ----------------
import kb as _kb
def _page_guard(wx_session):
    if not PANEL_PASS or not session_ok(wx_session): return LOGIN_HTML.replace("__ERR__", "")
    return None
_kb.register(app, check_key, _db, lambda: ANTHROPIC_API_KEY, lambda: ANALYZE_MODEL, (IN_PRICE, OUT_PRICE), _page_guard, panel_file)


# ---------------- Kayıtlar sayfası (dökümleri listele / ara / filtrele) ----------------
import re as _re2
@app.get("/kayitlar", response_class=HTMLResponse)
def kayitlar_page(wx_session: str | None = Cookie(None)):
    g = _page_guard(wx_session)
    return g or panel_file("kayitlar.html")

_FT_READY = {"ok": None}
def _ensure_fulltext(cur):
    if _FT_READY["ok"] is not None: return _FT_READY["ok"]
    try:
        cur.execute("SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='kayit_dokumleri' AND INDEX_NAME='ft_dokum'")
        if cur.fetchone()[0] == 0: cur.execute("ALTER TABLE kayit_dokumleri ADD FULLTEXT INDEX ft_dokum (dokum)")
        _FT_READY["ok"] = True
    except Exception: _FT_READY["ok"] = False
    return _FT_READY["ok"]

@app.get("/admin/kayitlar")
def kayitlar(q: str | None = None, yon: str | None = None, dahili: str | None = None, tur: str | None = None, etiket: str | None = None,
             tarih1: str | None = None, tarih2: str | None = None, min_puan: int | None = None, page: int = 1, per: int = 25,
             authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session)
    conn = _db(); cur = conn.cursor()
    where, args = ["k.durum='tamam'"], []
    if yon in ("gelen", "giden"): where.append("k.yon=%s"); args.append(yon)
    if dahili: where.append("k.dahili=%s"); args.append(dahili)
    if tur == "analiz_yok": where.append("g.drive_id IS NULL")
    elif tur: where.append("g.gorusme_turu=%s"); args.append(tur)
    if etiket: where.append("g.etiket=%s"); args.append(etiket)
    if tarih1 and _re2.fullmatch(r"\d{4}-\d{2}-\d{2}", tarih1): where.append("k.kayit_tarihi>=%s"); args.append(tarih1)
    if tarih2 and _re2.fullmatch(r"\d{4}-\d{2}-\d{2}", tarih2): where.append("k.kayit_tarihi<=%s"); args.append(tarih2)
    if min_puan is not None: where.append("k.genel_puan>=%s"); args.append(min_puan)
    q = (q or "").strip()
    digits = _re2.sub(r"\D", "", q)
    if q and digits and len(digits) >= 4 and len(digits) >= len(_re2.sub(r"[\s()+\-.]", "", q)):
        # telefon araması: 0532..., 532..., +90 532..., 90532... hepsi eşleşsin
        if digits.startswith("90") and len(digits) >= 12: digits = digits[2:]
        if digits.startswith("0"): digits = digits[1:]
        where.append("k.telefon LIKE %s"); args.append(f"%{digits}%")
        q = ""
    if q:
        ft = _ensure_fulltext(cur); conn.commit()
        if ft and len(q) >= 3 and not any(c in q for c in "+-*\"()<>~@"):
            where.append("MATCH(k.dokum) AGAINST(%s IN BOOLEAN MODE)"); args.append(" ".join("+" + w + "*" for w in q.split() if len(w) >= 2))
        else:
            where.append("k.dokum LIKE %s"); args.append(f"%{q}%")
    W = " AND ".join(where)
    base = "FROM kayit_dokumleri k LEFT JOIN gorusme_ozet g ON g.drive_id=k.drive_id WHERE " + W
    cur.execute("SELECT COUNT(*) " + base, args); total = cur.fetchone()[0]
    per = max(5, min(per, 100)); page = max(1, page)
    cur.execute("SELECT k.drive_id, k.kayit_tarihi, k.yon, k.dahili, k.telefon, k.boyut_kb, k.islem_sn, k.genel_puan, g.gorusme_turu, g.etiket, g.sonuc, g.ozet, "
                "CASE WHEN %s<>'' THEN SUBSTRING(k.dokum, GREATEST(1, LOCATE(%s, k.dokum)-80), 220) ELSE LEFT(k.dokum, 200) END " + base +
                " ORDER BY k.kayit_tarihi DESC, k.id DESC LIMIT %s OFFSET %s", [q, q.split()[0] if q else ""] + args + [per, (page - 1) * per])
    rows = [{"drive_id": r[0], "tarih": str(r[1]), "yon": r[2], "dahili": r[3], "telefon": r[4], "boyut_kb": r[5], "islem_sn": r[6], "puan": r[7],
             "tur": r[8], "etiket": r[9], "sonuc": r[10], "ozet": r[11], "snippet": r[12]} for r in cur.fetchall()]
    conn.close()
    return {"toplam": total, "sayfa": page, "per": per, "kayitlar": rows}

@app.get("/admin/kayitlar/filtreler")
def kayit_filtreler(authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session)
    conn = _db(); cur = conn.cursor()
    cur.execute("SELECT gorusme_turu, COUNT(*) FROM gorusme_ozet GROUP BY 1 ORDER BY 2 DESC"); turler = [[r[0], r[1]] for r in cur.fetchall()]
    cur.execute("SELECT dahili, COUNT(*) FROM kayit_dokumleri WHERE durum='tamam' GROUP BY 1 ORDER BY 2 DESC"); dahililer = [[r[0], r[1]] for r in cur.fetchall()]
    cur.execute("SELECT etiket, COUNT(*) FROM gorusme_ozet GROUP BY 1"); etiketler = [[r[0], r[1]] for r in cur.fetchall()]
    cur.execute("SELECT MIN(kayit_tarihi), MAX(kayit_tarihi) FROM kayit_dokumleri WHERE durum='tamam'"); mn, mx = cur.fetchone()
    try:
        cur.execute("SELECT calisma, durum, bulunan, yeni, islenen, atlanan, hata, analiz_yapilan, analiz_usd FROM gece_log ORDER BY id DESC LIMIT 10"); gece = [[str(r[0])] + list(r[1:]) for r in cur.fetchall()]
    except Exception: gece = []
    conn.close()
    return {"turler": turler, "dahililer": dahililer, "etiketler": etiketler, "tarih_min": str(mn), "tarih_max": str(mx), "gece": gece}

@app.get("/admin/kayit/{drive_id}")
def kayit_detay(drive_id: str, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    check_key(authorization, wx_session)
    conn = _db(); cur = conn.cursor()
    cur.execute("SELECT dosya_adi, kayit_tarihi, yon, dahili, telefon, boyut_kb, islem_sn, genel_puan, dokum, analiz FROM kayit_dokumleri WHERE drive_id=%s", (drive_id,))
    r = cur.fetchone(); conn.close()
    if not r: raise HTTPException(404, "bulunamadı")
    return {"drive_id": drive_id, "dosya": r[0], "tarih": str(r[1]), "yon": r[2], "dahili": r[3], "telefon": r[4], "boyut_kb": r[5], "islem_sn": r[6], "puan": r[7],
            "dokum": r[8], "analiz": json.loads(r[9]) if r[9] else None}

def _ft_bg():
    try:
        conn = _db(); cur = conn.cursor(); _ensure_fulltext(cur); conn.commit(); conn.close()
    except Exception: pass
_th.Thread(target=_ft_bg, daemon=True).start()


# ---------------- Dışarıda (Claude plan) yapılan analizleri içe aktarma ----------------
class AnalizImport(BaseModel):
    kayitlar: list[dict]   # [{"drive_id":..., "analiz":{...}}]

@app.post("/admin/analyze_import")
def analyze_import(body: AnalizImport, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    """Claude'un sohbet/plan üzerinden ürettiği analiz JSON'larını, API analiziyle aynı tablolara yazar."""
    check_key(authorization, wx_session)
    conn = _db(); cur = conn.cursor()
    for ddl in EXTRACT_DDL: cur.execute(ddl)
    ok, err = 0, []
    for k in body.kayitlar:
        did, out = k.get("drive_id"), k.get("analiz")
        if not did or not isinstance(out, dict): err.append(f"{did}: eksik"); continue
        try:
            cur.execute("SELECT kayit_tarihi, yon, dahili FROM kayit_dokumleri WHERE drive_id=%s", (did,)); r = cur.fetchone()
            if not r: err.append(f"{did}: kayıt yok"); continue
            kal = out.get("kalite", {}) if isinstance(out.get("kalite"), dict) else {}
            cur.execute("UPDATE kayit_dokumleri SET analiz=%s, genel_puan=%s, temsilci_sp=%s, sonuc=%s WHERE drive_id=%s",
                        (json.dumps(out, ensure_ascii=False), kal.get("genel_puan"), out.get("temsilci_speaker"), (out.get("sonuc") or None), did))
            if not out.get("ham"): extract_rows(cur, did, r[0], r[1], r[2], out)
            conn.commit(); ok += 1
        except Exception as e:
            conn.rollback(); err.append(f"{did}: {str(e)[:120]}")
    conn.close(); return {"yazilan": ok, "hata": err[:20]}

@app.get("/admin/dokumler")
def dokumler(since: str | None = None, until: str | None = None, yon: str | None = None, dahili: str | None = None, only_unanalyzed: bool = True, limit: int = 500,
             authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
    """Analiz için ham dökümleri toplu verir (Claude plan ile analiz edilecekler)."""
    check_key(authorization, wx_session)
    conn = _db(); cur = conn.cursor()
    where, args = ["durum='tamam'", "CHAR_LENGTH(dokum) > 200"], []
    if only_unanalyzed: where.append("analiz IS NULL")
    if since and _re2.fullmatch(r"\d{4}-\d{2}-\d{2}", since): where.append("kayit_tarihi>=%s"); args.append(since)
    if until and _re2.fullmatch(r"\d{4}-\d{2}-\d{2}", until): where.append("kayit_tarihi<=%s"); args.append(until)
    if yon in ("gelen", "giden"): where.append("yon=%s"); args.append(yon)
    if dahili: where.append("dahili=%s"); args.append(dahili)
    cur.execute("SELECT drive_id, kayit_tarihi, yon, dahili, telefon, dokum FROM kayit_dokumleri WHERE " + " AND ".join(where) + " ORDER BY kayit_tarihi DESC, id DESC LIMIT %s", args + [max(1, min(limit, 2000))])
    rows = [{"drive_id": r[0], "tarih": str(r[1]), "yon": r[2], "dahili": r[3], "telefon": r[4], "dokum": r[5]} for r in cur.fetchall()]
    conn.close(); return rows
