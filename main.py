import os
import asyncio
import logging
import secrets
from datetime import datetime, timezone, timedelta

import aiohttp
import aiosqlite
from aiohttp import web
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "payments.db")
PORT = int(os.getenv("PORT", "8080"))

ADMIN_ID = 1045593643
USDT_ADDRESS = "TDqFWyuqJUK2HmLwkFxn1FMieGe6fUCkQH"
USDT_NETWORK = "TRC20"
RUB_PER_USDT = 92  # обновляй по рынку
USDT_DECIMALS = 6  # USDT TRC20

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("paybot")

tg = None

# =========================
# DB
# =========================
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS payments (
    order_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount_rub INTEGER NOT NULL,
    amount_usdt REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    paid_at TEXT
)
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(CREATE_SQL)
        await db.commit()

# =========================
# HELPERS
# =========================
def rub_to_usdt(amount_rub: int) -> float:
    return round(amount_rub / RUB_PER_USDT, 2)

# =========================
# TRON CHECK
# =========================
async def check_usdt_received(expected_amount: float) -> bool:
    """
    Проверяет последние входящие TRC20 на адрес.
    Публичный endpoint может меняться — проверь перед продом.
    """
    url = f"https://apilist.tronscanapi.com/api/token_trc20/transfers?relatedAddress={USDT_ADDRESS}&limit=20&start=0&direction=to"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()

        transfers = data.get("token_transfers", [])
        for tx in transfers:
            to_addr = tx.get("to_address")
            if to_addr != USDT_ADDRESS:
                continue

            raw_value = tx.get("quant", "0")
            try:
                value = int(raw_value) / (10 ** USDT_DECIMALS)
            except Exception:
                continue

            # допускаем небольшую погрешность округления
            if abs(value - expected_amount) <= 0.02:
                return True

        return False

    except Exception as e:
        log.warning(f"TRON check error: {e}")
        return False

# =========================
# BOT
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Отправь сумму в RUB (например 500), чтобы получить реквизиты оплаты."
    )

async def amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if not text.isdigit():
        return

    amount_rub = int(text)
    if amount_rub < 50 or amount_rub > 100000:
        await update.message.reply_text("Сумма: 50–100000 RUB.")
        return

    amount_usdt = rub_to_usdt(amount_rub)
    order_id = secrets.token_hex(12)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO payments(order_id,user_id,amount_rub,amount_usdt,status,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (
                order_id,
                update.effective_user.id,
                amount_rub,
                amount_usdt,
                "pending",
                datetime.now(timezone.utc).isoformat()
            )
        )
        await db.commit()

    kb = [[InlineKeyboardButton("Проверить оплату", callback_data=f"check:{order_id}")]]

    await update.message.reply_text(
        f"Заказ: {order_id}\n\n"
        f"Оплатить: {amount_usdt} USDT ({USDT_NETWORK})\n"
        f"Адрес:\n{USDT_ADDRESS}\n\n"
        f"Важно: отправь точную сумму.",
        reply_markup=InlineKeyboardMarkup(kb)
    )

    await context.bot.send_message(
        ADMIN_ID,
        f"🆕 ORDER\nuser={update.effective_user.id}\norder={order_id}\n{amount_rub} RUB / {amount_usdt} USDT"
    )

async def check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Проверяю сеть...")

    order_id = q.data.split(":")[1]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, amount_usdt, status FROM payments WHERE order_id=?",
            (order_id,)
        )
        row = await cur.fetchone()

        if not row:
            await q.message.reply_text("Заказ не найден.")
            return

        if row["status"] == "paid":
            await q.message.reply_text("Уже подтверждено.")
            return

        ok = await check_usdt_received(float(row["amount_usdt"]))

        if not ok:
            await q.message.reply_text("Платёж ещё не найден. Подожди ~1–2 минуты и попробуй снова.")
            return

        await db.execute(
            "UPDATE payments SET status='paid', paid_at=? WHERE order_id=? AND status='pending'",
            (
                datetime.now(timezone.utc).isoformat(),
                order_id
            )
        )
        await db.commit()

    await q.message.reply_text("✅ Оплата подтверждена.")
    await context.bot.send_message(
        ADMIN_ID,
        f"💰 PAID\norder={order_id}"
    )

# =========================
# CLEANUP
# =========================
async def cleanup():
    while True:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM payments WHERE status='pending' AND created_at < ?",
                    (cutoff,)
                )
                await db.commit()
        except Exception:
            log.exception("cleanup")
        await asyncio.sleep(3600)

# =========================
# HEALTH
# =========================
async def health(_):
    return web.Response(text="OK")

# =========================
# MAIN
# =========================
async def main():
    global tg

    await init_db()

    tg = ApplicationBuilder().token(BOT_TOKEN).build()
    tg.add_handler(CommandHandler("start", start))
    tg.add_handler(CallbackQueryHandler(check_callback, pattern=r"^check:"))
    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, amount_handler))

    await tg.initialize()
    await tg.start()
    await tg.updater.start_polling()

    app = web.Application()
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    asyncio.create_task(cleanup())

    log.info("started")

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
