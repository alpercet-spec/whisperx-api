import hashlib
import hmac
import os
import secrets
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
DIARIZE_MODEL = os.environ.get("DIARIZE_MODEL", "pyannote/speaker-diarization-3.1")
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
        path = tmp.name

    try:
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
        os.remove(path)

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
