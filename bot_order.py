import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent
APP_DIR = BASE_DIR / "app"
DB_PATH = APP_DIR / "database" / "bot.db"
CONFIG_PATH = APP_DIR / "config" / "config.json"
LOG_PATH = APP_DIR / "logs" / "system.log"
STATE_PATH = APP_DIR / "runtime" / "state.json"
PAYMENT_PROOFS_DIR = APP_DIR / "storage" / "payment_proofs"

WIB_TZ = "Asia/Jakarta"

STATE_IDLE = "IDLE"
STATE_VIEW_PRODUCT = "VIEW_PRODUCT"
STATE_SELECT_PRODUCT = "SELECT_PRODUCT"
STATE_SELECT_VARIANT = "SELECT_VARIANT"
STATE_CONFIRM_ORDER = "CONFIRM_ORDER"
STATE_PAYMENT_METHOD = "PAYMENT_METHOD"
STATE_WAITING_PAYMENT = "WAITING_PAYMENT"
STATE_WAITING_REVIEW = "WAITING_REVIEW"

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"
STATUS_MAINTENANCE = "MAINTENANCE"

ORDER_STATUS_PENDING = "PENDING"
ORDER_STATUS_WAITING_PAYMENT = "WAITING_PAYMENT"
ORDER_STATUS_WAITING_REVIEW = "WAITING_REVIEW"
ORDER_STATUS_CANCELLED = "CANCELLED"
ORDER_STATUS_CANCELLED_TIMEOUT = "CANCELLED_TIMEOUT"
ORDER_STATUS_COMPLETED = "COMPLETED"

PAYMENT_METHODS = {
    "QRIS": "💠 QRIS",
    "DANA": "🟦 DANA",
    "SEABANK": "🟩 SEABANK",
}


@dataclass
class Config:
    token: str
    channel_command_ids: List[int]
    channel_log_ids: List[int]
    status_bot: str
    timezone: str
    auto_cancel_minutes: int


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._ensure_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    status TEXT NOT NULL,
                    join_date TEXT NOT NULL,
                    last_activity TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    product_no INTEGER PRIMARY KEY AUTOINCREMENT,
                    nama_produk TEXT NOT NULL,
                    urutan INTEGER NOT NULL,
                    aktif INTEGER NOT NULL DEFAULT 1,
                    populer INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS variants (
                    varian_no INTEGER NOT NULL,
                    product_no INTEGER NOT NULL,
                    nama_varian TEXT NOT NULL,
                    harga INTEGER NOT NULL,
                    stok INTEGER NOT NULL,
                    aktif INTEGER NOT NULL DEFAULT 1,
                    deskripsi TEXT DEFAULT '',
                    PRIMARY KEY (varian_no, product_no),
                    FOREIGN KEY(product_no) REFERENCES products(product_no)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    produk TEXT NOT NULL,
                    varian TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    total INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payment_method TEXT,
                    payment_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    expired_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    order_id TEXT,
                    chat_id INTEGER,
                    username TEXT,
                    keterangan TEXT,
                    timestamp TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS bans (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    reason TEXT,
                    banned_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def ensure_user(self, chat_id: int, username: str) -> None:
        now = wib_now_iso()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
            if cursor.fetchone():
                cursor.execute(
                    "UPDATE users SET username = ?, last_activity = ? WHERE chat_id = ?",
                    (username, now, chat_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO users (chat_id, username, status, join_date, last_activity) VALUES (?, ?, ?, ?, ?)",
                    (chat_id, username, "active", now, now),
                )
            conn.commit()

    def is_banned(self, chat_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id FROM bans WHERE chat_id = ?", (chat_id,))
            return cursor.fetchone() is not None

    def add_log(self, event: str, order_id: Optional[str], chat_id: Optional[int], username: Optional[str], detail: str) -> None:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO logs (event, order_id, chat_id, username, keterangan, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (event, order_id, chat_id, username, detail, wib_now_iso()),
            )
            conn.commit()

    def list_products(self, only_active: bool = True) -> List[sqlite3.Row]:
        query = "SELECT * FROM products"
        params: Tuple = ()
        if only_active:
            query += " WHERE aktif = 1"
        query += " ORDER BY urutan ASC"
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()

    def list_popular_products(self) -> List[sqlite3.Row]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM products WHERE aktif = 1 AND populer = 1 ORDER BY urutan ASC"
            )
            return cursor.fetchall()

    def list_variants(self, product_no: int) -> List[sqlite3.Row]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM variants WHERE product_no = ? AND aktif = 1 ORDER BY varian_no ASC",
                (product_no,),
            )
            return cursor.fetchall()

    def get_variant(self, product_no: int, varian_no: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM variants WHERE product_no = ? AND varian_no = ?",
                (product_no, varian_no),
            )
            return cursor.fetchone()

    def create_order(self, chat_id: int, produk: str, varian: str, qty: int, total: int) -> str:
        order_id = f"ORD-{datetime.utcnow().strftime('%H%M%S')}{chat_id % 1000:03d}"
        created_at = wib_now_iso()
        expired_at = (wib_now() + timedelta(minutes=CONFIG.auto_cancel_minutes)).isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO orders (order_id, chat_id, produk, varian, qty, total, status, created_at, expired_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    chat_id,
                    produk,
                    varian,
                    qty,
                    total,
                    ORDER_STATUS_PENDING,
                    created_at,
                    expired_at,
                ),
            )
            conn.commit()
        return order_id

    def get_active_order(self, chat_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM orders WHERE chat_id = ? AND status IN (?, ?, ?) ORDER BY created_at DESC LIMIT 1",
                (chat_id, ORDER_STATUS_PENDING, ORDER_STATUS_WAITING_PAYMENT, ORDER_STATUS_WAITING_REVIEW),
            )
            return cursor.fetchone()

    def update_order_status(self, order_id: str, status: str) -> None:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE orders SET status = ? WHERE order_id = ?", (status, order_id))
            conn.commit()

    def update_payment_method(self, order_id: str, method: str, payment_message_id: Optional[int] = None) -> None:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE orders SET payment_method = ?, payment_message_id = ? WHERE order_id = ?",
                (method, payment_message_id, order_id),
            )
            conn.commit()

    def list_orders_pending_timeout(self) -> List[sqlite3.Row]:
        now = wib_now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM orders WHERE status IN (?, ?) AND expired_at <= ?",
                (ORDER_STATUS_PENDING, ORDER_STATUS_WAITING_PAYMENT, now),
            )
            return cursor.fetchall()


CONFIG: Config
DB: Database


def wib_now() -> datetime:
    return datetime.now().astimezone()


def wib_now_iso() -> str:
    return wib_now().isoformat()


def load_config() -> Config:
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return Config(
        token=raw.get("token", ""),
        channel_command_ids=raw.get("channel_command_ids", []),
        channel_log_ids=raw.get("channel_log_ids", []),
        status_bot=raw.get("status_bot", STATUS_OPEN),
        timezone=raw.get("timezone", WIB_TZ),
        auto_cancel_minutes=int(raw.get("auto_cancel_minutes", 20)),
    )


def save_state(state: Dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)


def load_state() -> Dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    with open(STATE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def set_user_state(chat_id: int, state: str) -> None:
    data = load_state()
    data[str(chat_id)] = state
    save_state(data)


def get_user_state(chat_id: int) -> str:
    data = load_state()
    return data.get(str(chat_id), STATE_IDLE)


def build_main_menu() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("🛍️ List Produk"), KeyboardButton("📦 Cek Stok")],
        [KeyboardButton("📘 Cara Order"), KeyboardButton("💬 Pesan Admin")],
        [KeyboardButton("ℹ️ Informasi User")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def build_product_keyboard(items: List[sqlite3.Row]) -> ReplyKeyboardMarkup:
    buttons = [[KeyboardButton(str(idx + 1))] for idx in range(len(items))]
    buttons.append([KeyboardButton("🔙 Kembali"), KeyboardButton("📘 Cara Order")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def build_product_inline(page: int, total_pages: int, show_popular: bool) -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = []
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"prod_page:{page-1}"))
        if page < total_pages:
            row.append(InlineKeyboardButton("➡️ Selanjutnya", callback_data=f"prod_page:{page+1}"))
        if row:
            buttons.append(row)
    if show_popular:
        buttons.append([InlineKeyboardButton("⭐ Produk Populer", callback_data="prod_popular")])
    return InlineKeyboardMarkup(buttons)


def build_popular_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali ke List Produk", callback_data="prod_back")]])


def build_payment_methods() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"pay_method:{key}")]
        for key, label in PAYMENT_METHODS.items()
    ]
    return InlineKeyboardMarkup(buttons)


def build_payment_actions() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("✅ Saya sudah bayar", callback_data="pay_done")],
        [InlineKeyboardButton("🔄 Ganti metode pembayaran", callback_data="pay_change")],
        [InlineKeyboardButton("❌ Batalkan pembayaran", callback_data="pay_cancel")],
    ]
    return InlineKeyboardMarkup(buttons)


def format_products(products: List[sqlite3.Row]) -> str:
    lines = ["📦 *Daftar Produk*\n"]
    for idx, product in enumerate(products, start=1):
        lines.append(f"{idx}. {product['nama_produk']}")
    return "\n".join(lines)


def format_variants(variants: List[sqlite3.Row]) -> str:
    lines = ["🧩 *Pilih Varian*\n"]
    for idx, variant in enumerate(variants, start=1):
        lines.append(
            f"{idx}. {variant['nama_varian']} | Rp{variant['harga']} | Stok {variant['stok']}"
        )
    return "\n".join(lines)


def status_console(status: str) -> None:
    if status == "CONNECTED":
        print("✅ CONNECTED — bot online & siap")
    elif status == "WARNING":
        print("⚠️ WARNING — bot jalan (tidak ada koneksi)")
    else:
        print("❌ DISCONNECTED — bot offline")


def is_command_channel(update: Update) -> bool:
    if update.effective_chat is None:
        return False
    return update.effective_chat.id in CONFIG.channel_command_ids


def parse_command_args(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_user is None or update.effective_chat is None:
            return
        DB.ensure_user(update.effective_user.id, update.effective_user.username or "")
        if DB.is_banned(update.effective_user.id):
            return
        set_user_state(update.effective_user.id, STATE_IDLE)
        text = (
            "👋 *Selamat datang di Bot Adrian Pasar Digital!*\n"
            f"🕒 WIB: {wib_now().strftime('%d %B %Y %H:%M:%S')}"
        )
        await update.message.reply_text(text, reply_markup=build_main_menu(), parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:  # noqa: BLE001
        logging.exception("start handler error")
        DB.add_log("ERROR", None, None, None, str(exc))


async def list_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_user is None or update.effective_chat is None:
            return
        if DB.is_banned(update.effective_user.id):
            return
        products = DB.list_products()
        page = 1
        per_page = 4
        total_pages = max(1, (len(products) + per_page - 1) // per_page)
        start_idx = (page - 1) * per_page
        page_items = products[start_idx : start_idx + per_page]
        set_user_state(update.effective_user.id, STATE_VIEW_PRODUCT)
        await update.message.reply_text(
            "🔔 Pilih produk dengan tombol angka di keyboard.",
            reply_markup=build_product_keyboard(page_items),
        )
        await update.message.reply_text(
            format_products(page_items),
            reply_markup=build_product_inline(page, total_pages, show_popular=True),
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["products"] = products
        context.user_data["page"] = page
        context.user_data["per_page"] = per_page
    except Exception as exc:  # noqa: BLE001
        logging.exception("list_products error")
        DB.add_log("ERROR", None, None, None, str(exc))


async def handle_product_pagination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        query = update.callback_query
        if query is None or update.effective_user is None:
            return
        await query.answer()
        products = context.user_data.get("products", DB.list_products())
        per_page = context.user_data.get("per_page", 4)
        total_pages = max(1, (len(products) + per_page - 1) // per_page)
        data = query.data or ""
        if data == "prod_popular":
            popular = DB.list_popular_products()
            await query.edit_message_text(
                format_products(popular) if popular else "⭐ Belum ada produk populer.",
                reply_markup=build_popular_inline(),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if data == "prod_back":
            page = context.user_data.get("page", 1)
        else:
            page = int(data.split(":")[1])
            context.user_data["page"] = page
        start_idx = (page - 1) * per_page
        page_items = products[start_idx : start_idx + per_page]
        await query.edit_message_text(
            format_products(page_items),
            reply_markup=build_product_inline(page, total_pages, show_popular=True),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:  # noqa: BLE001
        logging.exception("pagination error")
        DB.add_log("ERROR", None, None, None, str(exc))


async def handle_number_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_user is None or update.message is None:
            return
        if DB.is_banned(update.effective_user.id):
            return
        state = get_user_state(update.effective_user.id)
        text = update.message.text or ""
        if not text.isdigit():
            return
        choice = int(text)
        if state == STATE_VIEW_PRODUCT:
            products = context.user_data.get("products", DB.list_products())
            per_page = context.user_data.get("per_page", 4)
            page = context.user_data.get("page", 1)
            start_idx = (page - 1) * per_page
            page_items = products[start_idx : start_idx + per_page]
            if choice < 1 or choice > len(page_items):
                return
            product = page_items[choice - 1]
            variants = DB.list_variants(product["product_no"])
            if not variants:
                await update.message.reply_text("❗ Varian belum tersedia.")
                return
            set_user_state(update.effective_user.id, STATE_SELECT_VARIANT)
            context.user_data["selected_product"] = product
            await update.message.reply_text(
                format_variants(variants),
                reply_markup=build_product_keyboard(variants),
                parse_mode=ParseMode.MARKDOWN,
            )
            context.user_data["variants"] = variants
        elif state == STATE_SELECT_VARIANT:
            variants = context.user_data.get("variants", [])
            if choice < 1 or choice > len(variants):
                return
            variant = variants[choice - 1]
            if variant["stok"] <= 0:
                await update.message.reply_text("❗ Stok varian habis.")
                return
            product = context.user_data.get("selected_product")
            if product is None:
                return
            qty = 1
            total = variant["harga"] * qty
            order_id = DB.create_order(
                update.effective_user.id,
                product["nama_produk"],
                variant["nama_varian"],
                qty,
                total,
            )
            set_user_state(update.effective_user.id, STATE_PAYMENT_METHOD)
            context.user_data["active_order_id"] = order_id
            await update.message.reply_text(
                f"🧾 *Total pembayaran:* Rp{total}\nPilih metode pembayaran:",
                reply_markup=build_payment_methods(),
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as exc:  # noqa: BLE001
        logging.exception("number selection error")
        DB.add_log("ERROR", None, None, None, str(exc))


async def handle_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        query = update.callback_query
        if query is None or update.effective_user is None:
            return
        await query.answer()
        order_id = context.user_data.get("active_order_id")
        if not order_id:
            return
        method = (query.data or "").split(":")[1]
        DB.update_order_status(order_id, ORDER_STATUS_WAITING_PAYMENT)
        message = (
            f"🧾 *Total pembayaran:* Rp{DB.get_active_order(update.effective_user.id)['total']}\n"
            "⏳ Pembayaran akan dibatalkan otomatis dalam waktu 20 menit."
        )
        sent = await query.message.reply_text(
            message,
            reply_markup=build_payment_actions(),
            parse_mode=ParseMode.MARKDOWN,
        )
        DB.update_payment_method(order_id, method, sent.message_id)
        set_user_state(update.effective_user.id, STATE_WAITING_PAYMENT)
    except Exception as exc:  # noqa: BLE001
        logging.exception("payment method error")
        DB.add_log("ERROR", None, None, None, str(exc))


async def handle_payment_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        query = update.callback_query
        if query is None or update.effective_user is None:
            return
        await query.answer()
        order = DB.get_active_order(update.effective_user.id)
        if order is None:
            return
        if query.data == "pay_done":
            await query.message.reply_text("📎 Silakan kirim bukti pembayaran.")
            set_user_state(update.effective_user.id, STATE_WAITING_REVIEW)
            DB.update_order_status(order["order_id"], ORDER_STATUS_WAITING_REVIEW)
        elif query.data == "pay_change":
            await query.message.reply_text(
                "🔄 Pilih metode pembayaran baru:",
                reply_markup=build_payment_methods(),
            )
            set_user_state(update.effective_user.id, STATE_PAYMENT_METHOD)
        elif query.data == "pay_cancel":
            DB.update_order_status(order["order_id"], ORDER_STATUS_CANCELLED)
            set_user_state(update.effective_user.id, STATE_IDLE)
            await query.message.reply_text("❌ Pembayaran dibatalkan.", reply_markup=build_main_menu())
    except Exception as exc:  # noqa: BLE001
        logging.exception("payment action error")
        DB.add_log("ERROR", None, None, None, str(exc))


async def handle_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_user is None or update.message is None:
            return
        if get_user_state(update.effective_user.id) != STATE_WAITING_REVIEW:
            return
        order = DB.get_active_order(update.effective_user.id)
        if order is None:
            return
        file_obj = update.message.document or update.message.photo[-1] if update.message.photo else None
        if file_obj is None:
            return
        file = await context.bot.get_file(file_obj.file_id)
        PAYMENT_PROOFS_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{order['order_id']}_{file_obj.file_id}.dat"
        file_path = PAYMENT_PROOFS_DIR / filename
        await file.download_to_drive(custom_path=str(file_path))
        for channel_id in CONFIG.channel_log_ids:
            await context.bot.send_message(
                chat_id=channel_id,
                text=f"📎 Bukti pembayaran diterima untuk {order['order_id']}",
            )
        await update.message.reply_text("✅ Bukti pembayaran diterima. Mohon tunggu konfirmasi admin.")
    except Exception as exc:  # noqa: BLE001
        logging.exception("payment proof error")
        DB.add_log("ERROR", None, None, None, str(exc))


async def admin_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_chat is None or update.message is None:
            return
        if not is_command_channel(update):
            return
        text = update.message.text or ""
        cmd = text.split()[0].upper()
        args = parse_command_args(text)
        if cmd == "/CMD":
            await update.message.reply_text("📘 Daftar command admin tersedia sesuai dokumentasi.")
            return
        await update.message.reply_text("✅ Command diterima.")
    except Exception as exc:  # noqa: BLE001
        logging.exception("admin command error")
        DB.add_log("ERROR", None, None, None, str(exc))


async def auto_cancel(context: CallbackContext) -> None:
    try:
        orders = DB.list_orders_pending_timeout()
        for order in orders:
            DB.update_order_status(order["order_id"], ORDER_STATUS_CANCELLED_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        logging.exception("auto cancel error")
        DB.add_log("ERROR", None, None, None, str(exc))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled error", exc_info=context.error)
    DB.add_log("ERROR", None, None, None, str(context.error))


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main() -> None:
    global CONFIG
    global DB

    setup_logging()
    CONFIG = load_config()
    DB = Database(DB_PATH)

    if not CONFIG.token:
        status_console("WARNING")
        return

    application = ApplicationBuilder().token(CONFIG.token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("^🛍️ List Produk$"), list_products))
    application.add_handler(MessageHandler(filters.Regex("^\d+$"), handle_number_selection))
    application.add_handler(CallbackQueryHandler(handle_product_pagination, pattern="^(prod_page:|prod_popular|prod_back)"))
    application.add_handler(CallbackQueryHandler(handle_payment_method, pattern="^pay_method:"))
    application.add_handler(CallbackQueryHandler(handle_payment_action, pattern="^pay_(done|change|cancel)$"))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_payment_proof))
    application.add_handler(MessageHandler(filters.Regex("^/"), admin_commands))
    application.add_error_handler(error_handler)

    application.job_queue.run_repeating(auto_cancel, interval=60, first=10)
    status_console("CONNECTED")
    application.run_polling()


if __name__ == "__main__":
    main()
