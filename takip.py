"""Günlük takip listesi: her sabah dünün çağrılarından takip listesi çıkarır (Claude API) ve Gmail ile gönderir."""
import base64, datetime as dt, html, json, os, re, threading, time, urllib.request, zoneinfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from fastapi import Cookie, Header, HTTPException
from fastapi.responses import HTMLResponse

TZ = zoneinfo.ZoneInfo("Europe/Istanbul")
TAKIP_HOUR = int(os.environ.get("TAKIP_HOUR", "10"))
TAKIP_GONDEREN = os.environ.get("TAKIP_GONDEREN", "alper@oleclinic.com")
TAKIP_ALICILAR = os.environ.get("TAKIP_ALICILAR", "saniye@iconestetik.com,alpercet@gmail.com,tugba@cetinkayabeauty.com")
TAKIP_MODEL = os.environ.get("TAKIP_MODEL", "")
TAKIP_LINK = os.environ.get("TAKIP_LINK", "https://whisperx.alpercetinkaya.com/takip")  # boşsa analiz modeli
AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
STATE = {"durum": "bekliyor", "sonraki": None, "son": None, "son_hata": None}
DDL = """CREATE TABLE IF NOT EXISTS takip_rapor (
  tarih DATE PRIMARY KEY, veri JSON, html MEDIUMTEXT, usd DECIMAL(8,4), olusturma DATETIME,
  gonderildi DATETIME NULL, alicilar VARCHAR(500) NULL, hata TEXT NULL)"""


def tarih_tr(d: dt.date) -> str:
    return f"{d.day} {AYLAR[d.month - 1]} {d.year}"


def register(app, check_key, _db, api_key_getter, model_getter, prices, sa_json_getter, daily_state, page_guard=None, panel_file=None):
    in_p, out_p = prices

    # ---------- veri ----------
    def _cagrilar(d: dt.date):
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT id, yon, dahili, telefon, dokum, analiz FROM kayit_dokumleri "
                    "WHERE durum='tamam' AND DATE(kayit_tarihi)=%s ORDER BY id", (d.isoformat(),))
        rows = cur.fetchall(); conn.close()
        out, gelen = [], 0
        for i, (rid, yon, dahili, tel, dokum, analiz) in enumerate(rows, 1):
            a = {}
            try: a = json.loads(analiz) if analiz else {}
            except Exception: pass
            if yon == "gelen": gelen += 1
            out.append(f"--- #{i} | {yon} | dahili {dahili or '-'} | tel {tel or '-'}\n"
                       f"özet: {a.get('ozet') or '-'}\nsonuç: {a.get('sonuc') or '-'}\n{(dokum or '').strip()[:6000]}")
        return "\n\n".join(out), gelen, len(rows)

    def _claude(prompt: str):
        model = TAKIP_MODEL or model_getter()
        payload = {"model": model, "max_tokens": 16000, "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(payload).encode(),
            headers={"content-type": "application/json", "x-api-key": api_key_getter(), "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=600) as r: data = json.loads(r.read())
        text = "".join(b.get("text", "") for b in data.get("content", [])).strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m: raise RuntimeError("Claude JSON döndürmedi: " + text[:300])
        u = data.get("usage", {})
        usd = u.get("input_tokens", 0) / 1e6 * in_p + u.get("output_tokens", 0) / 1e6 * out_p
        return json.loads(m.group(0)), round(usd, 4)

    # ---------- e-posta HTML ----------
    def _html(d: dt.date, v: dict) -> str:
        e = lambda s: html.escape(str(s or ""))
        def tablo(baslik, renk, aciklama, satirlar, kol3):
            if not satirlar: return ""
            h = (f'<h2 style="font-size:17px;margin:26px 0 4px;color:{renk}">{e(baslik)} ({len(satirlar)})</h2>'
                 f'<p style="margin:0 0 10px;color:#555;font-size:13px">{e(aciklama)}</p>'
                 '<table cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:13px">'
                 '<tr style="background:#f3f4f6;text-align:left">'
                 f'<th style="border:1px solid #e5e7eb;width:18%">Telefon</th><th style="border:1px solid #e5e7eb;width:32%">Ne istedi / ne oldu</th>'
                 f'<th style="border:1px solid #e5e7eb;width:36%">{kol3}</th><th style="border:1px solid #e5e7eb;width:14%">Kime</th></tr>')
            for s in satirlar:
                tel = e(s.get("telefon")); num = re.sub(r"\D", "", s.get("telefon", "").split("(")[0])
                h += ('<tr style="vertical-align:top">'
                      f'<td style="border:1px solid #e5e7eb"><a href="tel:{num}" style="color:#0f766e;text-decoration:none"><b>{tel}</b></a></td>'
                      f'<td style="border:1px solid #e5e7eb">{e(s.get("ne"))}</td>'
                      f'<td style="border:1px solid #e5e7eb">{e(s.get("yap"))}</td>'
                      f'<td style="border:1px solid #e5e7eb">{e(s.get("kime"))}</td></tr>')
            return h + "</table>"
        gun = GUNLER[d.weekday()]
        b = (f'<div style="font-family:Segoe UI,Arial,sans-serif;color:#1a1d21;max-width:960px">'
             f'<h1 style="font-size:21px;margin:0 0 6px">Günlük Takip Listesi — {tarih_tr(d)} {gun}</h1>'
             f'<p style="font-size:14px;margin:0 0 6px">{e(v.get("ozet_cumle"))}</p>'
             f'<p style="font-size:13px;color:#555;margin:0">Aramadan önce ClinixXO\'da randevusu oluşmuş mu, WhatsApp\'tan dönülmüş mü kontrol edin. Fiyat söylemeden önce güncel fiyat listesine bakın.</p>')
        b += tablo("🔴 Bugün dönülmeli", "#b91c1c", "Verilmiş söz, şikayet ya da dönüş bekleyen talep. Gecikirse şikayete dönüşür.", v.get("bugun") or [], "Dönüşte ne yapılmalı")
        b += tablo("🟠 Randevu almak isteyip alamayanlar", "#c2410c", "Randevuya kararlıydı; görüşme dolu takvim, itiraz ya da beklenen bir adımda kaldı.", v.get("randevu") or [], "Dönüşte ne yapılmalı")
        b += tablo("🟡 İlgili ama kararsız", "#a16207", "Bilgi ya da fiyat alıp randevusuz ayrılanlar. Önce WhatsApp'tan kısa mesaj, cevap yoksa arama. Tek teklif, nazik kapanış.", v.get("kararsiz") or [], "Dönüşte ne söylenebilir")
        if not any(v.get(k) for k in ("bugun", "randevu", "kararsiz")):
            b += '<p style="margin-top:20px">Dün takip gerektiren müşteri çıkmadı. 🎉</p>'
        notlar = list(v.get("cozulen") or [])
        notlar.append(f"Randevu alanlar ({v.get('randevu_alan', '-')}) ve randevusu değişenler ({v.get('degisen', '-')}) listeye alınmadı.")
        b += ('<h2 style="font-size:15px;margin:26px 0 6px">Listeye alınmayanlar ve not</h2><ul style="font-size:13px;color:#444;padding-left:18px">'
              + "".join(f"<li>{e(n)}</li>" for n in notlar) + "</ul>")
        b += (f'<p style="font-size:13px;margin-top:20px"><a href="{TAKIP_LINK}?tarih={d.isoformat()}" style="color:#0f766e">Listeyi panelde aç ve durumları işaretle →</a></p>'
              '<p style="font-size:12px;color:#888;margin-top:24px">Bu liste görüşme dökümlerinden otomatik çıkarıldı; müşteri bu arada WhatsApp\'tan ya da asistanla görüşmüş olabilir.</p></div>')
        return b

    # ---------- Gmail ----------
    def _gonder(konu: str, govde: str, alicilar: list[str]):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        info = json.loads(sa_json_getter())
        c = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/gmail.send"]).with_subject(TAKIP_GONDEREN)
        g = build("gmail", "v1", credentials=c, cache_discovery=False)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = konu; msg["From"] = f"Çağrı Merkezi Takip <{TAKIP_GONDEREN}>"; msg["To"] = ", ".join(alicilar)
        msg.attach(MIMEText(govde, "html", "utf-8"))
        g.users().messages().send(userId="me", body={"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}).execute()

    # ---------- iş ----------
    def calistir(d: dt.date, gonder=True, alicilar=None, yeniden=False):
        STATE.update(durum="calisiyor", son_hata=None)
        try:
            conn = _db(); cur = conn.cursor(); cur.execute(DDL); conn.commit()
            cur.execute("SELECT veri, html, usd FROM takip_rapor WHERE tarih=%s", (d.isoformat(),)); r = cur.fetchone()
            if r and r[0] and not yeniden:
                v, body, usd = json.loads(r[0]), r[1], float(r[2] or 0)
            else:
                metin, gelen, toplam = _cagrilar(d)
                if gelen == 0:
                    STATE.update(durum="tamam", son=f"{d}: gelen çağrı yok, gönderilmedi"); conn.close(); return {"gelen": 0}
                with open(os.path.join(os.path.dirname(__file__), "takip_prompt.md"), encoding="utf-8") as f: tpl = f.read()
                prompt = tpl.replace("{tarih}", d.isoformat()).replace("{tarih_tr}", tarih_tr(d)).replace("{cagrilar}", metin)
                v, usd = _claude(prompt)
                body = _html(d, v)
                cur.execute("REPLACE INTO takip_rapor (tarih, veri, html, usd, olusturma) VALUES (%s,%s,%s,%s,NOW())",
                            (d.isoformat(), json.dumps(v, ensure_ascii=False), body, usd)); conn.commit()
            sayi = sum(len(v.get(k) or []) for k in ("bugun", "randevu", "kararsiz"))
            if gonder:
                al = alicilar or [a.strip() for a in TAKIP_ALICILAR.split(",") if a.strip()]
                _gonder(f"Günlük Takip Listesi — {tarih_tr(d)} ({sayi} müşteri)", body, al)
                cur.execute("UPDATE takip_rapor SET gonderildi=NOW(), alicilar=%s, hata=NULL WHERE tarih=%s", (",".join(al), d.isoformat())); conn.commit()
            conn.close()
            STATE.update(durum="tamam", son=f"{d}: {sayi} müşteri" + (", gönderildi" if gonder else ", önizleme") + f", ${usd}")
            return {"tarih": d.isoformat(), "takip": sayi, "usd": usd, "gonderildi": gonder}
        except Exception as ex:
            STATE.update(durum="hata", son_hata=str(ex)[:500])
            try:
                conn = _db(); cur = conn.cursor(); cur.execute(DDL)
                cur.execute("INSERT INTO takip_rapor (tarih, hata, olusturma) VALUES (%s,%s,NOW()) ON DUPLICATE KEY UPDATE hata=VALUES(hata)", (d.isoformat(), str(ex)[:2000]))
                conn.commit(); conn.close()
            except Exception: pass
            raise

    def _scheduler():
        while True:
            now = dt.datetime.now(TZ)
            nxt = now.replace(hour=TAKIP_HOUR, minute=0, second=0, microsecond=0)
            if nxt <= now: nxt += dt.timedelta(days=1)
            STATE["sonraki"] = nxt.isoformat(timespec="minutes")
            time.sleep((nxt - now).total_seconds())
            for _ in range(12):  # gece görevi hâlâ sürüyorsa en fazla 1 saat bekle
                if daily_state.get("durum") != "calisiyor": break
                time.sleep(300)
            try: calistir((dt.datetime.now(TZ) - dt.timedelta(days=1)).date())
            except Exception as ex: print("takip hata:", ex, flush=True)

    if os.environ.get("TAKIP_AKTIF", "1") == "1" and os.environ.get("DB_HOST") and api_key_getter():
        threading.Thread(target=_scheduler, daemon=True).start()
        print(f"Takip listesi aktif: her gün {TAKIP_HOUR:02d}:00 → {TAKIP_ALICILAR}", flush=True)

    # ---------- uç noktalar ----------
    def _tarih(t):
        if t:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", t): raise HTTPException(400, "tarih YYYY-MM-DD")
            return dt.date.fromisoformat(t)
        return (dt.datetime.now(TZ) - dt.timedelta(days=1)).date()

    @app.post("/admin/takip/run")
    def takip_run(tarih: str | None = None, gonder: int = 0, alici: str | None = None, yeniden: int = 0, bekle: int = 1,
                  authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        """gonder=0: sadece üret (önizleme). alici: test için virgüllü adres listesi."""
        check_key(authorization, wx_session)
        d = _tarih(tarih); al = [a.strip() for a in alici.split(",")] if alici else None
        if STATE["durum"] == "calisiyor": raise HTTPException(409, "zaten çalışıyor")
        if bekle:
            try: return calistir(d, bool(gonder), al, bool(yeniden))
            except Exception as ex: raise HTTPException(500, str(ex)[:500])
        threading.Thread(target=calistir, args=(d, bool(gonder), al, bool(yeniden)), daemon=True).start()
        return {"basladi": d.isoformat()}

    @app.get("/admin/takip/durum")
    def takip_durum(authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        conn = _db(); cur = conn.cursor(); cur.execute(DDL)
        cur.execute("SELECT tarih, usd, olusturma, gonderildi, alicilar, hata FROM takip_rapor ORDER BY tarih DESC LIMIT 14")
        son = [{"tarih": str(r[0]), "usd": float(r[1] or 0), "olusturma": str(r[2]), "gonderildi": str(r[3]) if r[3] else None,
                "alicilar": r[4], "hata": r[5]} for r in cur.fetchall()]
        conn.close()
        return {**STATE, "saat": TAKIP_HOUR, "gonderen": TAKIP_GONDEREN, "alicilar": TAKIP_ALICILAR, "son_raporlar": son}

    @app.get("/takip", response_class=HTMLResponse)
    def takip_sayfa(wx_session: str | None = Cookie(None)):
        g = page_guard(wx_session) if page_guard else None
        return g or panel_file("takip.html")

    @app.get("/admin/takip/liste")
    def takip_liste(limit: int = 60, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        conn = _db(); cur = conn.cursor(); cur.execute(DDL)
        cur.execute("SELECT tarih, veri, gonderildi, hata FROM takip_rapor ORDER BY tarih DESC LIMIT %s", (max(1, min(limit, 365)),))
        out = []
        for t, v, g, h in cur.fetchall():
            v = json.loads(v) if v else {}
            grp = {k: v.get(k) or [] for k in ("bugun", "randevu", "kararsiz")}
            acik = sum(1 for k in grp for x in grp[k] if (x.get("durum") or "Bekliyor") == "Bekliyor")
            out.append({"tarih": str(t), "tarih_tr": tarih_tr(t), "gun": GUNLER[t.weekday()], "toplam": sum(len(x) for x in grp.values()),
                        "acik": acik, "bugun": len(grp["bugun"]), "gonderildi": bool(g), "hata": bool(h and not v)})
        conn.close(); return out

    @app.get("/admin/takip/veri")
    def takip_veri(tarih: str | None = None, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        d = _tarih(tarih)
        conn = _db(); cur = conn.cursor(); cur.execute(DDL)
        cur.execute("SELECT veri, gonderildi, alicilar FROM takip_rapor WHERE tarih=%s", (d.isoformat(),)); r = cur.fetchone(); conn.close()
        if not r or not r[0]: raise HTTPException(404, "liste yok")
        return {"tarih": d.isoformat(), "tarih_tr": tarih_tr(d), "gun": GUNLER[d.weekday()], "veri": json.loads(r[0]),
                "gonderildi": str(r[1]) if r[1] else None, "alicilar": r[2]}

    DURUMLAR = ("Bekliyor", "Arandı – randevu", "Arandı – randevu yok", "Ulaşılamadı", "Gerek yoktu")

    @app.post("/admin/takip/isaretle")
    def takip_isaretle(tarih: str, grup: str, i: int, durum: str, not_: str | None = None,
                       authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        if grup not in ("bugun", "randevu", "kararsiz") or durum not in DURUMLAR: raise HTTPException(400, "geçersiz")
        d = _tarih(tarih)
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT veri FROM takip_rapor WHERE tarih=%s FOR UPDATE", (d.isoformat(),)); r = cur.fetchone()
        if not r or not r[0]: conn.close(); raise HTTPException(404, "liste yok")
        v = json.loads(r[0])
        if not (0 <= i < len(v.get(grup) or [])): conn.close(); raise HTTPException(400, "satır yok")
        v[grup][i]["durum"] = durum
        v[grup][i]["durum_zaman"] = dt.datetime.now(TZ).strftime("%d.%m %H:%M")
        cur.execute("UPDATE takip_rapor SET veri=%s WHERE tarih=%s", (json.dumps(v, ensure_ascii=False), d.isoformat())); conn.commit(); conn.close()
        return {"ok": True}

    @app.get("/takip/eposta", response_class=HTMLResponse)
    def takip_eposta(tarih: str | None = None, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        d = _tarih(tarih)
        conn = _db(); cur = conn.cursor(); cur.execute(DDL)
        cur.execute("SELECT html FROM takip_rapor WHERE tarih=%s", (d.isoformat(),)); r = cur.fetchone(); conn.close()
        if not r or not r[0]: return HTMLResponse(f"<p style='font-family:sans-serif'>{tarih_tr(d)} için liste yok.</p>", 404)
        return HTMLResponse('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                            '<body style="margin:16px">' + r[0])
