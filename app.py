import streamlit as st
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import datetime
import io
import json

st.set_page_config(page_title="Yours Klinik", page_icon="🩺", layout="centered")
st.title("🩺 Yours — Klinik Asistan")

client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])
SCOPES = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/drive.file"]
creds_info = json.loads(st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)

calendar_service = build("calendar", "v3", credentials=creds)
drive_service = build("drive", "v3", credentials=creds)

hasta_kodu = st.text_input("Hasta Kodu veya Adı", placeholder="Örn: AG-001")
ses_verisi = st.audio_input("Muayene Notunu Sesli Kaydet")
gorseller = st.file_uploader(
    "Röntgen / Klinik Fotoğraf Ekle (İsteğe Bağlı)",
    type=["jpg", "jpeg", "png"],
    accept_multiple_files=True
)

if st.button("✨ Özetle ve Takvim / Drive'a İşle", type="primary", use_container_width=True):
    if not ses_verisi:
        st.warning("Lütfen önce mikrofon simgesine basıp ses kaydı alın.")
    else:
        with st.spinner("Ses çözülüyor ve klinik özet hazırlanıyor..."):
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=("kayit.wav", ses_verisi.getvalue(), "audio/wav"),
                language="tr"
            )
            ham_metin = transcript.text

            yanit = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.2,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Sen bir Ortopedi ve Travmatoloji uzmanının klinik asistanısın. "
                            "Verilen ham muayene diktesini yalnızca şu üç başlık altında kısa, net "
                            "ve tıbbi terminolojiye uygun Türkçe olarak düzenle. Metinde olmayan hiçbir bilgiyi ekleme:\n"
                            "- Şikayet/Bulgu:\n- Uygulama/İşlem:\n- Plan:"
                        )
                    },
                    {"role": "user", "content": ham_metin}
                ]
            )
            ozet_not = yanit.choices[0].message.content.strip()

            drive_linkleri = []
            if gorseller:
                for dosya in gorseller:
                    dosya_adi = f"{hasta_kodu or 'Klinik'}_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}_{dosya.name}"
                    media = MediaIoBaseUpload(io.BytesIO(dosya.getvalue()), mimetype=dosya.type)
                    meta = {"name": dosya_adi, "parents": [st.secrets["DRIVE_FOLDER_ID"]]}
                    yuklenen = drive_service.files().create(body=meta, media_body=media, fields="id, webViewLink").execute()
                    drive_linkleri.append(f"📎 {dosya.name}: {yuklenen.get('webViewLink')}")

            aciklama_metni = ozet_not
            if drive_linkleri:
                aciklama_metni += "\n\n--- Ekli Görüntüler ---\n" + "\n".join(drive_linkleri)

            baslangic = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
            bitis = baslangic + datetime.timedelta(minutes=15)
            etkinlik = {
                "summary": f"Klinik Not - {hasta_kodu}" if hasta_kodu else "Klinik Not",
                "description": aciklama_metni,
                "start": {"dateTime": baslangic.isoformat()},
                "end": {"dateTime": bitis.isoformat()},
            }
            calendar_service.events().insert(calendarId=st.secrets["CALENDAR_ID"], body=etkinlik).execute()

        st.success("Klinik not ve görseller başarıyla Google Takvim ve Drive'a işlendi!")
        st.markdown(aciklama_metni)
