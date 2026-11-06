FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir torch==2.4.1 torchaudio==2.4.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir whisperx fastapi uvicorn[standard] python-multipart pymysql google-api-python-client google-auth

# Tüm uygulama dosyaları: yeni dosya eklense de Dockerfile değişmez, ağır katmanlar önbellekte kalır
COPY *.py *.md *.html ./

ENV HF_HOME=/models
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
