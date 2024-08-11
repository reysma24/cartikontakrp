import os
import re
import time
import asyncio
import logging
import datetime
import sqlite3

from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import ParseMode, Update, ChatType, InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Text
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.dispatcher.handler import CancelHandler
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.utils.exceptions import (BotBlocked, UserDeactivated, RetryAfter, ChatNotFound, BadRequest, NetworkError, CantInitiateConversation, CantTalkWithBots)
from aiogram.utils import exceptions
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher.webhook import get_new_configured_app
from aiogram.types import ParseMode
from aiohttp import web

#PROXY_URL = 'http://proxy.server:3128'

bot = Bot(token="7182437622:AAGZLxBi1H9PGhnFgiBVRUf2pZG2dFJCw2c")
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

in_progress = {}

channel_id = -1002079102928
GROUP_ADMIN = -1002147278286

logging.basicConfig(level=logging.INFO)

class ReportState(StatesGroup):
    waiting_for_report = State()

def get_db_connection():
    conn = sqlite3.connect('rpdatabase.db')
    return conn, conn.cursor()

class BanUserState(StatesGroup):
    awaiting_reason = State()

def get_chat_links():
    conn = sqlite3.connect('rpdatabase.db')
    c = conn.cursor()

    c.execute("SELECT * FROM chat_links")
    links = dict(c.fetchall())

    conn.close()  # Tutup koneksi setelah selesai
    return links

async def check_membership(user_id, chat_id):
    try:
        logging.info(f"Checking membership for user {user_id} in chat {chat_id}")
        member = await bot.get_chat_member(chat_id, user_id)
        logging.info(f"Membership status: {member.status}")
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logging.error(f"Error checking membership for chat {chat_id}: {str(e)}")
        return False

class UserCheckMiddleware(BaseMiddleware):
    async def on_pre_process_update(self, update: Update, data: dict):
        if update.message:
            user_id = update.message.from_user.id
        elif update.callback_query:
            user_id = update.callback_query.from_user.id
        else:
            return

        conn = sqlite3.connect('rpdatabase.db')
        c = conn.cursor()
        c.execute('SELECT * FROM user_profiles WHERE user_id = ?', (user_id,))
        result = c.fetchone()

        if not result:
            c.execute('INSERT INTO user_profiles (user_id, daily_token) VALUES (?, ?)', (user_id, 5))
            conn.commit()

        conn.close()

dp.middleware.setup(UserCheckMiddleware())

async def add_filterwords(words):
    conn = sqlite3.connect('rpdatabase.db')
    c = conn.cursor()
    added_words = []
    existing_words = []
    for word in words:
        try:
            c.execute('INSERT INTO filter_words (word) VALUES (?)', (word,))
            added_words.append(word)
        except sqlite3.IntegrityError:
            existing_words.append(word)
    conn.commit()
    conn.close()
    return added_words, existing_words

async def remove_filterwords(words):
    conn = sqlite3.connect('rpdatabase.db')
    c = conn.cursor()
    removed_words = []
    not_found_words = []
    for word in words:
        c.execute('DELETE FROM filter_words WHERE word = ?', (word,))
        if c.rowcount > 0:
            removed_words.append(word)
        else:
            not_found_words.append(word)
    conn.commit()
    conn.close()
    return removed_words, not_found_words

def generate_regex(word):
    """
    Generate a regex pattern that matches word and its common variations.
    """
    pattern = ''.join(f'{char}.?' for char in word)
    return re.compile(pattern, re.IGNORECASE)

async def is_filtered(message_text, filter_words):
    """
    Check if the message contains any of the filter words or their variations.
    """
    for word in filter_words:
        regex = generate_regex(word)
        if regex.search(message_text):
            return True
    return False

async def get_filterwords():
    """
    Retrieve filter words from the database.
    """
    conn = sqlite3.connect('rpdatabase.db')
    c = conn.cursor()
    c.execute('SELECT word FROM filter_words')
    words = [row[0] for row in c.fetchall()]
    conn.close()
    return words

async def get_user_data(user_id):
    conn = sqlite3.connect('rpdatabase.db')
    c = conn.cursor()

    c.execute('SELECT user_id FROM user_profiles WHERE user_id = ?', (user_id,))
    result = c.fetchone()

    conn.close()

    if result:
        return {'user_id': result[0]}  # Kembalikan user_id atau informasi lain yang ada di tabel
    else:
        return None

@dp.message_handler(commands='report')
async def cmd_report(message: types.Message):
    await message.reply("Silakan kirim laporan Anda. Jika ada screenshot, kirim juga dalam pesan yang sama.")
    await ReportState.waiting_for_report.set()

@dp.message_handler(state=ReportState.waiting_for_report, content_types=types.ContentTypes.ANY)
async def process_report(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username if message.from_user.username else "N/A"

    if message.photo:
        report_text = message.caption if message.caption else "N/A"
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        file_path = file_info.file_path
        downloaded_file = await bot.download_file(file_path)

        with open("photo.jpg", "wb") as new_file:
            new_file.write(downloaded_file.read())

        await bot.send_photo(GROUP_ADMIN, photo=open("photo.jpg", "rb"), caption=f"ID: {user_id}\nUsername: @{username}\nLaporan: {report_text}")
        os.remove("photo.jpg")
    else:
        report_text = message.text if message.text else "N/A"
        await bot.send_message(GROUP_ADMIN, f"ID: {user_id}\nUsername: @{username}\nLaporan: {report_text}")

    await message.reply("Terima kasih sudah melapor!")
    await state.finish()

@dp.message_handler(commands=['p'])
async def cmd_reply(message: types.Message):
    if message.chat.id != GROUP_ADMIN:
        await message.reply("Perintah ini hanya bisa digunakan oleh owner.")
        return

    if len(message.text.split()) < 3:
        await message.reply("Format salah. Gunakan /p [id] [pesan]")
        return

    args = message.text.split(maxsplit=2)
    user_id = int(args[1])
    reply_message = args[2]

    try:
        await bot.send_message(user_id, reply_message)
        await message.reply("Pesan berhasil dikirim.")
    except Exception as e:
        await message.reply(f"Error: {e}")

@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    # Pesan sapaan dan penjelasan
    welcome_text = (
        "Hai, selamat datang di bot <b>carikontakrp</b>! 😊\n\n"
        "Kamu bisa menggunakan beberapa hashtag berikut untuk berinteraksi:\n\n"
        "1. <code>#mutual</code> : Saling follow, simpan kontak, atau subscribe channel masing-masing.\n\n"
        "   Contoh: "
        "   <blockquote>#mutual hai, mari berteman</blockquote>\n\n"
        "2. <code>#hfw</code> : Help Forward (Bantu teruskan pesan/status).\n\n"
        "   Contoh:"
        "   <blockquote>#hfw cc asik https://t.me/exampl</blockquote>\n"
    )
    await message.answer(welcome_text, parse_mode=ParseMode.HTML)

@dp.message_handler(commands=['setlink'])
async def set_link(message: types.Message):

    if message.chat.id != GROUP_ADMIN:
        await message.reply("Perintah ini hanya bisa digunakan oleh owner.")
        return

    try:
        command, chat_id, link = message.text.split()

        # Cek keberadaan grup/channel dan status admin bot
        try:
            chat = await bot.get_chat(chat_id)
            bot_member = await bot.get_chat_member(chat_id, bot.id)
            if bot_member.status not in ['administrator', 'creator']:
                await message.reply("Bot tidak memiliki akses admin di grup atau channel ini.")
                return
        except Exception as e:
            await message.reply(f"Tidak dapat mengakses grup/channel: {str(e)}")
            return

        conn, c = get_db_connection()
        c.execute("INSERT OR REPLACE INTO chat_links (chat_id, link) VALUES (?, ?)", (chat_id, link))
        conn.commit()
        conn.close()
        await message.reply("Tautan berhasil diperbarui!")
    except ValueError:
        await message.reply("Format perintah salah. Gunakan /setlink <chat_id> <link>")

@dp.message_handler(commands=['listlink'])
async def list_links(message: types.Message):

    if message.chat.id != GROUP_ADMIN:
        await message.reply("Perintah ini hanya bisa digunakan oleh owner.")
        return

    conn, c = get_db_connection()
    c.execute("SELECT * FROM chat_links")
    chat_links = c.fetchall()
    conn.close()

    if chat_links:
        links_message = "\n".join([f"{chat_id} {link}" for chat_id, link in chat_links])
        await message.reply(f"Tautan yang tersimpan:\n{links_message}", parse_mode=ParseMode.MARKDOWN)
    else:
        await message.reply("Belum ada tautan yang tersimpan.")


# Perintah untuk menghapus tautan dari database
@dp.message_handler(commands=['removelink'])
async def remove_link(message: types.Message):

    if message.chat.id != GROUP_ADMIN:
        await message.reply("Perintah ini hanya bisa digunakan oleh owner.")
        return

    try:
        command, chat_id = message.text.split()
        conn, c = get_db_connection()
        c.execute("DELETE FROM chat_links WHERE chat_id=?", (chat_id,))
        conn.commit()
        conn.close()
        await message.reply("Tautan berhasil dihapus!")
    except ValueError:
        await message.reply("Format perintah salah. Gunakan /removelink <chat_id>")

@dp.message_handler(commands=['addfilterword'])
async def cmd_add_filterword(message: types.Message):

    if message.chat.id != GROUP_ADMIN:
        await message.reply("Perintah ini hanya bisa digunakan oleh owner.")
        return

    if not message.reply_to_message or not message.reply_to_message.text:
        await message.reply("Gunakan perintah ini dengan mereply pesan yang berisi kata yang ingin ditambahkan sebagai filterword.")
        return

    words = [word.strip() for word in message.reply_to_message.text.split(',')]
    added_words, existing_words = await add_filterwords(words)

    response = []
    if added_words:
        response.append(f"Kata-kata berikut berhasil ditambahkan ke filterwords: {', '.join(added_words)}.")
    if existing_words:
        response.append(f"Kata-kata berikut sudah ada di filterwords: {', '.join(existing_words)}.")

    await message.reply("\n".join(response))

@dp.message_handler(commands=['removefilterword'])
async def cmd_remove_filterword(message: types.Message):

    if message.chat.id != GROUP_ADMIN:
        await message.reply("Perintah ini hanya bisa digunakan oleh owner.")
        return

    if not message.reply_to_message or not message.reply_to_message.text:
        await message.reply("Gunakan perintah ini dengan mereply pesan yang berisi kata yang ingin dihapus dari filterword.")
        return

    words = [word.strip() for word in message.reply_to_message.text.split(',')]
    removed_words, not_found_words = await remove_filterwords(words)

    response = []
    if removed_words:
        response.append(f"Kata-kata berikut berhasil dihapus dari filterwords: {', '.join(removed_words)}.")
    if not_found_words:
        response.append(f"Kata-kata berikut tidak ditemukan di filterwords: {', '.join(not_found_words)}.")

    await message.reply("\n".join(response))

@dp.message_handler(commands=['listfilterwords'])
async def cmd_list_filterwords(message: types.Message):
    words = await get_filterwords()
    if words:
        await message.reply("Filter words:\n" + "\n".join(words))
    else:
        await message.reply("Tidak ada filter words yang tersimpan.")

@dp.message_handler(commands=['banuser'], state='*')
async def ban_user_command(message: types.Message):

    if message.chat.id != GROUP_ADMIN:
        await message.reply("Perintah ini hanya bisa digunakan oleh owner.")
        return

    args = message.get_args()
    if not args:
        await message.reply("Gunakan perintah /banuser [id].")
        return

    user_id_to_ban = int(args)
    await BanUserState.awaiting_reason.set()
    await dp.current_state().update_data(user_id_to_ban=user_id_to_ban)
    await message.reply(f"Silakan masukkan alasan untuk ban user dengan ID {user_id_to_ban}:")

@dp.message_handler(state=BanUserState.awaiting_reason, content_types=types.ContentTypes.TEXT)
async def process_ban_reason(message: types.Message, state: FSMContext):
    reason = message.text
    data = await state.get_data()
    user_id_to_ban = data['user_id_to_ban']

    # Simpan ke database
    conn = sqlite3.connect('rpdatabase.db')
    c = conn.cursor()
    c.execute('INSERT INTO banned_users (user_id, reason) VALUES (?, ?)', (user_id_to_ban, reason))
    conn.commit()
    conn.close()

    await state.finish()
    await message.reply(f"User dengan ID {user_id_to_ban} telah di-ban dengan alasan: {reason}")

@dp.message_handler(commands=['broadcast'])
async def cmd_broadcast(message: types.Message):
    if message.chat.id != GROUP_ADMIN:
        await message.reply("Perintah ini hanya bisa digunakan oleh owner.")
        return

    if not message.reply_to_message or not message.reply_to_message.text:
        await message.reply("Gunakan perintah ini dengan mereply pesan yang ingin dibroadcast.")
        return

    broadcast_content = message.reply_to_message.text
    await broadcast_message(broadcast_content)

@dp.message_handler(commands=['resetdailytoken'])
async def reset_daily_token_command(message: types.Message):
    # Cek apakah pesan dikirim dari grup yang benar
    if message.chat.id == GROUP_ADMIN:
        await reset_daily_tokens()
        await broadcast_message("DAILY TOKEN DI RESET, AYO KIRIM PROMOTE KAMU!!")
        await message.reply("Daily tokens telah direset dan pesan broadcast telah dikirim!")
    else:
        await message.reply("Perintah ini hanya bisa digunakan di grup yang ditentukan.")

@dp.message_handler(commands=['unban'], state='*')
async def unban_user_command(message: types.Message):

    if message.chat.id != GROUP_ADMIN:
        await message.reply("Perintah ini hanya bisa digunakan oleh owner.")
        return

    args = message.get_args()
    if not args:
        await message.reply("Gunakan perintah /unban [id].")
        return

    user_id_to_unban = int(args)

    # Hapus dari database
    conn = sqlite3.connect('rpdatabase.db')
    c = conn.cursor()
    c.execute('DELETE FROM banned_users WHERE user_id = ?', (user_id_to_unban,))
    conn.commit()
    conn.close()

    await message.reply(f"User dengan ID {user_id_to_unban} telah di-unban.")

@dp.message_handler(lambda message: any(hashtag.lower() in (message.text or message.caption or "").lower() for hashtag in ['#hfw', '#mutual']) and message.chat.type == 'private', content_types=types.ContentType.ANY)
async def handle_hashtag(message: types.Message, state: FSMContext):
    conn = sqlite3.connect('rpdatabase.db')
    c = conn.cursor()
    c.execute('SELECT reason FROM banned_users WHERE user_id = ?', (message.from_user.id,))
    result = c.fetchone()

    if result:
        reason = result[0]
        await message.reply(f"Kamu di-ban karena alasan: {reason}")
        conn.close()
        return

    if not message.from_user.username:
        await message.reply("Kamu tidak menggunakan username. Silahkan gunakan username di pengaturan Telegram kamu")
        conn.close()
        return

    # Check membership
    chat_links = get_chat_links()
    non_member_chats = []
    for chat_id in chat_links:
        if not await check_membership(message.from_user.id, chat_id):
            non_member_chats.append(chat_id)

    if non_member_chats:
        links = [f"[Group/Channel]({chat_links[chat_id]})" for chat_id in non_member_chats]
        await message.reply(
            "Silahkan masuk terlebih dahulu ke sini sebelum menggunakan botnya: " + ", ".join(links),
            parse_mode=ParseMode.MARKDOWN
        )
        conn.close()
        raise CancelHandler()

    if '#hfw' in (message.text or "").lower():
        if message.content_type == types.ContentType.TEXT:
            words_count = len((message.text or "").split())
            if words_count < 7:
                await message.reply("Pesan terlalu pendek, minimal harus ada 5 kata.")
                conn.close()
                return

            # Pengecekan Link Telegram
            links = [word for word in message.text.split() if word.startswith("https://t.me/")]
            if len(links) != 1:
                await message.reply("Pesan harus mengandung tepat 1 link Telegram yang valid.")
                conn.close()
                return

        # Check for filter words
        filter_words = await get_filterwords()
        if await is_filtered(message.text, filter_words):
            await message.reply("Bot mendeteksi adanya filterword, apakah kamu yakin mengirim ini? Pastikan tidak ada kata kasar.")
            return

    if '#mutual' in (message.text or "").lower():
        if message.content_type == types.ContentType.TEXT:
            words_count = len((message.text or "").split())
            if words_count < 5:
                await message.reply("Pesan terlalu pendek, minimal harus ada 5 kata.")
                conn.close()
                return

            # Pengecekan Link dan Karakter "@"
            if any(word.startswith("https://") or "@" in word for word in message.text.split()):
                await message.reply("Pesan tidak boleh mengandung link atau karakter '@'.")
                conn.close()
                return

        # Check for filter words
        filter_words = await get_filterwords()
        if await is_filtered(message.text, filter_words):
            await message.reply("Bot mendeteksi adanya filterword, apakah kamu yakin mengirim ini? Pastikan tidak ada kata kasar.")
            return

    await state.update_data(message_content=message, content_type=message.content_type)

    keyboard = InlineKeyboardMarkup(row_width=2)
    confirm_button = InlineKeyboardButton(text="Konfirmasi", callback_data="confirm_send")
    cancel_button = InlineKeyboardButton(text="Batal", callback_data="cancel_send")

    keyboard.add(confirm_button, cancel_button)

    await message.reply("Konfirmasi untuk mengirim pesan ini ke channel:", reply_markup=keyboard)

async def send_to_channels(message_content: types.Message, content_type: str):
    log_channel_id = -1002151200980  # ID channel untuk log
    bot = message_content.bot
    user_id = message_content.from_user.id
    user_username = message_content.from_user.username
    user_profile_link = f"https://t.me/{user_username}"

    try:
        # Inisialisasi variabel sent_message dan post_link
        sent_message = None
        post_link = None

        # Mengirim pesan berdasarkan tipe konten
        if content_type == types.ContentType.TEXT:
            if '#mutual' in (message_content.text or "").lower():
                text_without_hashtag = message_content.text.replace('#mutual', '').strip()
                keyboard = InlineKeyboardMarkup()
                button = InlineKeyboardButton(text="Profile", url=user_profile_link)
                keyboard.add(button)
                sent_message = await bot.send_message(
                    chat_id=channel_id,
                    text=text_without_hashtag,
                    reply_markup=keyboard,
                    parse_mode=types.ParseMode.HTML  # Ubah ke HTML
                )
            else:
                # Mencari link yang dimulai dengan "https://t.me/"
                link = [word for word in message_content.text.split() if word.startswith("https://t.me/")]
                if link:
                    link = link[0]
                else:
                    link = None

                # Menghapus link dari teks jika ada
                text_without_link = ' '.join([word for word in message_content.text.split() if not word.startswith("https://t.me/")])
                keyboard = InlineKeyboardMarkup()
                button = InlineKeyboardButton(text="Link", url=link) if link else None
                if button:
                    keyboard.add(button)
                sent_message = await bot.send_message(
                    chat_id=channel_id,
                    text=text_without_link,
                    reply_markup=keyboard if button else None,
                    parse_mode=types.ParseMode.HTML  # Ubah ke HTML
                )
        elif content_type == types.ContentType.PHOTO:
            if '#mutual' in (message_content.caption or "").lower():
                caption_without_hashtag = message_content.caption.replace('#mutual', '').strip()
                keyboard = InlineKeyboardMarkup()
                button = InlineKeyboardButton(text="Profile", url=user_profile_link)
                keyboard.add(button)
                sent_message = await bot.send_photo(
                    chat_id=channel_id,
                    photo=message_content.photo[-1].file_id,  # Mengambil resolusi tertinggi
                    caption=caption_without_hashtag,
                    reply_markup=keyboard,
                    parse_mode=types.ParseMode.HTML  # Ubah ke HTML
                )
            elif message_content.caption:
                link = [word for word in message_content.caption.split() if word.startswith("https://t.me/")]
                if link:
                    link = link[0]
                else:
                    link = None

                caption_without_link = ' '.join([word for word in message_content.caption.split() if not word.startswith("https://t.me/")])
                keyboard = InlineKeyboardMarkup()
                button = InlineKeyboardButton(text="Link", url=link) if link else None
                if button:
                    keyboard.add(button)
                sent_message = await bot.send_photo(
                    chat_id=channel_id,
                    photo=message_content.photo[-1].file_id,  # Mengambil resolusi tertinggi
                    caption=caption_without_link,
                    reply_markup=keyboard if button else None,
                    parse_mode=types.ParseMode.HTML  # Ubah ke HTML
                )
            else:
                sent_message = await bot.send_photo(
                    chat_id=channel_id,
                    photo=message_content.photo[-1].file_id,  # Mengambil resolusi tertinggi
                    parse_mode=types.ParseMode.HTML  # Ubah ke HTML
                )
        elif content_type == types.ContentType.VIDEO:
            if '#mutual' in (message_content.caption or "").lower():
                caption_without_hashtag = message_content.caption.replace('#mutual', '').strip()
                keyboard = InlineKeyboardMarkup()
                button = InlineKeyboardButton(text="Profile", url=user_profile_link)
                keyboard.add(button)
                sent_message = await bot.send_video(
                    chat_id=channel_id,
                    video=message_content.video.file_id,
                    caption=caption_without_hashtag,
                    reply_markup=keyboard,
                    parse_mode=types.ParseMode.HTML  # Ubah ke HTML
                )
            elif message_content.caption:
                link = [word for word in message_content.caption.split() if word.startswith("https://t.me/")]
                if link:
                    link = link[0]
                else:
                    link = None

                caption_without_link = ' '.join([word for word in message_content.caption.split() if not word.startswith("https://t.me/")])
                keyboard = InlineKeyboardMarkup()
                button = InlineKeyboardButton(text="Link", url=link) if link else None
                if button:
                    keyboard.add(button)
                sent_message = await bot.send_video(
                    chat_id=channel_id,
                    video=message_content.video.file_id,
                    caption=caption_without_link,
                    reply_markup=keyboard if button else None,
                    parse_mode=types.ParseMode.HTML  # Ubah ke HTML
                )
            else:
                sent_message = await bot.send_video(
                    chat_id=channel_id,
                    video=message_content.video.file_id,
                    parse_mode=types.ParseMode.HTML  # Ubah ke HTML
                )
        elif content_type == types.ContentType.DOCUMENT:
            sent_message = await bot.send_document(
                chat_id=channel_id,
                document=message_content.document.file_id,
                caption=message_content.caption,
                parse_mode=types.ParseMode.HTML  # Ubah ke HTML
            )

        # Membuat link post yang terkirim ke channel
        if sent_message:
            post_link = f"https://t.me/carikontakrp/{sent_message.message_id}"

        # Membuat dan mengirim catatan informasi pengirim
        log_message = f"[{user_id}] [{user_username}] [{post_link}]"
        await bot.send_message(
            chat_id=log_channel_id,
            text=log_message
        )

        return sent_message, None

    except Exception as e:
        return None, e




@dp.callback_query_handler(lambda c: c.data == 'confirm_send', state='*')
async def confirm_send_to_channel(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id

    if in_progress.get(user_id, False):
        await callback_query.answer("Sedang diproses, sabar ya...")
        return

    in_progress[user_id] = True
    try:
        data = await state.get_data()
        message_content = data['message_content']
        content_type = data['content_type']

        sent_message, _ = await send_to_channels(message_content, content_type)
        if sent_message:
            conn = sqlite3.connect('rpdatabase.db')
            c = conn.cursor()
            # Kurangi daily token dan ambil nilai token yang tersisa
            c.execute('UPDATE user_profiles SET daily_token = daily_token - 1 WHERE user_id = ?', (user_id,))
            conn.commit()
            c.execute('SELECT daily_token FROM user_profiles WHERE user_id = ?', (user_id,))
            remaining_tokens = c.fetchone()[0]
            conn.close()

            combined_message = (
              "Promote terkirim!\n"
             f"[Periksa pesanmu di sini.]({sent_message.url})\n\n"
             f"Tokenmu berkurang 1, sisa token: {remaining_tokens}\n")
            await callback_query.message.edit_text(combined_message, parse_mode="Markdown")
        else:
            await callback_query.message.edit_text("ERROR!!, Gagal mengirim pesan ke channel.\nSilahkan gunakan /report untuk membantu mengatasi ini")

        await callback_query.answer()
        await state.finish()
    finally:
        in_progress[user_id] = False

@dp.callback_query_handler(lambda c: c.data == 'cancel_send', state='*')
async def cancel_send_to_channel(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_text("Pengiriman dibatalkan.")
    await callback_query.answer()

@dp.message_handler(commands=['allbanuser'], state='*')
async def cek_all_ban_user_command(message: types.Message):
    conn = sqlite3.connect('rpdatabase.db')
    c = conn.cursor()
    c.execute('SELECT user_id, reason FROM banned_users')
    banned_users = c.fetchall()
    conn.close()

    if not banned_users:
        await message.reply("Tidak ada user yang di-ban.")
        return

    for user_id, reason in banned_users:
        try:
            user_info = await bot.get_chat(user_id)
            username = user_info.username or user_info.full_name
        except Exception as e:
            username = "Unknown"

        response_message = f"Username: {username}\nID: {user_id}\nReason: {reason}"
        await message.reply(response_message)


@dp.message_handler(lambda message: message.chat.type == types.ChatType.PRIVATE, content_types=types.ContentType.ANY)
async def handle_unknown(message: types.Message):
    await message.reply("Maaf, perintah tidak dikenali. Silakan ketik /start untuk memulai atau ikuti petunjuk yang diberikan")

async def broadcast_message(message: str):
    total_users = 0
    successful_sends = 0
    users_deleted = 0

    # Connect to the database
    conn = sqlite3.connect('rpdatabase.db')
    c = conn.cursor()

    # Fetch all user_ids from user_profiles table
    c.execute('SELECT user_id FROM user_profiles')
    user_ids = c.fetchall()

    # Send message to each user_id
    for user_id in user_ids:
        total_users += 1
        try:
            await bot.send_message(user_id[0], message)
            successful_sends += 1
        except BotBlocked:
            print(f"User {user_id[0]} blocked the bot, removing from database.")
            c.execute('DELETE FROM user_profiles WHERE user_id = ?', (user_id[0],))
            users_deleted += 1
        except UserDeactivated:
            print(f"User {user_id[0]} account is deactivated, removing from database.")
            c.execute('DELETE FROM user_profiles WHERE user_id = ?', (user_id[0],))
            users_deleted += 1
        except RetryAfter as e:
            print(f"User {user_id[0]} is rate limited. Retry after {e.timeout} seconds.")
            # Handle rate limit, for example delay and retry
            await asyncio.sleep(e.timeout)
            await broadcast_message(message)  # Retry sending the message
        except ChatNotFound:
            print(f"Chat not found for user {user_id[0]}, removing from database.")
            c.execute('DELETE FROM user_profiles WHERE user_id = ?', (user_id[0],))
            users_deleted += 1
        except CantInitiateConversation:
            print(f"Bot can't initiate conversation with user {user_id[0]}, removing from database.")
            c.execute('DELETE FROM user_profiles WHERE user_id = ?', (user_id[0],))
            users_deleted += 1
        except CantTalkWithBots:
            print(f"Bot can't send messages to bot {user_id[0]}, removing from database.")
            c.execute('DELETE FROM user_profiles WHERE user_id = ?', (user_id[0],))
            users_deleted += 1
        except BadRequest as e:
            print(f"Bad request error for user {user_id[0]}: {e}")
            # Handle other BadRequest errors
        except NetworkError as e:
            print(f"Network error for user {user_id[0]}: {e}")
            # Handle network errors
        except Exception as e:
            print(f"Failed to send message to {user_id[0]}: {e}")

    # Commit changes and close the database connection
    conn.commit()
    conn.close()

    report_message = (
        f"ROLEPLAY BASE\n\n"
        f"Broadcast complete!\n"
        f"Total users: {total_users}\n"
        f"Successful sends: {successful_sends}\n"
        f"Users deleted: {users_deleted}"
    )
    try:
        await bot.send_message(GROUP_ADMIN, report_message)
    except Exception as e:
        print(f"Failed to send report message to channel {GROUP_ADMIN}: {e}")

async def reset_daily_tokens():
    conn = sqlite3.connect('rpdatabase.db')
    c = conn.cursor()
    c.execute('UPDATE user_profiles SET daily_token = 5 WHERE daily_token < 5')
    conn.commit()
    conn.close()

async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)

async def on_shutdown(app):
    await bot.delete_webhook()
    await bot.close()
    await dp.storage.close()

# Inisialisasi app dengan webhook
WEBHOOK_PATH = '/webhook'
WEBHOOK_URL = f"https://mammoth-shaylynn-reyma24-928aa8cf.koyeb.app{WEBHOOK_PATH}"
#.koyeb.app/
app = get_new_configured_app(dispatcher=dp, path=WEBHOOK_PATH)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
