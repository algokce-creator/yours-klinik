import io
import re
import csv
import json
import time
import hmac
import base64
import urllib.parse
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo

import streamlit as st
import gspread
from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError

TR = ZoneInfo("Europe/Istanbul")


def simdi() -> datetime:
    return datetime.now(TR)


st.set_page_config(page_title="Yours | Klinik & Yaşam Asistanı", page_icon="🩺", layout="centered")

TAKIP_KATEGORILERI = [
    "Rutin Kontrol", "Geri Arama", "2. Doz", "Pansuman", "Sonradan Haber",
    "Hatır Sorma", "Fatura-Vergi", "Sosyal",
]
DURUMLAR = ["Bekliyor", "Randevu verildi", "Ulaşılamadı", "Geldi", "İptal"]
SUTUNLAR = [
    "Tarih", "Protokol", "Hasta_Ad_Maskeli", "Telefon", "Tani_Bolge",
    "Intermedya_Epikriz", "Kontrol_Tarihi", "Kontrol_Notu", "Intermedya_Islendi",
    "WA_Gonderildi", "Takip_Kategorisi", "Drive_Klasor_Linki",
    "Takip_Durumu", "Son_Guncelleme", "Kirmizi_Bayrak",
    "Takvim_Event_ID", "Kontrol_Event_ID", "Drive_Klasor_ID",
]
KIRMIZI_BAYRAKLAR = [
    "ateş", "akıntı", "kızarıklık", "baldır ağrısı", "baldır şiş",
    "uyuşma", "düşük ayak", "şiddetli ağrı", "nefes darlığı",
    "göğüs ağrısı", "kanama", "yara açıl", "pürülan", "enfeksiyon",
]
WA_SABLONLARI = {
    "Rutin Kontrol": (
        "Merhaba, Prof. Dr. Alper Gökçe muayenehanesinden ulaşıyoruz. "
        "{tarih}kontrol randevunuzu netleştirmek için dönüşünüzü rica ederiz."
    ),
    "2. Doz / Enjeksiyon": (
        "Merhaba, Prof. Dr. Alper Gökçe muayenehanesinden ulaşıyoruz. "
        "{tarih}2. doz enjeksiyon/tedavi planlamanız için uygun saatinizi paylaşabilir misiniz?"
    ),
    "Pansuman / Dikiş": (
        "Merhaba, Prof. Dr. Alper Gökçe muayenehanesinden ulaşıyoruz. "
        "{tarih}pansuman ve yara/dikiş kontrolünüz için randevunuzu teyit etmek isteriz."
    ),
    "Ameliyat Öncesi Hazırlık": (
        "Merhaba, Prof. Dr. Alper Gökçe muayenehanesinden ulaşıyoruz. "
        "{tarih}planlanan işleminiz öncesi tetkik ve hazırlık süreci hakkında bilgi vermek isteriz."
    ),
}

# ==============================================================================
# 1. KİMLİK DOĞRULAMA (Sabit zamanlı karşılaştırma + 5 denemede 10 dk kilit)
# ==============================================================================
if not st.secrets.get("APP_PIN") or not st.secrets.get("ASISTAN_PIN"):
    st.error("APP_PIN ve ASISTAN_PIN secrets içinde tanımlı değil. Uygulama kilitli.")
    st.stop()


@st.cache_resource
def giris_kaydi():
    return {"hatalar": [], "kilit_bitis": 0.0}


def pin_esit(girilen: str, anahtar: str) -> bool:
    beklenen = str(st.secrets.get(anahtar, ""))
    return bool(beklenen) and hmac.compare_digest(girilen.encode(), beklenen.encode())


if "rol" not in st.session_state:
    st.session_state.rol = None

if not st.session_state.rol:
    kayit = giris_kaydi()
    an = time.time()
    st.markdown("### 🔐 Yours Giriş")
    if an < kayit["kilit_bitis"]:
        kalan = int((kayit["kilit_bitis"] - an) // 60) + 1
        st.error(f"Çok fazla hatalı deneme. {kalan} dakika sonra tekrar deneyin.")
        st.stop()
    pin = st.text_input("Erişim PIN Kodunuz", type="password", max_chars=12)
    if st.button("Giriş Yap", use_container_width=True):
        if pin_esit(pin, "APP_PIN"):
            st.session_state.rol = "hekim"
            kayit["hatalar"].clear()
            st.rerun()
        elif pin_esit(pin, "ASISTAN_PIN"):
            st.session_state.rol = "asistan"
            kayit["hatalar"].clear()
            st.rerun()
        else:
            kayit["hatalar"] = [t for t in kayit["hatalar"] if an - t < 600] + [an]
            if len(kayit["hatalar"]) >= 5:
                kayit["kilit_bitis"] = an + 600
                kayit["hatalar"].clear()
            st.error("Hatalı PIN kodu.")
    st.stop()

# ==============================================================================
# 2. GOOGLE SERVİSLERİ
# ==============================================================================
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


@st.cache_resource
def google_servisleri():
    sa_info = json.loads(base64.b64decode(st.secrets["GCP_KEY_B64"]).decode("utf-8"))
    sa_creds = SACredentials.from_service_account_info(sa_info, scopes=SCOPES)

    drive_creds = sa_creds
    if st.secrets.get("DRIVE_USER_TOKEN_B64"):
        tok = json.loads(base64.b64decode(st.secrets["DRIVE_USER_TOKEN_B64"]).decode("utf-8"))
        drive_creds = UserCredentials.from_authorized_user_info(
            tok, scopes=["https://www.googleapis.com/auth/drive"]
        )

    cal = build("calendar", "v3", credentials=sa_creds, cache_discovery=False)
    drv = build("drive", "v3", credentials=drive_creds, cache_discovery=False)
    gc_ = gspread.authorize(sa_creds)
    return cal, drv, gc_


cal_service, drive_service, gc = google_servisleri()
SHEET_ID = st.secrets["GOOGLE_SHEET_ID"]
CALENDAR_ID = str(st.secrets.get("CALENDAR_ID", "")).strip()
ROOT_DRIVE_FOLDER = str(st.secrets.get("DRIVE_FOLDER_ID", "")).strip()

if not CALENDAR_ID or CALENDAR_ID == "primary":
    st.error("CALENDAR_ID secrets içinde kendi takvim adresiniz olmalı (örn. algokce@gmail.com).")
    st.stop()


def temiz_telefon(tel_raw) -> str:
    t = re.sub(r"\D", "", str(tel_raw or ""))
    if t.startswith("0"):
        t = "90" + t[1:]
    elif len(t) == 10 and t.startswith("5"):
        t = "90" + t
    return t


def telefon_gecerli(t: str) -> bool:
    return bool(re.fullmatch(r"905\d{9}", t))


ASISTAN_TEL = temiz_telefon(st.secrets.get("ASISTAN_TEL", ""))


def tarih_coz(deger):
    try:
        return date.fromisoformat(str(deger).strip()[:10])
    except (ValueError, TypeError):
        return None


OLUMSUZLAMA = re.compile(
    r"\b(yok|yoktur|yoktu|olmayan|olmadan|negatif|izlenmedi|saptanmadı|tariflemiyor|tarif etmiyor|reddediyor)\b"
)


def kirmizi_bayrak_bul(metin: str) -> str:
    """Cümle parçası bazında arar; 'ateş yok', 'akıntı izlenmedi' gibi olumsuz ifadeleri saymaz."""
    bulunan = set()
    for parca in re.split(r"[.;\n,]", (metin or "").lower()):
        if OLUMSUZLAMA.search(parca):
            continue
        for k in KIRMIZI_BAYRAKLAR:
            if k in parca:
                bulunan.add(k)
    return ", ".join(sorted(bulunan))


# --- Drive yardımcıları ---
def q_kacir(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def guvenli_ad(s: str, uzunluk: int = 80) -> str:
    s = re.sub(r"[^\w\-.]+", "_", str(s or ""), flags=re.UNICODE).strip("_.")
    return s[:uzunluk] or "Genel"


def klasor_bul_olustur(parent_id: str, ad: str):
    q = (
        f"name='{q_kacir(ad)}' and '{q_kacir(parent_id)}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    mevcut = drive_service.files().list(
        q=q, fields="files(id, webViewLink)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    if mevcut:
        f = mevcut[0]
    else:
        f = drive_service.files().create(
            body={"name": ad, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
            fields="id, webViewLink", supportsAllDrives=True,
        ).execute()
    return f["id"], f.get("webViewLink") or f"https://drive.google.com/drive/folders/{f['id']}"


def drive_yukle(parent_id: str, ad: str, icerik: bytes, mimetype: str):
    media = MediaIoBaseUpload(io.BytesIO(icerik), mimetype=mimetype or "application/octet-stream", resumable=True)
    drive_service.files().create(
        body={"name": ad, "parents": [parent_id]},
        media_body=media, fields="id", supportsAllDrives=True,
    ).execute()


# --- Sheet & Değişiklik Kaydı (Önbellekli Okuma + Satır Doğrulama) ---
@st.cache_resource
def is_listesi():
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("Klinik_Is_Listesi")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Klinik_Is_Listesi", rows=1000, cols=len(SUTUNLAR))
        ws.append_row(SUTUNLAR)
        return ws, list(SUTUNLAR)

    baslik = ws.row_values(1)
    eksik = [c for c in SUTUNLAR if c not in baslik]
    if eksik:
        gerekli = len(baslik) + len(eksik)
        if ws.col_count < gerekli:
            ws.add_cols(gerekli - ws.col_count)
        for i, c in enumerate(eksik):
            ws.update_cell(1, len(baslik) + i + 1, c)
        baslik = baslik + eksik
    return ws, baslik


@st.cache_data(ttl=20, show_spinner=False)
def kayitlari_oku():
    ws, _ = is_listesi()
    return ws.get_all_records()


@st.cache_resource
def log_sayfasi():
    sh = gc.open_by_key(SHEET_ID)
    try:
        return sh.worksheet("Islem_Gecmisi")
    except gspread.WorksheetNotFound:
        lws = sh.add_worksheet(title="Islem_Gecmisi", rows=2000, cols=5)
        lws.append_row(["Zaman", "Rol", "Protokol", "Islem", "Detay"])
        return lws


def log_yaz(protokol: str, islem: str, detay: str = ""):
    try:
        log_sayfasi().append_row([
            simdi().strftime("%Y-%m-%d %H:%M:%S"),
            st.session_state.get("rol", ""),
            protokol,
            islem,
            detay,
        ], value_input_option="RAW")
    except Exception as e:
        st.toast(f"⚠️ İşlem geçmişine yazılamadı: {e}")


class SatirDegisti(Exception):
    pass


def satir_guncelle(ws, baslik, satir_no: int, alanlar: dict, protokol: str = "", log_islem: str = ""):
    if protokol:
        mevcut = ws.cell(satir_no, baslik.index("Protokol") + 1).value or ""
        eski = alanlar.get("_eski_protokol", protokol)
        if mevcut.strip() != str(eski).strip():
            kayitlari_oku.clear()
            raise SatirDegisti("Liste değişmiş görünüyor, sayfayı yenileyip tekrar deneyin.")
    alanlar = {k: v for k, v in alanlar.items() if not k.startswith("_")}
    alanlar["Son_Guncelleme"] = simdi().strftime("%Y-%m-%d %H:%M")
    for ad, deger in alanlar.items():
        if ad in baslik:
            ws.update_cell(satir_no, baslik.index(ad) + 1, deger)
    kayitlari_oku.clear()
    if log_islem:
        log_yaz(protokol, log_islem, json.dumps(alanlar, ensure_ascii=False))


def guvenli_guncelle(*args, **kwargs) -> bool:
    try:
        satir_guncelle(*args, **kwargs)
        return True
    except SatirDegisti as e:
        st.error(str(e))
    except Exception as e:
        st.error(f"Güncellenemedi: {e}")
    return False


# --- Yapay zekâ ---
def json_coz(metin: str) -> dict:
    m = (metin or "").strip()
    m = re.sub(r"^```(?:json)?\s*|\s*```$", "", m)
    return json.loads(m)


def llm_json(sistem: str, kullanici: str) -> dict:
    if st.secrets.get("GEMINI_API_KEY"):
        from google import genai
        client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
        r = client.models.generate_content(
            model=st.secrets.get("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=kullanici,
            config={
                "system_instruction": sistem,
                "response_mime_type": "application/json",
                "temperature": 0.1,
            },
        )
        return json_coz(r.text)

    from openai import OpenAI
    client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": sistem}, {"role": "user", "content": kullanici}],
        temperature=0.1,
    )
    return json_coz(r.choices[0].message.content)


def cikis_butonu():
    if st.button("Çıkış"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()


# ==============================================================================
# MOD A: ASİSTAN MASASI
# ==============================================================================
if st.session_state.rol == "asistan":
    c1, c2 = st.columns([4, 1])
    with c1:
        st.title("📋 Klinik Asistan Masası")
    with c2:
        cikis_butonu()

    ws, baslik = is_listesi()
    kayitlar = kayitlari_oku()
    if not kayitlar:
        st.info("Henüz işlenecek muayene kaydı bulunmuyor.")
        st.stop()

    bugun = simdi().date()
    filtre = st.radio(
        "Görünüm:", ["Bekleyen İşlemler", "Takipteki Hastalar", "Tüm Kayıtlar"], horizontal=True
    )

    indeksli = list(enumerate(kayitlar))
    indeksli.sort(
        key=lambda x: (
            bool(str(x[1].get("Kirmizi_Bayrak", "")).strip()),
            x[0],
        ),
        reverse=True,
    )

    gosterilen = 0
    for i, row in indeksli:
        satir_no = i + 2
        islendi = str(row.get("Intermedya_Islendi", "")).upper() == "EVET"
        wa_gitti = str(row.get("WA_Gonderildi", "")).upper() == "EVET"
        durum = str(row.get("Takip_Durumu", "")).strip() or "Bekliyor"
        kontrol = tarih_coz(row.get("Kontrol_Tarihi"))
        acik_takip = bool(kontrol) and durum not in ("Geldi", "İptal")
        gecikmis = acik_takip and kontrol < bugun
        bayrak = str(row.get("Kirmizi_Bayrak", "")).strip()
        prot = str(row.get("Protokol", ""))

        if filtre == "Bekleyen İşlemler" and islendi and not gecikmis and not bayrak:
            continue
        if filtre == "Takipteki Hastalar" and not acik_takip:
            continue
        gosterilen += 1

        ikon = "🚨" if bayrak else ("🔴" if gecikmis else ("✅" if islendi else "⏳"))
        kategori = row.get("Takip_Kategorisi", "Rutin Kontrol")
        baslik_metni = (
            f"{ikon} {row.get('Tarih', '')} | {prot} — "
            f"{row.get('Tani_Bolge', '')} [{kategori}] · {durum}"
        )

        with st.expander(baslik_metni, expanded=bool(bayrak) or (not islendi) or gecikmis):
            if bayrak:
                st.error(f"🚨 KIRMIZI BAYRAK ({bayrak}) — Standart mesaj göndermeyin, ÖNCE HEKİME SORUN!")
            if prot.startswith("ATANMAMIS"):
                st.warning("Bu kayıt henüz bir protokole eşleştirilmedi.")

            st.markdown("**1. İntermedya Standart Tıbbi Kayıt:**")
            st.code(row.get("Intermedya_Epikriz", ""), language="text")

            drive_link = row.get("Drive_Klasor_Linki", "")
            if drive_link:
                st.markdown(f"📁 [Hasta dosya ve onam klasörünü aç]({drive_link})")

            if kontrol or row.get("Kontrol_Notu"):
                gec = " — **GECİKMİŞ**" if gecikmis else ""
                st.info(f"📅 Takip: {row.get('Kontrol_Tarihi', '')}{gec}\n\n{row.get('Kontrol_Notu', '')}")

            a1, a2 = st.columns(2)
            with a1:
                tel = temiz_telefon(row.get("Telefon", ""))
                if bayrak:
                    st.warning("Kırmızı bayrak nedeniyle otomatik WhatsApp kilitli.")
                elif telefon_gecerli(tel):
                    sablon_sec = st.selectbox(
                        "Mesaj şablonu", list(WA_SABLONLARI.keys()), key=f"sab_{satir_no}"
                    )
                    t_str = f"{kontrol.strftime('%d.%m.%Y')} tarihli " if kontrol else ""
                    wa_mesaj = WA_SABLONLARI[sablon_sec].format(tarih=t_str)
                    st.link_button(
                        "📲 Hastaya WhatsApp Aç",
                        f"https://wa.me/{tel}?text={urllib.parse.quote(wa_mesaj)}",
                        use_container_width=True,
                    )
                    if not wa_gitti:
                        if st.button("✔ WhatsApp gönderildi", key=f"wa_{satir_no}", use_container_width=True):
                            if guvenli_guncelle(ws, baslik, satir_no, {"WA_Gonderildi": "EVET"}, prot, "WA_Gonderildi"):
                                st.rerun()
                    else:
                        st.caption("WhatsApp gönderildi olarak işaretli.")
                elif row.get("Telefon"):
                    st.caption("⚠️ Telefon numarası geçersiz görünüyor.")
            with a2:
                if not islendi:
                    if st.button("✅ İntermedya'ya İşlendi", key=f"im_{satir_no}", use_container_width=True):
                        if guvenli_guncelle(ws, baslik, satir_no, {"Intermedya_Islendi": "EVET"}, prot, "Intermedya_Islendi"):
                            st.rerun()
                else:
                    st.success("İntermedya'ya işlendi.")

            if kontrol:
                y1, y2 = st.columns([3, 1])
                with y1:
                    yeni_durum = st.selectbox(
                        "Takip durumu", DURUMLAR,
                        index=DURUMLAR.index(durum) if durum in DURUMLAR else 0,
                        key=f"dr_{satir_no}",
                    )
                with y2:
                    st.write("")
                    if st.button("Kaydet", key=f"drk_{satir_no}"):
                        if guvenli_guncelle(ws, baslik, satir_no, {"Takip_Durumu": yeni_durum}, prot, "Takip_Durumu"):
                            st.rerun()

    if gosterilen == 0:
        st.success("Bu görünümde bekleyen kayıt yok.")
    st.stop()

# ==============================================================================
# MOD B: HEKİM EKRANI
# ==============================================================================
c1, c2 = st.columns([4, 1])
with c1:
    st.title("🩺 Yours | Klinik & Yaşam")
with c2:
    cikis_butonu()

tab_giris, tab_hafiza, tab_eslestir, tab_brifing = st.tabs([
    "🎙️ Yeni Kayıt", "🗂️ Hafıza Kartı & Arama", "🔗 Eşleştirme", "🧠 Takvim & WhatsApp"
])

ANALIZ_KOMUTU = """
Sen Prof. Dr. Alper Gökçe'nin (Ortopedi ve Travmatoloji) klinik asistanısın. Bugünün tarihi: {bugun}.
Ham metni yapılandır ve SADECE geçerli JSON döndür.

KESİN KURALLAR:
- Metinde açıkça geçmeyen bulgu, test, tanı veya tedaviyi ASLA uydurma. Bilinmeyen alana "Belirtilmedi" yaz.
- Yalnızca bariz ses tanıma yazım hatalarını doğru tıbbi terime çevir.
- ICD-10 yalnızca öneridir; emin değilsen boş bırak.
- Kontrol süresi geçiyorsa ("2 hafta sonra") tarihi bugünden hesapla.
- Metinde telefon numarası geçiyorsa çıkar (onay ekranında hekim doğrulayacak), yoksa boş bırak.

JSON şeması:
{{
  "tur": "klinik" veya "sosyal",
  "protokol": "Metinde açıkça geçen protokol/dosya no, yoksa boş",
  "hasta_maskeli": "Ad soyad baş harfleri (örn. A.Y.) veya boş",
  "telefon": "Varsa 05XXXXXXXXX formatında telefon, yoksa boş",
  "takip_kategorisi": "{kategoriler} değerlerinden biri",
  "bolge_tani": "Anatomik bölge ve kısa tanı",
  "icd10_kodu": "Önerilen ICD-10 kodu ve adı veya boş",
  "intermedya_epikriz": "ŞİKAYET: ...\\nHİKAYE: ...\\nMUAYENE BULGUSU: ...\\nLAB / GÖRÜNTÜLEME: ...\\nTANI (ICD-10): ...\\nTEDAVİ / PLAN: ...",
  "baslik": "Sosyal notlar için kısa başlık",
  "kontrol_var_mi": true veya false,
  "kontrol_tarihi": "YYYY-MM-DD veya boş",
  "kontrol_notu": "Net aksiyon notu veya boş"
}}
"""

# ------------------------------------------------------------------------------
# SEKME 1: YENİ KAYIT + ONAY EKRANI
# ------------------------------------------------------------------------------
with tab_giris:
    gelen_metin = st.query_params.get("metin", "")
    ses_dosyasi = None
    if st.secrets.get("OPENAI_API_KEY") and not st.secrets.get("BULUT_SES_KAPALI", False):
        ses_dosyasi = st.audio_input("1. Doğrudan Ses Kaydet (Opsiyonel)")

    ham_metin = st.text_area(
        "2. Ham Metin (Ses Kaydedici / WhatsApp / Yerel Whisper)",
        value=gelen_metin, height=150,
        placeholder="Örn: Protokol 2026-420, sağ diz medial gonartroz, 2. doz PRP 2 hafta sonra, tel 0532...",
    )

    u1, u2 = st.columns(2)
    with u1:
        goruntu_videolar = st.file_uploader(
            "3A. Klinik Görsel / Röntgen / Video",
            type=["jpg", "jpeg", "png", "mp4", "mov", "avi"], accept_multiple_files=True,
        )
    with u2:
        onam_belgeleri = st.file_uploader(
            "3B. Aydınlatılmış Onam & PDF Evrak",
            type=["pdf", "jpg", "jpeg", "png"], accept_multiple_files=True,
        )

    if st.button("🔍 Analiz Et (Taslak Oluştur)", use_container_width=True):
        metin = ham_metin.strip()
        try:
            if ses_dosyasi is not None:
                from openai import OpenAI
                with st.spinner("Ses kaydı yazıya dönüştürülüyor..."):
                    tr_ = OpenAI(api_key=st.secrets["OPENAI_API_KEY"]).audio.transcriptions.create(
                        model="whisper-1", file=("kayit.wav", ses_dosyasi.getvalue(), "audio/wav"), language="tr",
                    )
                    metin = (metin + "\n" + tr_.text).strip()
            if not metin:
                st.warning("Lütfen ses kaydı alın veya metin girin.")
            else:
                with st.spinner("Taslak hazırlanıyor..."):
                    taslak = llm_json(
                        ANALIZ_KOMUTU.format(bugun=simdi().strftime("%Y-%m-%d"), kategoriler=" / ".join(TAKIP_KATEGORILERI)),
                        metin,
                    )
                st.session_state.taslak = taslak
                st.session_state.taslak_metin = metin
                st.session_state.taslak_id = str(int(time.time()))
        except Exception as e:
            st.error(f"Analiz başarısız: {e}")

    if "taslak" in st.session_state:
        t = st.session_state.taslak
        tid = st.session_state.taslak_id
        st.divider()
        st.subheader("4. Kontrol Et ve Onayla")

        bayrak_onizleme = kirmizi_bayrak_bul(st.session_state.taslak_metin + " " + str(t.get("intermedya_epikriz", "")))
        if bayrak_onizleme:
            st.error(f"🚨 KIRMIZI BAYRAK TESPİT EDİLDİ: {bayrak_onizleme}")

        with st.form(f"onay_{tid}"):
            tur = st.radio("Kayıt türü", ["klinik", "sosyal"],
                           index=0 if t.get("tur") != "sosyal" else 1, horizontal=True)
            f1, f2 = st.columns(2)
            with f1:
                protokol = st.text_input("Protokol No", value=str(t.get("protokol", "") or ""),
                                         help="Boş kalırsa benzersiz ATANMAMIS koduyla kaydedilir.")
                hasta_maskeli = st.text_input("Baş harfler", value=str(t.get("hasta_maskeli", "") or ""))
            with f2:
                telefon = st.text_input("Telefon (Doğrulayın/Girin)", value=str(t.get("telefon", "") or ""),
                                        placeholder="05XX XXX XX XX")
                kat_ai = t.get("takip_kategorisi", "Rutin Kontrol")
                kategori = st.selectbox("Takip kategorisi", TAKIP_KATEGORILERI,
                                        index=TAKIP_KATEGORILERI.index(kat_ai) if kat_ai in TAKIP_KATEGORILERI else 0)
            bolge_tani = st.text_input("Bölge / Tanı", value=str(t.get("bolge_tani", "") or ""))
            icd10 = st.text_input("ICD-10 (Öneri)", value=str(t.get("icd10_kodu", "") or ""))
            baslik_sosyal = st.text_input("Başlık (Sosyal not için)", value=str(t.get("baslik", "") or ""))
            epikriz = st.text_area("İntermedya Epikrizi", value=str(t.get("intermedya_epikriz", "") or ""), height=220)

            k_var = st.checkbox("Takip / kontrol var", value=bool(t.get("kontrol_var_mi")))
            k_tarih_vars = tarih_coz(t.get("kontrol_tarihi")) or (simdi().date() + timedelta(days=14))
            k1, k2 = st.columns(2)
            with k1:
                k_tarih = st.date_input("Kontrol tarihi", value=k_tarih_vars, format="DD.MM.YYYY")
            with k2:
                k_not = st.text_input("Kontrol notu", value=str(t.get("kontrol_notu", "") or ""))

            b1, b2 = st.columns(2)
            with b1:
                kaydet = st.form_submit_button("✨ Onayla ve Kaydet", type="primary", use_container_width=True)
            with b2:
                vazgec = st.form_submit_button("Vazgeç", use_container_width=True)

        if vazgec:
            for k in ("taslak", "taslak_metin", "taslak_id"):
                st.session_state.pop(k, None)
            st.rerun()

        if kaydet:
            tel_temiz = temiz_telefon(telefon)
            if telefon.strip() and not telefon_gecerli(tel_temiz):
                st.error("Telefon numarası geçersiz. 05XX XXX XX XX biçiminde girin veya boş bırakın.")
                st.stop()

            an = simdi()
            klinik = tur == "klinik"
            protokol = protokol.strip() or f"ATANMAMIS-{an:%Y%m%d-%H%M%S}"
            bayrak = kirmizi_bayrak_bul(epikriz + " " + k_not) if klinik else ""

            drive_link, drive_klasor_id, yuklenenler, drive_uyari = "", "", [], ""
            dosya_var = bool(goruntu_videolar or onam_belgeleri)

            try:
                with st.spinner("Kaydediliyor..."):
                    if ROOT_DRIVE_FOLDER and dosya_var:
                        try:
                            klasor_adi = f"{an.year}_{guvenli_ad(protokol)}" if klinik else f"{an.year}_Kisisel_Notlar"
                            drive_klasor_id, drive_link = klasor_bul_olustur(ROOT_DRIVE_FOLDER, klasor_adi)
                            damga = an.strftime("%Y%m%d_%H%M")
                            if goruntu_videolar:
                                gv_id, _ = klasor_bul_olustur(drive_klasor_id, "01_Goruntu_ve_Video")
                                for d in goruntu_videolar:
                                    drive_yukle(gv_id, f"{damga}_{guvenli_ad(d.name, 120)}", d.getvalue(), d.type)
                                    yuklenenler.append(d.name)
                            if onam_belgeleri:
                                on_id, _ = klasor_bul_olustur(drive_klasor_id, "02_Aydinlatilmis_Onam_ve_Evrak")
                                for d in onam_belgeleri:
                                    drive_yukle(on_id, f"{damga}_{guvenli_ad(d.name, 120)}", d.getvalue(), d.type)
                                    yuklenenler.append(d.name)
                        except HttpError:
                            drive_uyari = (
                                "⚠️ Dosyalar Drive kota kısıtı nedeniyle yüklenemedi (Takvim ve Sheet kaydı yine de tamamlandı). "
                                "Drive yüklemesi için Paylaşılan Drive veya DRIVE_USER_TOKEN_B64 gerekir."
                            )

                    drive_satiri = f"\n📁 Drive: {drive_link}" if drive_link else ""

                    if klinik:
                        ana_baslik = f"[{protokol}] | {kategori} | {bolge_tani}"
                        ana_aciklama = f"{epikriz}{drive_satiri}"
                        renk = "11"
                    else:
                        ana_baslik = f"{kategori}: {baslik_sosyal or 'Kişisel Not'}"
                        ana_aciklama = f"{epikriz or st.session_state.taslak_metin}{drive_satiri}"
                        renk = "10"

                    ev_ana = cal_service.events().insert(calendarId=CALENDAR_ID, body={
                        "summary": ana_baslik, "description": ana_aciklama, "colorId": renk,
                        "start": {"dateTime": an.isoformat(), "timeZone": "Europe/Istanbul"},
                        "end": {"dateTime": (an + timedelta(minutes=20)).isoformat(), "timeZone": "Europe/Istanbul"},
                    }).execute()
                    takvim_event_id = ev_ana.get("id", "")

                    kontrol_event_id = ""
                    if k_var and k_tarih:
                        k_dt = datetime.combine(k_tarih, dtime(10, 0), tzinfo=TR)
                        k_baslik = f"🔔 [{protokol}] {kategori.upper()} | {bolge_tani}" if klinik else f"🔔 {k_not or ana_baslik}"
                        ev_k = cal_service.events().insert(calendarId=CALENDAR_ID, body={
                            "summary": k_baslik, "description": f"{k_not}{drive_satiri}\n\n{epikriz}", "colorId": "6",
                            "start": {"dateTime": k_dt.isoformat(), "timeZone": "Europe/Istanbul"},
                            "end": {"dateTime": (k_dt + timedelta(minutes=30)).isoformat(), "timeZone": "Europe/Istanbul"},
                            "reminders": {"useDefault": False, "overrides": [
                                {"method": "popup", "minutes": 1440}, {"method": "popup", "minutes": 120}]},
                        }).execute()
                        kontrol_event_id = ev_k.get("id", "")

                    if klinik and kategori != "Fatura-Vergi":
                        ws, baslik = is_listesi()
                        degerler = {
                            "Tarih": an.strftime("%Y-%m-%d %H:%M"),
                            "Protokol": protokol,
                            "Hasta_Ad_Maskeli": hasta_maskeli,
                            "Telefon": tel_temiz if telefon.strip() else "",
                            "Tani_Bolge": f"{bolge_tani} ({icd10})" if icd10 else bolge_tani,
                            "Intermedya_Epikriz": epikriz,
                            "Kontrol_Tarihi": k_tarih.isoformat() if k_var else "",
                            "Kontrol_Notu": k_not if k_var else "",
                            "Intermedya_Islendi": "HAYIR",
                            "WA_Gonderildi": "HAYIR",
                            "Takip_Kategorisi": kategori,
                            "Drive_Klasor_Linki": drive_link,
                            "Takip_Durumu": "Bekliyor" if k_var else "",
                            "Son_Guncelleme": an.strftime("%Y-%m-%d %H:%M"),
                            "Kirmizi_Bayrak": bayrak,
                            "Takvim_Event_ID": takvim_event_id,
                            "Kontrol_Event_ID": kontrol_event_id,
                            "Drive_Klasor_ID": drive_klasor_id,
                        }
                        ws.append_row([degerler.get(h, "") for h in baslik], value_input_option="RAW")
                        kayitlari_oku.clear()
                        log_yaz(protokol, "Yeni_Kayit", f"{kategori} | {bolge_tani}")

            except Exception as e:
                st.error(f"Kayıt tamamlanamadı: {e}")
                st.stop()

            for k in ("taslak", "taslak_metin", "taslak_id"):
                st.session_state.pop(k, None)
            st.success(f"✅ Kaydedildi: {ana_baslik}")
            if drive_uyari:
                st.warning(drive_uyari)
            if protokol.startswith("ATANMAMIS"):
                st.info("Protokol girilmedi. 'Eşleştirme' sekmesinden sonradan bağlayabilirsiniz.")
            if drive_link:
                st.markdown(f"📁 [Hasta klasörünü aç]({drive_link})")
            if yuklenenler:
                st.caption("Yüklenen dosyalar: " + ", ".join(yuklenenler))

# ------------------------------------------------------------------------------
# SEKME 2: HAFIZA KARTI, ARAMA & CSV YEDEK
# ------------------------------------------------------------------------------
with tab_hafiza:
    st.subheader("🗂️ Hasta Hafıza Kartı & Arama")
    tum_kayitlar = kayitlari_oku()

    if not tum_kayitlar:
        st.info("Henüz kayıt bulunmuyor.")
    else:
        s1, s2, s3 = st.columns([2, 1, 1])
        with s1:
            arama = st.text_input("Protokol, Tanı, ICD-10 veya Epikriz içinde ara", placeholder="Örn: 2026-420, PRP, M17, HTO")
        with s2:
            kat_filtre = st.selectbox("Kategori", ["Tümü"] + TAKIP_KATEGORILERI)
        with s3:
            sadece_bayrak = st.checkbox("Sadece 🚨 Bayrak")

        eslesenler = []
        for r in reversed(tum_kayitlar):
            if kat_filtre != "Tümü" and r.get("Takip_Kategorisi") != kat_filtre:
                continue
            if sadece_bayrak and not str(r.get("Kirmizi_Bayrak", "")).strip():
                continue
            if arama.strip():
                hedef_metin = " ".join([
                    str(r.get("Protokol", "")), str(r.get("Tani_Bolge", "")),
                    str(r.get("Intermedya_Epikriz", "")), str(r.get("Kontrol_Notu", "")),
                ]).lower()
                if arama.strip().lower() not in hedef_metin:
                    continue
            eslesenler.append(r)

        st.caption(f"Bulunan kayıt: {len(eslesenler)}")

        protokoller = list(dict.fromkeys(str(r.get("Protokol", "")) for r in eslesenler))
        for p in protokoller[:25]:
            vizitler = [r for r in eslesenler if str(r.get("Protokol", "")) == p]
            son = vizitler[0]
            bayrak = str(son.get("Kirmizi_Bayrak", "")).strip()
            baslik_k = f"{'🚨 ' if bayrak else ''}{p} — {son.get('Tani_Bolge', '')} ({len(vizitler)} vizit)"
            with st.expander(baslik_k, expanded=(len(protokoller) == 1)):
                if bayrak:
                    st.error(f"🚨 Kırmızı Bayrak: {bayrak}")
                st.markdown(f"**Son Vizit:** {son.get('Tarih', '')} | **Durum:** {son.get('Takip_Durumu', '-')}")
                if son.get("Kontrol_Tarihi") or son.get("Kontrol_Notu"):
                    st.info(f"📅 **Bekleyen Kontrol:** {son.get('Kontrol_Tarihi', '')} — {son.get('Kontrol_Notu', '')}")
                if son.get("Drive_Klasor_Linki"):
                    st.markdown(f"📁 [Drive Klasörü]({son.get('Drive_Klasor_Linki')})")
                for v in vizitler[:3]:
                    st.markdown(f"**{v.get('Tarih', '')} [{v.get('Takip_Kategorisi', '')}]:**")
                    st.code(v.get("Intermedya_Epikriz", ""), language="text")

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(tum_kayitlar[0].keys()))
        writer.writeheader()
        writer.writerows(tum_kayitlar)
        st.download_button(
            "📥 Tüm Klinik Listesini Yedekle (CSV)",
            data=buf.getvalue().encode("utf-8-sig"),
            file_name=f"yours_yedek_{simdi():%Y%m%d}.csv",
            mime="text/csv",
            use_container_width=True,
        )

# ------------------------------------------------------------------------------
# SEKME 3: ATANMAMIŞ KAYITLARI PROTOKOLE EŞLEŞTİRME
# ------------------------------------------------------------------------------
with tab_eslestir:
    st.subheader("🔗 Atanmamış Kayıtları Protokole Bağla")
    ws, baslik = is_listesi()
    kayitlar = kayitlari_oku()
    atanmamislar = [(i + 2, r) for i, r in enumerate(kayitlar) if str(r.get("Protokol", "")).startswith("ATANMAMIS")]

    if not atanmamislar:
        st.success("Eşleştirme bekleyen ATANMAMIS kayıt yok.")
    else:
        for satir_no, r in reversed(atanmamislar):
            eski_prot = str(r.get("Protokol", ""))
            with st.expander(f"⏳ {eski_prot} | {r.get('Tarih', '')} — {r.get('Tani_Bolge', '')}", expanded=True):
                st.code(r.get("Intermedya_Epikriz", ""), language="text")
                yeni_prot = st.text_input("Gerçek Protokol No", key=f"ep_{satir_no}", placeholder="Örn: 2026-425")
                if st.button("✅ Eşleştir (Sheet + Takvim + Drive)", key=f"eb_{satir_no}", type="primary"):
                    yeni_p = yeni_prot.strip()
                    if not yeni_p:
                        st.warning("Lütfen protokol numarası girin.")
                    else:
                        guncellendi = guvenli_guncelle(
                            ws, baslik, satir_no,
                            {"Protokol": yeni_p, "_eski_protokol": eski_prot},
                            yeni_p, f"Eslestirme:{eski_prot}->{yeni_p}"
                        )
                        if guncellendi:
                            for eid_col in ("Takvim_Event_ID", "Kontrol_Event_ID"):
                                eid = str(r.get(eid_col, "")).strip()
                                if eid:
                                    try:
                                        ev = cal_service.events().get(calendarId=CALENDAR_ID, eventId=eid).execute()
                                        ev["summary"] = ev.get("summary", "").replace(eski_prot, yeni_p)
                                        cal_service.events().update(calendarId=CALENDAR_ID, eventId=eid, body=ev).execute()
                                    except Exception:
                                        pass
                            did = str(r.get("Drive_Klasor_ID", "")).strip()
                            if did:
                                try:
                                    drive_service.files().update(
                                        fileId=did,
                                        body={"name": f"{simdi().year}_{guvenli_ad(yeni_p)}"},
                                        supportsAllDrives=True,
                                    ).execute()
                                except Exception:
                                    pass
                            st.success(f"✅ {eski_prot} → {yeni_p} olarak güncellendi.")
                            st.rerun()

# ------------------------------------------------------------------------------
# SEKME 4: TAKVİM TARAMA & ASİSTAN WHATSAPP PAKETİ (EPİKRİZ DETAYLI)
# ------------------------------------------------------------------------------
with tab_brifing:
    st.subheader("🧠 Takvim Tarama & Asistan WhatsApp Paketi")
    gun_sayisi = st.slider("Tarama aralığı (gün)", 1, 14, 7)

    if st.button("🔄 Tüm Takvim & Klinik Listesini Şimdi Tara", use_container_width=True):
        try:
            with st.spinner("Takvim ve klinik kayıtlar analiz ediliyor..."):
                an = simdi()
                bugun = an.date()
                events = cal_service.events().list(
                    calendarId=CALENDAR_ID,
                    timeMin=(an - timedelta(days=1)).isoformat(),
                    timeMax=(an + timedelta(days=gun_sayisi)).isoformat(),
                    singleEvents=True, orderBy="startTime",
                ).execute().get("items", [])
                takvim = "\n".join(
                    f"- [{ev['start'].get('dateTime', ev['start'].get('date', ''))}] {ev.get('summary', '')}"
                    f" :: {ev.get('description', '').replace(chr(10), ' ')[:300]}"
                    for ev in events
                )

                bekleyenler = []
                for r in kayitlari_oku():
                    d = str(r.get("Takip_Durumu", "")).strip() or "Bekliyor"
                    k = tarih_coz(r.get("Kontrol_Tarihi"))
                    islendi = str(r.get("Intermedya_Islendi", "")).upper() == "EVET"
                    if (k and d not in ("Geldi", "İptal")) or not islendi:
                        bekleyenler.append(r)

                sonuc = llm_json(
                    "Sen Prof. Dr. Alper Gökçe'nin klinik ve yaşam asistanısın. SADECE JSON döndür: "
                    '{"hekim_brifingi": "düz metin", "asistan_whatsapp_paketi": "düz metin"}',
                    f"Bugün: {bugun}. Aşağıdaki verileri analiz et:\n"
                    "1) hekim_brifingi: (a) Bugün aranacak / gecikmiş / kırmızı bayraklı takipler, "
                    "(b) 2. doz / pansuman / dikiş / kontrol, (c) Fatura / vergi / idari, (d) Kişisel ve sosyal ajanda.\n"
                    "2) asistan_whatsapp_paketi: SOSYAL, KİŞİSEL ve FATURA/VERGİ notlarını TAMAMEN ÇIKAR. "
                    "Yalnızca İntermedya'ya girilecek klinik hastaların Protokol, Şikayet, Hikaye, Muayene, Lab/Görüntüleme, "
                    "Tanı (ICD-10), Tedavi/Plan epikriz detaylarını ve aranacak hasta numaralarını WhatsApp'ta okunması kolay düz metin olarak yaz.\n\n"
                    f"TAKVİM:\n{takvim}\n\nBEKLEYEN KLİNİK KAYITLAR:\n{json.dumps(bekleyenler, ensure_ascii=False)}",
                )
                st.session_state.hekim_brifingi = sonuc.get("hekim_brifingi", "")
                st.session_state.asistan_wa_paketi = sonuc.get("asistan_whatsapp_paketi", "")
        except Exception as e:
            st.error(f"Tarama başarısız: {e}")

    if st.session_state.get("hekim_brifingi"):
        st.markdown("### 🩺 Size Özel Günlük Brifing")
        st.info(st.session_state.hekim_brifingi)

    if st.session_state.get("asistan_wa_paketi"):
        st.markdown("### 📋 Asistana Gidecek İntermedya Epikriz & Arama Paketi")
        paket = st.session_state.asistan_wa_paketi
        st.code(paket, language="text")
        if telefon_gecerli(ASISTAN_TEL):
            encoded = urllib.parse.quote(paket)
            if len(encoded) > 1800:
                st.warning("Paket uzun olduğu için WhatsApp linki kesilebilir; yukarıdaki kutunun sağ üstünden kopyalayıp yapıştırmanız daha güvenlidir.")
            st.link_button(
                "📲 Asistana WhatsApp ile Tek Tıkla Gönder",
                f"https://wa.me/{ASISTAN_TEL}?text={encoded[:1800]}",
                type="primary", use_container_width=True,
            )
        else:
            st.warning("WhatsApp butonu için Secrets'a `ASISTAN_TEL = \"905XXXXXXXXX\"` ekleyin.")
