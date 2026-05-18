import os
import logging
import asyncio
from flask import Flask, request
from pydantic import BaseModel, Field
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from langchain_openai import ChatOpenAI
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser

# Setup logging untuk memantau logs di dashboard Vercel
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# =====================================================================
# 1. KONFIGURASI API KEY (Mengambil dari Environment Variables Vercel)
# =====================================================================
from pathlib import Path
from dotenv import load_dotenv
# Modifikasi load_dotenv agar mencari file .env di root folder (satu tingkat di atas folder api)
base_dir = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=base_dir / '.env')

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Inisialisasi komponen AI
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, openai_api_key=OPENAI_API_KEY)
search_tool = DuckDuckGoSearchRun()

# Inisialisasi Aplikasi Telegram (Tanpa .run_polling())
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

# Database sementara di memori Serverless (Hanya bertahan selama instance Vercel aktif)
# Untuk produksi skala besar, disarankan menggunakan Redis seperti Upstash
USER_DATA_STORE = {}

# =====================================================================
# 2. STRUKTUR DATA OUTPUT (Pydantic)
# =====================================================================
class ProductDetail(BaseModel):
    harga: str = Field(description="Harga produk dalam Rupiah, contoh: Rp 2.500.000")
    rating: str = Field(description="Rating produk tersebut, contoh: 4.8/5 atau 'Tidak ada'")
    jumlah_pembeli: str = Field(description="Jumlah produk terjual/pembeli, contoh: '15 Terjual' atau 'Tidak ada'")
    link: str = Field(description="URL/Link lengkap menuju toko e-commerce tersebut")
    status_filter: str = Field(description="Tulis 'LOLOS' jika ada produk termahal dengan pembeli > 3. Tulis 'TIDAK_ADA_YANG_COCOK' jika semua hasil pencarian pembelinya <= 3 atau tidak punya rating.")

output_parser = PydanticOutputParser(pydantic_object=ProductDetail)

# =====================================================================
# 3. PROMPT UNTUK AGEN AI
# =====================================================================
prompt_ketat = ChatPromptTemplate.from_template(
    "Anda adalah Agen AI Analis Pasar E-commerce Indonesia.\n"
    "Nama Produk yang dicari: {nama_produk}\n\n"
    "Kumpulan Hasil Pencarian Internet:\n{hasil_pencarian}\n\n"
    "CRITERIA / ATURAN FILTER WAJIB:\n"
    "1. Cari semua produk dari hasil pencarian di atas yang memiliki RATING dan JUMLAH PEMBELI/TERJUAL LEBIH DARI 3.\n"
    "2. Dari daftar produk yang lolos syarat, pilihlah SATU produk yang memiliki HARGA TERMAHAL.\n"
    "3. Jika TIDAK ADA SATUPUN produk yang memenuhi syarat, set status_filter menjadi 'TIDAK_ADA_YANG_COCOK'.\n\n"
    "{format_instruksi}"
)

prompt_bebas = ChatPromptTemplate.from_template(
    "Anda adalah Agen AI Analis Pasar E-commerce Indonesia.\n"
    "Nama Produk yang dicari: {nama_produk}\n\n"
    "Kumpulan Hasil Pencarian Internet:\n{hasil_pencarian}\n\n"
    "CRITERIA / ATURAN BARU:\n"
    "Abaikan semua aturan rating dan jumlah pembeli. Cari dan pilih SATU produk yang memiliki HARGA PALING MAHAL/TINGGI dari hasil pencarian di atas.\n\n"
    "{format_instruksi}"
)

# =====================================================================
# 4. LOGIKA TELEGRAM HANDLERS
# =====================================================================

async def start_command(update: Update, context):
    chat_id = update.message.chat_id
    USER_DATA_STORE[chat_id] = {} # Reset data session
    
    teks_perkenalan = (
        "🤖 **Perkenalkan, Saya MasScoutBot (Serverless v1.0)!**\n"
        "Asisten AI pribadi Anda yang siap berburu produk termahal dan paling kredibel di marketplace Indonesia.\n\n"
        "📝 **Cara Penggunaan Bot:**\n"
        "1. Langsung **ketik nama produk** yang ingin Anda cari (Contoh: `Logitech MX Master 3S`).\n"
        "2. Saya akan menyisir internet untuk mencari produk dengan **Harga Termahal** yang memiliki **Rating & Pembeli > 3**.\n"
        "3. Jika filter ketat tidak terpenuhi, Anda akan diberikan opsi mode bebas.\n\n"
        "Silakan ketik nama produk Anda di bawah ini! 👇"
    )
    await update.message.reply_text(teks_perkenalan, parse_mode="Markdown")

async def handle_message(update: Update, context):
    chat_id = update.message.chat_id
    nama_produk = update.message.text
    
    # Inisialisasi data user jika belum ada
    if chat_id not in USER_DATA_STORE:
        USER_DATA_STORE[chat_id] = {}
        
    USER_DATA_STORE[chat_id]['nama_produk'] = nama_produk
    
    pesan_tunggu = await update.message.reply_text(f"🔍 **MasScoutBot** sedang melacak `{nama_produk}`...\nMohon tunggu sebentar.", parse_mode="Markdown")
    
    try:
        # Jalankan pencarian
        query_pencarian = f"{nama_produk} terjual site:tokopedia.com OR site:shopee.co.id OR site:blibli.com"
        hasil_mentah_web = search_tool.run(query_pencarian)
        USER_DATA_STORE[chat_id]['hasil_mentah_web'] = hasil_mentah_web
        
        # Analisis AI Mode Ketat
        prompt_siap = prompt_ketat.fill(nama_produk=nama_produk, hasil_pencarian=hasil_mentah_web, format_instruksi=output_parser.get_format_instructions())
        respon_ai = llm.invoke(prompt_siap)
        hasil = output_parser.parse(respon_ai.content)
        
        # Hapus pesan tunggu
        await context.bot.delete_message(chat_id=chat_id, message_id=pesan_tunggu.message_id)
        
        if hasil.status_filter == "LOLOS":
            format_balasan = (
                f"✅ **Produk Termahal Ditemukan!**\n"
                f"🎯 _(Sesuai Kriteria: Memiliki Rating & Pembeli > 3)_\n\n"
                f"📦 **Nama Produk:** {nama_produk}\n"
                f"💰 **Harga Termahal:** {hasil.harga}\n"
                f"⭐ **Rating:** {hasil.rating}\n"
                f"👥 **Jumlah Pembeli:** {hasil.jumlah_pembeli}\n\n"
                f"🔗 **Link Produk:** {hasil.link}"
            )
            await update.message.reply_text(format_balasan, parse_mode="Markdown")
        else:
            keyboard = [
                [
                    InlineKeyboardButton("Ya, Cari Termahal Saja", callback_data='mode_bebas'),
                    InlineKeyboardButton("Tidak, Batalkan", callback_data='batalkan')
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                f"⚠️ **Kriteria Ketat Tidak Terpenuhi!**\n\n"
                f"Tidak ditemukan produk '{nama_produk}' yang memiliki Rating DAN Pembeli > 3.\n"
                f"Apakah Anda ingin mengabaikan filter pembeli dan tetap menampilkan produk dengan **Harga Termahal**?",
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Error handle_message: {e}")
        await update.message.reply_text("❌ Terjadi kesalahan saat melacak produk, silakan coba lagi beberapa saat lagi.")

async def handle_callback(update: Update, context):
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    user_info = USER_DATA_STORE.get(chat_id, {})
    
    nama_produk = user_info.get('nama_produk')
    hasil_mentah_web = user_info.get('hasil_mentah_web')
    
    if query.data == 'mode_bebas':
        await query.edit_message_text(text=f"🔄 Mengabaikan kriteria... Mencari produk paling mahal untuk '{nama_produk}'...")
        try:
            prompt_siap = prompt_bebas.fill(nama_produk=nama_produk, hasil_pencarian=hasil_mentah_web, format_instruksi=output_parser.get_format_instructions())
            respon_ai = llm.invoke(prompt_siap)
            hasil = output_parser.parse(respon_ai.content)
            
            format_balasan = (
                f"⚠️ **Hasil Pencarian (Filter Diabaikan):**\n\n"
                f"📦 **Nama Produk:** {nama_produk}\n"
                f"💰 **Harga Termahal:** {hasil.harga}\n"
                f"⭐ **Rating:** {hasil.rating}\n"
                f"👥 **Jumlah Pembeli:** {hasil.jumlah_pembeli}\n\n"
                f"🔗 **Link Produk:** {hasil.link}"
            )
            await query.message.reply_text(format_balasan, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error mode_bebas: {e}")
            await query.message.reply_text("❌ Gagal memproses data mode bebas.")
            
    elif query.data == 'batalkan':
        await query.edit_message_text(text="❌ Pencarian dibatalkan. Silakan masukkan nama produk baru.")

# Mendaftarkan Handlers ke dalam Aplikasi Telegram
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("help", start_command))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# =====================================================================
# 5. ENDPOINT FLASK (WEBHOOK ENTRY POINT)
# =====================================================================

@app.route('/', methods=['GET'])
def index():
    return "MasScoutBot API is Running Sucesfully", 200

@app.route('/', methods=['POST'])
def webhook():
    if request.method == "POST":
        try:
            # Membaca update JSON dari Telegram Webhook
            data = request.get_json()
            if data:
                update = Update.de_json(data, telegram_app.bot)
                
                # Menjalankan loop event asinkronus internal Flask untuk memproses update
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                # Inisialisasi aplikasi telegram secara asinkronus sementara untuk menerima request
                loop.run_until_complete(telegram_app.initialize())
                loop.run_until_complete(telegram_app.process_update(update))
                loop.run_until_complete(telegram_app.shutdown())
                loop.close()
                
            return "OK", 200
        except Exception as e:
            logger.error(f"Webhook Error: {e}")
            return f"Internal Error: {e}", 500
    return "Method Not Allowed", 400

if __name__ == '__main__':
    # Untuk testing lokal (bukan saat di deploy ke vercel)
    app.run(debug=True, port=5000)