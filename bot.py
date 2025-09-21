import os
import logging
import json
import random
from dotenv import load_dotenv
from datetime import datetime, timedelta
from io import BytesIO
import asyncio
import re
from math import ceil

from fpdf import FPDF
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, error, InputMediaPhoto, User
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.helpers import escape_markdown

# --- Konfiguration ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYPAL_USER = os.getenv("PAYPAL_USER")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
AGE_ANNA = os.getenv("AGE_ANNA", "18")
AGE_LUNA = os.getenv("AGE_LUNA", "21")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")
NOTIFICATION_GROUP_ID = os.getenv("NOTIFICATION_GROUP_ID")

BTC_WALLET = "1FcgMLNBDLiuDSDip7AStuP19sq47LJB12"
ETH_WALLET = "0xeeb8FDc4aAe71B53934318707d0e9747C5c66f6e"

PRICES = {"bilder": {10: 5, 25: 10, 35: 15}, "videos": {10: 15, 25: 25, 35: 30}}
VOUCHER_FILE = "vouchers.json"
STATS_FILE = "stats.json"
MEDIA_DIR = "image"
DISCOUNT_MSG_HEADER = "--- BOT DISCOUNT DATA (DO NOT DELETE) ---"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Hilfsfunktionen ---
def load_vouchers():
    try:
        with open(VOUCHER_FILE, "r") as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return {"amazon": [], "paysafe": []}

def save_vouchers(vouchers):
    with open(VOUCHER_FILE, "w") as f: json.dump(vouchers, f, indent=2)

def load_stats():
    try:
        with open(STATS_FILE, "r") as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"pinned_message_id": None, "discount_message_id": None, "users": {}, "admin_logs": {}, "events": {}}

def save_stats(stats):
    with open(STATS_FILE, "w") as f: json.dump(stats, f, indent=4)

# --- Rabatt-Persistenz ---
async def save_discounts_to_telegram(context: ContextTypes.DEFAULT_TYPE):
    if not NOTIFICATION_GROUP_ID: return
    stats = load_stats(); discounts_to_save = {}
    for user_id, user_data in stats.get("users", {}).items():
        if "discounts" in user_data: discounts_to_save[user_id] = user_data["discounts"]
    json_string = json.dumps(discounts_to_save, indent=2); message_text = f"{DISCOUNT_MSG_HEADER}\n<tg-spoiler>{json_string}</tg-spoiler>"; discount_message_id = stats.get("discount_message_id")
    try:
        if discount_message_id: await context.bot.edit_message_text(chat_id=NOTIFICATION_GROUP_ID, message_id=discount_message_id, text=message_text, parse_mode='HTML')
        else: raise error.BadRequest("No discount message ID found")
    except error.BadRequest:
        logger.warning("Discount message not found or invalid, creating a new one.")
        try:
            sent_message = await context.bot.send_message(chat_id=NOTIFICATION_GROUP_ID, text=message_text, parse_mode='HTML')
            stats["discount_message_id"] = sent_message.message_id; save_stats(stats)
        except Exception as e: logger.error(f"Could not create a new discount persistence message: {e}")

async def load_discounts_from_telegram(application: Application):
    if not NOTIFICATION_GROUP_ID: logger.info("No notification group ID, skipping discount restore."); return
    logger.info("Attempting to restore discounts from Telegram message..."); stats = load_stats(); discount_message_id = stats.get("discount_message_id")
    if not discount_message_id: logger.warning("No discount message ID in stats.json. Cannot restore discounts."); return
    try:
        message = await application.bot.get_message(chat_id=NOTIFICATION_GROUP_ID, message_id=discount_message_id)
        json_match = re.search(r'<tg-spoiler>(.*)</tg-spoiler>', message.text_html, re.DOTALL)
        if not json_match: logger.error("Could not find spoiler tag in discount message."); return
        discounts_data = json.loads(json_match.group(1)); users_updated = 0
        for user_id, discounts in discounts_data.items():
            if user_id in stats["users"]: stats["users"][user_id]["discounts"] = discounts; users_updated += 1
        if users_updated > 0: save_stats(stats); logger.info(f"Successfully restored discounts for {users_updated} users.")
        else: logger.info("No discounts found in the persistence message to restore.")
    except Exception as e: logger.error(f"An unexpected error occurred during discount restore: {e}")

async def track_event(event_name: str, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if str(user_id) == ADMIN_USER_ID: return
    stats = load_stats(); stats["events"][event_name] = stats["events"].get(event_name, 0) + 1; save_stats(stats); await update_pinned_summary(context)

def is_user_banned(user_id: int) -> bool:
    stats = load_stats(); user_data = stats.get("users", {}).get(str(user_id), {}); return user_data.get("banned", False)

def get_discounted_price(base_price: int, discount_data: dict, package_key: str) -> int:
    """Calculates the final price based on the discount type and value."""
    if not discount_data: return -1
    discount_type = discount_data.get("type")
    
    if discount_type == "percent": # Global percentage (Referral, Inactivity)
        value = discount_data.get("value", 0)
        new_price = base_price * (1 - value / 100)
        return ceil(new_price)
    
    elif discount_type == "euro": # Admin-set Euro amounts
        packages = discount_data.get("packages", {})
        if package_key in packages:
            return max(1, base_price - packages[package_key])
            
    elif discount_type == "percent_packages": # Admin-set percentages
        packages = discount_data.get("packages", {})
        if package_key in packages:
            value = packages[package_key]
            new_price = base_price * (1 - value / 100)
            return ceil(new_price)
            
    return -1

def get_package_button_text(media_type: str, amount: int, user_id: int) -> str:
    stats = load_stats(); user_data = stats.get("users", {}).get(str(user_id), {})
    base_price = PRICES[media_type][amount]; package_key = f"{media_type}_{amount}"
    label = f"{amount} {media_type.capitalize()}"
    
    discount_price = get_discounted_price(base_price, user_data.get("discounts"), package_key)
    
    if discount_price != -1: return f"{label} ~{base_price}~{discount_price}€ ✨"
    else: return f"{label} {base_price}€"

async def check_user_status(user_id: int, context: ContextTypes.DEFAULT_TYPE, ref_id: str = None):
    if str(user_id) == ADMIN_USER_ID: return "admin", False, None
    stats = load_stats(); user_id_str = str(user_id); now = datetime.now(); user_data = stats.get("users", {}).get(user_id_str)
    is_new_user = user_data is None
    if is_new_user:
        stats.get("users", {})[user_id_str] = {
            "first_start": now.isoformat(), "last_start": now.isoformat(), "discount_sent": False,
            "preview_clicks": 0, "viewed_sisters": [], "payments_initiated": [], "banned": False,
            "referrer_id": ref_id, "referrals": [], "successful_referrals": 0, "reward_triggered_for_referrer": False
        }
        if ref_id and ref_id in stats["users"]: stats["users"][ref_id].setdefault("referrals", []).append(user_id_str)
        save_stats(stats); await update_pinned_summary(context); return "new", True, stats["users"][user_id_str]
    last_start_dt = datetime.fromisoformat(user_data.get("last_start"))
    if now - last_start_dt > timedelta(hours=24):
        stats["users"][user_id_str]["last_start"] = now.isoformat(); save_stats(stats); return "returning", True, stats["users"][user_id_str]
    stats["users"][user_id_str]["last_start"] = now.isoformat(); save_stats(stats); return "active", False, stats["users"][user_id_str]

async def send_or_update_admin_log(context: ContextTypes.DEFAULT_TYPE, user: User, event_text: str = ""):
    if not NOTIFICATION_GROUP_ID or str(user.id) == ADMIN_USER_ID: return
    user_id_str = str(user.id); stats = load_stats(); admin_logs = stats.get("admin_logs", {}); user_data = stats.get("users", {}).get(user_id_str, {}); log_message_id = admin_logs.get(user_id_str, {}).get("message_id")
    user_mention = f"[{escape_markdown(user.first_name, version=2)}](tg://user?id={user.id})"; discount_emoji = "💸" if user_data.get("discount_sent") or "discounts" in user_data else ""; banned_emoji = "🚫" if user_data.get("banned") else ""
    first_start_str = "N/A"
    if user_data.get("first_start"): first_start_str = datetime.fromisoformat(user_data["first_start"]).strftime('%Y-%m-%d %H:%M')
    viewed_sisters_list = user_data.get("viewed_sisters", []); viewed_sisters_str = f"(Gesehen: {', '.join(s.upper() for s in sorted(viewed_sisters_list))})" if viewed_sisters_list else ""; preview_clicks = user_data.get("preview_clicks", 0); payments = user_data.get("payments_initiated", []); payments_str = "\n".join(f"   • {p}" for p in payments) if payments else "   • Keine"
    base_text = (f"👤 *Nutzer-Aktivität* {discount_emoji}{banned_emoji}\n\n" f"*Nutzer:* {user_mention} (`{user.id}`)\n" f"*Erster Start:* `{first_start_str}`\n\n" f"🖼️ *Vorschau-Klicks:* {preview_clicks}/25 {viewed_sisters_str}\n\n" f"💰 *Bezahlversuche*\n{payments_str}")
    final_text = f"{base_text}\n\n`Letzte Aktion: {event_text}`".strip()
    try:
        if log_message_id: await context.bot.edit_message_text(chat_id=NOTIFICATION_GROUP_ID, message_id=log_message_id, text=final_text, parse_mode='Markdown')
        else:
            sent_message = await context.bot.send_message(chat_id=NOTIFICATION_GROUP_ID, text=final_text, parse_mode='Markdown')
            admin_logs.setdefault(user_id_str, {})["message_id"] = sent_message.message_id; stats["admin_logs"] = admin_logs; save_stats(stats)
    except error.BadRequest as e:
        if "message to edit not found" in str(e):
            logger.warning(f"Admin log for user {user.id} not found (ID: {log_message_id}). Sending a new one.")
            try:
                sent_message = await context.bot.send_message(chat_id=NOTIFICATION_GROUP_ID, text=final_text, parse_mode='Markdown')
                admin_logs.setdefault(user_id_str, {})["message_id"] = sent_message.message_id; stats["admin_logs"] = admin_logs; save_stats(stats)
            except Exception as e_new: logger.error(f"Failed to send replacement admin log for user {user.id}: {e_new}")
        else: logger.error(f"BadRequest on admin log for user {user.id}: {e}")
    except error.TelegramError as e:
        if 'message is not modified' not in str(e): logger.warning(f"Temporary error updating admin log for user {user.id} (ID: {log_message_id}): {e}")

async def update_pinned_summary(context: ContextTypes.DEFAULT_TYPE):
    if not NOTIFICATION_GROUP_ID: return
    stats = load_stats(); user_count = len(stats.get("users", {})); active_users_24h = 0; now = datetime.now()
    for user_data in stats.get("users", {}).values():
        last_start_dt = datetime.fromisoformat(user_data.get("last_start", "1970-01-01T00:00:00"))
        if now - last_start_dt <= timedelta(hours=24): active_users_24h += 1
    events = stats.get("events", {})
    text = (f"📊 *Bot-Statistik Dashboard*\n" f"🕒 _Letztes Update:_ `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n\n" f"👥 *Nutzerübersicht*\n" f"   • Gesamt: *{user_count}*\n" f"   • Aktiv (24h): *{active_users_24h}*\n" f"   • Starts: *{events.get('start_command', 0)}*\n\n" f"💰 *Bezahl-Interesse*\n" f"   • PayPal: *{events.get('payment_paypal', 0)}*\n" f"   • Krypto: *{events.get('payment_crypto', 0)}*\n" f"   • Gutschein: *{events.get('payment_voucher', 0)}*\n\n" f"🖱️ *Klick-Verhalten*\n" f"   • Vorschau (KS): *{events.get('preview_ks', 0)}*\n" f"   • Vorschau (GS): *{events.get('preview_gs', 0)}*\n" f"   • Preise (KS): *{events.get('prices_ks', 0)}*\n" f"   • Preise (GS): *{events.get('prices_gs', 0)}*\n" f"   • 'Nächstes Bild': *{events.get('next_preview', 0)}*\n" f"   • Paketauswahl: *{events.get('package_selected', 0)}*")
    pinned_id = stats.get("pinned_message_id")
    try:
        if pinned_id: await context.bot.edit_message_text(chat_id=NOTIFICATION_GROUP_ID, message_id=pinned_id, text=text, parse_mode='Markdown')
        else: raise error.BadRequest("Keine ID")
    except (error.BadRequest, error.Forbidden):
        logger.warning("Konnte Dashboard nicht bearbeiten, erstelle neu.")
        try:
            sent_message = await context.bot.send_message(chat_id=NOTIFICATION_GROUP_ID, text=text, parse_mode='Markdown')
            stats["pinned_message_id"] = sent_message.message_id; save_stats(stats)
            await context.bot.pin_chat_message(chat_id=NOTIFICATION_GROUP_ID, message_id=sent_message.message_id, disable_notification=True)
        except Exception as e_new: logger.error(f"Konnte Dashboard nicht erstellen/anpinnen: {e_new}")

async def restore_stats_from_pinned_message(application: Application):
    if not NOTIFICATION_GROUP_ID: logger.info("Keine NOTIFICATION_GROUP_ID gesetzt, Wiederherstellung übersprungen."); return
    logger.info("Versuche, Statistiken wiederherzustellen...")
    try:
        chat = await application.bot.get_chat(chat_id=NOTIFICATION_GROUP_ID)
        if not chat.pinned_message or "Bot-Statistik Dashboard" not in chat.pinned_message.text: logger.warning("Keine passende Dashboard-Nachricht gefunden."); return
        pinned_text = chat.pinned_message.text; stats = load_stats()
        def extract(p, t): return int(re.search(p, t, re.DOTALL).group(1)) if re.search(p, t, re.DOTALL) else 0
        user_count = extract(r"Gesamt:\s*\*(\d+)\*", pinned_text)
        if len(stats.get("users", {})) < user_count:
            for i in range(user_count - len(stats.get("users", {}))): stats["users"][f"restored_user_{i}"] = {"first_start": "1970-01-01T00:00:00", "last_start": "1970-01-01T00:00:00"}
        stats['events']['start_command'] = extract(r"Starts:\s*\*(\d+)\*", pinned_text); stats['events']['payment_paypal'] = extract(r"PayPal:\s*\*(\d+)\*", pinned_text); stats['events']['payment_crypto'] = extract(r"Krypto:\s*\*(\d+)\*", pinned_text); stats['events']['payment_voucher'] = extract(r"Gutschein:\s*\*(\d+)\*", pinned_text); stats['events']['preview_ks'] = extract(r"Vorschau \(KS\):\s*\*(\d+)\*", pinned_text); stats['events']['preview_gs'] = extract(r"Vorschau \(GS\):\s*\*(\d+)\*", pinned_text); stats['events']['prices_ks'] = extract(r"Preise \(KS\):\s*\*(\d+)\*", pinned_text); stats['events']['prices_gs'] = extract(r"Preise \(GS\):\s*\*(\d+)\*", pinned_text); stats['events']['next_preview'] = extract(r"'Nächstes Bild':\s*\*(\d+)\*", pinned_text); stats['events']['package_selected'] = extract(r"Paketauswahl:\s*\*(\d+)\*", pinned_text)
        stats['pinned_message_id'] = chat.pinned_message.message_id; save_stats(stats); logger.info("Statistiken erfolgreich wiederhergestellt.")
    except Exception as e: logger.error(f"Fehler bei Wiederherstellung: {e}")

def get_media_files(schwester_code: str, media_type: str) -> list:
    matching_files = []; target_prefix = f"{schwester_code.lower()}_{media_type.lower()}"
    if not os.path.isdir(MEDIA_DIR): logger.error(f"Media-Verzeichnis '{MEDIA_DIR}' nicht gefunden!"); return []
    for filename in os.listdir(MEDIA_DIR):
        normalized_filename = filename.lower().lstrip('•-_ ').replace(' ', '_')
        if normalized_filename.startswith(target_prefix): matching_files.append(os.path.join(MEDIA_DIR, filename))
    return matching_files

async def cleanup_previous_messages(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    if "messages_to_delete" in context.user_data:
        for msg_id in context.user_data["messages_to_delete"]:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except error.TelegramError: pass
        context.user_data["messages_to_delete"] = []

async def send_preview_message(update: Update, context: ContextTypes.DEFAULT_TYPE, schwester_code: str):
    await cleanup_previous_messages(update.effective_chat.id, context); chat_id = update.effective_chat.id; image_paths = get_media_files(schwester_code, "vorschau"); image_paths.sort()
    if not image_paths: await context.bot.send_message(chat_id=chat_id, text="Ups! Ich konnte gerade keine passenden Inhalte finden...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Zurück", callback_data="main_menu")]])); return
    context.user_data[f'preview_index_{schwester_code}'] = 0; image_to_show_path = image_paths[0]
    with open(image_to_show_path, 'rb') as photo_file: photo_message = await context.bot.send_photo(chat_id=chat_id, photo=photo_file, protect_content=True)
    if schwester_code == 'gs': caption = f"Heyy ich bin Lara, ich bin {AGE_ANNA} Jahre alt und mache mit meiner Schwester zusammen 🌶️ videos und Bilder falls du lust hast speziele videos zu bekommen schreib mir 😏 @lara_groner"
    else: caption = f"Heyy, mein name ist Luna ich bin {AGE_LUNA} Jahre alt und mache 🌶️ videos und Bilder. wenn du Spezielle wünsche hast schreib meiner Schwester für mehr.\nMeine Schwester: @lara_groner"
    keyboard_buttons = [[InlineKeyboardButton("🛍️ Zu den Preisen", callback_data=f"select_schwester:{schwester_code}:prices")], [InlineKeyboardButton("🖼️ Nächstes Bild", callback_data=f"next_preview:{schwester_code}")], [InlineKeyboardButton("« Zurück zum Hauptmenü", callback_data="main_menu")]]
    text_message = await context.bot.send_message(chat_id=chat_id, text=caption, reply_markup=InlineKeyboardMarkup(keyboard_buttons))
    context.user_data["messages_to_delete"] = [photo_message.message_id, text_message.message_id]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user; chat_id = update.effective_chat.id
    if is_user_banned(user.id):
        if update.message: await update.message.reply_text("Du bist von der Nutzung dieses Bots ausgeschlossen.")
        elif update.callback_query: await update.callback_query.answer("Du bist von der Nutzung dieses Bots ausgeschlossen.", show_alert=True)
        return
    
    ref_id = None
    if context.args and context.args[0].startswith("ref_"):
        potential_ref_id = context.args[0].split('_')[1]
        if potential_ref_id.isdigit(): ref_id = potential_ref_id
    try:
        status, should_notify, user_data = await check_user_status(user.id, context, ref_id)
        await track_event("start_command", context, user.id)
        is_eligible_for_discount = False
        if user_data and not user_data.get("discount_sent") and "discounts" not in user_data:
            first_start_dt = datetime.fromisoformat(user_data.get("first_start"))
            if datetime.now() - first_start_dt > timedelta(hours=2):
                is_eligible_for_discount = True; stats = load_stats()
                stats["users"][str(user.id)]["discounts"] = {"type": "percent", "value": 20}
                stats["users"][str(user.id)]["discount_sent"] = True
                save_stats(stats); await save_discounts_to_telegram(context)
                await send_or_update_admin_log(context, user, event_text="20% Rabatt (Inaktivität >2h)")
        if is_eligible_for_discount:
            discount_notification_text = ("🎁 DEIN PERSÖNLICHES ANGEBOT! 🎁\n\n" "Wir haben dir gerade einen exklusiven 20% Rabatt auf alle Pakete gutgeschrieben!\n\n" "Klicke hier, um deine neuen, reduzierten Preise zu sehen und direkt zuzuschlagen:")
            discount_notification_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💸 Zu meinen exklusiven Preisen 💸", callback_data="show_price_options")]])
            await context.bot.send_message(chat_id=chat_id, text=discount_notification_text, reply_markup=discount_notification_keyboard)
        if should_notify:
            event_text = "Bot gestartet (neuer Nutzer)" if status == "new" else "Bot erneut gestartet"
            if status == "new" and ref_id: event_text += f" (geworben von {ref_id})"
            await send_or_update_admin_log(context, user, event_text=event_text)
    except Exception as e:
        logger.error(f"Error in start admin logic for user {user.id}: {e}")
        try:
            error_message = "Hoppla! Im Hintergrund ist ein kleiner technischer Fehler aufgetreten. Das beeinträchtigt dich aber nicht. Viel Spaß!"
            if update.callback_query: await context.bot.send_message(chat_id, error_message)
            else: await update.message.reply_text(error_message)
        except Exception as e_reply: logger.error(f"Could not even send error reply to user {user.id}: {e_reply}")
    
    await cleanup_previous_messages(chat_id, context)
    welcome_text = ( "Herzlich Willkommen! ✨\n\n" "Hier kannst du eine Vorschau meiner Inhalte sehen oder direkt ein Paket auswählen. " "Die gesamte Bedienung erfolgt über die Buttons.")
    keyboard = [[InlineKeyboardButton(" Vorschau", callback_data="show_preview_options"), InlineKeyboardButton(" Preise & Pakete", callback_data="show_price_options")], [InlineKeyboardButton("🤝 Freunde einladen", callback_data="referral_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        query = update.callback_query; await query.answer()
        try: await query.edit_message_text(welcome_text, reply_markup=reply_markup)
        except error.TelegramError:
            try: await query.delete_message()
            except Exception: pass
            msg = await context.bot.send_message(chat_id=chat_id, text=welcome_text, reply_markup=reply_markup); context.user_data["messages_to_delete"] = [msg.message_id]
    else:
        if update.message is None: return
        msg = await update.message.reply_text(welcome_text, reply_markup=reply_markup); context.user_data["messages_to_delete"] = [msg.message_id]

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer(); data = query.data; chat_id = update.effective_chat.id; user = update.effective_user
    if is_user_banned(user.id): await query.answer("Du bist von der Nutzung dieses Bots ausgeschlossen.", show_alert=True); return
    if data == "main_menu": await start(update, context); return
    if data.startswith("admin_"):
        if str(user.id) != ADMIN_USER_ID: await query.answer("⛔️ Keine Berechtigung.", show_alert=True); return
        if not data.startswith(("admin_discount", "admin_delete_", "admin_preview_")):
            for key in list(context.user_data.keys()):
                if key.startswith(('rabatt_', 'awaiting_')): del context.user_data[key]
        if data == "admin_main_menu": await show_admin_menu(update, context)
        elif data == "admin_show_vouchers": await show_vouchers_panel(update, context)
        elif data == "admin_stats_users":
            stats = load_stats(); user_count = len(stats.get("users", {})); text = f"📊 *Nutzer-Statistiken*\n\nGesamtzahl der Nutzer: *{user_count}*"; keyboard = [[InlineKeyboardButton("« Zurück zum Admin-Menü", callback_data="admin_main_menu")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        elif data == "admin_stats_clicks":
            stats = load_stats(); events = stats.get("events", {}); text = "🖱️ *Klick-Statistiken*\n\n"
            if not events: text += "Noch keine Klicks erfasst."
            else:
                for event, count in sorted(events.items()): text += f"- `{event}`: *{count}* Klicks\n"
            keyboard = [[InlineKeyboardButton("« Zurück zum Admin-Menü", callback_data="admin_main_menu")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        elif data == "admin_reset_stats":
            text = "⚠️ *Bist du sicher?*\n\nAlle Statistiken werden unwiderruflich auf Null zurückgesetzt."; keyboard = [[InlineKeyboardButton("✅ Ja, zurücksetzen", callback_data="admin_reset_stats_confirm")], [InlineKeyboardButton("❌ Nein, abbrechen", callback_data="admin_main_menu")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        elif data == "admin_reset_stats_confirm":
            stats = load_stats(); stats["users"] = {}; stats["admin_logs"] = {}; stats["events"] = {key: 0 for key in stats["events"]}; save_stats(stats); await update_pinned_summary(context); await query.edit_message_text("✅ Alle Statistiken wurden zurückgesetzt.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Zurück zum Admin-Menü", callback_data="admin_main_menu")]]))
        elif data == "admin_discount_start": text = "💸 *Rabatt-Manager: Typ wählen*\n\nWelche Art von Rabatt möchtest du vergeben?"; keyboard = [[InlineKeyboardButton("Euro (€) Rabatt", callback_data="admin_discount_set_type_euro"), InlineKeyboardButton("Prozent (%) Rabatt", callback_data="admin_discount_set_type_percent")], [InlineKeyboardButton("❌ Abbrechen", callback_data="admin_main_menu")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data in ["admin_discount_set_type_euro", "admin_discount_set_type_percent"]:
            context.user_data['rabatt_in_progress'] = True; context.user_data['rabatt_data'] = {"packages": {}}; context.user_data['rabatt_type'] = "euro" if data.endswith("euro") else "percent"
            text = "💸 *Rabatt-Manager: Zielgruppe*\n\nAn wen soll der Rabatt gesendet werden?"; keyboard = [[InlineKeyboardButton("Alle Nutzer", callback_data="admin_discount_target_all")], [InlineKeyboardButton("Bestimmter Nutzer", callback_data="admin_discount_target_specific")], [InlineKeyboardButton("❌ Abbrechen", callback_data="admin_main_menu")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "admin_discount_target_all": context.user_data['rabatt_target_type'] = 'all'; await prompt_for_discount_value(update, context)
        elif data == "admin_discount_target_specific": context.user_data['rabatt_target_type'] = 'specific'; context.user_data['awaiting_user_id_for_discount'] = True; text = "Bitte sende mir jetzt die numerische ID des Nutzers, der den Rabatt erhalten soll."; keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="admin_main_menu")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("admin_discount_select_package:"):
            package_key = data.split(":")[1]; context.user_data['rabatt_data']["packages"][package_key] = context.user_data.get('rabatt_value'); await show_discount_package_menu(update, context)
        elif data == "admin_discount_percent_apply_all": await apply_all_packages_and_finalize(update, context)
        elif data == "admin_discount_finalize": await finalize_discount_action(update, context)
        elif data == "admin_user_manage": await show_user_management_menu(update, context)
        elif data in ["admin_user_ban_start", "admin_user_unban_start"]:
            action = "sperren" if data == "admin_user_ban_start" else "entsperren"; context.user_data[f'awaiting_user_id_for_{action}'] = True; text = f"Bitte sende mir die numerische Nutzer-ID des Nutzers, den du *{action}* möchtest."; keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="admin_user_manage")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        elif data == "admin_manage_discounts": await show_manage_discounts_menu(update, context)
        elif data == "admin_delete_all_discounts_confirm": text = "⚠️ *Bist du sicher?*\n\nDiese Aktion löscht *alle* aktiven, vom Admin vergebenen Rabatte für *alle* Nutzer unwiderruflich."; keyboard = [[InlineKeyboardButton("✅ Ja, alle Rabatte löschen", callback_data="admin_delete_all_discounts_execute")], [InlineKeyboardButton("❌ Abbrechen", callback_data="admin_manage_discounts")]]; await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "admin_delete_all_discounts_execute": await execute_delete_all_discounts(update, context)
        elif data == "admin_delete_user_discount_start": context.user_data['awaiting_user_id_for_discount_deletion'] = True; text = "👤 *Rabatt für Nutzer löschen*\n\nBitte sende mir die numerische ID des Nutzers, dessen Rabatte du löschen möchtest."; keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="admin_manage_discounts")]]; await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("admin_delete_user_discount_execute:"): user_id_to_clear = data.split(":")[1]; await execute_delete_user_discount(update, context, user_id_to_clear)
        elif data == "admin_preview_limit_start": context.user_data['awaiting_user_id_for_preview_limit'] = True; text = "🖼️ *Vorschau-Limit anpassen*\n\nBitte sende die ID des Nutzers, dessen Vorschau-Limit du anpassen möchtest."; keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="admin_user_manage")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        elif data.startswith("admin_preview_reset:"): user_id_to_manage = data.split(":")[1]; await execute_manage_preview_limit(update, context, user_id_to_manage, 'reset')
        elif data.startswith("admin_preview_increase:"): user_id_to_manage = data.split(":")[1]; await execute_manage_preview_limit(update, context, user_id_to_manage, 'increase')
        return

    elif data == "referral_menu":
        stats = load_stats(); user_data = stats.get("users", {}).get(str(user.id), {})
        referral_count = len(user_data.get("referrals", [])); successful_referrals = user_data.get("successful_referrals", 0)
        bot_username = (await context.bot.get_me()).username; ref_link = f"https://t.me/{bot_username}?start=ref_{user.id}"
        text = ("🤝 *Freunde einladen & Belohnung erhalten*\n\n" "Teile deinen persönlichen Link mit Freunden. Wenn sich ein neuer Nutzer über deinen Link anmeldet *und einen Kauf tätigt*, erhältst du eine Belohnung!\n\n" f"🔗 *Dein persönlicher Link:*\n`{ref_link}`\n\n" "💡 *So funktioniert's:*\n" f"Dein Link hat das Format `https://t.me/VIPANNA2008BOT?start=ref_{user.id}`. Jeder, der darauf klickt und den Bot startet, wird dir zugeordnet.\n\n" "📈 *Dein Status:*\n" f"   - Geworbene Freunde: *{referral_count}*\n" f"   - Erfolgreiche Käufe: *{successful_referrals}*")
        keyboard = [[InlineKeyboardButton("« Zurück zum Hauptmenü", callback_data="main_menu")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True); return
        
    if data == "download_vouchers_pdf":
        await query.answer("PDF wird erstellt..."); vouchers = load_vouchers(); pdf = FPDF(); pdf.add_page(); pdf.set_font("Arial", size=16); pdf.cell(0, 10, "Gutschein Report", ln=True, align='C'); pdf.ln(10); pdf.set_font("Arial", 'B', size=14); pdf.cell(0, 10, "Amazon Gutscheine", ln=True); pdf.set_font("Arial", size=12)
        if vouchers.get("amazon", []):
            for code in vouchers["amazon"]: pdf.cell(0, 8, f"- {code.encode('latin-1', 'ignore').decode('latin-1')}", ln=True)
        else: pdf.cell(0, 8, "Keine vorhanden.", ln=True)
        pdf.ln(5); pdf.set_font("Arial", 'B', size=14); pdf.cell(0, 10, "Paysafe Gutscheine", ln=True); pdf.set_font("Arial", size=12)
        if vouchers.get("paysafe", []):
            for code in vouchers["paysafe"]: pdf.cell(0, 8, f"- {code.encode('latin-1', 'ignore').decode('latin-1')}", ln=True)
        else: pdf.cell(0, 8, "Keine vorhanden.", ln=True)
        pdf_buffer = BytesIO(pdf.output(dest='S').encode('latin-1')); pdf_buffer.seek(0); today_str = datetime.now().strftime("%Y-%m-%d"); await context.bot.send_document(chat_id=chat_id, document=pdf_buffer, filename=f"Gutschein-Report_{today_str}.pdf", caption="Hier ist dein aktueller Gutschein-Report."); return

    if data in ["show_preview_options", "show_price_options"]:
        action = "preview" if "preview" in data else "prices"; text = "Für wen interessierst du dich?"; keyboard = [[InlineKeyboardButton("Kleine Schwester", callback_data=f"select_schwester:ks:{action}"), InlineKeyboardButton("Große Schwester", callback_data=f"select_schwester:gs:{action}")], [InlineKeyboardButton("« Zurück", callback_data="main_menu")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("select_schwester:"):
        await cleanup_previous_messages(chat_id, context)
        try: await query.message.delete()
        except error.TelegramError: pass
        _, schwester_code, action = data.split(":"); stats = load_stats(); user_data = stats.get("users", {}).get(str(user.id), {}); preview_clicks = user_data.get("preview_clicks", 0); viewed_sisters = user_data.get("viewed_sisters", [])
        if action == "preview" and preview_clicks >= 25 and schwester_code in viewed_sisters:
            await query.answer("Du hast dein Vorschau-Limit von 25 Klicks bereits erreicht.", show_alert=True)
            msg = await context.bot.send_message(chat_id, "Du hast dein Vorschau-Limit von 25 Klicks bereits erreicht.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Zurück zum Hauptmenü", callback_data="main_menu")]])); context.user_data["messages_to_delete"] = [msg.message_id]; return
        if schwester_code not in viewed_sisters:
            viewed_sisters.append(schwester_code); user_data["viewed_sisters"] = viewed_sisters; stats["users"][str(user.id)] = user_data; save_stats(stats)
        await track_event(f"{action}_{schwester_code}", context, user.id); await send_or_update_admin_log(context, user, event_text=f"Schaut sich {action} von {schwester_code.upper()} an")
        if action == "preview": await send_preview_message(update, context, schwester_code)
        elif action == "prices":
            image_paths = get_media_files(schwester_code, "preis"); image_paths.sort()
            if not image_paths: await context.bot.send_message(chat_id=chat_id, text="Ups! Ich konnte gerade keine passenden Inhalte finden...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Zurück", callback_data="main_menu")]])); return
            random_image_path = random.choice(image_paths)
            with open(random_image_path, 'rb') as photo_file: photo_message = await context.bot.send_photo(chat_id=chat_id, photo=photo_file, protect_content=True)
            caption = "Wähle dein gewünschtes Paket:"
            keyboard_buttons = [
                [InlineKeyboardButton(get_package_button_text("bilder", 10, user.id), callback_data="select_package:bilder:10"), InlineKeyboardButton(get_package_button_text("videos", 10, user.id), callback_data="select_package:videos:10")],
                [InlineKeyboardButton(get_package_button_text("bilder", 25, user.id), callback_data="select_package:bilder:25"), InlineKeyboardButton(get_package_button_text("videos", 25, user.id), callback_data="select_package:videos:25")],
                [InlineKeyboardButton(get_package_button_text("bilder", 35, user.id), callback_data="select_package:bilder:35"), InlineKeyboardButton(get_package_button_text("videos", 35, user.id), callback_data="select_package:videos:35")],
                [InlineKeyboardButton("« Zurück zum Hauptmenü", callback_data="main_menu")]]
            text_message = await context.bot.send_message(chat_id=chat_id, text=caption, reply_markup=InlineKeyboardMarkup(keyboard_buttons))
            context.user_data["messages_to_delete"] = [photo_message.message_id, text_message.message_id]

    elif data.startswith("next_preview:"):
        stats = load_stats(); user_data = stats.get("users", {}).get(str(user.id), {}); preview_clicks = user_data.get("preview_clicks", 0)
        if preview_clicks >= 25:
            await query.answer("Vorschau-Limit erreicht!", show_alert=True); await cleanup_previous_messages(chat_id, context); _, schwester_code = data.split(":")
            limit_text = "Du hast dein Vorschau-Limit von 25 Klicks erreicht. Sieh dir jetzt die Preise an, um mehr zu sehen!"
            limit_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"🛍️ Preise für {schwester_code.upper()} ansehen", callback_data=f"select_schwester:{schwester_code}:prices")], [InlineKeyboardButton("« Zurück zum Hauptmenü", callback_data="main_menu")]])
            msg = await context.bot.send_message(chat_id, text=limit_text, reply_markup=limit_keyboard); context.user_data["messages_to_delete"] = [msg.message_id]; return
        user_data["preview_clicks"] = preview_clicks + 1; stats["users"][str(user.id)] = user_data; save_stats(stats); await track_event("next_preview", context, user.id); _, schwester_code = data.split(":"); await send_or_update_admin_log(context, user, event_text=f"Nächstes Bild ({schwester_code.upper()})"); image_paths = get_media_files(schwester_code, "vorschau"); image_paths.sort(); index_key = f'preview_index_{schwester_code}'; current_index = context.user_data.get(index_key, 0); next_index = (current_index + 1) % len(image_paths) if image_paths else 0; context.user_data[index_key] = next_index;
        if not image_paths: return
        image_to_show_path = image_paths[next_index]; photo_message_id = context.user_data.get("messages_to_delete", [None])[0]
        if photo_message_id:
            try:
                with open(image_to_show_path, 'rb') as photo_file: media = InputMediaPhoto(photo_file); await context.bot.edit_message_media(chat_id=chat_id, message_id=photo_message_id, media=media)
            except error.TelegramError as e: logger.warning(f"Konnte Bild nicht bearbeiten, sende neu: {e}"); await send_preview_message(update, context, schwester_code)

    elif data.startswith("select_package:"):
        await cleanup_previous_messages(chat_id, context);
        try: await query.message.delete()
        except error.TelegramError: pass
        await track_event("package_selected", context, user.id); _, media_type, amount_str = data.split(":"); amount = int(amount_str); base_price = PRICES[media_type][amount];
        stats = load_stats(); user_data = stats.get("users", {}).get(str(user.id), {}); package_key = f"{media_type}_{amount}"
        price = get_discounted_price(base_price, user_data.get("discounts"), package_key)
        if price == -1: price = base_price
        price_str = f"~{base_price}€~ *{price}€* (Rabatt)" if price != base_price else f"*{price}€*"
        text = f"Du hast das Paket **{amount} {media_type.capitalize()}** für {price_str} ausgewählt.\n\nWie möchtest du bezahlen?"; keyboard = [[InlineKeyboardButton(" PayPal", callback_data=f"pay_paypal:{media_type}:{amount}")], [InlineKeyboardButton(" Gutschein", callback_data=f"pay_voucher:{media_type}:{amount}")], [InlineKeyboardButton("🪙 Krypto", callback_data=f"pay_crypto:{media_type}:{amount}")], [InlineKeyboardButton("« Zurück zu den Preisen", callback_data="show_price_options")]]; msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'); context.user_data["messages_to_delete"] = [msg.message_id]

    elif data.startswith(("pay_paypal:", "pay_voucher:", "pay_crypto:", "show_wallet:", "voucher_provider:")):
        parts = data.split(":"); media_type = parts[1]; amount_str = parts[2]; amount = int(amount_str); base_price = PRICES[media_type][amount]
        stats = load_stats(); user_data = stats.get("users", {}).get(str(user.id), {}); package_key = f"{media_type}_{amount}"
        price = get_discounted_price(base_price, user_data.get("discounts"), package_key)
        if price == -1: price = base_price
        async def update_payment_log(payment_method: str, price_val: int):
            stats_log = load_stats(); user_data_log = stats_log.get("users", {}).get(str(user.id))
            if user_data_log:
                payment_info = f"{payment_method}: {price_val}€"
                if payment_info not in user_data_log.get("payments_initiated", []):
                    user_data_log.setdefault("payments_initiated", []).append(payment_info); save_stats(stats_log)
            await send_or_update_admin_log(context, user, event_text=f"Bezahlmethode '{payment_method}' für {price_val}€ gewählt")
        if data.startswith("pay_paypal:"):
            await track_event("payment_paypal", context, user.id); await update_payment_log("PayPal", price); paypal_link = f"https://paypal.me/{PAYPAL_USER}/{price}"; text = (f"Super! Klicke auf den Link, um die Zahlung für **{amount} {media_type.capitalize()}** in Höhe von **{price}€** abzuschließen.\n\nGib als Verwendungszweck bitte deinen Telegram-Namen an.\n\n➡️ [Hier sicher bezahlen]({paypal_link})"); keyboard = [[InlineKeyboardButton("« Zurück zur Bezahlwahl", callback_data=f"select_package:{media_type}:{amount}")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True)
        elif data.startswith("pay_voucher:"):
            await track_event("payment_voucher", context, user.id); await update_payment_log("Gutschein", price); text = "Welchen Gutschein möchtest du einlösen?"; keyboard = [[InlineKeyboardButton("Amazon", callback_data=f"voucher_provider:amazon:{media_type}:{amount}"), InlineKeyboardButton("Paysafe", callback_data=f"voucher_provider:paysafe:{media_type}:{amount}")], [InlineKeyboardButton("« Zurück zur Bezahlwahl", callback_data=f"select_package:{media_type}:{amount}")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("pay_crypto:"):
            await track_event("payment_crypto", context, user.id); await update_payment_log("Krypto", price); text = "Bitte wähle die gewünschte Kryptowährung:"; keyboard = [[InlineKeyboardButton("Bitcoin (BTC)", callback_data=f"show_wallet:btc:{media_type}:{amount}"), InlineKeyboardButton("Ethereum (ETH)", callback_data=f"show_wallet:eth:{media_type}:{amount}")], [InlineKeyboardButton("« Zurück zur Bezahlwahl", callback_data=f"select_package:{media_type}:{amount}")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data.startswith("show_wallet:"):
            _, crypto_type, _, _ = parts; wallet_address = BTC_WALLET if crypto_type == "btc" else ETH_WALLET; crypto_name = "Bitcoin (BTC)" if crypto_type == "btc" else "Ethereum (ETH)"; text = (f"Zahlung mit **{crypto_name}** für **{price}€**.\n\nBitte sende den Betrag an die folgende Adresse und bestätige es hier, sobald du fertig bist:\n\n`{wallet_address}`"); keyboard = [[InlineKeyboardButton("« Zurück zur Krypto-Wahl", callback_data=f"pay_crypto:{media_type}:{amount}")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        elif data.startswith("voucher_provider:"):
            _, provider, _, _ = parts; context.user_data["awaiting_voucher"] = provider; text = f"Bitte sende mir jetzt deinen {provider.capitalize()}-Gutschein-Code als einzelne Nachricht."; keyboard = [[InlineKeyboardButton("Abbrechen", callback_data=f"pay_voucher:{media_type}:{amount_str}")]]; await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "🔒 *Admin-Menü*\n\nWähle eine Option:"
    keyboard = [[InlineKeyboardButton("📊 Nutzer-Statistiken", callback_data="admin_stats_users"), InlineKeyboardButton("🖱️ Klick-Statistiken", callback_data="admin_stats_clicks")], [InlineKeyboardButton("🎟️ Gutscheine", callback_data="admin_show_vouchers"), InlineKeyboardButton("💸 Rabatt senden", callback_data="admin_discount_start")], [InlineKeyboardButton("👤 Nutzer verwalten", callback_data="admin_user_manage"), InlineKeyboardButton("📢 Broadcast senden", callback_data="admin_broadcast_start")], [InlineKeyboardButton("💸 Rabatte verwalten", callback_data="admin_manage_discounts")], [InlineKeyboardButton("🔄 Statistiken zurücksetzen", callback_data="admin_reset_stats")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else: await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_user_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "👤 *Nutzerverwaltung*\n\nWähle eine Aktion aus:"
    keyboard = [[InlineKeyboardButton("🚫 Nutzer sperren", callback_data="admin_user_ban_start")], [InlineKeyboardButton("✅ Nutzer entsperren", callback_data="admin_user_unban_start")], [InlineKeyboardButton("🖼️ Vorschau-Limit anpassen", callback_data="admin_preview_limit_start")], [InlineKeyboardButton("« Zurück", callback_data="admin_main_menu")]]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def show_vouchers_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vouchers = load_vouchers(); amazon_codes = "\n".join([f"- `{code}`" for code in vouchers.get("amazon", [])]) or "Keine"; paysafe_codes = "\n".join([f"- `{code}`" for code in vouchers.get("paysafe", [])]) or "Keine"
    text = (f"*Eingelöste Gutscheine*\n\n*Amazon:*\n{amazon_codes}\n\n*Paysafe:*\n{paysafe_codes}"); keyboard = [[InlineKeyboardButton("📄 Vouchers als PDF laden", callback_data="download_vouchers_pdf")], [InlineKeyboardButton("« Zurück zum Admin-Menü", callback_data="admin_main_menu")]]; reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user; text_input = update.message.text
    if str(user.id) == ADMIN_USER_ID:
        if context.user_data.get('rabatt_in_progress'): await handle_admin_discount_input(update, context); return
        if context.user_data.get('awaiting_user_id_for_sperren'): await handle_admin_user_management_input(update, context, "sperren"); return
        if context.user_data.get('awaiting_user_id_for_entsperren'): await handle_admin_user_management_input(update, context, "entsperren"); return
        if context.user_data.get('awaiting_user_id_for_discount_deletion'): await handle_admin_delete_user_discount_input(update, context); return
        if context.user_data.get('awaiting_user_id_for_preview_limit'): await handle_admin_preview_limit_input(update, context); return

    if context.user_data.get("awaiting_voucher"):
        provider = context.user_data.pop("awaiting_voucher"); code = text_input; vouchers = load_vouchers(); vouchers[provider].append(code); save_vouchers(vouchers)
        notification_text = (f"📬 *Neuer Gutschein erhalten!*\n\n*Anbieter:* {provider.capitalize()}\n*Code:* `{code}`\n*Von Nutzer:* {escape_markdown(user.first_name, version=2)} (`{user.id}`)")
        await send_permanent_admin_notification(context, notification_text); await send_or_update_admin_log(context, user, event_text=f"Gutschein '{provider}' eingereicht")
        await process_referral_reward(user.id, context)
        await update.message.reply_text("Vielen Dank! Dein Gutschein wurde übermittelt und wird nun geprüft. Ich melde mich bei dir."); await asyncio.sleep(2); await start(update, context)

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if str(update.effective_user.id) != ADMIN_USER_ID: await update.message.reply_text("⛔️ Du hast keine Berechtigung für diesen Befehl."); return
    await show_admin_menu(update, context)

async def prompt_for_discount_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['awaiting_rabatt_value'] = True
    unit = "Euro (€)" if context.user_data.get('rabatt_type') == 'euro' else "Prozent (%)"
    text = f"Bitte sende mir jetzt den Rabattwert als Zahl (z.B. `5` für 5 {unit})."
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Abbrechen", callback_data="admin_main_menu")]]))

async def show_discount_package_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rabatt_data = context.user_data.get('rabatt_data', {}); rabatt_value = context.user_data.get('rabatt_value'); unit = "%" if context.user_data.get('rabatt_type') == 'percent' else "€"
    def get_button_text(pkg, name): return f"✅ {name}" if pkg in rabatt_data.get("packages", {}) else name
    keyboard = [[InlineKeyboardButton(get_button_text('bilder_10', "Bilder 10"), callback_data="admin_discount_select_package:bilder_10"), InlineKeyboardButton(get_button_text('videos_10', "Videos 10"), callback_data="admin_discount_select_package:videos_10")], [InlineKeyboardButton(get_button_text('bilder_25', "Bilder 25"), callback_data="admin_discount_select_package:bilder_25"), InlineKeyboardButton(get_button_text('videos_25', "Videos 25"), callback_data="admin_discount_select_package:videos_25")], [InlineKeyboardButton(get_button_text('bilder_35', "Bilder 35"), callback_data="admin_discount_select_package:bilder_35"), InlineKeyboardButton(get_button_text('videos_35', "Videos 35"), callback_data="admin_discount_select_package:videos_35")]]
    if context.user_data.get('rabatt_type') == 'percent': keyboard.append([InlineKeyboardButton("✅ Auf alle Pakete anwenden", callback_data="admin_discount_percent_apply_all")])
    keyboard.append([InlineKeyboardButton("➡️ Ausgewählte anwenden & senden", callback_data="admin_discount_finalize")]); keyboard.append([InlineKeyboardButton("❌ Abbrechen", callback_data="admin_main_menu")])
    target_desc = "Alle Nutzer" if context.user_data.get('rabatt_target_type') == 'all' else f"Nutzer `{context.user_data.get('rabatt_target_id')}`"
    text = f"💸 *Rabatt-Manager: Pakete wählen*\n\nZiel: {target_desc}\nWert: *{rabatt_value}{unit}*\n\nWähle die Pakete aus, für die der Rabatt gelten soll."
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def handle_admin_discount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_input = update.message.text
    if context.user_data.get('awaiting_user_id_for_discount'):
        if not text_input.isdigit(): await update.message.reply_text("⚠️ Bitte gib eine gültige, numerische Nutzer-ID ein."); return
        stats = load_stats()
        if text_input not in stats["users"]: await update.message.reply_text(f"⚠️ Nutzer mit der ID `{text_input}` wurde nicht gefunden. Bitte überprüfe die ID."); return
        context.user_data['rabatt_target_id'] = text_input; context.user_data['awaiting_user_id_for_discount'] = False; await prompt_for_discount_value(update, context)
    elif context.user_data.get('awaiting_rabatt_value'):
        if not text_input.isdigit(): await update.message.reply_text("⚠️ Bitte gib einen gültigen Rabattwert als Zahl ein."); return
        context.user_data['rabatt_value'] = int(text_input); context.user_data['awaiting_rabatt_value'] = False
        # Create a fake Update object to call the next function
        fake_query = type('FakeQuery', (), {'message': update.message, 'edit_message_text': update.message.reply_text})
        fake_update = type('FakeUpdate', (), {'callback_query': fake_query()})
        await show_discount_package_menu(fake_update, context)

async def apply_all_packages_and_finalize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rabatt_value = context.user_data.get('rabatt_value')
    for media_type, amounts in PRICES.items():
        for amount in amounts:
            package_key = f"{media_type}_{amount}"
            context.user_data['rabatt_data']['packages'][package_key] = rabatt_value
    await finalize_discount_action(update, context)

async def finalize_discount_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; rabatt_data = context.user_data.get('rabatt_data', {})
    if not rabatt_data.get("packages"): await query.answer("Es wurden keine Pakete ausgewählt.", show_alert=True); return
    stats = load_stats(); target_ids = []
    target_type = context.user_data.get('rabatt_target_type')
    if target_type == 'all': target_ids = list(stats["users"].keys())
    elif target_type == 'specific':
        target_id = context.user_data.get('rabatt_target_id')
        if target_id: target_ids.append(target_id)
    if not target_ids: await query.edit_message_text("Fehler: Kein Ziel für den Rabatt gefunden.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Zurück", callback_data="admin_main_menu")]])); return
    
    discount_type = context.user_data.get('rabatt_type')
    final_discount_obj = {"type": f"{discount_type}_packages", "packages": rabatt_data["packages"]}
    
    success_count = 0; fail_count = 0
    discount_notification_text = ("🎁 DEIN PERSÖNLICHES ANGEBOT! 🎁\n\n" "Wir haben dir gerade einen exklusiven Rabatt auf ausgewählte Pakete gutgeschrieben!\n\n" "Klicke hier, um deine neuen, reduzierten Preise zu sehen und direkt zuzuschlagen:")
    discount_notification_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💸 Zu meinen exklusiven Preisen 💸", callback_data="show_price_options")]])
    for user_id in target_ids:
        if user_id in stats["users"] and not stats["users"][user_id].get("banned", False):
            stats["users"][user_id]["discounts"] = final_discount_obj
            try: await context.bot.send_message(chat_id=user_id, text=discount_notification_text, reply_markup=discount_notification_keyboard); success_count += 1
            except (error.Forbidden, error.BadRequest): fail_count += 1
    save_stats(stats); await save_discounts_to_telegram(context)
    for key in list(context.user_data.keys()):
        if key.startswith('rabatt_'): del context.user_data[key]
    final_text = f"✅ Rabatt-Aktion abgeschlossen!\n\n- Erfolgreich gesendet an: *{success_count} Nutzer*\n- Fehlgeschlagen/Blockiert: *{fail_count} Nutzer*"
    await query.edit_message_text(final_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Zurück zum Admin-Menü", callback_data="admin_main_menu")]]), parse_mode='Markdown')

async def handle_admin_user_management_input(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    user_id_to_manage = update.message.text; context.user_data[f'awaiting_user_id_for_{action}'] = False
    if not user_id_to_manage.isdigit(): await update.message.reply_text("⚠️ Ungültige ID. Bitte gib eine numerische Nutzer-ID ein."); return
    stats = load_stats()
    if user_id_to_manage not in stats["users"]: await update.message.reply_text(f"⚠️ Nutzer mit der ID `{user_id_to_manage}` nicht gefunden."); return
    stats["users"][user_id_to_manage]["banned"] = True if action == "sperren" else False; save_stats(stats)
    verb = "gesperrt" if action == "sperren" else "entsperrt"; await update.message.reply_text(f"✅ Nutzer `{user_id_to_manage}` wurde erfolgreich *{verb}*."); await show_admin_menu(update, context)

async def show_manage_discounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "💸 *Rabatte verwalten*\n\nHier kannst du aktive, vom Admin vergebene Rabatte einsehen und löschen."
    keyboard = [[InlineKeyboardButton("🗑️ Alle Rabatte löschen", callback_data="admin_delete_all_discounts_confirm")], [InlineKeyboardButton("👤 Rabatt für Nutzer löschen", callback_data="admin_delete_user_discount_start")], [InlineKeyboardButton("« Zurück zum Admin-Menü", callback_data="admin_main_menu")]]
    await update.callback_query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def execute_delete_all_discounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = load_stats(); cleared_count = 0
    for user_id in stats["users"]:
        if "discounts" in stats["users"][user_id]:
            del stats["users"][user_id]["discounts"]; cleared_count += 1
    save_stats(stats); await save_discounts_to_telegram(context) # Sync
    text = f"✅ Erfolgreich!\n\nAlle Rabatte von *{cleared_count}* Nutzern wurden entfernt."; await update.callback_query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Zurück", callback_data="admin_manage_discounts")]]))

async def handle_admin_delete_user_discount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['awaiting_user_id_for_discount_deletion'] = False
    user_id_to_clear = update.message.text
    if not user_id_to_clear.isdigit(): await update.message.reply_text("⚠️ Ungültige ID. Bitte gib eine numerische Nutzer-ID ein."); return
    stats = load_stats(); user_data = stats.get("users", {}).get(user_id_to_clear)
    if not user_data or "discounts" not in user_data: await update.message.reply_text(f"ℹ️ Nutzer mit ID `{user_id_to_clear}` hat keine aktiven Rabatte."); return
    text = f"Nutzer `{user_id_to_clear}` hat aktive Rabatte.\n\nSollen diese wirklich gelöscht werden?"; keyboard = [[InlineKeyboardButton("✅ Ja, löschen", callback_data=f"admin_delete_user_discount_execute:{user_id_to_clear}")], [InlineKeyboardButton("❌ Abbrechen", callback_data="admin_manage_discounts")]]; await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def execute_delete_user_discount(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id_to_clear: str):
    stats = load_stats()
    if user_id_to_clear in stats["users"] and "discounts" in stats["users"][user_id_to_clear]:
        del stats["users"][user_id_to_clear]["discounts"]; save_stats(stats); await save_discounts_to_telegram(context) # Sync
        text = f"✅ Rabatte für Nutzer `{user_id_to_clear}` wurden erfolgreich entfernt."; await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Zurück", callback_data="admin_manage_discounts")]]))
    else: await update.callback_query.edit_message_text(f"ℹ️ Fehler: Nutzer `{user_id_to_clear}` hat keine Rabatte (mehr).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Zurück", callback_data="admin_manage_discounts")]]))

async def handle_admin_preview_limit_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['awaiting_user_id_for_preview_limit'] = False
    user_id_to_manage = update.message.text
    if not user_id_to_manage.isdigit(): await update.message.reply_text("⚠️ Ungültige ID. Bitte gib eine numerische Nutzer-ID ein."); return
    stats = load_stats()
    if user_id_to_manage not in stats["users"]: await update.message.reply_text(f"⚠️ Nutzer mit der ID `{user_id_to_manage}` nicht gefunden."); return
    current_clicks = stats['users'][user_id_to_manage].get('preview_clicks', 0)
    text = f"Nutzer `{user_id_to_manage}` hat aktuell *{current_clicks}* Vorschau-Klicks.\n\nWas möchtest du tun?"
    keyboard = [[InlineKeyboardButton("Auf 0 zurücksetzen", callback_data=f"admin_preview_reset:{user_id_to_manage}")], [InlineKeyboardButton("Um 25 erhöhen", callback_data=f"admin_preview_increase:{user_id_to_manage}")], [InlineKeyboardButton("❌ Abbrechen", callback_data="admin_user_manage")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def execute_manage_preview_limit(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str, action: str):
    stats = load_stats(); user_data = stats["users"].get(user_id)
    if not user_data: await update.callback_query.edit_message_text(f"Fehler: Nutzer {user_id} nicht gefunden.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Zurück", callback_data="admin_user_manage")]])); return
    current_clicks = user_data.get('preview_clicks', 0)
    if action == 'reset': new_clicks = 0; verb = "zurückgesetzt"
    else: new_clicks = current_clicks + 25; verb = "erhöht"
    stats["users"][user_id]['preview_clicks'] = new_clicks; save_stats(stats)
    text = f"✅ Vorschau-Limit für Nutzer `{user_id}` wurde auf *{new_clicks}* {verb}."
    await update.callback_query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Zurück", callback_data="admin_user_manage")]]))

async def process_referral_reward(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    stats = load_stats(); user_id_str = str(user_id); user_data = stats["users"].get(user_id_str, {})
    if user_data.get("reward_triggered_for_referrer"): return
    referrer_id = user_data.get("referrer_id")
    if referrer_id and referrer_id in stats["users"]:
        stats["users"][user_id_str]["reward_triggered_for_referrer"] = True
        stats["users"][referrer_id]["successful_referrals"] = stats["users"][referrer_id].get("successful_referrals", 0) + 1
        
        reward_discount = {"type": "percent", "value": 60} 
        
        stats["users"][referrer_id]["discounts"] = reward_discount
        save_stats(stats); await save_discounts_to_telegram(context)
        
        reward_text = ("🎉 *Belohnung erhalten!*\n\n" "Ein von dir geworbener Freund hat gerade seinen ersten Kauf getätigt. Als Dankeschön haben wir dir einen exklusiven *60% Rabatt auf ALLES* gutgeschrieben!")
        try: await context.bot.send_message(chat_id=referrer_id, text=reward_text, parse_mode='Markdown')
        except (error.Forbidden, error.BadRequest): logger.warning(f"Could not send referral reward notification to user {referrer_id}")

async def post_init(application: Application):
    await restore_stats_from_pinned_message(application)
    await load_discounts_from_telegram(application)

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    if WEBHOOK_URL:
        port = int(os.environ.get("PORT", 8443)); application.run_webhook(listen="0.0.0.0", port=port, url_path=BOT_TOKEN, webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    else:
        logger.info("Starte Bot im Polling-Modus"); application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
