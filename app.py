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
    path = raw_path + ".wav"

    try:
        # Ön işleme: mono 16 kHz, telefon bandı filtresi, ses seviyesi normalizasyonu
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
        for p in (raw_path, path):
            if os.path.exists(p):
                os.remove(p)

    segments = [
        {
            "start": round(s["start"], 2),
            "end": round(s["end"], 2),
            "speaker": s.get("speaker", "UNKNOWN"),
            "text": s["text"].strip(),
        }
        for s in result["segments"]
    ]
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

ANALYZE_PROMPT = """Sen bir estetik, dermatoloji ve kalıcı makyaj kliniğinin çağrı merkezi kalite uzmanısın.
Aşağıda bir telefon görüşmesinin otomatik dökümü var. Konuşmacılar SPEAKER_00 / SPEAKER_01 olarak etiketli;
önce hangisinin TEMSİLCİ (klinik çalışanı) hangisinin MÜŞTERİ olduğunu bağlamdan belirle.
Döküm 8 kHz telefon kaydından geldiği için kelime hataları olabilir; klinik terimlerini bağlamdan düzelt
(ör. "kaş bildirme" = kaş microblading, "biyo-link" = microblading, "kıl tekniği", "pudralama", "kontrol seansı", "dolgu", "botoks", "lazer epilasyon").

Sadece aşağıdaki JSON şemasında yanıt ver, başka hiçbir şey yazma:
{
  "temsilci": "SPEAKER_00 veya SPEAKER_01",
  "ozet": "2-3 cümle görüşme özeti",
  "musteri_talebi": "müşteri ne istedi",
  "sonuc": "randevu alındı / bilgi verildi / WhatsApp'a yönlendirildi / çözümsüz / diğer",
  "puanlar": {
    "karsilama": 0-10,
    "dinleme_ve_anlama": 0-10,
    "bilgi_dogrulugu": 0-10,
    "satis_ve_yonlendirme": 0-10,
    "nezaket_ve_ton": 0-10,
    "kapanis": 0-10
  },
  "genel_puan": 0-100,
  "guclu_yonler": ["..."],
  "gelistirilecekler": ["..."],
  "kacirilan_firsat": "varsa satış/randevu fırsatı, yoksa null",
  "uyari": "ciddi hata, yanlış bilgi, kaba davranış varsa; yoksa null"
}

GÖRÜŞME DÖKÜMÜ:
"""


class AnalyzeIn(BaseModel):
    dialogue: str


@app.post("/analyze")
def analyze(
    body: AnalyzeIn,
    authorization: str | None = Header(None),
    wx_session: str | None = Cookie(None),
):
    check_key(authorization, wx_session)
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY ayarlanmadı")

    payload = {
        "model": ANALYZE_MODEL,
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": ANALYZE_PROMPT + body.dialogue[:30000]}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"Claude API hatası: {e.read().decode()[:300]}")

    text = "".join(b.get("text", "") for b in data.get("content", []))
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ham": text}


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
