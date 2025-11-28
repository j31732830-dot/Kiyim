import logging
import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiohttp import web

from database import (
    init_db,
    list_products_by_category,
    get_product,
    get_order,
    create_order,
    add_order_item,
    add_product,
    get_order_total,
    list_orders,
    get_order_items,
    update_order_status,
    list_all_products,
    update_product,
    delete_product,
    get_settings,
    set_categories as update_categories_in_db,
    set_menu_rows as update_menu_rows_in_db,
)

BOT_TOKEN = "8157782936:AAHhp9dImUyPG53oVqP1F56dOQ2GR1iDgt4"
ADMIN_ID = os.getenv("ADMIN_ID")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "123login123")
ALLOWED_STATUSES = {"pending", "processing", "paid", "shipped", "delivered", "cancelled"}
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook/payment")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


CARTS = {}
CHECKOUT = {}
LOGGED_ADMINS = set()
PENDING_ADMIN_PASSWORD = set()
ADMIN_PRODUCT_FLOW = {}

DEFAULT_CATEGORIES = [
    "👗 Qizlar kiyimlari",
    "🧥 O‘g‘il bolalar kiyimlari",
    "🍼 Yangi tug‘ilganlar",
    "👟 Poyabzallar",
    "🧸 O‘yinchoqlar",
    "🎒 Aksessuarlar",
]

CATEGORIES = list(DEFAULT_CATEGORIES)

DEFAULT_MENU_ROWS = [
    ("👗 Qizlar kiyimlari", "🧥 O‘g‘il bolalar kiyimlari"),
    ("🍼 Yangi tug‘ilganlar", "👟 Poyabzallar"),
    ("🧸 O‘yinchoqlar", "🎒 Aksessuarlar"),
    ("/cart", "📞 Aloqa", "ℹ️ Ma'lumot"),
]

MENU_ROWS = [tuple(row) for row in DEFAULT_MENU_ROWS]

ADMIN_MENU_ROWS = [
    ("📋 Oxirgi buyurtmalar", "🔍 Buyurtmani ko‘rish"),
    ("⚙️ Statusni o‘zgartirish", "🚪 Admin chiqish"),
    ("🗂 Mahsulotlar", "➕ Mahsulot qo‘shish"),
    ("✏️ Mahsulotni tahrirlash", "➖ Mahsulotni o‘chirish"),
    ("🧾 Menyuni sozlash",),
]

PRODUCT_FLOW_STEPS = ("name", "category", "price", "desc", "photo")
SKIP_WORDS = {"skip", "/skip"}
PRODUCT_FIELD_LABELS = {
    "name": "nom",
    "category": "kategoriya",
    "price": "narx",
    "desc": "tavsif",
    "photo": "rasm",
}


def _normalize_menu_rows(rows):
    normalized = []
    for row in rows or []:
        buttons = [btn for btn in row if btn]
        if buttons:
            normalized.append(tuple(buttons))
    return normalized or [tuple(row) for row in DEFAULT_MENU_ROWS]


def apply_settings(settings):
    global CATEGORIES, MENU_ROWS
    categories = settings.get("categories") if settings else None
    menu_rows = settings.get("menu_rows") if settings else None
    if categories:
        CATEGORIES = categories
    if menu_rows:
        MENU_ROWS = _normalize_menu_rows(menu_rows)


async def load_settings():
    settings = await get_settings()
    apply_settings(settings)


def _format_current_value(step, data):
    value = data.get(step)
    if value is None:
        return None
    if step == "photo":
        return "rasm saqlangan"
    if step == "price":
        return f"{value} so'm"
    if step == "desc" and len(str(value)) > 60:
        return str(value)[:57] + "..."
    return str(value)


def build_product_step_prompt(flow):
    step = flow["step"]
    editing = flow["action"] == "edit"
    prompts = {
        "name": "Mahsulot nomini kiriting.",
        "category": (
            "Kategoriya nomini kiriting yoki quyidagi tugmalardan tanlang."
            if CATEGORIES
            else "Kategoriya nomini kiriting."
        ),
        "price": "Mahsulot narxini so'mda kiriting (masalan, 150000).",
        "desc": "Mahsulot tavsifini kiriting.",
        "photo": "Mahsulot rasmini yuboring (Telegram photo) yoki file_id/URL kiriting.",
    }
    text = prompts.get(step, "Ma'lumotni kiriting.")
    if step == "category" and CATEGORIES:
        text += "\nMavjudlari: " + ", ".join(CATEGORIES)
    if editing:
        current = _format_current_value(step, flow["data"])
        if current:
            text += f"\nJoriy qiymat: {current}"
        text += "\nO‘zgartirmaslik uchun /skip yuboring."
    text += "\nBekor qilish uchun /cancel yuboring."
    return text


def build_category_keyboard():
    if not CATEGORIES:
        return None
    rows = []
    row = []
    for idx, cat in enumerate(CATEGORIES):
        row.append(types.InlineKeyboardButton(text=cat, callback_data=f"cat:{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([types.InlineKeyboardButton(text="Bekor qilish", callback_data="cat_cancel")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def send_product_step_prompt(msg: types.Message, flow: dict):
    text = build_product_step_prompt(flow)
    reply_markup = None
    if flow["step"] == "category":
        reply_markup = build_category_keyboard()
    await msg.answer(text, reply_markup=reply_markup)


def build_main_menu():
    keyboard = []
    for row in MENU_ROWS:
        keyboard.append([types.KeyboardButton(text=btn) for btn in row])
    return types.ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Bo'lim tanlang",
    )


def build_admin_menu():
    keyboard = []
    for row in ADMIN_MENU_ROWS:
        keyboard.append([types.KeyboardButton(text=btn) for btn in row])
    return types.ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Admin amallarini tanlang",
    )


def is_admin(user_id: int) -> bool:
    if ADMIN_ID and str(user_id) == str(ADMIN_ID):
        return True
    return user_id in LOGGED_ADMINS


def admin_help_text():
    statuses = ", ".join(sorted(ALLOWED_STATUSES))
    return (
        "Admin buyruqlari:\n"
        "/orders — oxirgi 10 buyurtma ro‘yxati\n"
        "/order <id> — aniq buyurtmani ko‘rish\n"
        "/setstatus <id> <status> — statusni o‘zgartirish\n"
        "/products — mahsulotlar ro‘yxati\n"
        "/product <id> — mahsulot tafsiloti\n"
        "/add_product — yangi mahsulot qo‘shish\n"
        "/edit_product <id> — mahsulotni tahrirlash\n"
        "/delete_product <id> — mahsulotni o‘chirish\n"
        "/set_categories kat1|kat2|...\n"
        "/set_menu row1btn1|row1btn2;row2btn1|row2btn2\n"
        f"Statuslar: {statuses}"
    )


def admin_only_message():
    return "Bu buyruq faqat adminlar uchun. /login orqali parolni kiriting."


def logout_admin(user_id: int):
    LOGGED_ADMINS.discard(user_id)
    PENDING_ADMIN_PASSWORD.discard(user_id)
    ADMIN_PRODUCT_FLOW.pop(user_id, None)


def format_order_summary(order):
    order_id, _, fullname, _, phone, total, status, created_ts = order
    return f"#{order_id} | {fullname} | {phone} | {total} so'm | {status} | {created_ts.split('T')[0]}"


async def send_recent_orders(msg: types.Message):
    orders = await list_orders(limit=10)
    if not orders:
        await msg.answer("Hozircha buyurtmalar yo‘q.", reply_markup=build_admin_menu())
        return
    text = "Oxirgi buyurtmalar:\n" + "\n".join(format_order_summary(o) for o in orders)
    await msg.answer(text)


def format_product_summary(product):
    pid, name, category, price, _, _ = product
    return f"#{pid} | {name} | {category} | {price} so'm"


async def send_product_list(msg: types.Message, limit: int = 20):
    products = await list_all_products(limit=limit)
    if not products:
        await msg.answer("Mahsulotlar bazada topilmadi.", reply_markup=build_admin_menu())
        return
    lines = ["Mahsulotlar ro‘yxati:"]
    lines.extend(format_product_summary(p) for p in products)
    lines.append("Batafsil ma'lumot uchun /product <id> yuboring.")
    await msg.answer("\n".join(lines), reply_markup=build_admin_menu())


def begin_product_flow(user_id: int, action: str, data=None, product_id=None):
    flow = {
        "action": action,
        "step": PRODUCT_FLOW_STEPS[0],
        "data": data or {},
        "product_id": product_id,
    }
    ADMIN_PRODUCT_FLOW[user_id] = flow
    return flow


def cancel_product_flow(user_id: int):
    ADMIN_PRODUCT_FLOW.pop(user_id, None)


async def start_add_product_flow(msg: types.Message):
    user = msg.from_user.id
    flow = begin_product_flow(user, "add")
    await msg.answer("Mahsulot qo‘shish boshlandi.")
    await send_product_step_prompt(msg, flow)


async def start_edit_product_flow(msg: types.Message, product_id: int, product_tuple):
    pid, name, category, price, desc, photo = product_tuple
    data = {
        "name": name,
        "category": category,
        "price": price,
        "desc": desc or "",
        "photo": photo or "",
    }
    flow = begin_product_flow(msg.from_user.id, "edit", data=data, product_id=pid)
    await msg.answer(f"Mahsulot #{pid} tahriri boshlandi.")
    await send_product_step_prompt(msg, flow)


@dp.message(Command("login"))
async def admin_login(msg: types.Message):
    user = msg.from_user.id
    if is_admin(user):
        return await msg.answer(
            "Allaqachon admin paneldasiz.\n" + admin_help_text(),
            reply_markup=build_admin_menu(),
        )
    PENDING_ADMIN_PASSWORD.add(user)
    await msg.answer("Admin parolini yuboring.")


@dp.message(lambda m: m.from_user.id in PENDING_ADMIN_PASSWORD)
async def handle_admin_password(msg: types.Message):
    user = msg.from_user.id
    password = msg.text.strip() if msg.text else ""
    PENDING_ADMIN_PASSWORD.discard(user)
    if password == ADMIN_PASSWORD:
        LOGGED_ADMINS.add(user)
        await msg.answer(
            "✅ Admin paneliga muvaffaqiyatli kirdingiz.\n" + admin_help_text(),
            reply_markup=build_admin_menu(),
        )
    else:
        await msg.answer("❌ Parol noto‘g‘ri. /login orqali qayta urinib ko‘ring.")


@dp.message(Command("logout"))
async def admin_logout(msg: types.Message):
    user = msg.from_user.id
    if not is_admin(user):
        return await msg.answer("Siz admin rejimida emassiz.")
    logout_admin(user)
    await msg.answer("Admin rejimdan chiqdingiz.", reply_markup=build_main_menu())


@dp.message(Command("admin"))
async def admin_panel(msg: types.Message):
    user = msg.from_user.id
    if not is_admin(user):
        return await msg.answer(admin_only_message())
    await msg.answer("Admin paneli:\n" + admin_help_text(), reply_markup=build_admin_menu())


@dp.message(lambda m: is_admin(m.from_user.id) and m.text == "📋 Oxirgi buyurtmalar")
async def admin_recent_orders_button(msg: types.Message):
    await send_recent_orders(msg)


@dp.message(lambda m: is_admin(m.from_user.id) and m.text == "🔍 Buyurtmani ko‘rish")
async def admin_view_prompt(msg: types.Message):
    await msg.answer("Buyurtma raqamini /order <id> ko‘rinishida yuboring.")


@dp.message(lambda m: is_admin(m.from_user.id) and m.text == "⚙️ Statusni o‘zgartirish")
async def admin_status_prompt(msg: types.Message):
    await msg.answer(
        "Statusni o‘zgartirish uchun /setstatus <id> <status> yuboring.\n" + admin_help_text()
    )


@dp.message(lambda m: is_admin(m.from_user.id) and m.text == "🚪 Admin chiqish")
async def admin_logout_button(msg: types.Message):
    logout_admin(msg.from_user.id)
    await msg.answer("Admin rejimdan chiqdingiz.", reply_markup=build_main_menu())


@dp.message(lambda m: is_admin(m.from_user.id) and m.text == "🗂 Mahsulotlar")
async def admin_products_button(msg: types.Message):
    await send_product_list(msg)


@dp.message(lambda m: is_admin(m.from_user.id) and m.text == "➕ Mahsulot qo‘shish")
async def admin_add_product_button(msg: types.Message):
    await start_add_product_flow(msg)


@dp.message(lambda m: is_admin(m.from_user.id) and m.text == "✏️ Mahsulotni tahrirlash")
async def admin_edit_product_button(msg: types.Message):
    await msg.answer("Foydalanish: /edit_product <id>")


@dp.message(lambda m: is_admin(m.from_user.id) and m.text == "➖ Mahsulotni o‘chirish")
async def admin_delete_product_button(msg: types.Message):
    await msg.answer("Foydalanish: /delete_product <id>")


@dp.message(lambda m: is_admin(m.from_user.id) and m.text == "🧾 Menyuni sozlash")
async def admin_menu_settings_button(msg: types.Message):
    await msg.answer(
        "Menyuni yangilash uchun buyruqlar:\n"
        "/set_categories kat1|kat2|...\n"
        "/set_menu row1btn1|row1btn2;row2btn1|row2btn2\n"
        "Eslatma: Tugmalar soni foydalanuvchi uchun qulay bo‘lishi kerak.",
        reply_markup=build_admin_menu(),
    )


@dp.message(Command("orders"))
async def admin_recent_orders_command(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer(admin_only_message())
    await send_recent_orders(msg)


@dp.message(Command("order"))
async def admin_order_detail(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer(admin_only_message())
    parts = msg.text.split()
    if len(parts) < 2:
        return await msg.answer("Foydalanish: /order <id>")
    try:
        order_id = int(parts[1])
    except ValueError:
        return await msg.answer("Buyurtma ID faqat raqam bo‘lishi kerak.")
    order = await get_order(order_id)
    if not order:
        return await msg.answer("Buyurtma topilmadi.")
    items = await get_order_items(order_id)
    _, user_id, fullname, address, phone, total, status, created_ts = order
    lines = [
        f"Buyurtma #{order_id} ({status})",
        f"Foydalanuvchi: {user_id}",
        f"FIO: {fullname}",
        f"Manzil: {address}",
        f"Telefon: {phone}",
        f"Jami: {total} so'm",
        f"Sana: {created_ts}",
        "Mahsulotlar:",
    ]
    if not items:
        lines.append("- Mahsulotlar topilmadi.")
    else:
        for _, _, product_id, qty, price in items:
            product = await get_product(product_id)
            name = product[1] if product else f"Mahsulot #{product_id}"
            lines.append(f"- {name} x{qty} — {price * qty} so'm")
    await msg.answer("\n".join(lines))


@dp.message(Command("setstatus"))
async def admin_set_status(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer(admin_only_message())
    parts = msg.text.split()
    if len(parts) < 3:
        return await msg.answer("Foydalanish: /setstatus <id> <status>")
    try:
        order_id = int(parts[1])
    except ValueError:
        return await msg.answer("Buyurtma ID faqat raqam bo‘lishi kerak.")
    status = parts[2].lower()
    if status not in ALLOWED_STATUSES:
        return await msg.answer("Yaroqsiz status. " + admin_help_text())
    updated = await update_order_status(order_id, status)
    if not updated:
        return await msg.answer("Buyurtma topilmadi.")
    await msg.answer(f"Buyurtma #{order_id} statusi '{status}' ga o‘zgartirildi.")


@dp.message(Command("products"))
async def admin_products_command(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer(admin_only_message())
    await send_product_list(msg)


@dp.message(Command("product"))
async def admin_product_detail_command(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer(admin_only_message())
    parts = msg.text.split()
    if len(parts) < 2:
        return await msg.answer("Foydalanish: /product <id>")
    try:
        product_id = int(parts[1])
    except ValueError:
        return await msg.answer("Mahsulot ID faqat raqam bo‘lishi kerak.")
    product = await get_product(product_id)
    if not product:
        return await msg.answer("Mahsulot topilmadi.")
    pid, name, category, price, desc, photo = product
    caption = (
        f"#{pid} — {name}\n"
        f"Kategoriya: {category}\n"
        f"Narx: {price} so'm\n"
        f"Tavsif: {desc or '-'}"
    )
    if photo:
        try:
            await msg.answer_photo(photo, caption=caption)
            return
        except Exception:
            caption += f"\nRasm ID/URL: {photo}"
    await msg.answer(caption)


@dp.message(Command("add_product"))
async def admin_add_product_command(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer(admin_only_message())
    await start_add_product_flow(msg)


@dp.message(Command("edit_product"))
async def admin_edit_product_command(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer(admin_only_message())
    parts = msg.text.split()
    if len(parts) < 2:
        return await msg.answer("Foydalanish: /edit_product <id>")
    try:
        product_id = int(parts[1])
    except ValueError:
        return await msg.answer("Mahsulot ID faqat raqam bo‘lishi kerak.")
    product = await get_product(product_id)
    if not product:
        return await msg.answer("Mahsulot topilmadi.")
    await start_edit_product_flow(msg, product_id, product)


@dp.message(Command("delete_product"))
async def admin_delete_product_command(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer(admin_only_message())
    parts = msg.text.split()
    if len(parts) < 2:
        return await msg.answer("Foydalanish: /delete_product <id>")
    try:
        product_id = int(parts[1])
    except ValueError:
        return await msg.answer("Mahsulot ID faqat raqam bo‘lishi kerak.")
    deleted = await delete_product(product_id)
    if not deleted:
        return await msg.answer("Mahsulot topilmadi.")
    await msg.answer(f"Mahsulot #{product_id} o‘chirildi.")


@dp.message(Command("set_categories"))
async def admin_set_categories(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer(admin_only_message())
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer("Foydalanish: /set_categories kat1|kat2|...")
    categories = [c.strip() for c in parts[1].split("|") if c.strip()]
    if not categories:
        return await msg.answer("Kamida bitta kategoriya kiriting.")
    await update_categories_in_db(categories)
    await load_settings()
    await msg.answer("Kategoriyalar yangilandi.", reply_markup=build_main_menu())


@dp.message(Command("set_menu"))
async def admin_set_menu(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer(admin_only_message())
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer(
            "Foydalanish: /set_menu row1btn1|row1btn2;row2btn1|row2btn2|row2btn3"
        )
    raw_rows = [row.strip() for row in parts[1].split(";") if row.strip()]
    menu_rows = []
    for row in raw_rows:
        buttons = [btn.strip() for btn in row.split("|") if btn.strip()]
        if buttons:
            menu_rows.append(buttons)
    if not menu_rows:
        return await msg.answer("Kamida bitta tugma qatori kiriting.")
    await update_menu_rows_in_db(menu_rows)
    await load_settings()
    await msg.answer("Menyu tugmalari yangilandi.", reply_markup=build_main_menu())


async def finalize_product_flow(msg: types.Message, flow: dict):
    user = msg.from_user.id
    data = flow["data"]
    missing = [step for step in PRODUCT_FLOW_STEPS if not data.get(step)]
    if flow["action"] == "add" and missing:
        flow["step"] = missing[0]
        missing_labels = [PRODUCT_FIELD_LABELS.get(step, step) for step in missing]
        await msg.answer(
            "Quyidagi maydonlarni to‘ldiring: " + ", ".join(missing_labels) + ".\n"
            + build_product_step_prompt(flow)
        )
        return
    if flow["action"] == "add":
        pid = await add_product(
            data["name"],
            data["category"],
            data["price"],
            data["desc"],
            data["photo"],
        )
        await msg.answer(f"✅ Mahsulot #{pid} qo‘shildi.")
    else:
        pid = flow["product_id"]
        updated = await update_product(
            pid,
            name=data.get("name"),
            category=data.get("category"),
            price=data.get("price"),
            desc=data.get("desc"),
            photo=data.get("photo"),
        )
        if updated:
            await msg.answer(f"✏️ Mahsulot #{pid} yangilandi.")
        else:
            await msg.answer("Mahsulotni yangilashda xatolik yuz berdi.")
    cancel_product_flow(user)


@dp.message(lambda m: is_admin(m.from_user.id) and m.from_user.id in ADMIN_PRODUCT_FLOW)
async def admin_product_flow_handler(msg: types.Message):
    user = msg.from_user.id
    flow = ADMIN_PRODUCT_FLOW[user]
    step = flow["step"]
    editing = flow["action"] == "edit"
    text = (msg.text or "").strip() if msg.text else ""
    if text.lower() == "/cancel":
        cancel_product_flow(user)
        await msg.answer("Jarayon bekor qilindi.")
        return
    skip = editing and text and text.lower() in SKIP_WORDS
    data = flow["data"]

    if step == "name":
        if not skip:
            if not text:
                await msg.answer("Mahsulot nomini kiriting.")
                return
            data["name"] = text
        flow["step"] = "category"
        await send_product_step_prompt(msg, flow)
        return

    if step == "category":
        if not skip:
            if not text:
                await msg.answer("Kategoriya nomini kiriting.")
                return
            data["category"] = text
        flow["step"] = "price"
        await send_product_step_prompt(msg, flow)
        return

    if step == "price":
        if not skip:
            try:
                price = int(text)
                if price <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                await msg.answer("Narx faqat musbat raqam bo‘lishi kerak.")
                return
            data["price"] = price
        flow["step"] = "desc"
        await send_product_step_prompt(msg, flow)
        return

    if step == "desc":
        if not skip:
            if not text:
                await msg.answer("Mahsulot tavsifini kiriting.")
                return
            data["desc"] = text
        flow["step"] = "photo"
        await send_product_step_prompt(msg, flow)
        return

    if step == "photo":
        if skip:
            await finalize_product_flow(msg, flow)
            return
        photo_id = None
        if msg.photo:
            photo_id = msg.photo[-1].file_id
        elif text:
            photo_id = text
        if not photo_id:
            await msg.answer("Telegram photo yoki file_id/URL yuboring.")
            return
        data["photo"] = photo_id
        await finalize_product_flow(msg, flow)
        return


@dp.callback_query(lambda c: c.data and (c.data == "cat_cancel" or c.data.startswith("cat:")))
async def admin_category_select(callback: types.CallbackQuery):
    user = callback.from_user.id
    if not is_admin(user):
        await callback.answer("Ruxsat berilmagan.", show_alert=True)
        return
    flow = ADMIN_PRODUCT_FLOW.get(user)
    if not flow or flow["step"] != "category":
        await callback.answer("Faol jarayon topilmadi.", show_alert=True)
        return
    data = flow["data"]
    if callback.data == "cat_cancel":
        cancel_product_flow(user)
        await callback.message.answer("Jarayon bekor qilindi.")
        await callback.answer("Bekor qilindi.")
        return
    try:
        idx = int(callback.data.split(":", 1)[1])
        category = CATEGORIES[idx]
    except (ValueError, IndexError):
        await callback.answer("Kategoriya topilmadi.", show_alert=True)
        return
    data["category"] = category
    flow["step"] = "price"
    await callback.answer(f"{category} tanlandi.")
    await send_product_step_prompt(callback.message, flow)

@dp.message(Command("start"))
@dp.message(Command("menu"))
async def start(msg: types.Message):
    kb = build_main_menu()
    await msg.answer(
        "👶 Bolalar buyumlari do‘koniga xush kelibsiz!\n"
        "Kiyim-kechak, o‘yinchoqlar va aksessuarlarni tanlash uchun menyudan kerakli bo‘limni bosing.",
        reply_markup=kb
    )


@dp.message(lambda m: m.text == "ℹ️ Ma'lumot")
async def info_message(msg: types.Message):
    await msg.answer(
        "Bolalar butiki 0-12 yoshgacha bo‘lgan bolalar uchun kiyim-kechak, poyabzal, aksessuar va o‘yinchoqlarni taqdim etadi.\n"
        "Har bir bo‘limga o‘ting va savatcha orqali buyurtma bering. Savollaringiz bo‘lsa, 📞 Aloqa tugmasi orqali murojaat qiling."
    )


@dp.message(lambda m: m.text == "📞 Aloqa")
async def contact_message(msg: types.Message):
    await msg.answer(
        "📞 Aloqa markazi: +998 90 123 45 67\n"
        "📍 Manzil: Toshkent shahri, Shayxontohur tumani\n"
        "✉️ Telegram: @KidsShopSupport"
    )

# show category
@dp.message(lambda m: m.text in CATEGORIES)
async def show_category(msg: types.Message):
    cat = msg.text
    products = await list_products_by_category(cat)
    if not products:
        await msg.answer("Bu bo‘limda mahsulot yo‘q.")
        return
    for p in products:
        pid, name, price, desc, photo = p
        caption = f"🛒 {name}\n💵 Narx: {price} so'm\n{desc}\n\n/t{pid} — Savatchaga qo'shish"
        # If photo is file_id
        try:
            await msg.answer_photo(photo, caption=caption)
        except Exception:
            await msg.answer(caption)

# add to cart via /t{product_id}
@dp.message(lambda m: m.text and m.text.startswith("/t"))
async def add_to_cart(msg: types.Message):
    user = msg.from_user.id
    pid = int(msg.text[2:])
    CARTS.setdefault(user, {})
    CARTS[user][pid] = CARTS[user].get(pid, 0) + 1
    await msg.answer("✅ Mahsulot savatchaga qo‘shildi. /cart orqali savatchani ko‘ring.")

# view cart
@dp.message(Command("cart"))
async def view_cart(msg: types.Message):
    user = msg.from_user.id
    cart = CARTS.get(user, {})
    if not cart:
        return await msg.answer("Savatcha bo‘sh.")
    lines = []
    total = 0
    for pid, qty in cart.items():
        pr = await get_product(pid)
        if not pr: continue
        _, name, _, price, _, _ = pr
        subtotal = price * qty
        total += subtotal
        lines.append(f"{name} x{qty} — {subtotal} so'm (/remove_{pid} o'chirish)")
    lines.append(f"\nJami: {total} so'm\n/checkout — To‘lovga o‘tish")
    await msg.answer("\n".join(lines))

@dp.message(lambda m: m.text and m.text.startswith("/remove_"))
async def remove_item(msg: types.Message):
    user = msg.from_user.id
    pid = int(msg.text.split("_",1)[1])
    cart = CARTS.get(user, {})
    if pid in cart:
        del cart[pid]
        await msg.answer("Mahsulot o‘chirildi.")
    else:
        await msg.answer("Bu mahsulot savatchada yo‘q.")

# checkout
@dp.message(Command("checkout"))
async def checkout(msg: types.Message):
    user = msg.from_user.id
    cart = CARTS.get(user, {})
    if not cart:
        return await msg.answer("Savatcha bo‘sh.")
    total = 0
    items = []
    for pid, qty in cart.items():
        pr = await get_product(pid)
        if not pr: continue
        _, name, _, price, _, _ = pr
        total += price * qty
        items.append((pid, qty, name, price))
    CHECKOUT[user] = {"items": items, "total": total}
    await msg.answer(f"Buyurtma jami: {total} so'm\nIsm, manzil va telefoningizni quyidagi formatda yuboring:\nMasalan:\nAli — Toshkent, Shayxontohur — +998901234567")

@dp.message(lambda m: m.text and "—" in m.text and "+" in m.text)  # simple parser
async def receive_address(msg: types.Message):
    user = msg.from_user.id
    info = CHECKOUT.get(user)
    if not info:
        return
    # Very simple parse: assume "Name — Address — Phone"
    try:
        parts = [p.strip() for p in msg.text.split("—")]
        fullname = parts[0]
        address = parts[1]
        phone = parts[2]
    except Exception:
        return await msg.answer("Format xato. Iltimos: Ism — Manzil — +998... tarzida yuboring.")
    total = info["total"]
    # create order in DB
    order_id = await create_order(user, fullname, address, phone, total)
    for pid, qty, name, price in info["items"]:
        await add_order_item(order_id, pid, qty, price)
    # Clear cart and checkout data after saving to DB
    CARTS.pop(user, None)
    CHECKOUT.pop(user, None)
    # Prepare payment options
    text = (f"Buyurtma qabul qilindi — #{order_id}\nJami: {total} so'm\n"
            "To‘lovni tanlang:\n1) Payme/Click onlayn (bank kartasi)\n2) USDT (TRC20)\n\n"
            f"/pay_payme_{order_id}  — Payme\n/pay_click_{order_id} — Click\n/pay_usdt_{order_id} — USDT (TRC20)")
    await msg.answer(text)
    # notify admin if configured
    if ADMIN_ID:
        await bot.send_message(
            int(ADMIN_ID),
            f"Yangi buyurtma #{order_id}\nFIO: {fullname}\nManzil: {address}\nTel: {phone}\nJami: {total} so'm",
        )

# ----- Payment link creation (shablon) -----
# NOTE: quyidagi funksiyalarga real API chaqiruvlari joylashtiring (HTTP request bilan).
def create_payment_link_payme(order_id, amount):
    # Bu yerda Payme API chaqiruvi bo'lishi kerak: order_id, amount, return_url va sign
    # Hozir shunchaki misol link:
    return f"{WEBHOOK_HOST}/fake_payme_pay?order={order_id}&amount={amount}"

def create_payment_link_click(order_id, amount):
    return f"{WEBHOOK_HOST}/fake_click_pay?order={order_id}&amount={amount}"

def create_payment_details_usdt(order_id, amount):
    # nusxa: foydalanuvchiga TRC20 wallet va summa yuborish
    wallet = os.getenv("USDT_WALLET", "TXXXXXXXXXXXXXXXXXXXXXXXX")
    # convert so'm -> USDT estimate — siz kursni o'zingiz hisoblaysiz
    usdt_amount = round(amount / 120000)  # misol: 120,000 so'm = 1 USDT
    return wallet, usdt_amount

# payment commands
@dp.message(lambda m: m.text and m.text.startswith("/pay_payme_"))
async def pay_payme(msg: types.Message):
    order_id = int(msg.text.split("_")[-1])
    total = await get_order_total(order_id)
    if total is None:
        return await msg.answer("Buyurtma topilmadi. Iltimos, /cart orqali qayta xarid qiling.")
    link = create_payment_link_payme(order_id, total)
    await msg.answer(f"To‘lov sahifasiga o‘ting: {link}")

@dp.message(lambda m: m.text and m.text.startswith("/pay_click_"))
async def pay_click(msg: types.Message):
    order_id = int(msg.text.split("_")[-1])
    total = await get_order_total(order_id)
    if total is None:
        return await msg.answer("Buyurtma topilmadi. Iltimos, /cart orqali qayta xarid qiling.")
    link = create_payment_link_click(order_id, total)
    await msg.answer(f"To‘lov sahifasiga o‘ting: {link}")

@dp.message(lambda m: m.text and m.text.startswith("/pay_usdt_"))
async def pay_usdt(msg: types.Message):
    order_id = int(msg.text.split("_")[-1])
    total = await get_order_total(order_id)
    if total is None:
        return await msg.answer("Buyurtma topilmadi. Iltimos, /cart orqali qayta xarid qiling.")
    wallet, usdt_amount = create_payment_details_usdt(order_id, total)
    await msg.answer(f"USDT (TRC20) yuboring:\nWallet: `{wallet}`\nSumma: {usdt_amount} USDT\n"
                     "To‘lov qilinganidan so‘ng to‘lov txidini yuboring.")

# Webhook endpoint for payment callbacks (aiohttp)
async def handle_payment_callback(request):
    data = await request.json()
    # Bu yerda Payme yoki Clickning callback formatini tekshirish va tasdiqlash kerak
    # Agar to'lov muvaffaqiyatli bo'lsa, order_id ni topib orders.status ni 'paid' ga o'zgartiring va adminga yuboring
    # MISOL:
    order_id = data.get("order")
    paid = data.get("paid", False)
    if paid:
        # update order status in DB (yozib qo'ying)
        if ADMIN_ID:
            await bot.send_message(int(ADMIN_ID), f"Buyurtma #{order_id} to‘landi.")
    return web.json_response({"ok": True})

app = web.Application()
app.router.add_post(WEBHOOK_PATH, handle_payment_callback)

async def on_startup():
    await init_db()
    await load_settings()

async def main():
    await on_startup()
    # start aiohttp server in background
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()
    # start bot
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
