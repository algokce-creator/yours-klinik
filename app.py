import streamlit as st
import json
import io
import os
from datetime import datetime, timedelta
from openai import OpenAI
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

st.set_page_config(page_title="Yours — Klinik & Yaşam Asistanı", page_icon="🩺", layout="centered")

# --- MACRODROID PAYLAŞIM PARAMETRESİNİ YAKALA (PIN ÖNCESİ KORUMA) ---
incoming_text = st.query_params.get("metin", "")
if incoming_text and "pending_shared_text" not in st.session_state:
    st.session_state.pending_shared_text = incoming_text

# --- 1. PIN KORUMASI ---
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("🔐 Yours — Güvenli Giriş")
    if st.session_state.get("pending_shared_text"):
        st.info("📲 Telefondan aktarılan ses metni hazır bekliyor. Devam etmek için PIN girin.")
    pin_input = st.text_input("Klinik PIN Kodunu Girin:", type="password")
    if st.button("Giriş Yap", type="primary"):
        correct_pin = str(st.secrets.get("CLINIC_PIN", "1968"))
        if pin_input == correct_pin:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("❌ Hatalı PIN kodu.")
    st.stop()

client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

def get_google_services():
    possible_keys = [
        "GOOGLE_CREDENTIALS_JSON",
        "GOOGLE_CREDENTIALS",
        "google_credentials",
        "GOOGLE_TOKEN_JSON",
        "gcp_service_account",
        "GOOGLE_SERVICE_ACCOUNT",
    ]
    raw_creds = None
    for k in possible_keys:
        if k in st.secrets:
            raw_creds = st.secrets[k]
            break

    if raw_creds is None:
        st.error(f"Google kimlik anahtarı bulunamadı. Mevcut Secrets anahtarları: {list(st.secrets.keys())}")
        st.stop()

    creds_info = json.loads(raw_creds) if isinstance(raw_creds, str) else dict(raw_creds)

    if creds_info.get("type") == "service_account":
        from google.oauth2.service_account import Credentials as ServiceAccountCredentials
        scopes = [
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_service_account_info(creds_info, scopes=scopes)
    else:
        creds = Credentials.from_authorized_user_info(creds_info)

    return build("calendar", "v3", credentials=creds), build("drive", "v3", credentials=creds)

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

# --- SESSION STATE BAŞLATMA & MACRODROID METNİNİ AKTARMA ---
if "transcript_text" not in st.session_state:
    st.session_state.transcript_text = ""
if "last_audio_bytes" not in st.session_state:
    st.session_state.last_audio_bytes = None

if st.session_state.get("pending_shared_text"):
    st.session_state.transcript_text = st.session_state.pending_shared_text
    del st.session_state.pending_shared_text
    st.query_params.clear()

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
        patient_code = st.text_input("Hasta Kodu / Baş Harf", placeholder="Boş kalırsa Atanmamış")
    with col3:
        region = st.selectbox(
            "Anatomik Bölge",
            ["Diz", "Omuz", "Kalça", "Ayak / Ayak Bileği", "Omurga", "El / El Bileği", "Diğer"]
        )
    diagnosis_hint = st.text_input("Ön Tanı / İşlem (İsteğe Bağlı)", placeholder="Örn: 2. Metatars Ödem, OW-HTO, PRP")
else:
    personal_category = st.selectbox(
        "Kişisel Kategori",
        ["📌 Hatırlatma / Görev", "🛒 Alışveriş Listesi", "🎭 Sosyal / Aile / Etkinlik", "💡 Genel Fikir / Not"]
    )

st.subheader("1. Ses Kaydı Al veya Dosya Yükle")
audio_value = st.audio_input("Doğrudan tarayıcıdan konuşun")
uploaded_audio = st.file_uploader("Veya Xiaomi Ses Kaydedici dosyasını yükleyin (m4a, mp3, wav)", type=["m4a", "mp3", "wav"])

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

edited_transcript = st.text_area(
    "2. Ham Metin (Xiaomi kaydediciden aktarılan veya sesle dökülen metin):",
    value=st.session_state.transcript_text,
    height=130,
    placeholder="MacroDroid ile paylaştığınızda veya ses kaydı aldığınızda metin burada görünecek..."
)

uploaded_images = []
if "Klinik" in record_mode:
    uploaded_images = st.file_uploader(
        "3. Röntgen / Klinik Fotoğraf Ekle (İsteğe Bağlı)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True
    )

if st.button("✨ Onayla ve Takvim / Drive'a İşle", type="primary", use_container_width=True):
    if not edited_transcript.strip():
        st.warning("Lütfen önce ses kaydı alın veya metin girin.")
        st.stop()

    with st.spinner("Yapay zeka analiz ediyor ve Takvim / Drive'a işliyor..."):
        cal_service, drive_service = get_google_services()
        root_folder_id = st.secrets.get("DRIVE_FOLDER_ID") or st.secrets.get("drive_folder_id")
        today_iso = now.strftime("%Y-%m-%d")

        system_prompt = f"""
        Bugünün tarihi: {today_iso}.
        Sen Prof. Dr. Alper Gökçe'nin (Ortopedi ve Travmatoloji Uzmanı) akıllı asistanısın.
        Kullanıcı modu: {record_mode}.
        Görevin metni analiz edip SADECE geçerli bir JSON döndürmektir.
        Kurallar:
        - Eğer metin tıbbi değil de kişisel/sosyal/alışveriş notu ise 'tur' alanını 'KISISEL' yap.
        - Eğer tıbbi ise 'tur' alanını 'KLINIK' yap, ortopedik terminolojiyi koru.
        - Metinde ileri tarihli bir kontrol, pansuman veya hatırlatma geçiyorsa 'kontrol_tarihi' alanını 'YYYY-MM-DD' formatında hesapla; yoksa null yap.
        - 'intermedya_metni' alanına İntermedya hasta dosyasına doğrudan yapıştırılabilecek kısa, resmi epikriz/muayene cümlesi yaz.

        JSON Şeması:
        {{
          "tur": "KLINIK" veya "KISISEL",
          "baslik": "Kısa başlık (Tanı veya Konu)",
          "ozet_maddeler": ["Madde 1", "Madde 2", "Madde 3"],
          "icd10_onerisi": "Tıbbi ise olası ICD-10 kodu ve adı, değilse boş",
          "intermedya_metni": "İntermedya kartına yapıştırılacak resmi klinik özet",
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

        if ai_data.get("tur") == "KLINIK" and "Klinik" in record_mode:
            safe_code = patient_code.strip() if patient_code.strip() else f"ATANMAMIS_{now.strftime('%H%M')}"
            diag_text = diagnosis_hint.strip() if diagnosis_hint.strip() else ai_data.get("baslik", "Klinik Değerlendirme")

            if root_folder_id:
                year_folder_id = get_or_create_folder(drive_service, year_str, root_folder_id)
                patient_folder_name = f"{protocol_no}_{safe_code}".replace(" ", "_")
                patient_folder_id = get_or_create_folder(drive_service, patient_folder_name, year_folder_id)

                if st.session_state.last_audio_bytes:
                    audio_media = MediaIoBaseUpload(io.BytesIO(st.session_state.last_audio_bytes), mimetype="audio/wav")
                    drive_service.files().create(
                        body={"name": f"{protocol_no}_Ses_{date_stamp}.wav", "parents": [patient_folder_id]},
                        media_body=audio_media
                    ).execute()

                for idx, img in enumerate(uploaded_images, start=1):
                    img_media = MediaIoBaseUpload(io.BytesIO(img.getvalue()), mimetype=img.type)
                    ext = os.path.splitext(img.name)[1] or ".jpg"
                    drive_service.files().create(
                        body={"name": f"{protocol_no}_{region}_Gorsel_{idx}_{date_stamp}{ext}", "parents": [patient_folder_id]},
                        media_body=img_media
                    ).execute()

            cal_title = f"[{protocol_no}] - [{region}] - [{diag_text}]"
            bullets_str = "\n".join([f"• {m}" for m in ai_data.get("ozet_maddeler", [])])
            cal_desc = (
                f"Hasta Kodu: {safe_code}\n"
                f"ICD-10 Önerisi: {ai_data.get('icd10_onerisi', '-')}\n\n"
                f"Klinik Özet:\n{bullets_str}"
            )
            color_id = "9"
        else:
            cat_label = personal_category if "Kişisel" in record_mode else "📌 Kişisel/Sosyal"
            cal_title = f"{cat_label}: {ai_data.get('baslik', 'Kişisel Not')}"
            bullets_str = "\n".join([f"• {m}" for m in ai_data.get("ozet_maddeler", [])])
            cal_desc = f"{bullets_str}\n\nDetay: {edited_transcript}"
            color_id = "2"

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
                    "reminders": {
                        "useDefault": False,
                        "overrides": [
                            {"method": "popup", "minutes": 1440},
                            {"method": "popup", "minutes": 120},
                            {"method": "email", "minutes": 1440},
                        ],
                    },
                }
            ).execute()

    st.success("✅ Kayıt başarıyla Takvim ve Drive'a işlendi!")
    st.markdown(f"**Başlık:** `{cal_title}`")
    if ai_data.get("tur") == "KLINIK":
        st.caption("📋 İntermedya'ya Yapıştırılacak Metin:")
        st.code(ai_data.get("intermedya_metni", ""), language=None)
    if kontrol_tarihi:
        st.info(f"📅 Otomatik takip/hatırlatma eklendi: **{kontrol_tarihi}** — {ai_data.get('kontrol_aciklama')}")
