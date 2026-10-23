import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL, PAYMENT_CARD, SYSTEM_PROMPT
from store import normalize_kind

logger = logging.getLogger(__name__)

client = genai.Client(api_key=GEMINI_API_KEY)

MAX_HISTORY = 40
FALLBACK = ["Hozir aniqlab aytaman aka"]
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]")
CARD_DIGITS = re.sub(r"\D", "", PAYMENT_CARD)

histories: dict[str, list[tuple[str, str]]] = defaultdict(list)
awaiting_receipt: dict[str, bool] = {}

SHOW_TEMPLATES_TOOL = types.FunctionDeclaration(
    name="show_templates",
    description=(
        "Mijozga tayyor shablonlarni yuborish. Mijoz namuna, shablon yoki varyant "
        "so'raganda yoki formatni tanlaganda chaqir"
    ),
    parameters={
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "description": "Mijoz so'ragan format: video, sayt yoki rasm. Formatni aniq yoz",
            },
        },
        "required": [],
    },
)

GENERATE_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[types.Tool(function_declarations=[SHOW_TEMPLATES_TOOL])],
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
)

ROLE_LABELS = {"user": "Mijoz", "assistant": "Masudbek", "event": "Tizim", "admin": "Masudbek (o'zi)"}


@dataclass
class Reply:
    messages: list[str]
    show_templates: bool = False
    template_kind: str | None = None
    payment_shown: bool = False
    tokens: int = 0


def _append(key: str, role: str, text: str) -> None:
    history = histories[key]
    history.append((role, text))
    del history[:-MAX_HISTORY]


def reset(key: str) -> None:
    histories.pop(key, None)
    awaiting_receipt.pop(key, None)


def add_user(key: str, text: str) -> None:
    _append(key, "user", text)


def add_event(key: str, text: str) -> None:
    _append(key, "event", text)


def add_assistant(key: str, text: str) -> None:
    _append(key, "assistant", text)


def add_admin(key: str, text: str) -> None:
    _append(key, "admin", text)


def transcript(key: str, last: int = 12) -> str:
    return "\n".join(f"{ROLE_LABELS[r]}: {t}" for r, t in histories[key][-last:])


DEADLINE_RE = re.compile(
    r"(soat|minut).{0,40}(tayyor|tashlab|qilib ber|qiber|beramiz|beraman)"
    r"|(tayyor|tashlab|qilib ber|qiber).{0,40}(soat|minut)",
    re.IGNORECASE,
)
DEADLINE_LINE = "Uzog'i bilan 1 kunda tayyor bo'ladi"


def _usage_tokens(response) -> int:
    meta = getattr(response, "usage_metadata", None)
    if not meta:
        return 0
    total = getattr(meta, "total_token_count", None)
    if total:
        return int(total)
    prompt = int(getattr(meta, "prompt_token_count", 0) or 0)
    output = int(getattr(meta, "candidates_token_count", 0) or 0)
    return prompt + output


def split_messages(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = EMOJI_RE.sub("", line).strip().strip("-•*").strip()
        line = line.rstrip(".").strip()
        if DEADLINE_RE.search(line):
            line = DEADLINE_LINE
        if line and line not in out:
            out.append(line)
    return out[:4]


def _ask_sync(key: str, text: str, name: str | None) -> Reply:
    if name and not any(r == "event" and t.startswith("Mijozning Telegram ismi") for r, t in histories[key]):
        _append(key, "event", f"Mijozning Telegram ismi: {name}")
    _append(key, "user", text)
    prompt = (
        f"Suhbat:\n{transcript(key, MAX_HISTORY)}\n\n"
        "Masudbek sifatida keyingi javobni yoz"
    )
    response = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt, config=GENERATE_CONFIG
    )

    texts: list[str] = []
    reply = Reply(messages=[])
    parts = response.candidates[0].content.parts if response.candidates else []
    for part in parts or []:
        if getattr(part, "function_call", None) and part.function_call.name == "show_templates":
            reply.show_templates = True
            reply.template_kind = normalize_kind((part.function_call.args or {}).get("format"))
        elif getattr(part, "text", None) and not getattr(part, "thought", False):
            texts.append(part.text)

    reply.messages = split_messages("\n".join(texts))
    if not reply.messages:
        reply.messages = ["Hozir varyantlarni tashlab beraman"] if reply.show_templates else FALLBACK

    joined = "\n".join(reply.messages)
    if CARD_DIGITS and CARD_DIGITS in re.sub(r"\D", "", joined):
        reply.payment_shown = True
        awaiting_receipt[key] = True

    _append(key, "assistant", joined)
    reply.tokens = _usage_tokens(response)
    return reply


async def ask(key: str, text: str, name: str | None = None) -> Reply:
    try:
        reply = await asyncio.to_thread(_ask_sync, key, text, name)
    except Exception:
        logger.exception("Gemini xatosi")
        reply = Reply(messages=FALLBACK)
    chat_id = store.telegram_id_from_key(key)
    if chat_id:
        await store.remember_client(chat_id)
        if reply.tokens:
            await store.add_tokens(reply.tokens)
    return reply
