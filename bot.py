import asyncio
import html
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("raxigame-bot")

# ------------------------------------------------------------------ config
# TEST BOT: values below are defaults. Railway variables override them.
# For the real bot, remove the hardcoded token and set BOT_TOKEN in Railway only.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8855764082:AAFlVjvA-q0d51ia7WJItJakhRV5wT8kwmw")
DATABASE_URL = os.environ["DATABASE_URL"]
MAIN_ADMIN_ID = int(os.environ.get("MAIN_ADMIN_ID", "5245462296"))
SEED_ADMINS = {MAIN_ADMIN_ID, 8148605224} | {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
IST = ZoneInfo("Asia/Kolkata")

# (minimum recharge, gift amount) - highest first
TIERS = [(50000, 2000), (25000, 1000), (12000, 500), (5000, 250), (2000, 120), (1000, 50)]
GIFT_LEVELS = [gift for _, gift in reversed(TIERS)]  # 50, 120, 250, 500, 1000, 2000

DEFAULT_WELCOME = "👋 Welcome to RaxiGame! 🎮\n\n🆔 Please send your UID to continue."

MSG_UID_BAD = "❌ UID is not correct. Please send a valid UID."
MSG_UID_OK = "✅ Uid Received, Please Send Your Today Total Recharge Amount. Example - 3000 💰"
MSG_AMOUNT_BAD = "⚠️ Invalid amount. Please send numbers only. Example - 3000"
MSG_NOT_ELIGIBLE = "🚫 Recharge amount not eligible."
MSG_ALREADY = "⏰ You have already claimed today's gift code."
MSG_NO_CODE = "😔 Gift code is not available right now. Please try again later."

pool: asyncpg.Pool | None = None
ADMINS: set[int] = set()


# ---------------------------------------------------------------- database
async def post_init(app: Application):
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gift_codes (
            gift INT PRIMARY KEY,
            code TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admins (
            tg_id BIGINT PRIMARY KEY,
            added_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS users (
            tg_id BIGINT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS claims (
            id BIGSERIAL PRIMARY KEY,
            tg_id BIGINT NOT NULL,
            uid TEXT NOT NULL,
            amount BIGINT NOT NULL,
            gift INT NOT NULL,
            code TEXT NOT NULL,
            claim_date DATE NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tg_id, claim_date),
            UNIQUE (uid, claim_date)
        );
        """
    )
    for aid in SEED_ADMINS:
        await pool.execute(
            "INSERT INTO admins (tg_id) VALUES ($1) ON CONFLICT DO NOTHING", aid
        )
    rows = await pool.fetch("SELECT tg_id FROM admins")
    ADMINS.clear()
    ADMINS.update(r["tg_id"] for r in rows)
    log.info("Database ready. Admins: %s", ADMINS)


async def post_shutdown(app: Application):
    if pool:
        await pool.close()


async def get_setting(key: str, default: str = "") -> str:
    row = await pool.fetchrow("SELECT value FROM settings WHERE key=$1", key)
    return row["value"] if row else default


async def set_setting(key: str, value: str):
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        key,
        value,
    )


async def track_user(update: Update):
    if update.effective_user:
        await pool.execute(
            "INSERT INTO users (tg_id) VALUES ($1) ON CONFLICT DO NOTHING",
            update.effective_user.id,
        )


def today_ist():
    return datetime.now(IST).date()


def gift_for(amount: int):
    for minimum, gift in TIERS:
        if amount >= minimum:
            return gift
    return None


# ------------------------------------------------------------- user flow
async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await get_setting("welcome_text", DEFAULT_WELCOME)
    photo = await get_setting("welcome_photo", "")
    chat_id = update.effective_chat.id
    if photo:
        await context.bot.send_photo(chat_id, photo, caption=text, parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await track_user(update)
    context.user_data.clear()
    context.user_data["state"] = "uid"
    await send_welcome(update, context)


async def already_claimed(tg_id: int, uid: str) -> bool:
    row = await pool.fetchrow(
        "SELECT 1 FROM claims WHERE claim_date=$1 AND (tg_id=$2 OR uid=$3)",
        today_ist(),
        tg_id,
        uid,
    )
    return row is not None


async def handle_uid(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if not re.fullmatch(r"[0-9]{1,9}", text):
        await update.effective_message.reply_text(MSG_UID_BAD)
        return
    uid = str(int(text))
    if await already_claimed(update.effective_user.id, uid):
        await update.effective_message.reply_text(MSG_ALREADY)
        return
    context.user_data["uid"] = uid
    context.user_data["state"] = "amount"
    await update.effective_message.reply_text(MSG_UID_OK)


async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    msg = update.effective_message
    clean = text.replace(",", "").replace(" ", "")
    if not re.fullmatch(r"[0-9]{1,12}", clean) or int(clean) == 0:
        await msg.reply_text(MSG_AMOUNT_BAD)
        return

    amount = int(clean)
    gift = gift_for(amount)
    if gift is None:
        context.user_data.clear()
        context.user_data["state"] = "uid"
        await msg.reply_text(MSG_NOT_ELIGIBLE)
        return

    uid = context.user_data["uid"]
    tg_id = update.effective_user.id

    row = await pool.fetchrow("SELECT code FROM gift_codes WHERE gift=$1", gift)
    if not row:
        await msg.reply_text(MSG_NO_CODE)
        return
    code = row["code"]

    inserted = await pool.fetchrow(
        "INSERT INTO claims (tg_id, uid, amount, gift, code, claim_date) "
        "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING RETURNING id",
        tg_id,
        uid,
        amount,
        gift,
        code,
        today_ist(),
    )
    context.user_data.clear()
    context.user_data["state"] = "uid"
    if not inserted:
        await msg.reply_text(MSG_ALREADY)
        return

    await msg.reply_text(
        f"✅ Your request has been automatically approved.\n\n"
        f"🎁 Giftcode Amount: {gift}Rs\n\n"
        f"<code>{html.escape(code)}</code>\n\n"
        f"👇 Click the button below to copy your giftcode.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📋 Copy Giftcode", copy_text=CopyTextButton(text=code))]]
        ),
    )


# ------------------------------------------------------------ admin panel
def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id in ADMINS


def is_main(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == MAIN_ADMIN_ID


def admin_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎁 Gift Code Stock", callback_data="adm:stock")],
            [InlineKeyboardButton("✏️ Edit Welcome Message", callback_data="adm:welcome")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="adm:bc")],
            [InlineKeyboardButton("👥 Manage Admins", callback_data="adm:admins")],
        ]
    )


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data.pop("admin_action", None)
    await update.effective_message.reply_text("🛠 Admin Panel", reply_markup=admin_menu())


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update) and context.user_data.pop("admin_action", None):
        await update.effective_message.reply_text("🚫 Cancelled.", reply_markup=admin_menu())


async def stock_text() -> str:
    rows = await pool.fetch("SELECT gift, code FROM gift_codes")
    codes = {r["gift"]: r["code"] for r in rows}
    lines = ["🎁 <b>Gift Code Stock</b>\n"]
    for minimum, gift in reversed(TIERS):
        code = codes.get(gift)
        shown = f"<code>{html.escape(code)}</code>" if code else "❌ Not set"
        lines.append(f"💎 {gift}Rs (recharge {minimum:,}+): {shown}")
    return "\n".join(lines)


async def admins_view(update: Update):
    rows = await pool.fetch("SELECT tg_id FROM admins ORDER BY created_at")
    lines = ["👥 <b>Admins</b>\n"]
    buttons = []
    for r in rows:
        aid = r["tg_id"]
        tag = " ⭐ main" if aid == MAIN_ADMIN_ID else ""
        lines.append(f"• <code>{aid}</code>{tag}")
        if is_main(update) and aid != MAIN_ADMIN_ID:
            buttons.append([InlineKeyboardButton(f"➖ Remove {aid}", callback_data=f"adm:rm:{aid}")])
    buttons.insert(0, [InlineKeyboardButton("➕ Add Admin", callback_data="adm:addadmin")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="adm:home")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Not allowed", show_alert=True)
        return
    await q.answer()
    data = q.data

    if data == "adm:home":
        context.user_data.pop("admin_action", None)
        await q.edit_message_text("🛠 Admin Panel", reply_markup=admin_menu())

    elif data == "adm:stock":
        context.user_data.pop("admin_action", None)
        buttons = [
            [InlineKeyboardButton(f"✏️ Set {gift}Rs code", callback_data=f"adm:tier:{gift}")]
            for gift in GIFT_LEVELS
        ]
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="adm:home")])
        await q.edit_message_text(
            await stock_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("adm:tier:"):
        gift = int(data.split(":")[2])
        context.user_data["admin_action"] = f"tier:{gift}"
        await q.message.reply_text(f"🎁 Send the new {gift}Rs gift code.\n\n/cancel to stop.")

    elif data == "adm:welcome":
        context.user_data["admin_action"] = "welcome"
        await q.message.reply_text(
            "✏️ Send the new welcome message.\n\n"
            "🖼 Photo with caption = image + text\n"
            "📝 Plain text = text only (removes the image)\n"
            "✨ Bold/italic/links you format in Telegram are kept.\n\n"
            "/cancel to stop."
        )

    elif data == "adm:admins":
        context.user_data.pop("admin_action", None)
        text, kb = await admins_view(update)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "adm:addadmin":
        context.user_data["admin_action"] = "addadmin"
        await q.message.reply_text(
            "➕ Send the numeric Telegram ID of the new admin.\n"
            "(They can get it from @userinfobot)\n\n/cancel to stop."
        )

    elif data.startswith("adm:rm:"):
        if not is_main(update):
            await q.answer("Only the main admin can remove admins", show_alert=True)
            return
        aid = int(data.split(":")[2])
        if aid != MAIN_ADMIN_ID:
            await pool.execute("DELETE FROM admins WHERE tg_id=$1", aid)
            ADMINS.discard(aid)
        text, kb = await admins_view(update)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "adm:bc":
        context.user_data["admin_action"] = "broadcast"
        await q.message.reply_text(
            "📢 Send the message you want to broadcast.\n"
            "Text, photo, video, anything works.\n\n/cancel to stop."
        )

    elif data == "adm:bccancel":
        context.user_data.pop("bc", None)
        await q.edit_message_text("🚫 Broadcast cancelled.")

    elif data == "adm:bcgo":
        bc = context.user_data.pop("bc", None)
        if not bc:
            await q.edit_message_text("⚠️ Nothing to send. Start again from /admin.")
            return
        await q.edit_message_text("🚀 Broadcast started. You'll get a report when it's done.")
        context.application.create_task(
            run_broadcast(context.bot, q.message.chat_id, bc[0], bc[1])
        )


async def run_broadcast(bot, admin_chat: int, from_chat: int, message_id: int):
    rows = await pool.fetch("SELECT tg_id FROM users")
    sent = failed = 0
    for r in rows:
        for attempt in range(2):
            try:
                await bot.copy_message(r["tg_id"], from_chat, message_id)
                sent += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramError:
                failed += 1
                break
        else:
            failed += 1
        await asyncio.sleep(0.05)  # stay under Telegram's rate limit
    await bot.send_message(
        admin_chat, f"📢 Broadcast finished\n\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )


async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    msg = update.effective_message

    if action.startswith("tier:"):
        gift = int(action.split(":")[1])
        code = (msg.text or "").strip()
        if not code:
            await msg.reply_text("⚠️ Please send the gift code as text.")
            return
        await pool.execute(
            "INSERT INTO gift_codes (gift, code) VALUES ($1,$2) "
            "ON CONFLICT (gift) DO UPDATE SET code = EXCLUDED.code",
            gift,
            code,
        )
        context.user_data.pop("admin_action", None)
        await msg.reply_text(f"✅ {gift}Rs gift code updated.", reply_markup=admin_menu())

    elif action == "welcome":
        if msg.photo:
            await set_setting("welcome_photo", msg.photo[-1].file_id)
            if msg.caption:
                await set_setting("welcome_text", msg.caption_html)
        elif msg.text:
            await set_setting("welcome_photo", "")
            await set_setting("welcome_text", msg.text_html)
        else:
            await msg.reply_text("⚠️ Please send text or a photo with caption.")
            return
        context.user_data.pop("admin_action", None)
        await msg.reply_text("✅ Welcome message updated. Preview below 👇")
        await send_welcome(update, context)
        await msg.reply_text("🛠 Admin Panel", reply_markup=admin_menu())

    elif action == "addadmin":
        text = (msg.text or "").strip()
        if not re.fullmatch(r"[0-9]{5,15}", text):
            await msg.reply_text("⚠️ Send a valid numeric Telegram ID.")
            return
        new_id = int(text)
        await pool.execute(
            "INSERT INTO admins (tg_id, added_by) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            new_id,
            update.effective_user.id,
        )
        ADMINS.add(new_id)
        context.user_data.pop("admin_action", None)
        await msg.reply_text(f"✅ <code>{new_id}</code> is now an admin.", parse_mode=ParseMode.HTML)
        try:
            await context.bot.send_message(new_id, "🎉 You have been added as an admin. Send /admin to open the panel.")
        except TelegramError:
            await msg.reply_text("ℹ️ Couldn't notify them (they need to open the bot first). They can still use /admin.")
        text2, kb = await admins_view(update)
        await msg.reply_text(text2, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif action == "broadcast":
        context.user_data.pop("admin_action", None)
        context.user_data["bc"] = (msg.chat_id, msg.message_id)
        total = await pool.fetchval("SELECT count(*) FROM users")
        await msg.reply_text("👀 Preview of your broadcast:")
        await context.bot.copy_message(msg.chat_id, msg.chat_id, msg.message_id)
        await msg.reply_text(
            f"📢 Send this to {total} user(s)?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Send", callback_data="adm:bcgo"),
                        InlineKeyboardButton("❌ Cancel", callback_data="adm:bccancel"),
                    ]
                ]
            ),
        )


# ------------------------------------------------------------ router
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await track_user(update)

    action = context.user_data.get("admin_action")
    if action and is_admin(update):
        await handle_admin_input(update, context, action)
        return

    if not msg.text:
        return
    text = msg.text.strip()
    if context.user_data.get("state") == "amount":
        await handle_amount(update, context, text)
    else:
        await handle_uid(update, context, text)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
