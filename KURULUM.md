# WhisperX API – Kurulum (Easypanel)

## 1. Hugging Face token al (bir kez, ücretsiz)
1. https://huggingface.co → hesap aç / giriş yap
2. Şu iki sayfada "Agree and access repository" butonuna bas:
   - https://huggingface.co/pyannote/speaker-diarization-3.1
   - https://huggingface.co/pyannote/segmentation-3.0
3. https://huggingface.co/settings/tokens → "New token" → Read yetkisi → oluştur, kopyala (hf_... ile başlar)

## 2. Dosyaları sunucuya at
Sunucuda terminalde:
```
mkdir -p /root/whisperx-api
```
Bu klasöre `Dockerfile` ve `app.py` dosyalarını koy (scp, Easypanel file manager veya nano ile yapıştır).

## 3. Easypanel'de servis oluştur
1. Proje seç (örn. asistan) → **+ Service → App** → isim: `whisperx`
2. **Source** → **Docker Build**:
   - Build path: `/root/whisperx-api` (ya da GitHub'a koyduysan repo)
3. **Environment**:
```
HF_TOKEN=hf_BURAYA_TOKEN
API_KEY=istedigin-gizli-anahtar
WHISPER_MODEL=large-v3-turbo
COMPUTE_TYPE=int8
THREADS=6
LANGUAGE=tr
```
4. **Mounts** → Volume: name `whisperx-models`, mount path `/models`
5. **Domains** → `whisperx.alpercetinkaya.com`, port `8000`, HTTPS
6. **Resources** (varsa) → Memory limit en az 8 GB
7. **Deploy**

İlk build 5-10 dk, ilk açılışta modeller iner (~3 GB) 5-10 dk daha. Logs'ta `Hazır.` görünce çalışıyor.

## 4. Test
```
curl https://whisperx.alpercetinkaya.com/health

curl https://whisperx.alpercetinkaya.com/transcribe \
  -H "Authorization: Bearer istedigin-gizli-anahtar" \
  -F file=@kayit.mp3 \
  -F language=tr \
  -F min_speakers=2 -F max_speakers=2
```

Dönen JSON:
```
{
  "text": "tüm metin...",
  "dialogue": "[SPEAKER_00] Alo iyi günler...\n[SPEAKER_01] Buyrun...",
  "segments": [{"start":0.5,"end":3.2,"speaker":"SPEAKER_00","text":"..."}]
}
```
Çağrı merkezi için `min_speakers=2 max_speakers=2` ver; `dialogue` alanı doğrudan kalite analizi prompt'una gider.

## 5. n8n'de kullanım
HTTP Request node:
- Method: POST
- URL: `https://whisperx.alpercetinkaya.com/transcribe`
- Header: `Authorization` = `Bearer istedigin-gizli-anahtar`
- Body: Form-Data → `file` (Binary, önceki node'dan), `language` = `tr`, `min_speakers` = `2`, `max_speakers` = `2`
- Timeout: 600000 (10 dk) — uzun kayıtlar için

## Notlar
- Aynı anda tek kayıt işlenir (sıraya girer). 10 dk ses ≈ 4-8 dk işlem (8 çekirdek CPU).
- Daha hızlı ama biraz daha hatalı: `WHISPER_MODEL=medium`
- Konuşmacı etiketleri her kayıtta SPEAKER_00/01 olarak gelir; hangisinin temsilci olduğunu ilk cümleden ("Alo, X Klinik") analiz prompt'unda belirlet.
