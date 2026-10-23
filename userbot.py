"""Shaxsiy akkauntdan ("Onlayn Taklifnoma || admin") mijozlarga javob beradigan userbot.

Birinchi ishga tushirishda telefon raqam va Telegram kodini so'raydi,
keyin `masudbek.session` faylida saqlab qoladi.
"""
import asyncio
import io
import logging
import time
from collections import defaultdict

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User

import agent
import store
from config import API_HASH, API_ID, GEMINI_API_KEY, TAKEOVER_MINUTES, USERBOT_SESSION

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("userbot")

if not (API_ID and API_HASH):
    raise SystemExit("API_ID / API_HASH topilmadi. .env faylini tekshiring.")
if not GEMINI_API_KEY:
    raise SystemExit("GEMINI_API_KEY topilmadi. .env faylini tekshiring.")

DEBOUNCE_SECONDS = 3
SEND_GAP_SECONDS = 1.0

session = StringSession(USERBOT_SESSION) if USERBOT_SESSION else "masudbek"
client = TelegramClient(session, API_ID, API_HASH)

sent_by_ai: set[tuple[int, int]] = set()
paused_until: dict[int, float] = {}
buffers: dict[int, list[str]] = defaultdict(list)
last_message_at: dict[int, float] = {}
locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
me_id: int = 0


def _key(chat_id: int) -> str:
    return f"u{chat_id}"


def _is_paused(chat_id: int) -> bool:
    return paused_until.get(chat_id, 0) > time.time()


async def send(chat_id: int, text: str = "", file=None, *, video: bool = False):
    if file:
        msg = await client.send_file(
            chat_id,
            file,
            caption=text or None,
            supports_streaming=video,
        )
    else:
        msg = await client.send_message(chat_id, text)
    sent_by_ai.add((chat_id, msg.id))
    return msg


async def notify_me(text: str) -> None:
    try:
        await client.send_message("me", text[:4000])
    except Exception:
        logger.exception("Saved Messages'ga yozilmadi")


async def send_templates(chat_id: int, kind: str | None) -> None:
    if not kind:
        return
    try:
        items = await store.list_templates_for(kind)
    except Exception:
        logger.exception("Shablonlarni olishda xato")
        items = []
    if not items:
        label = store.KIND_LABELS.get(kind, kind)
        await send(chat_id, "Hozir aniqlab aytaman aka")
        await notify_me(f"⚠️ Mijoz {chat_id} {label} namuna so'radi, bazada bu format yo'q")
        return

    items = items[:10]
    for i, item in enumerate(items, 1):
        spec = store.delivery(item, i)
        try:
            if spec["mode"] == "video" and spec["file"]:
                data, filename = await store.download_media(spec["file"])
                buf = io.BytesIO(data)
                buf.name = filename
                await send(chat_id, spec["text"], file=buf, video=True)
            elif spec["mode"] == "photo" and spec["file"]:
                await send(chat_id, spec["text"], file=spec["file"])
            else:
                await send(chat_id, spec["text"])
        except Exception:
            logger.exception("Shablon media yuborilmadi: %s", item.get("id"))
            await send(chat_id, spec["text"])
            if spec["mode"] == "video":
                await notify_me(f"⚠️ Video fayl yuborilmadi: #{item.get('id')} {item.get('title')}")
        await asyncio.sleep(0.5)

    agent.add_event(_key(chat_id), store.summary(items))
    await send(chat_id, "Tanlang hohlagan shablonizni")


async def respond(chat_id: int, text: str, sender: User) -> None:
    key = _key(chat_id)
    async with client.action(chat_id, "typing"):
        name = " ".join(filter(None, [sender.first_name, sender.last_name])) or None
        reply = await agent.ask(key, text, name)
        for i, line in enumerate(reply.messages):
            if i:
                await asyncio.sleep(SEND_GAP_SECONDS)
            await send(chat_id, line)

    if reply.show_templates:
        await send_templates(chat_id, reply.template_kind)

    if reply.payment_shown:
        name = " ".join(filter(None, [sender.first_name, sender.last_name])) or "—"
        username = f"@{sender.username}" if sender.username else "—"
        await notify_me(
            f"💳 Mijozga karta yuborildi\n👤 {name} ({username}) 🆔 {chat_id}\n\n{agent.transcript(key)}"
        )


async def flush_later(chat_id: int, stamp: float, sender: User) -> None:
    await asyncio.sleep(DEBOUNCE_SECONDS)
    if last_message_at.get(chat_id) != stamp:
        return
    async with locks[chat_id]:
        texts = buffers.pop(chat_id, [])
        if not texts or _is_paused(chat_id):
            return
        try:
            await respond(chat_id, "\n".join(texts), sender)
        except Exception:
            logger.exception("Javob yuborishda xato")


@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def on_incoming(event: events.NewMessage.Event) -> None:
    sender = await event.get_sender()
    if not isinstance(sender, User) or sender.bot or sender.is_self:
        return
    logger.info("Xabar keldi: %s id=%s contact=%s", sender.first_name, sender.id, sender.contact)
    if await store.is_staff(sender.id):
        logger.info("Admin, javob berilmaydi: %s", sender.id)
        return
    chat_id = event.chat_id
    key = _key(chat_id)
    await store.remember_client(sender.id)

    if _is_paused(chat_id):
        agent.add_user(key, event.raw_text or "[media]")
        return

    if event.photo or event.document:
        if agent.awaiting_receipt.get(key):
            agent.awaiting_receipt[key] = False
            agent.add_event(key, "Mijoz chek yubordi")
            await store.mark_paid(sender.id)
            await send(chat_id, "Chekni oldim hozir ko'rib chiqamiz")
            await notify_me(f"🧾 Mijoz {chat_id} chek yubordi")
            await event.forward_to("me")
            return
        text = event.raw_text or "[mijoz rasm/fayl yubordi]"
    else:
        text = (event.raw_text or "").strip() or "[mijoz ovozli xabar yoki stiker yubordi]"

    stamp = time.time()
    buffers[chat_id].append(text)
    last_message_at[chat_id] = stamp
    asyncio.create_task(flush_later(chat_id, stamp, sender))


@client.on(events.NewMessage(outgoing=True, func=lambda e: e.is_private))
async def on_outgoing(event: events.NewMessage.Event) -> None:
    chat_id = event.chat_id
    if chat_id == me_id:
        return
    await asyncio.sleep(0.5)
    if (chat_id, event.id) in sent_by_ai:
        return

    text = (event.raw_text or "").strip()
    if text.lower() == ".ai on":
        paused_until.pop(chat_id, None)
        await event.delete()
        await notify_me(f"✅ AI yoqildi: {chat_id}")
        return
    if text.lower() == ".ai off":
        paused_until[chat_id] = float("inf")
        await event.delete()
        await notify_me(f"⏸ AI o'chirildi: {chat_id}")
        return

    paused_until[chat_id] = time.time() + TAKEOVER_MINUTES * 60
    agent.add_admin(_key(chat_id), text or "[media]")


async def main() -> None:
    global me_id
    await client.start()
    me = await client.get_me()
    me_id = me.id
    logger.info("Userbot ishga tushdi: %s (@%s)", me.first_name, me.username)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
