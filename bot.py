import asyncio
import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import agent
import store
from config import ADMIN_ID, BOT_TOKEN, GEMINI_API_KEY, SUPABASE_KEY, SUPABASE_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN topilmadi. .env faylini tekshiring.")
if not GEMINI_API_KEY:
    raise SystemExit("GEMINI_API_KEY topilmadi. .env faylini tekshiring.")
if not (SUPABASE_URL and SUPABASE_KEY):
    raise SystemExit("SUPABASE_URL / SUPABASE_KEY topilmadi. .env faylini tekshiring.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

is_admin = F.from_user.id == int(ADMIN_ID) if ADMIN_ID else F.from_user.id == -1
wizards: dict[int, dict] = {}
WIZARD_PROMPTS = {
    "title": "Shablon nomi?",
    "demo": "Demo havola (https://...)",
    "media": "Media: rasm/video yuboring yoki https havola. Bekor: /cancel",
    "admin_id": "Telegram ID yuboring. Bekor: /cancel",
}
CATEGORY_BUTTONS = (
    ("Nikoh", "wedding"),
    ("Qiz bazmi", "qiz_bazmi"),
    ("Sunnat", "sunnat"),
    ("Maktab", "maktab"),
    ("Tug'ilgan kun", "birthday"),
)


def _key(user_id: int) -> str:
    return f"b{user_id}"


def admin_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Shablon qo'shish", callback_data="adm:add")],
        [InlineKeyboardButton(text="Shablonlar", callback_data="adm:list")],
        [InlineKeyboardButton(text="Yoqish / o'chirish", callback_data="adm:toggles")],
    ]
    if store.is_superadmin(user_id):
        rows.append([
            InlineKeyboardButton(text="Admin qo'shish", callback_data="adm:addadmin"),
            InlineKeyboardButton(text="Admin olish", callback_data="adm:deladmin"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_admin_menu(message: Message, user_id: int) -> None:
    await message.answer("Admin panel. Tugmani tanlang.", reply_markup=admin_menu_kb(user_id))


def category_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"adm:cat:{value}")]
        for label, value in CATEGORY_BUTTONS
    ] + [[InlineKeyboardButton(text="Bekor", callback_data="adm:menu")]])


async def send_lines(message: Message, lines: list[str]) -> None:
    for i, line in enumerate(lines):
        if i:
            await bot.send_chat_action(message.chat.id, action="typing")
            await asyncio.sleep(0.8)
        await message.answer(line)


async def send_templates(message: Message, kind: str | None) -> None:
    key = _key(message.chat.id)
    try:
        items = await store.list_templates_for(kind)
    except Exception:
        logger.exception("Shablonlarni olishda xato")
        items = []
    if not items:
        await message.answer("Hozir aniqlab aytaman aka")
        await notify_admin(message.chat.id, "Mijoz shablon so'radi, lekin bazada shablon yo'q")
        return

    for i, item in enumerate(items[:10], 1):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Shuni tanlayman", callback_data=f"tpl:{item['id']}")]]
        )
        text = store.caption(item, i)
        try:
            if item.get("media_url") and item.get("media_type") == "video":
                await message.answer_video(item["media_url"], caption=text, reply_markup=kb)
            elif item.get("media_url"):
                await message.answer_photo(item["media_url"], caption=text, reply_markup=kb)
            else:
                await message.answer(text, reply_markup=kb)
        except Exception:
            logger.exception("Shablon yuborilmadi: %s", item.get("id"))
            await message.answer(text, reply_markup=kb)

    agent.add_event(key, store.summary(items[:10]))
    await message.answer("Tanlang hohlagan shablonizni")


async def handle_reply(message: Message, reply: agent.Reply, user) -> None:
    await send_lines(message, reply.messages)
    if reply.show_templates:
        await send_templates(message, reply.template_kind)
    if reply.payment_shown:
        await notify_admin(
            message.chat.id,
            "💳 Mijozga karta yuborildi\n\n" + agent.transcript(_key(message.chat.id)),
            user=user,
        )


async def notify_admin(chat_id: int, text: str, user=None) -> None:
    if not ADMIN_ID or str(chat_id) == str(ADMIN_ID):
        return
    who = ""
    if user:
        username = f"@{user.username}" if user.username else "—"
        who = f"👤 {user.full_name} ({username}) 🆔 {user.id}\n"
    try:
        await bot.send_message(int(ADMIN_ID), f"{who}{text}"[:4000])
    except Exception:
        logger.exception("Admin xabari yuborilmadi")


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    if await store.is_staff(message.from_user.id):
        wizards.pop(message.from_user.id, None)
        await show_admin_menu(message, message.from_user.id)
        return
    key = _key(message.from_user.id)
    agent.reset(key)
    lines = ["Assalomu alaykum", "Nima xizmat"]
    agent.add_assistant(key, "\n".join(lines))
    await send_lines(message, lines)


async def deny_unless_staff(message: Message) -> bool:
    if await store.is_staff(message.from_user.id):
        return False
    await message.answer("Bu buyruq faqat admin uchun")
    return True


def _saved_template_text(item: dict) -> str:
    demo = item.get("demo_url") or item.get("url") or "—"
    media = item.get("video_url") or item.get("preview_url") or item.get("media_url") or "—"
    return (
        "✅ Shablon saqlandi\n"
        f"ID: {item['id']}\n"
        f"Nomi: {item['title']}\n"
        f"Kategoriya: {item.get('category') or '—'}\n"
        f"Demo: {demo}\n"
        f"Media: {media}"
    )


async def save_catalog(message: Message, payload: dict) -> None:
    try:
        item = await store.add_template(**payload)
    except Exception as exc:
        logger.exception("Shablon saqlanmadi")
        await message.answer(f"Shablon saqlanmadi: {exc}\nAvval supabase_setup.sql ni qayta Run qiling")
        return
    await message.answer(_saved_template_text(item))


@dp.message(Command("addadmin"))
async def add_admin_cmd(message: Message, command: CommandObject) -> None:
    if not store.is_superadmin(message.from_user.id):
        await message.answer("Bu buyruq faqat super admin uchun")
        return
    target = (command.args or "").strip()
    if not target.isdigit():
        await message.answer("Format: /addadmin 123456789")
        return
    try:
        await store.upsert_admin(target)
    except Exception as exc:
        logger.exception("Admin qo'shilmadi")
        await message.answer(f"Saqlanmadi: {exc}\nAvval supabase_setup.sql ni qayta Run qiling")
        return
    await message.answer(f"✅ Foydalanuvchi ({target}) muvaffaqiyatli admin qilindi.")


@dp.message(Command("removeadmin"))
async def remove_admin_cmd(message: Message, command: CommandObject) -> None:
    if not store.is_superadmin(message.from_user.id):
        await message.answer("Bu buyruq faqat super admin uchun")
        return
    target = (command.args or "").strip()
    if not target.isdigit():
        await message.answer("Format: /removeadmin 123456789")
        return
    try:
        result = await store.remove_admin(target)
    except Exception as exc:
        logger.exception("Admin o'chirilmadi")
        await message.answer(f"O'chirilmadi: {exc}")
        return
    if result == "superadmin":
        await message.answer("Super adminni o'chirib bo'lmaydi")
    elif result == "missing":
        await message.answer("Bu foydalanuvchi admin emas")
    else:
        await message.answer(f"✅ Foydalanuvchi ({target}) adminlikdan olindi.")


@dp.message(Command("addtemplate"))
async def add_template_cmd(message: Message, command: CommandObject) -> None:
    if await deny_unless_staff(message):
        return
    if not (command.args or "").strip():
        wizards[message.from_user.id] = {"step": "title", "data": {}, "mode": "template"}
        await message.answer(WIZARD_PROMPTS["title"])
        return
    parsed = store.parse_catalog_line(command.args)
    if isinstance(parsed, str):
        await message.answer(parsed)
        return
    await save_catalog(message, parsed)


@dp.message(Command("cancel"))
async def cancel_wizard(message: Message) -> None:
    if wizards.pop(message.from_user.id, None):
        await message.answer("Bekor qilindi", reply_markup=admin_menu_kb(message.from_user.id))


@dp.message(F.text, lambda message: message.from_user and message.from_user.id in wizards)
async def wizard_step(message: Message) -> None:
    if await deny_unless_staff(message):
        wizards.pop(message.from_user.id, None)
        return
    state = wizards[message.from_user.id]
    value = message.text.strip()
    step = state["step"]
    if step == "category":
        await message.answer("Kategoriyani tugmadan tanlang", reply_markup=category_kb())
        return
    if step == "admin_id":
        wizards.pop(message.from_user.id, None)
        if not value.isdigit():
            await message.answer("ID raqam bo'lishi kerak", reply_markup=admin_menu_kb(message.from_user.id))
            return
        try:
            if state.get("mode") == "removeadmin":
                result = await store.remove_admin(value)
                text = {
                    "superadmin": "Super adminni o'chirib bo'lmaydi",
                    "missing": "Bu foydalanuvchi admin emas",
                    "removed": f"✅ Foydalanuvchi ({value}) adminlikdan olindi.",
                }[result]
            else:
                await store.upsert_admin(value)
                text = f"✅ Foydalanuvchi ({value}) muvaffaqiyatli admin qilindi."
        except Exception as exc:
            text = f"Saqlanmadi: {exc}"
        await message.answer(text, reply_markup=admin_menu_kb(message.from_user.id))
        return
    if step == "demo" and not value.startswith(("http://", "https://")):
        await message.answer("Demo havola https:// bilan boshlanishi kerak")
        return
    if step == "media" and not value.startswith(("http://", "https://")):
        await message.answer("Media uchun https havola yozing yoki rasm/video yuboring")
        return
    state["data"][step] = value
    nxt = {"title": "category", "demo": "media"}.get(step)
    if nxt:
        state["step"] = nxt
        if nxt == "category":
            await message.answer("Kategoriyani tanlang", reply_markup=category_kb())
        else:
            await message.answer(WIZARD_PROMPTS[nxt])
        return
    wizards.pop(message.from_user.id, None)
    data = state["data"]
    parsed = store.parse_catalog_line(
        f"{data['title']} | {data['category']} | {data['demo']} | {data['media']}"
    )
    if isinstance(parsed, str):
        await message.answer(parsed)
        return
    await save_catalog(message, parsed)
    await message.answer("Menyu", reply_markup=admin_menu_kb(message.from_user.id))


async def save_wizard_file(message: Message) -> None:
    state = wizards.get(message.from_user.id)
    if not state or state.get("step") != "media":
        await show_admin_menu(message, message.from_user.id)
        return
    if message.photo:
        file_id, mime, filename, media_type = message.photo[-1].file_id, "image/jpeg", "preview.jpg", "photo"
    elif message.video:
        file_id = message.video.file_id
        mime = message.video.mime_type or "video/mp4"
        filename = message.video.file_name or "preview.mp4"
        media_type = "video"
    elif message.document:
        mime = message.document.mime_type or ""
        file_id = message.document.file_id
        filename = message.document.file_name or "file"
        media_type = "video" if mime.startswith("video") else "photo"
    else:
        await message.answer("Rasm, video yoki https havola yuboring")
        return
    try:
        buffer = await bot.download(file_id)
        media_url = await store.upload_media(buffer.read(), filename, mime)
    except Exception as exc:
        logger.exception("Media yuklanmadi")
        await message.answer(f"Fayl yuklanmadi: {exc}\nQayta yuboring yoki havola yozing")
        return
    data = state["data"]
    data["media"] = media_url
    wizards.pop(message.from_user.id, None)
    parsed = store.parse_catalog_line(
        f"{data['title']} | {data['category']} | {data['demo']} | {media_url}"
    )
    if isinstance(parsed, str):
        await message.answer(parsed)
        return
    if media_type == "video":
        parsed["kind"] = "video"
        parsed["video_url"] = media_url
        parsed["preview_url"] = None
        parsed["media_type"] = "video"
    await save_catalog(message, parsed)
    await message.answer("Menyu", reply_markup=admin_menu_kb(message.from_user.id))


async def send_template_list(message: Message) -> None:
    try:
        items = await store.list_templates()
    except Exception as exc:
        await message.answer(f"Ro'yxat olinmadi: {exc}")
        return
    if not items:
        await message.answer("Aktiv shablon yo'q")
        return
    lines = []
    for index, item in enumerate(items, 1):
        demo = escape(item.get("demo_url") or item.get("url") or "—")
        lines.append(
            f"{index}. <b>{escape(item['title'])}</b> — {escape(item.get('category') or item.get('kind') or '—')}\n"
            f"ID: {item['id']}\n{demo}"
        )
    await message.answer("\n\n".join(lines), parse_mode="HTML")


@dp.message(Command("templates"))
async def templates_cmd(message: Message) -> None:
    if await deny_unless_staff(message):
        return
    await send_template_list(message)


@dp.message(Command("toggle_template"))
async def toggle_template_cmd(message: Message, command: CommandObject) -> None:
    if await deny_unless_staff(message):
        return
    template_id = (command.args or "").strip()
    if not template_id.isdigit():
        await message.answer("Format: /toggle_template 3")
        return
    try:
        item = await store.toggle_template(template_id)
    except Exception as exc:
        await message.answer(f"O'zgarmadi: {exc}")
        return
    if not item:
        await message.answer("Bunday ID topilmadi")
        return
    status = "yoqildi" if item.get("is_active") else "o'chirildi"
    await message.answer(f"✅ #{item['id']} {item['title']} — {status}")


@dp.message(Command("shablonlar"), is_admin)
async def admin_list_templates(message: Message) -> None:
    items = await store.list_templates()
    if not items:
        await message.answer(
            "Hali shablon yo'q\n\n"
            "Qo'shish: rasm yoki video yuboring, caption:\n"
            "#shablon Nom | https://havola | sayt\n"
            "Turi: video, sayt yoki rasm (havola ixtiyoriy)"
        )
        return
    lines = [
        f"#{item['id']} — {item['title']} ({item.get('kind')})" + (f"\n{item['url']}" if item.get("url") else "")
        for item in items
    ]
    await message.answer("📋 Shablonlar:\n\n" + "\n\n".join(lines) + "\n\nO'chirish: /ochir ID")


@dp.message(Command("ochir"), is_admin)
async def admin_delete_template(message: Message, command: CommandObject) -> None:
    template_id = (command.args or "").strip()
    if not template_id.isdigit():
        await message.answer("Format: /ochir 3")
        return
    ok = await store.deactivate_template(template_id)
    await message.answer("✅ O'chirildi" if ok else "Bunday ID topilmadi")


@dp.message(is_admin, F.caption.regexp(r"(?i)#shablon"))
async def admin_add_template_media(message: Message) -> None:
    spec = store.parse_template_spec(message.caption or "")
    if not spec:
        await message.answer("Format: #shablon Nom | https://havola | video")
        return
    title, url, kind = spec

    media = None
    if message.photo:
        media = (message.photo[-1].file_id, "photo", "image/jpeg", "preview.jpg")
    elif message.video:
        media = (message.video.file_id, "video", message.video.mime_type or "video/mp4",
                 message.video.file_name or "preview.mp4")
        kind = "video"
    elif message.document:
        mime = message.document.mime_type or ""
        media_type = "video" if mime.startswith("video") else "photo"
        media = (message.document.file_id, media_type, mime, message.document.file_name or "file")

    media_url = media_type = None
    if media:
        file_id, media_type, mime, filename = media
        try:
            buffer = await bot.download(file_id)
            media_url = await store.upload_media(buffer.read(), filename, mime)
        except Exception as exc:
            logger.exception("Media yuklanmadi")
            await message.answer(f"Faylni Supabase'ga yuklab bo'lmadi: {exc}\n(Bot orqali maksimal 20 MB)")
            return

    try:
        item = await store.add_template(title=title, kind=kind, url=url, media_url=media_url, media_type=media_type)
    except Exception as exc:
        logger.exception("Shablon saqlanmadi")
        await message.answer(f"Shablon saqlanmadi: {exc}")
        return
    await message.answer(f"✅ Shablon qo'shildi #{item['id']}\n{item['title']} ({item['kind']})")


@dp.message(is_admin, F.text.regexp(r"(?i)^#shablon\s+"))
async def admin_add_template_text(message: Message) -> None:
    spec = store.parse_template_spec(message.text or "")
    if not spec:
        await message.answer("Format: #shablon Nom | https://havola | sayt")
        return
    title, url, kind = spec
    try:
        item = await store.add_template(title=title, kind=kind, url=url)
    except Exception as exc:
        logger.exception("Shablon saqlanmadi")
        await message.answer(f"Shablon saqlanmadi: {exc}")
        return
    await message.answer(f"✅ Shablon qo'shildi #{item['id']}\n{item['title']} ({item['kind']})")


@dp.callback_query(F.data.startswith("adm:"))
async def admin_buttons(callback: CallbackQuery) -> None:
    user = callback.from_user
    if not await store.is_staff(user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    action = callback.data.split(":", 2)
    kind = action[1]
    await callback.answer()

    if kind == "menu":
        wizards.pop(user.id, None)
        await callback.message.answer("Admin panel. Tugmani tanlang.", reply_markup=admin_menu_kb(user.id))
        return
    if kind == "add":
        wizards[user.id] = {"step": "title", "data": {}, "mode": "template"}
        await callback.message.answer(WIZARD_PROMPTS["title"])
        return
    if kind == "cat":
        state = wizards.get(user.id)
        if not state or state.get("step") != "category":
            await callback.message.answer("Avval shablon qo'shishni boshlang", reply_markup=admin_menu_kb(user.id))
            return
        state["data"]["category"] = action[2]
        state["step"] = "demo"
        await callback.message.answer(WIZARD_PROMPTS["demo"])
        return
    if kind == "list":
        await send_template_list(callback.message)
        await callback.message.answer("Menyu", reply_markup=admin_menu_kb(user.id))
        return
    if kind == "toggles":
        items = await store.list_templates(active_only=False)
        if not items:
            await callback.message.answer("Shablon yo'q", reply_markup=admin_menu_kb(user.id))
            return
        rows = []
        for item in items[:20]:
            mark = "yoniq" if item.get("is_active") else "o'chiq"
            rows.append([InlineKeyboardButton(
                text=f"{mark}: {item['title'][:28]}",
                callback_data=f"adm:toggle:{item['id']}",
            )])
        rows.append([InlineKeyboardButton(text="Menyu", callback_data="adm:menu")])
        await callback.message.answer("Shablonni bosing", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return
    if kind == "toggle":
        item = await store.toggle_template(action[2])
        if not item:
            await callback.message.answer("Topilmadi", reply_markup=admin_menu_kb(user.id))
            return
        status = "yoqildi" if item.get("is_active") else "o'chirildi"
        await callback.message.answer(f"✅ #{item['id']} {item['title']} — {status}", reply_markup=admin_menu_kb(user.id))
        return
    if kind in ("addadmin", "deladmin"):
        if not store.is_superadmin(user.id):
            await callback.message.answer("Bu faqat super admin uchun")
            return
        wizards[user.id] = {"step": "admin_id", "data": {}, "mode": "removeadmin" if kind == "deladmin" else "addadmin"}
        await callback.message.answer(WIZARD_PROMPTS["admin_id"])


@dp.callback_query(F.data.startswith("tpl:"))
async def on_template_chosen(callback: CallbackQuery) -> None:
    item = await store.get_template(callback.data.split(":", 1)[1])
    if not item:
        await callback.answer("Shablon topilmadi", show_alert=True)
        return
    await callback.answer("Tanlandi")

    user = callback.from_user
    if await store.is_staff(user.id):
        await callback.message.answer("Admin panel. Tugmani tanlang.", reply_markup=admin_menu_kb(user.id))
        return
    store.save_order({
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "template_id": item["id"],
        "template_title": item["title"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    await notify_admin(user.id, f"🎨 Shablon tanlandi: #{item['id']} {item['title']}", user=user)

    reply = await agent.ask(_key(user.id), f"«{item['title']}» shablonini tanladim", user.full_name)
    await handle_reply(callback.message, reply, user)


@dp.message(F.photo | F.video | F.document)
async def receipt_handler(message: Message) -> None:
    if await store.is_staff(message.from_user.id):
        state = wizards.get(message.from_user.id)
        if state and state.get("step") == "media":
            await save_wizard_file(message)
            return
        if not (message.caption and "#shablon" in message.caption.lower()):
            await show_admin_menu(message, message.from_user.id)
        return
    key = _key(message.from_user.id)
    if agent.awaiting_receipt.get(key):
        agent.awaiting_receipt[key] = False
        agent.add_event(key, "Mijoz chek yubordi")
        await message.answer("Chekni oldim hozir ko'rib chiqamiz")
        await notify_admin(message.chat.id, "🧾 Mijoz chek yubordi", user=message.from_user)
        if ADMIN_ID:
            await message.forward(int(ADMIN_ID))
        return
    reply = await agent.ask(key, message.caption or "[mijoz rasm/fayl yubordi]", message.from_user.full_name)
    await handle_reply(message, reply, message.from_user)


@dp.message(F.text)
async def chat_handler(message: Message) -> None:
    text = message.text.strip()
    if not text:
        return
    if await store.is_staff(message.from_user.id):
        await show_admin_menu(message, message.from_user.id)
        return
    await bot.send_chat_action(message.chat.id, action="typing")
    reply = await agent.ask(_key(message.from_user.id), text, message.from_user.full_name)
    await handle_reply(message, reply, message.from_user)
    await notify_admin(message.chat.id, f"💬 {text}", user=message.from_user)


async def main() -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
