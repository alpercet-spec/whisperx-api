"""Bilgi tabanı 2. katman: aday satırları Claude ile birleştir (tekilleştir + çelişki tespiti),
onay paneli uçları ve Retell agent için Markdown export."""
import io, json, os, threading, time, urllib.request, urllib.error, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import Cookie, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse
from pydantic import BaseModel

KB_DDL = """CREATE TABLE IF NOT EXISTS bilgi_tabani (
  id INT AUTO_INCREMENT PRIMARY KEY, kategori VARCHAR(16) NOT NULL, konu VARCHAR(48), metin TEXT NOT NULL, ek JSON NULL,
  destek INT DEFAULT 1, ornekler JSON NULL, celiski TEXT NULL, durum VARCHAR(12) DEFAULT 'bekliyor', nihai TEXT NULL,
  olusturma TIMESTAMP DEFAULT CURRENT_TIMESTAMP, guncelleme TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX(kategori, konu, durum)) CHARACTER SET utf8mb4"""

KB_STOP = {"flag": False}
KB_STATE = {"durum": "bekliyor", "adim": None, "cagri": 0, "input_tokens": 0, "output_tokens": 0, "tahmini_usd": 0.0, "son_hata": None, "uretilen": {}}

ORTAK = """Sen bir estetik/dermatoloji/kalıcı makyaj kliniğinin çağrı merkezi görüşmelerinden çıkarılmış ham adayları
birleştiren bir editörsün. Amaç: aynı müşteri temsilcisi gibi konuşacak bir yapay zeka agent için temiz bir bilgi tabanı.
KURALLAR:
- Aynı anlama gelen satırları TEK bir net, genel geçer Türkçe cümlede birleştir. Kısa ve doğrudan yaz.
- FİYAT, TUTAR, İNDİRİM, TL içeren bilgileri tamamen ATLA (fiyat canlı servisten gelecek).
- Tek bir müşteriye özel, tek seferlik durumları (belirli kişinin randevusu, kişisel şikayet) ATLA; sadece agent'ın öğrenmesi gereken genel bilgi/kural/kalıp kalsın.
- Birbiriyle ÇELİŞEN satırlar varsa ikisini de ayrı ayrı tut ve her birinin "celiski" alanına çelişkiyi tek cümleyle yaz (örn. "Başka görüşmelerde kontrolün ücretli olduğu söyleniyor"). Çelişki yoksa null.
- Her çıktı elemanının "kaynak" alanına birleştirdiğin girdi satırlarının [idx] numaralarını yaz (en az 1).
- Girdideki (n=…) o ifadenin kaç görüşmede geçtiğidir; önem sırasına göre sırala (çok geçen üstte).
- SADECE tek satırlık geçerli JSON dizi döndür, açıklama yazma, markdown kullanma."""

PROMPTS = {
 "bilgi": ORTAK + """
Kategori: OLGUSAL BİLGİ (konu: {konu}). Klinik, doktorlar, işlem detayları, süreler, politikalar, iletişim gibi gerçekler.
Çıktı elemanı: {"metin":"...", "kaynak":[idx,...], "celiski":null}""",
 "kural": ORTAK + """
Kategori: KURAL / POLİTİKA. Temsilcinin uyduğu "şunu yap / şunu yapma / şu durumda şöyle davran" kuralları.
Her kuralı emir kipinde, agent'a talimat gibi yaz (örn. "Botoks randevusu alırken hamilelik durumunu sor").
Çıktı elemanı: {"metin":"...", "kaynak":[idx,...], "celiski":null}""",
 "soru": ORTAK + """
Kategori: TEMSİLCİNİN MÜŞTERİYE SORDUĞU STANDART SORULAR. Aynı amaçlı soruları birleştir; metin olarak temsilcinin gerçekten kullandığı en doğal söyleyişi koru (yeniden yazma).
"varyantlar" alanına 1-3 farklı söyleyişi ekle. "amac" alanına sorunun ne için sorulduğunu 3-6 kelimeyle yaz.
Çıktı elemanı: {"metin":"...", "varyantlar":["..."], "amac":"...", "kaynak":[idx,...], "celiski":null}""",
 "kalip": ORTAK + """
Kategori: TEMSİLCİ KONUŞMA KALIPLARI (tür: {konu}). Aynı işlevi gören cümleleri birleştir; metin olarak temsilcinin GERÇEKTEN söylediği en temsilî cümleyi koru (düzeltme, yeniden yazma; sadece döküm hatası varsa düzelt).
"varyantlar" alanına 1-3 farklı söyleyişi ekle. Fiyat geçen cümlelerde tutarı [FİYAT] ile değiştir, cümleyi atma.
Çıktı elemanı: {"metin":"...", "varyantlar":["..."], "kaynak":[idx,...], "celiski":null}""",
 "hizmet": ORTAK + """
Kategori: HİZMET ADI EŞLEŞTİRME. Girdi: "müşteri ifadesi → normalize ad". Amaç: kliniğin verdiği her hizmet için tek bir kanonik ad ve müşterilerin bu hizmeti nasıl adlandırdığının listesi.
- Aynı hizmeti anlatan ifadeleri tek elemanda topla; "metin" = kanonik hizmet adı (Türkçe, fiyat listesinde geçebilecek biçimde, örn. "Dudak Renklendirme", "Botoks (tam yüz)", "BBL Lazer").
- "diger" normalize adını kullanma; gerçek hizmet adını çıkar (BBL, Baby Face, Kolajen, U-Lift, ön görüşme vb.).
- "varyantlar" = müşteri ifadeleri (tekilleştirilmiş, en fazla 10). "grup" = üst kategori: kalici_makyaj | botoks | dolgu | lazer | cilt | mezoterapi | cerrahi | kontrol | diger.
Çıktı elemanı: {"metin":"...", "varyantlar":["..."], "grup":"...", "kaynak":[idx,...], "celiski":null}""",
 "soru_cevap": ORTAK + """
Kategori: MÜŞTERİ SORUSU → TEMSİLCİ CEVABI. Aynı soruyu (farklı sözcüklerle) soran satırları birleştir.
"metin" = soru (genel biçimde), "cevap" = temsilcinin verdiği cevapların birleştirilmiş, tutarlı, kısa hali (fiyat varsa [FİYAT] yaz, tutar yazma).
Cevaplar birbirini tutmuyorsa en sık verilen cevabı yaz ve "celiski" alanında diğerini belirt.
Çıktı elemanı: {"metin":"soru", "cevap":"...", "kaynak":[idx,...], "celiski":null}""",
}

def register(app, check_key, _db, api_key_getter, model_getter, prices, page_guard, panel_file):
    IN_PRICE, OUT_PRICE = prices

    def _claude(prompt, max_tokens=16000):
        payload = {"model": model_getter(), "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(payload).encode(),
            headers={"content-type": "application/json", "x-api-key": api_key_getter(), "anthropic-version": "2023-06-01"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=300) as r: data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                if attempt < 2 and e.code in (429, 529, 500, 503): time.sleep(8 * (attempt + 1)); continue
                raise
        u = data.get("usage", {})
        KB_STATE["cagri"] += 1; KB_STATE["input_tokens"] += u.get("input_tokens", 0); KB_STATE["output_tokens"] += u.get("output_tokens", 0)
        KB_STATE["tahmini_usd"] = round(KB_STATE["input_tokens"] / 1e6 * IN_PRICE + KB_STATE["output_tokens"] / 1e6 * OUT_PRICE, 3)
        text = "".join(b.get("text", "") for b in data.get("content", [])).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            text = text.rsplit("```", 1)[0].strip()
        s, e = text.find("["), text.rfind("]")
        if s < 0 or e < 0: raise ValueError("JSON dizi bulunamadı")
        return json.loads(text[s:e + 1])

    def _consolidate(kategori, konu, items, chunk=90, max_level=4):
        """items: [{"metin","n","ids":[...],"ek":{}}] -> aynı yapıda birleştirilmiş liste. Çok seviyeli: parça parça birleştir, sonra birleşenleri tekrar birleştir."""
        level = 0
        while True:
            level += 1
            chunks = [items[i:i + chunk] for i in range(0, len(items), chunk)]
            if level > 1 and len(chunks) == 1 and len(items) <= chunk: pass
            def run(part):
                lines = "\n".join(f"[{i}] (n={it['n']}) {it['metin']}" + (f" → {it['ek'].get('norm')}" if kategori == 'hizmet' and it.get('ek', {}).get('norm') else "") for i, it in enumerate(part))
                prompt = PROMPTS[kategori].replace("{konu}", str(konu or "")) + "\n\nGİRDİ:\n" + lines
                out = _claude(prompt)
                res = []
                for o in out:
                    if not isinstance(o, dict) or not o.get("metin"): continue
                    src = [i for i in (o.get("kaynak") or []) if isinstance(i, int) and 0 <= i < len(part)]
                    if not src: src = []
                    n = sum(part[i]["n"] for i in src) or 1
                    ids = []
                    for i in src:
                        for d in part[i]["ids"]:
                            if d not in ids: ids.append(d)
                    ek = {}
                    for k in ("varyantlar", "amac", "grup", "cevap"):
                        if o.get(k): ek[k] = o[k]
                    # alt seviyeden gelen varyantları koru
                    if "varyantlar" in ek or kategori in ("kalip", "soru", "hizmet"):
                        vs = list(ek.get("varyantlar") or [])
                        for i in src:
                            for v in (part[i].get("ek") or {}).get("varyantlar") or []:
                                if v not in vs and v != o["metin"]: vs.append(v)
                        if vs: ek["varyantlar"] = vs[:10]
                    cel = o.get("celiski") or None
                    for i in src:
                        pc = (part[i].get("ek") or {}).get("celiski")
                        if pc and not cel: cel = pc
                    if cel: ek["celiski"] = cel
                    res.append({"metin": str(o["metin"]).strip(), "n": n, "ids": ids[:6], "ek": ek})
                return res
            merged = []
            if len(chunks) == 1:
                merged = run(chunks[0])
            else:
                with ThreadPoolExecutor(max_workers=4) as ex:
                    futs = {ex.submit(run, c): k for k, c in enumerate(chunks)}
                    results = [None] * len(chunks)
                    for f in as_completed(futs): results[futs[f]] = f.result()
                for r in results: merged.extend(r or [])
            KB_STATE["adim"] = f"{kategori}/{konu or '-'} seviye {level}: {len(items)} → {len(merged)}"
            if len(chunks) == 1 or level >= max_level or len(merged) >= len(items) * 0.85:
                return merged
            items = merged

    def _items_from_rows(rows, ek_fn=None):
        """rows: (metin, drive_id) -> distinct metin, count, ids."""
        agg = {}
        for r in rows:
            m = (r[0] or "").strip()
            if len(m) < 4: continue
            key = m.lower()
            a = agg.setdefault(key, {"metin": m, "n": 0, "ids": [], "ek": (ek_fn(r) if ek_fn else {})})
            a["n"] += 1
            if r[1] and r[1] not in a["ids"] and len(a["ids"]) < 6: a["ids"].append(r[1])
        return sorted(agg.values(), key=lambda x: -x["n"])

    def _save(cur, kategori, konu, merged):
        for m in merged:
            ek = dict(m.get("ek") or {}); cel = ek.pop("celiski", None)
            cur.execute("INSERT INTO bilgi_tabani (kategori,konu,metin,ek,destek,ornekler,celiski) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (kategori, konu, m["metin"][:4000], json.dumps(ek, ensure_ascii=False) if ek else None, m["n"], json.dumps(m["ids"]), cel))
        KB_STATE["uretilen"][f"{kategori}/{konu or '-'}"] = len(merged)

    def _build_worker(kategoriler, force):
        KB_STOP["flag"] = False
        KB_STATE.update(durum="calisiyor", adim="hazırlık", cagri=0, input_tokens=0, output_tokens=0, tahmini_usd=0.0, son_hata=None, uretilen={}, baslangic=time.time())
        try:
            conn = _db(); cur = conn.cursor(); cur.execute(KB_DDL); conn.commit()
            for kat in kategoriler:
                if KB_STOP["flag"]: break
                cur.execute("DELETE FROM bilgi_tabani WHERE kategori=%s" + ("" if force else " AND durum='bekliyor'"), (kat,)); conn.commit()
                groups = []  # (konu, items)
                if kat == "bilgi":
                    cur.execute("SELECT konu FROM bilgi_adaylari GROUP BY konu HAVING COUNT(*)>=3")
                    for (konu,) in cur.fetchall():
                        cur.execute("SELECT bilgi, drive_id FROM bilgi_adaylari WHERE konu=%s AND (guven IS NULL OR guven<>'dusuk')", (konu,))
                        groups.append((konu, _items_from_rows(cur.fetchall())))
                elif kat == "kural":
                    cur.execute("SELECT kural, drive_id FROM kural_adaylari"); groups.append((None, _items_from_rows(cur.fetchall())))
                elif kat == "soru":
                    cur.execute("SELECT soru, drive_id FROM standart_sorular"); groups.append((None, _items_from_rows(cur.fetchall())))
                elif kat == "kalip":
                    cur.execute("SELECT tur FROM kaliplar GROUP BY tur HAVING COUNT(*)>=5")
                    for (tur,) in cur.fetchall():
                        cur.execute("SELECT cumle, drive_id FROM kaliplar WHERE tur=%s", (tur,))
                        items = _items_from_rows(cur.fetchall())[:500]
                        groups.append((tur, items))
                elif kat == "hizmet":
                    cur.execute("SELECT musteri_ifadesi, drive_id, normalize_ad FROM hizmet_eslestirme WHERE musteri_ifadesi IS NOT NULL")
                    groups.append((None, _items_from_rows(cur.fetchall(), lambda r: {"norm": r[2]})))
                elif kat == "soru_cevap":
                    cur.execute("SELECT CONCAT('S: ', musteri_sorusu, ' | C: ', COALESCE(temsilci_cevabi,'')), drive_id FROM soru_cevap WHERE bilgi_icerir=1 ORDER BY kayit_tarihi DESC LIMIT 4000")
                    groups.append((None, _items_from_rows(cur.fetchall())))
                for konu, items in groups:
                    if not items or KB_STOP["flag"]: continue
                    try:
                        merged = _consolidate(kat, konu, items)
                        _save(cur, kat, konu, merged); conn.commit()
                    except Exception as e:
                        conn.rollback(); KB_STATE["son_hata"] = f"{kat}/{konu}: {str(e)[:300]}"
            conn.close(); KB_STATE["durum"] = "tamam"; KB_STATE["adim"] = "bitti"
        except Exception as e:
            KB_STATE.update(durum="hata", son_hata=str(e)[:500])
        KB_STATE["sure_sn"] = round(time.time() - KB_STATE["baslangic"])

    # ---------------- uçlar ----------------
    @app.post("/admin/kb/build")
    def kb_build(kategoriler: str = "bilgi,kural,hizmet,soru,kalip,soru_cevap", force: bool = False, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        if not api_key_getter(): raise HTTPException(500, "ANTHROPIC_API_KEY ayarlanmadı")
        if KB_STATE["durum"] == "calisiyor": return KB_STATE
        kats = [k.strip() for k in kategoriler.split(",") if k.strip() in PROMPTS]
        threading.Thread(target=_build_worker, args=(kats, force), daemon=True).start()
        return {"baslatildi": True, "kategoriler": kats, "force": force}

    @app.post("/admin/kb/stop")
    def kb_stop(authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session); KB_STOP["flag"] = True; return {"durduruluyor": True, "not": "çalışan grup bitince durur"}

    @app.get("/admin/kb/dump")
    def kb_dump(kategori: str, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        """Aday satırları tekilleştirilmiş (metin, n, ids, ek) listesi olarak verir; dışarıda birleştirme için."""
        check_key(authorization, wx_session)
        conn = _db(); cur = conn.cursor(); out = {}
        if kategori == "bilgi":
            cur.execute("SELECT konu FROM bilgi_adaylari GROUP BY konu HAVING COUNT(*)>=3")
            for (konu,) in cur.fetchall():
                cur.execute("SELECT bilgi, drive_id FROM bilgi_adaylari WHERE konu=%s AND (guven IS NULL OR guven<>'dusuk')", (konu,)); out[konu] = _items_from_rows(cur.fetchall())
        elif kategori == "kural":
            cur.execute("SELECT kural, drive_id FROM kural_adaylari"); out["-"] = _items_from_rows(cur.fetchall())
        elif kategori == "soru":
            cur.execute("SELECT soru, drive_id FROM standart_sorular"); out["-"] = _items_from_rows(cur.fetchall())
        elif kategori == "kalip":
            cur.execute("SELECT tur FROM kaliplar GROUP BY tur HAVING COUNT(*)>=5")
            for (tur,) in cur.fetchall():
                cur.execute("SELECT cumle, drive_id FROM kaliplar WHERE tur=%s", (tur,)); out[tur] = _items_from_rows(cur.fetchall())
        elif kategori == "hizmet":
            cur.execute("SELECT musteri_ifadesi, drive_id, normalize_ad FROM hizmet_eslestirme WHERE musteri_ifadesi IS NOT NULL"); out["-"] = _items_from_rows(cur.fetchall(), lambda r: {"norm": r[2]})
        elif kategori == "soru_cevap":
            cur.execute("SELECT CONCAT('S: ', musteri_sorusu, ' | C: ', COALESCE(temsilci_cevabi,'')), drive_id FROM soru_cevap WHERE bilgi_icerir=1 ORDER BY kayit_tarihi DESC LIMIT 4000"); out["-"] = _items_from_rows(cur.fetchall())
        else: raise HTTPException(400, "geçersiz kategori")
        conn.close(); return out

    class KbImport(BaseModel):
        kategori: str
        konu: str | None = None
        satirlar: list[dict]   # {"metin","n","ids","ek":{...,"celiski"}}
        sil: bool = True       # aynı kategori/konu 'bekliyor' satırlarını önce sil

    @app.post("/admin/kb/import")
    def kb_import(body: KbImport, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        if body.kategori not in PROMPTS: raise HTTPException(400, "geçersiz kategori")
        conn = _db(); cur = conn.cursor(); cur.execute(KB_DDL)
        if body.sil:
            cur.execute("DELETE FROM bilgi_tabani WHERE kategori=%s AND durum='bekliyor' AND " + ("konu=%s" if body.konu else "konu IS NULL"), (body.kategori, body.konu) if body.konu else (body.kategori,))
        rows = [{"metin": r.get("metin", ""), "n": int(r.get("n") or 1), "ids": list(r.get("ids") or [])[:6], "ek": r.get("ek") or {}} for r in body.satirlar if r.get("metin")]
        _save(cur, body.kategori, body.konu, rows); conn.commit(); conn.close()
        return {"yazilan": len(rows)}

    @app.get("/admin/kb/status")
    def kb_status(authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session); return KB_STATE

    @app.get("/admin/kb/ozet")
    def kb_ozet(authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        conn = _db(); cur = conn.cursor(); cur.execute(KB_DDL); conn.commit()
        cur.execute("SELECT kategori, konu, durum, COUNT(*) FROM bilgi_tabani GROUP BY 1,2,3"); rows_ = cur.fetchall()
        out = {}
        cur.execute("SELECT JSON_UNQUOTE(JSON_EXTRACT(ek,'$.alan')), COUNT(*) FROM bilgi_tabani WHERE durum='bekliyor' GROUP BY 1")
        out["_alan"] = {r[0] or "-": r[1] for r in cur.fetchall()}
        for kat, konu, durum, n in rows_:
            g = out.setdefault(kat, {}).setdefault(konu or "-", {"bekliyor": 0, "onayli": 0, "duzeltildi": 0, "reddedildi": 0})
            g[durum] = n
        conn.close(); return out

    @app.get("/admin/kb/list")
    def kb_list(kategori: str = "bilgi", konu: str | None = None, durum: str | None = None, q: str | None = None, limit: int = 500, alan: str | None = None, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        conn = _db(); cur = conn.cursor()
        sql = "SELECT id,kategori,konu,metin,ek,destek,ornekler,celiski,durum,nihai FROM bilgi_tabani WHERE kategori=%s"; args = [kategori]
        if konu and konu != "-": sql += " AND konu=%s"; args.append(konu)
        if konu == "-": sql += " AND konu IS NULL"
        if durum: sql += " AND durum=%s"; args.append(durum)
        if q: sql += " AND (metin LIKE %s OR nihai LIKE %s)"; args += [f"%{q}%", f"%{q}%"]
        if alan in ("kalici_makyaj", "klinik"): sql += " AND JSON_UNQUOTE(JSON_EXTRACT(ek,'$.alan'))=%s"; args.append(alan)
        sql += " ORDER BY (JSON_EXTRACT(ek,'$.soru') IS NOT NULL) DESC, (celiski IS NOT NULL) DESC, destek DESC, id LIMIT %s"; args.append(limit)
        cur.execute(sql, args)
        rows = [{"id": r[0], "kategori": r[1], "konu": r[2], "metin": r[3], "ek": json.loads(r[4]) if r[4] else {}, "destek": r[5],
                 "ornekler": json.loads(r[6]) if r[6] else [], "celiski": r[7], "durum": r[8], "nihai": r[9]} for r in cur.fetchall()]
        conn.close(); return rows

    class KbUpdate(BaseModel):
        durum: str | None = None
        nihai: str | None = None
        ek: dict | None = None

    @app.post("/admin/kb/item/{item_id}")
    def kb_update(item_id: int, body: KbUpdate, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        if body.durum and body.durum not in ("bekliyor", "onayli", "duzeltildi", "reddedildi"): raise HTTPException(400, "geçersiz durum")
        conn = _db(); cur = conn.cursor()
        sets, args = [], []
        if body.durum: sets.append("durum=%s"); args.append(body.durum)
        if body.nihai is not None: sets.append("nihai=%s"); args.append(body.nihai.strip() or None)
        if body.ek is not None: sets.append("ek=%s"); args.append(json.dumps(body.ek, ensure_ascii=False))
        if not sets: return {"ok": False}
        args.append(item_id); cur.execute(f"UPDATE bilgi_tabani SET {', '.join(sets)} WHERE id=%s", args); conn.commit(); conn.close()
        return {"ok": True}

    class KbBulk(BaseModel):
        ids: list[int]
        durum: str

    @app.post("/admin/kb/bulk")
    def kb_bulk(body: KbBulk, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        if body.durum not in ("bekliyor", "onayli", "reddedildi") or not body.ids: raise HTTPException(400, "geçersiz")
        conn = _db(); cur = conn.cursor()
        cur.execute(f"UPDATE bilgi_tabani SET durum=%s WHERE id IN ({','.join(['%s'] * len(body.ids))})", [body.durum] + body.ids)
        n = cur.rowcount; conn.commit(); conn.close(); return {"guncellenen": n}

    class KbNew(BaseModel):
        kategori: str
        konu: str | None = None
        metin: str
        ek: dict | None = None

    @app.post("/admin/kb/new")
    def kb_new(body: KbNew, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        if body.kategori not in PROMPTS: raise HTTPException(400, "geçersiz kategori")
        conn = _db(); cur = conn.cursor(); cur.execute(KB_DDL)
        cur.execute("INSERT INTO bilgi_tabani (kategori,konu,metin,ek,destek,ornekler,durum) VALUES (%s,%s,%s,%s,0,'[]','onayli')",
                    (body.kategori, body.konu, body.metin.strip(), json.dumps(body.ek, ensure_ascii=False) if body.ek else None))
        conn.commit(); nid = cur.lastrowid; conn.close(); return {"id": nid}

    @app.get("/admin/dokum/{drive_id}")
    def dokum(drive_id: str, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT kayit_tarihi, yon, dahili, genel_puan, dokum FROM kayit_dokumleri WHERE drive_id=%s", (drive_id,))
        r = cur.fetchone(); conn.close()
        if not r: raise HTTPException(404, "bulunamadı")
        return {"drive_id": drive_id, "tarih": str(r[0]), "yon": r[1], "dahili": r[2], "puan": r[3], "dokum": r[4]}

    # ---------------- export ----------------
    KAT_BASLIK = {"bilgi": "Klinik Bilgi Tabanı", "kural": "Kurallar ve Politikalar", "hizmet": "Hizmet Adları ve Müşteri İfadeleri",
                  "soru": "Temsilcinin Standart Soruları", "kalip": "Konuşma Kalıpları", "soru_cevap": "Sık Sorulan Sorular ve Cevaplar"}

    def _export_md(cur, kat):
        cur.execute("SELECT konu, COALESCE(nihai, metin), ek, destek, celiski FROM bilgi_tabani WHERE kategori=%s AND durum IN ('onayli','duzeltildi') ORDER BY konu, destek DESC", (kat,))
        rows = cur.fetchall()
        out = [f"# {KAT_BASLIK.get(kat, kat)}", ""]
        if kat in ("bilgi", "kalip"):
            cur_konu = object()
            for konu, metin, ek, destek, cel in rows:
                if konu != cur_konu: cur_konu = konu; out += ["", f"## {konu or 'Genel'}", ""]
                ek = json.loads(ek) if ek else {}
                line = f"- {metin}"
                if ek.get("varyantlar"): line += "\n  - Diğer söyleyişler: " + " | ".join(ek["varyantlar"][:4])
                out.append(line)
        elif kat == "hizmet":
            grp = {}
            for konu, metin, ek, destek, cel in rows:
                ek = json.loads(ek) if ek else {}
                grp.setdefault(ek.get("grup") or "diger", []).append((metin, ek.get("varyantlar") or []))
            for g, lst in grp.items():
                out += ["", f"## {g}", ""]
                for metin, vs in lst: out.append(f"- **{metin}** — müşteri ifadeleri: " + (", ".join(vs) if vs else "-"))
        elif kat == "soru_cevap":
            for konu, metin, ek, destek, cel in rows:
                ek = json.loads(ek) if ek else {}
                out += [f"**S:** {metin}", f"**C:** {ek.get('cevap', '')}", ""]
        elif kat == "soru":
            for konu, metin, ek, destek, cel in rows:
                ek = json.loads(ek) if ek else {}
                line = f"- {metin}" + (f" _(amaç: {ek['amac']})_" if ek.get("amac") else "")
                if ek.get("varyantlar"): line += "\n  - " + " | ".join(ek["varyantlar"][:3])
                out.append(line)
        else:
            for konu, metin, ek, destek, cel in rows: out.append(f"- {metin}")
        return "\n".join(out) + "\n"

    def _export_ornekler(cur, n=30):
        cur.execute("""SELECT g.drive_id, g.kayit_tarihi, g.dahili, g.gorusme_turu, g.genel_puan, k.dokum FROM gorusme_ozet g JOIN kayit_dokumleri k ON k.drive_id=g.drive_id
                       WHERE g.etiket='iyi_ornek' AND g.yon='gelen' ORDER BY g.genel_puan DESC, g.kayit_tarihi DESC LIMIT %s""", (n,))
        out = ["# Örnek Görüşmeler (en iyi puanlı gelen çağrılar)", ""]
        for i, (did, tarih, dahili, tur, puan, dokum) in enumerate(cur.fetchall(), 1):
            out += [f"## Örnek {i} — {tur or ''} · puan {puan} · dahili {dahili} · {tarih}", "", "```", (dokum or "").strip(), "```", ""]
        return "\n".join(out)

    @app.get("/admin/kb/export")
    def kb_export(kategori: str | None = None, ornek: int = 30, authorization: str | None = Header(None), wx_session: str | None = Cookie(None)):
        check_key(authorization, wx_session)
        conn = _db(); cur = conn.cursor(); cur.execute(KB_DDL)
        if kategori:
            txt = _export_md(cur, kategori); conn.close()
            return PlainTextResponse(txt, media_type="text/markdown; charset=utf-8")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for kat in PROMPTS: z.writestr(f"{kat}.md", _export_md(cur, kat))
            z.writestr("ornek_gorusmeler.md", _export_ornekler(cur, ornek))
        conn.close(); buf.seek(0)
        return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=agent_bilgi_tabani.zip"})

    @app.get("/bilgi", response_class=HTMLResponse)
    def kb_page(wx_session: str | None = Cookie(None)):
        guard = page_guard(wx_session)
        if guard: return guard
        return panel_file("kb.html")

    @app.get("/bilgi/mobil", response_class=HTMLResponse)
    def kb_mobile(wx_session: str | None = Cookie(None)):
        guard = page_guard(wx_session)
        if guard: return guard
        return panel_file("kb_mobil.html")

    return {"consolidate": _consolidate, "items_from_rows": _items_from_rows, "build_worker": _build_worker, "export_md": _export_md}
