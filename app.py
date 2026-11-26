import json
import base64
import urllib.parse
from datetime import datetime, timedelta
import streamlit as st
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

st.set_page_config(page_title="Yours | Klinik & Yaşam Asistanı", page_icon="🩺", layout="centered")

# --- 1. ÇİFT ROLLÜ KİMLİK DOĞRULAMA (HEKİM vs ASİSTAN) ---
if "rol" not in st.session_state:
    st.session_state.rol = None

if not st.session_state.rol:
    st.markdown("### 🔐 Yours Giriş")
    pin = st.text_input("Erişim PIN Kodunuz", type="password", max_chars=8)
    if st.button("Giriş Yap", use_container_width=True):
        if pin == st.secrets.get("APP_PIN"):
            st.session_state.rol = "hekim"
            st.rerun()
        elif pin == st.secrets.get("ASISTAN_PIN"):
            st.session_state.rol = "asistan"
            st.rerun()
        else:
            st.error("Hatalı PIN kodu.")
    st.stop()

# --- 2. GOOGLE BAĞLANTILARI (TAKVİM, DRIVE, SHEETS) ---
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

@st.cache_resource
def get_google_services():
    raw_json = base64.b64decode(st.secrets["GCP_KEY_B64"]).decode("utf-8")
    creds_info = json.loads(raw_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    cal_service = build("calendar", "v3", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    gc = gspread.authorize(creds)
    return cal_service, drive_service, gc

cal_service, drive_service, gc = get_google_services()
SHEET_ID = st.secrets["GOOGLE_SHEET_ID"]
CALENDAR_ID = st.secrets.get("CALENDAR_ID", "primary")
ROOT_DRIVE_FOLDER = st.secrets.get("DRIVE_FOLDER_ID", "")

def get_or_create_is_listesi_ws():
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("Klinik_Is_Listesi")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Klinik_Is_Listesi", rows=1000, cols=10)
        ws.append_row([
            "Tarih", "Protokol", "Hasta_Ad_Maskeli", "Telefon",
            "Tani_Bolge", "Intermedya_Epikriz", "Kontrol_Tarihi",
            "Kontrol_Notu", "Intermedya_Islendi", "WA_Gonderildi"
        ])
    return ws

# ==============================================================================
# MOD A: ASİSTAN MASASI (YALNIZCA İNTERMEDYA EPİKRİZ & WHATSAPP HATIRLATMA)
# ==============================================================================
if st.session_state.rol == "asistan":
    col_h1, col_h2 = st.columns([4, 1])
    with col_h1:
        st.title("📋 Klinik Asistan Masası")
    with col_h2:
        if st.button("Çıkış"):
            st.session_state.rol = None
            st.rerun()

    st.caption("Prof. Dr. Alper Gökçe Muayenehanesi • İntermedya & Kontrol Koordinasyonu")
    ws = get_or_create_is_listesi_ws()
    kayitlar = ws.get_all_records()

    if not kayitlar:
        st.info("Henüz işlenecek muayene kaydı bulunmuyor.")
        st.stop()

    filtre = st.radio("Görünüm:", ["Bekleyen İşlemler", "Tüm Kayıtlar"], horizontal=True)

    for idx, row in enumerate(reversed(kayitlar), start=1):
        gercek_satir = len(kayitlar) - idx + 2
        islendi_mi = str(row.get("Intermedya_Islendi", "")).upper() == "EVET"
        if filtre == "Bekleyen İşlemler" and islendi_mi:
            continue

        baslik = f"{'✅' if islendi_mi else '⏳'} {row.get('Tarih', '')} | {row.get('Protokol', 'Protokol Yok')} — {row.get('Tani_Bolge', '')}"
        with st.expander(baslik, expanded=not islendi_mi):
            st.markdown("**1. İntermedya Epikriz Metni (Sağ üstten tek tıkla kopyalayın):**")
            st.code(row.get("Intermedya_Epikriz", ""), language="text")

            tel = str(row.get("Telefon", "")).strip().replace(" ", "").replace("+", "")
            if tel.startswith("0"):
                tel = "90" + tel[1:]
            kontrol_tarih = str(row.get("Kontrol_Tarihi", "")).strip()
            kontrol_not = str(row.get("Kontrol_Notu", "")).strip()

            c1, c2 = st.columns(2)
            with c1:
                if kontrol_tarih:
                    st.info(f"📅 **Kontrol Planı:** {kontrol_tarih}\n{kontrol_not}")
                    if tel:
                        wa_mesaj = (
                            f"Merhaba, Prof. Dr. Alper Gökçe muayenehanesinden ulaşıyoruz. "
                            f"{kontrol_tarih} tarihi civarındaki kontrol randevunuzu netleştirmek için dönüşünüzü rica ederiz."
                        )
                        wa_link = f"https://wa.me/{tel}?text={urllib.parse.quote(wa_mesaj)}"
                        st.link_button("📲 WhatsApp Business ile Gönder", wa_link, use_container_width=True)
                else:
                    st.caption("Kontrol randevusu belirtilmemiş.")

            with c2:
                if not islendi_mi:
                    if st.button("✅ İntermedya'ya İşlendi", key=f"im_{gercek_satir}", use_container_width=True):
                        ws.update_cell(gercek_satir, 9, "EVET")
                        st.success("İntermedya durumu güncellendi.")
                        st.rerun()
                else:
                    st.success("İntermedya'ya işlendi.")
    st.stop()

# ==============================================================================
# MOD B: HEKİM EKRANI (PROF. DR. ALPER GÖKÇE)
# ==============================================================================
col_t1, col_t2 = st.columns([4, 1])
with col_t1:
    st.title("🩺 Yours | Klinik & Yaşam")
with col_t2:
    if st.button("Çıkış"):
        st.session_state.rol = None
        st.rerun()

gelen_metin = st.query_params.get("metin", "")

st.subheader("1. Ses veya Metin Girişi")
ses_dosyasi = st.audio_input("Doğrudan Ses Kaydet (Opsiyonel)")
ham_metin = st.text_area(
    "2. Ham Metin (MacroDroid'den otomatik dolar veya doğrudan yazabilirsiniz)",
    value=gelen_metin,
    height=150,
    placeholder="Örn: Protokol 2026-418, sağ diz medial gonartroz, OW-HTO planlandı, 3 hafta sonra kontrol..."
)

yuklenen_gorseller = st.file_uploader(
    "3. Röntgen / MR / Klinik Fotoğraf Ekle (Opsiyonel)",
    type=["jpg", "jpeg", "png", "pdf"],
    accept_multiple_files=True
)

if st.button("✨ Onayla ve Takvim / Drive / Asistan Masasına İşle", type="primary", use_container_width=True):
    client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

    if ses_dosyasi is not None:
        with st.spinner("Ses kaydı yazıya dönüştürülüyor..."):
            transkript = client.audio.transcriptions.create(
                model="whisper-1",
                file=("kayit.wav", ses_dosyasi.getvalue(), "audio/wav"),
                language="tr"
            )
            ham_metin = (ham_metin + "\n" + transkript.text).strip()

    if not ham_metin:
        st.warning("Lütfen ses kaydı alın veya metin girin.")
        st.stop()

    with st.spinner("Klinik/kişisel ayrım yapılıyor, İntermedya özeti ve takvim planı oluşturuluyor..."):
        sistem_komutu = """
        Sen Prof. Dr. Alper Gökçe'nin akıllı klinik ve yaşam asistanısın.
        Verilen metni analiz et ve SADECE geçerli bir JSON nesnesi döndür:
        {
          "tur": "klinik" veya "kisisel",
          "protokol": "Varsa protokol/dosya no, yoksa 'PROT-YOK'",
          "hasta_maskeli": "Ad soyad baş harfleri (Örn: A.Y.) veya boş",
          "telefon": "Varsa 05XXXXXXXXX formatında telefon, yoksa boş",
          "bolge": "Anatomik bölge (Örn: Sağ Diz, Sol Ayak Bileği)",
          "tani": "Kısa klinik tanı",
          "intermedya_epikriz": "İntermedya hasta kartına yapıştırılacak resmi, yapılandırılmış (Şikayet, Muayene/Görüntüleme, Tanı, Plan) tıbbi epikriz metni",
          "takvim_baslik": "Klinik ise '[Protokol] - [Bölge] - [Tanı]', kişisel ise kısa başlık",
          "takvim_tarih": "YYYY-MM-DD formatında bugünün veya belirtilen işlemin tarihi",
          "kontrol_var_mi": true veya false,
          "kontrol_tarihi": "Kontrol randevusu belirtildiyse YYYY-MM-DD (bugün: """ + datetime.now().strftime("%Y-%m-%d") + """ baz alınarak hesapla), yoksa boş",
          "kontrol_notu": "Örn: 3. hafta dikiş/grafi kontrolü"
        }
        """
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": sistem_komutu},
                {"role": "user", "content": ham_metin}
            ],
            temperature=0.1
        )
        veri = json.loads(resp.choices[0].message.content)

    # --- GOOGLE TAKVİM İŞLEME ---
    bugun_str = veri.get("takvim_tarih") or datetime.now().strftime("%Y-%m-%d")
    baslangic_dt = datetime.strptime(bugun_str, "%Y-%m-%d").replace(hour=10, minute=0)
    bitis_dt = baslangic_dt + timedelta(minutes=30)

    etkinlik = {
        "summary": veri.get("takvim_baslik", "Klinik Not"),
        "description": veri.get("intermedya_epikriz", ham_metin),
        "colorId": "11" if veri.get("tur") == "klinik" else "10",
        "start": {"dateTime": baslangic_dt.isoformat(), "timeZone": "Europe/Istanbul"},
        "end": {"dateTime": bitis_dt.isoformat(), "timeZone": "Europe/Istanbul"},
    }
    cal_service.events().insert(calendarId=CALENDAR_ID, body=etkinlik).execute()

    if veri.get("kontrol_var_mi") and veri.get("kontrol_tarihi"):
        k_dt = datetime.strptime(veri["kontrol_tarihi"], "%Y-%m-%d").replace(hour=10, minute=0)
        kontrol_etkinlik = {
            "summary": f"🔔 KONTROL: {veri.get('takvim_baslik')}",
            "description": f"{veri.get('kontrol_notu')}\n\n{veri.get('intermedya_epikriz', '')}",
            "colorId": "6",
            "start": {"dateTime": k_dt.isoformat(), "timeZone": "Europe/Istanbul"},
            "end": {"dateTime": (k_dt + timedelta(minutes=30)).isoformat(), "timeZone": "Europe/Istanbul"},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 1440},
                    {"method": "popup", "minutes": 120},
                ],
            },
        }
        cal_service.events().insert(calendarId=CALENDAR_ID, body=kontrol_etkinlik).execute()

    # --- GOOGLE DRIVE GÖRSEL ARŞİVLEME ---
    if yuklenen_gorseller and ROOT_DRIVE_FOLDER:
        klasor_adi = f"{datetime.now().year}_{veri.get('protokol', 'Genel')}"
        q = f"name='{klasor_adi}' and '{ROOT_DRIVE_FOLDER}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        mevcut = drive_service.files().list(q=q, fields="files(id)").execute().get("files", [])
        if mevcut:
            hedef_klasor_id = mevcut[0]["id"]
        else:
            meta = {"name": klasor_adi, "mimeType": "application/vnd.google-apps.folder", "parents": [ROOT_DRIVE_FOLDER]}
            hedef_klasor_id = drive_service.files().create(body=meta, fields="id").execute()["id"]

        for dosya in yuklenen_gorseller:
            media = MediaIoBaseUpload(dosya, mimetype=dosya.type, resumable=True)
            drive_service.files().create(
                body={"name": dosya.name, "parents": [hedef_klasor_id]},
                media_body=media,
                fields="id"
            ).execute()

    # --- KİLİNİK KAYITSA ASİSTAN MASASINA (GOOGLE SHEETS) DÜŞÜR ---
    if veri.get("tur") == "klinik":
        ws = get_or_create_is_listesi_ws()
        ws.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            veri.get("protokol", ""),
            veri.get("hasta_maskeli", ""),
            veri.get("telefon", ""),
            f"{veri.get('bolge', '')} - {veri.get('tani', '')}",
            veri.get("intermedya_epikriz", ""),
            veri.get("kontrol_tarihi", ""),
            veri.get("kontrol_notu", ""),
            "HAYIR",
            "HAYIR"
        ])

    st.success("✅ İşlem tamamlandı! Takvime eklendi ve klinik kayıtsa Asistan Masasına aktarıldı.")
    if veri.get("tur") == "klinik":
        st.markdown("**İntermedya Epikriz Çıktısı:**")
        st.code(veri.get("intermedya_epikriz", ""), language="text")
