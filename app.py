import os
import tempfile
import threading

import whisperx
from whisperx.diarize import DiarizationPipeline
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile

HF_TOKEN = os.environ["HF_TOKEN"]
API_KEY = os.environ.get("API_KEY", "")
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
try:
    diarize_model = DiarizationPipeline(token=HF_TOKEN, device=DEVICE)
except TypeError:
    diarize_model = DiarizationPipeline(use_auth_token=HF_TOKEN, device=DEVICE)
print("Hazır.", flush=True)


def check_key(authorization: str | None):
    if API_KEY and authorization != f"Bearer {API_KEY}":
        raise HTTPException(401, "Geçersiz API key")


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
):
    check_key(authorization)

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
