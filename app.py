import streamlit as st
import json
import io
import os
from datetime import datetime, timedelta
from openai import OpenAI
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# --- SAYFA AYARLARI ---
st.set_page_config(page_title="Yours — Klinik & Kişisel Asistan", page_icon="🩺", layout="centered")

# --- 1. PIN KORUMASI ---
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("🔐 Yours — Güvenli Giriş")
    pin_input = st.text_input("Klinik PIN Kodunu Girin:", type="password")
    if st.button("Giriş Yap", type="primary"):
        correct_pin = str(st.secrets.get("CLINIC_PIN", "1968"))
        if pin_input == correct_pin:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("❌ Hatalı PIN kodu.")
    st.stop()

# --- API & GOOGLE BAĞLANTILARI ---
client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

def get_google_services():
    creds_info = json.loads(st.secrets["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_authorized_user_info(creds_info)
    cal_service = build("calendar", "v3", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    return cal_service, drive_service

def get_or_create_folder(drive_service, folder_name, parent_id):
    query = (
        f"name='{folder_name}' and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = drive_service.files().create(body=file_metadata, fields="id").execute()
    return folder["id"]

# --- SESSION STATE BAŞLATMA ---
if "transcript_text" not in st.session_state:
    st.session_state.transcript_text = ""
if "last_audio_bytes" not in st.session_state:
    st.session_state.last_audio_bytes = None

# --- ANA ARAYÜZ ---
st.title("🩺 Yours — Klinik & Yaşam Asistanı")

record_mode = st.radio(
    "Kayıt Türü Seçin:",
    ["🩺 Klinik Muayene (KVKK Anonim)", "🌿 Kişisel / Sosyal / Alışveriş Notu"],
    horizontal=True
)

now = datetime.now()
year_str = now.strftime("%Y")
date_stamp = now.strftime("%Y%m%d_%H%M")

if "Klinik" in record_mode:
    col1, col2, col3 = st.columns(3)
    with col1:
        protocol_no = st.text_input("Protokol No", value=f"{year_str}-{now.strftime('%m%d%H%M')}")
    with col2:
        patient_code = st.text_input("Hasta Kodu / Baş Harf", placeholder="Örn: A.Y. veya AG-042")
    with col3:
        region = st.selectbox(
            "Anatomik Bölge",
            ["Diz", "Omuz", "Kalça", "Ayak / Ayak Bileği", "Omurga", "El / El Bileği", "Diğer"]
        )
    diagnosis_hint = st.text_input("Ön Tanı / İşlem (İsteğe Bağlı)", placeholder="Örn: Medial Gonartroz, OW-HTO, PRP")
else:
    personal_category = st.selectbox(
        "Kişisel Kategori",
        ["📌 Hatırlatma / Görev", "🛒 Alışveriş Listesi", "🎭 Sosyal / Aile / Etkinlik", "💡 Genel Fikir / Not"]
    )

# --- SES KAYDI VEYA DOSYA YÜKLEME ---
st.subheader("1. Ses Kaydı Al veya Yükle")
audio_value = st.audio_input("Mikrofona basıp konuşun")
uploaded_audio = st.file_uploader("Veya çevrimdışı ses dosyası yükleyin (m4a, mp3, wav)", type=["m4a", "mp3", "wav"])

active_audio = audio_value if audio_value is not None else uploaded_audio

if active_audio is not None:
    audio_bytes = active_audio.getvalue()
    if audio_bytes != st.session_state.last_audio_bytes:
        st.session_state.last_audio_bytes = audio_bytes
        with st.spinner("Ses yazıya dökülüyor (Whisper)..."):
            audio_bio = io.BytesIO(audio_bytes)
            audio_bio.name = getattr(active_audio, "name", "kayit.wav")
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_bio,
                language="tr"
            )
            st.session_state.transcript_text = transcript.text

# --- TRANSKRİPT DÜZENLEME (HUMAN-IN-THE-LOOP) ---
edited_transcript = st.text_area(
    "2. Ham Metin (Gerekirse tıbbi terimleri veya detayları düzeltip onaylayın):",
    value=st.session_state.transcript_text,
    height=130,
    placeholder="Ses kaydı yaptığınızda metin burada görünecek..."
)

# --- GÖRSEL / RÖNTGEN YÜKLEME ---
uploaded_images = []
if "Klinik" in record_mode:
    uploaded_images = st.file_uploader(
        "3. Röntgen / Klinik Fotoğraf Ekle (İsteğe Bağlı)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True
    )

# --- İŞLEME VE KAYDETME ---
if st.button("✨ Onayla ve Takvim / Drive'a İşle", type="primary", use_container_width=True):
    if not edited_transcript.strip():
        st.warning("Lütfen önce ses kaydı alın veya metin girin.")
        st.stop()

    with st.spinner("Yapay zeka analiz ediyor, kontrol tarihi hesaplıyor ve kaydediyor..."):
        cal_service, drive_service = get_google_services()
        root_folder_id = st.secrets["DRIVE_FOLDER_ID"]
        today_iso = now.strftime("%Y-%m-%d")

        # GPT Yapılandırılmış Çıktı (JSON)
        system_prompt = f"""
        Bugünün tarihi: {today_iso}.
        Sen Prof. Dr. Alper Gökçe'nin (Ortopedi ve Travmatoloji Uzmanı) akıllı asistanısın.
        Kullanıcı modu: {record_mode}.
        Görevin metni analiz edip SADECE geçerli bir JSON döndürmektir.
        Kurallar:
        - Eğer metin tıbbi değil de kişisel/sosyal/alışveriş notu ise 'tur' alanını 'KISISEL' yap.
        - Eğer tıbbi ise 'tur' alanını 'KLINIK' yap, ortopedik terminolojiyi koru.
        - Metinde '2 hafta sonra kontrol', '10 gün sonra pansuman', 'yarın saat 15:00' gibi ileri tarihli bir eylem varsa 'kontrol_tarihi' alanını 'YYYY-MM-DD' formatında hesapla; yoksa null yap.
        - 'kontrol_aciklama' alanına kısa hatırlatma başlığı yaz.

        JSON Şeması:
        {{
          "tur": "KLINIK" veya "KISISEL",
          "baslik": "Kısa başlık (Tanı veya Konu)",
          "ozet_maddeler": ["Madde 1", "Madde 2", "Madde 3"],
          "icd10_onerisi": "Tıbbi ise olası ICD-10 kodu ve adı, değilse boş",
          "kontrol_tarihi": "YYYY-MM-DD veya null",
          "kontrol_aciklama": "Kontrol veya hatırlatma açıklaması veya null"
        }}
        """

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": edited_transcript}
            ],
            temperature=0.2
        )
        ai_data = json.loads(response.choices[0].message.content)

        # Kayıt Türüne Göre Akış
        if ai_data.get("tur") == "KLINIK" and "Klinik" in record_mode:
            safe_code = patient_code.strip() if patient_code.strip() else "ANONIM"
            diag_text = diagnosis_hint.strip() if diagnosis_hint.strip() else ai_data.get("baslik", "Klinik Değerlendirme")
            
            # Drive Klasör Hiyerarşisi: Klinik_Dosyalar / 2026 / 2026-042_Kod
            year_folder_id = get_or_create_folder(drive_service, year_str, root_folder_id)
            patient_folder_name = f"{protocol_no}_{safe_code}".replace(" ", "_")
            patient_folder_id = get_or_create_folder(drive_service, patient_folder_name, year_folder_id)

            # Ham Ses Yedeğini Drive'a Kaydet
            if st.session_state.last_audio_bytes:
                audio_media = MediaIoBaseUpload(io.BytesIO(st.session_state.last_audio_bytes), mimetype="audio/wav")
                drive_service.files().create(
                    body={"name": f"{protocol_no}_Ses_{date_stamp}.wav", "parents": [patient_folder_id]},
                    media_body=audio_media
                ).execute()

            # Röntgen / Görselleri Yükle
            for idx, img in enumerate(uploaded_images, start=1):
                img_media = MediaIoBaseUpload(io.BytesIO(img.getvalue()), mimetype=img.type)
                ext = os.path.splitext(img.name)[1] or ".jpg"
                drive_service.files().create(
                    body={"name": f"{protocol_no}_{region}_Gorsel_{idx}_{date_stamp}{ext}", "parents": [patient_folder_id]},
                    media_body=img_media
                ).execute()

            # Takvim Etkinliği (KVKK Anonim: Hasta gerçek adı yok, sadece Protokol + Bölge + Tanı)
            cal_title = f"[{protocol_no}] - [{region}] - [{diag_text}]"
            bullets_str = "\n".join([f"• {m}" for m in ai_data.get("ozet_maddeler", [])])
            cal_desc = (
                f"Hasta Kodu: {safe_code}\n"
                f"ICD-10 Önerisi: {ai_data.get('icd10_onerisi', '-')}\n\n"
                f"Klinik Özet:\n{bullets_str}\n\n"
                f"Ham Not: {edited_transcript}"
            )
            color_id = "9"  # Lacivert (Klinik)

        else:
            # Kişisel / Sosyal / Alışveriş Akışı
            cat_label = personal_category if "Kişisel" in record_mode else "📌 Kişisel/Sosyal"
            cal_title = f"{cat_label}: {ai_data.get('baslik', 'Kişisel Not')}"
            bullets_str = "\n".join([f"• {m}" for m in ai_data.get("ozet_maddeler", [])])
            cal_desc = f"{bullets_str}\n\nDetay: {edited_transcript}"
            color_id = "2"  # Adaçayı Yeşili (Kişisel/Sosyal)

        # Ana Takvim Kaydı
        event_start = now.isoformat()
        event_end = (now + timedelta(minutes=20)).isoformat()
        cal_service.events().insert(
            calendarId="primary",
            body={
                "summary": cal_title,
                "description": cal_desc,
                "colorId": color_id,
                "start": {"dateTime": event_start, "timeZone": "Europe/Istanbul"},
                "end": {"dateTime": event_end, "timeZone": "Europe/Istanbul"},
            }
        ).execute()

        # Otomatik Kontrol / İleri Tarihli Hatırlatma Kaydı
        kontrol_tarihi = ai_data.get("kontrol_tarihi")
        if kontrol_tarihi:
            k_title = (
                f"[KONTROL] [{protocol_no}] - {ai_data.get('kontrol_aciklama', 'Kontrol Randevusu')}"
                if ai_data.get("tur") == "KLINIK"
                else f"[HATIRLATMA] {ai_data.get('kontrol_aciklama', ai_data.get('baslik'))}"
            )
            cal_service.events().insert(
                calendarId="primary",
                body={
                    "summary": k_title,
                    "description": f"Otomatik oluşturulan takip kaydı.\nKaynak: {cal_title}",
                    "colorId": "11" if ai_data.get("tur") == "KLINIK" else "5",
                    "start": {"dateTime": f"{kontrol_tarihi}T10:00:00", "timeZone": "Europe/Istanbul"},
                    "end": {"dateTime": f"{kontrol_tarihi}T10:30:00", "timeZone": "Europe/Istanbul"},
                }
            ).execute()

    st.success("✅ Kayıt başarıyla işlendi!")
    st.markdown(f"**Başlık:** `{cal_title}`")
    for m in ai_data.get("ozet_maddeler", []):
        st.markdown(f"- {m}")
    if kontrol_tarihi:
        st.info(f"📅 Otomatik takip/hatırlatma eklendi: **{kontrol_tarihi}** — {ai_data.get('kontrol_aciklama')}")
