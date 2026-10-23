import json
import logging
import mimetypes
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from config import STAFF_IDS, SUPER_ADMIN_CHAT_ID, SUPABASE_BUCKET, SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

TABLE = "templates"
ADMINS = "admin_users"
CLIENTS = "chat_clients"
JOBS = "paid_jobs"
USAGE = "usage_counter"
KINDS = ("video", "sayt", "rasm")
CATEGORIES = {
    "wedding": "wedding",
    "nikoh": "wedding",
    "toy": "wedding",
    "to'y": "wedding",
    "qiz_bazmi": "qiz_bazmi",
    "qiz": "qiz_bazmi",
    "sunnat": "sunnat",
    "maktab": "maktab",
    "birthday": "birthday",
    "tugilgan": "birthday",
}
ORDERS_FILE = Path(__file__).resolve().parent / "orders.json"


def _headers(extra: dict | None = None) -> dict:
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    if extra:
        headers.update(extra)
    return headers


KIND_LABELS = {"video": "Video", "sayt": "Veb-sayt", "rasm": "Rasm"}


def normalize_kind(value: str | None) -> str | None:
    v = (value or "").lower()
    if "combo" in v:
        return None
    if re.search(r"(^|[^a-z])(video|vd)([^a-z]|$)", v):
        return "video"
    if re.search(r"(^|[^a-z])(sayt|veb|web|website)([^a-z]|$)", v):
        return "sayt"
    if re.search(r"(^|[^a-z])(rasm|png|karta)([^a-z]|$)", v):
        return "rasm"
    return None


def is_superadmin(chat_id: int | str) -> bool:
    return bool(SUPER_ADMIN_CHAT_ID) and str(chat_id) == SUPER_ADMIN_CHAT_ID


async def get_admin(chat_id: int | str) -> dict | None:
    params = {"select": "*", "telegram_chat_id": f"eq.{chat_id}"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/{ADMINS}", params=params, headers=_headers())
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None


async def is_staff(chat_id: int | str) -> bool:
    if is_superadmin(chat_id) or str(chat_id) in STAFF_IDS:
        return True
    try:
        return await get_admin(chat_id) is not None
    except httpx.HTTPError:
        return False


async def upsert_admin(chat_id: int | str) -> dict:
    existing = await get_admin(chat_id)
    if existing and existing.get("role") == "SUPERADMIN":
        return existing
    payload = {"telegram_chat_id": str(chat_id), "role": "ADMIN"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/{ADMINS}?on_conflict=telegram_chat_id",
            json=payload,
            headers=_headers({"Prefer": "resolution=merge-duplicates,return=representation"}),
        )
        r.raise_for_status()
        return r.json()[0]


def telegram_id_from_key(key: str) -> str | None:
    if key and key[0] in "bu" and key[1:].isdigit():
        return key[1:]
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _count_header(response: httpx.Response) -> int:
    raw = response.headers.get("content-range", "")
    total = raw.rsplit("/", 1)[-1] if raw else ""
    return int(total) if total.isdigit() else 0


async def remember_client(chat_id: int | str) -> None:
    payload = {"telegram_id": str(chat_id), "last_seen": _now()}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/{CLIENTS}?on_conflict=telegram_id",
                json=payload,
                headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            )
            r.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Klient saqlanmadi")


async def mark_paid(chat_id: int | str) -> None:
    await remember_client(chat_id)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/{JOBS}",
                json={"telegram_id": str(chat_id)},
                headers=_headers({"Prefer": "return=minimal"}),
            )
            r.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Pullik ish saqlanmadi")


async def add_tokens(amount: int) -> None:
    if amount <= 0:
        return
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/add_tokens",
                json={"amount": int(amount)},
                headers=_headers(),
            )
            r.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Token yozilmadi")


async def stats() -> dict:
    headers = _headers({"Prefer": "count=exact", "Range": "0-0"})
    async with httpx.AsyncClient(timeout=20) as client:
        clients = await client.get(
            f"{SUPABASE_URL}/rest/v1/{CLIENTS}",
            params={"select": "telegram_id"},
            headers=headers,
        )
        clients.raise_for_status()
        paid = await client.get(
            f"{SUPABASE_URL}/rest/v1/{JOBS}",
            params={"select": "id"},
            headers=headers,
        )
        paid.raise_for_status()
        usage = await client.get(
            f"{SUPABASE_URL}/rest/v1/{USAGE}",
            params={"select": "tokens", "id": "eq.1"},
            headers=_headers(),
        )
        usage.raise_for_status()
        rows = usage.json()
    return {
        "clients": _count_header(clients),
        "paid": _count_header(paid),
        "tokens": int(rows[0]["tokens"]) if rows else 0,
    }


async def remove_admin(chat_id: int | str) -> str:
    if is_superadmin(chat_id):
        return "superadmin"
    existing = await get_admin(chat_id)
    if existing and existing.get("role") == "SUPERADMIN":
        return "superadmin"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.delete(
            f"{SUPABASE_URL}/rest/v1/{ADMINS}",
            params={"telegram_chat_id": f"eq.{chat_id}"},
            headers=_headers({"Prefer": "return=representation"}),
        )
        r.raise_for_status()
        return "removed" if r.json() else "missing"


async def list_templates(kind: str | None = None, *, active_only: bool = True, category: str | None = None) -> list[dict]:
    params = {"select": "*", "order": "id.asc"}
    if active_only:
        params["is_active"] = "eq.true"
    if kind:
        params["kind"] = f"eq.{kind}"
    if category:
        params["category"] = f"eq.{category}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/{TABLE}", params=params, headers=_headers())
        r.raise_for_status()
        return r.json()


async def list_templates_for(kind: str | None) -> list[dict]:
    """Faqat so'ralgan formatdagi namunalar. Boshqa formatga tushmaydi."""
    if not kind:
        return []
    return await list_templates(kind)


async def get_template(template_id: str | int) -> dict | None:
    params = {"select": "*", "id": f"eq.{template_id}"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/{TABLE}", params=params, headers=_headers())
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None


async def upload_media(data: bytes, filename: str, content_type: str | None = None) -> str:
    ext = Path(filename).suffix or mimetypes.guess_extension(content_type or "") or ""
    path = f"{uuid.uuid4().hex}{ext}"
    content_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{path}",
            content=data,
            headers=_headers({"Content-Type": content_type, "x-upsert": "false"}),
        )
        r.raise_for_status()
    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{path}"


def file_name_from_url(url: str, default: str = "namuna.mp4") -> str:
    name = Path(url.split("?", 1)[0]).name
    if not name or "." not in name:
        return default
    return name


async def download_media(url: str) -> tuple[bytes, str]:
    """Saqlangan videoni fayl sifatida yuklab oladi. Havola mijozga ketmaydi."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("Video fayl topilmadi")
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
    content_type = (r.headers.get("content-type") or "").split(";", 1)[0].lower()
    if content_type.startswith("text/") or content_type in {"application/json", "application/xml"}:
        raise ValueError("Bu video fayl emas")
    if not r.content:
        raise ValueError("Video fayl bo'sh")
    default = "namuna.jpg" if content_type.startswith("image/") else "namuna.mp4"
    return r.content, file_name_from_url(url, default)


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip() or response.reason_phrase
    return body.get("message") or body.get("hint") or response.text.strip() or response.reason_phrase


def _check(response: httpx.Response) -> None:
    if response.is_success:
        return
    detail = _error_detail(response)
    raise httpx.HTTPStatusError(
        f"{response.status_code}: {detail}",
        request=response.request,
        response=response,
    )


async def _writable_columns() -> set[str]:
    """Jadvaldagi haqiqiy ustunlar. Yo'q ustun yuborilsa Supabase 400 qaytaradi."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/{TABLE}",
            params={"select": "*", "limit": "1"},
            headers=_headers(),
        )
        _check(r)
        rows = r.json()
    found = set(rows[0]) if rows else set()
    found -= {"id", "created_at"}
    return found or {"title", "kind", "url", "media_url", "media_type", "is_active"}


async def add_template(
    title: str,
    kind: str,
    url: str | None = None,
    media_url: str | None = None,
    media_type: str | None = None,
    category: str | None = None,
    demo_url: str | None = None,
    preview_url: str | None = None,
    video_url: str | None = None,
) -> dict:
    payload = {
        "title": title,
        "kind": kind,
        "url": url,
        "media_url": media_url,
        "media_type": media_type,
        "category": category,
        "demo_url": demo_url or url,
        "preview_url": preview_url,
        "video_url": video_url,
    }
    allowed = await _writable_columns()
    payload = {k: v for k, v in payload.items() if v is not None and k in allowed}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/{TABLE}",
            json=payload,
            headers=_headers({"Prefer": "return=representation"}),
        )
        _check(r)
        return r.json()[0]


async def toggle_template(template_id: str | int) -> dict | None:
    item = await get_template(template_id)
    if not item:
        return None
    active = not bool(item.get("is_active"))
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.patch(
            f"{SUPABASE_URL}/rest/v1/{TABLE}",
            params={"id": f"eq.{template_id}"},
            json={"is_active": active},
            headers=_headers({"Prefer": "return=representation"}),
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None


def normalize_category(value: str) -> str | None:
    key = value.strip().lower().replace(" ", "_").replace("-", "_")
    return CATEGORIES.get(key)


def _is_http(value: str) -> bool:
    return value.startswith("https://") or value.startswith("http://")


def _is_video_file(value: str) -> bool:
    path = value.lower().split("?", 1)[0]
    return path.endswith((".mp4", ".mov", ".webm", ".m4v"))


def parse_catalog_line(text: str) -> dict | str:
    """`Nom | kategoriya | demo | media` -> payload yoki xato matni."""
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 4 or not all(parts[:4]):
        return "Format: /addtemplate Nom | kategoriya | demo | media"
    title, category_raw, demo, media = parts[:4]
    category = normalize_category(category_raw)
    if not category:
        return "Kategoriya: wedding, qiz_bazmi, sunnat, maktab, birthday"
    if not _is_http(demo) or not _is_http(media):
        return "Demo va media https:// bilan boshlanishi kerak"
    video = _is_video_file(media)
    return {
        "title": title,
        "kind": "video" if video else "sayt",
        "category": category,
        "url": demo,
        "demo_url": demo,
        "preview_url": None if video else media,
        "video_url": media if video else None,
        "media_url": media if video or not media.startswith("https://t.me/") else None,
        "media_type": "video" if video else ("photo" if not media.startswith("https://t.me/") else None),
    }


async def deactivate_template(template_id: str | int) -> bool:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.patch(
            f"{SUPABASE_URL}/rest/v1/{TABLE}",
            params={"id": f"eq.{template_id}"},
            json={"is_active": False},
            headers=_headers({"Prefer": "return=representation"}),
        )
        r.raise_for_status()
        return bool(r.json())


def parse_template_spec(text: str) -> tuple[str, str | None, str] | None:
    """`#shablon Nom | https://havola | video` -> (nom, havola, tur)."""
    body = re.split(r"(?i)#shablon", text, maxsplit=1)[-1]
    parts = [p.strip() for p in body.split("|") if p.strip()]
    if not parts:
        return None
    title, url, kind = parts[0], None, "sayt"
    for part in parts[1:]:
        if part.lower().startswith("http"):
            url = part
        elif normalize_kind(part):
            kind = normalize_kind(part)
    return title, url, kind


def sample_payload(data: dict, media_url: str | None = None, media_type: str | None = None) -> dict:
    """Admin tanlagan format bo'yicha Supabase qatori."""
    kind = data.get("kind") or "sayt"
    payload = {
        "title": data["title"],
        "kind": kind,
        "category": data.get("category"),
    }
    if kind == "video":
        payload.update({
            "url": media_url,
            "video_url": media_url,
            "media_url": media_url,
            "media_type": "video",
        })
    else:
        demo = data.get("demo")
        payload.update({"url": demo, "demo_url": demo, "kind": "sayt"})
        if media_url:
            payload.update({
                "preview_url": media_url,
                "media_url": media_url,
                "media_type": media_type or "photo",
            })
    return {k: v for k, v in payload.items() if v is not None}


def delivery(item: dict, index: int | None = None) -> dict:
    """Mijozga yuborish: matn va video/rasm fayli, faqat shu namunaning formati."""
    head = f"{index}. {item['title']}" if index else item["title"]
    kind = item.get("kind") or "sayt"
    if kind == "video":
        file = item.get("video_url") or item.get("media_url")
        return {"text": head, "file": file, "mode": "video" if file else "text"}
    if kind == "rasm":
        file = item.get("preview_url") or item.get("media_url")
        return {"text": head, "file": file, "mode": "photo" if file else "text"}
    url = item.get("demo_url") or item.get("url") or ""
    text = f"{head}\n{url}".strip()
    preview = item.get("preview_url") or item.get("media_url")
    if preview and item.get("media_type") != "video":
        return {"text": text, "file": preview, "mode": "photo"}
    return {"text": text, "file": None, "mode": "text"}


def caption(item: dict, index: int | None = None) -> str:
    return delivery(item, index)["text"]


def summary(items: list[dict]) -> str:
    listed = "; ".join(
        f"{i}. {item['title']} ({item.get('kind')}, id {item['id']})"
        for i, item in enumerate(items, 1)
    )
    return f"Mijozga shablonlar yuborildi: {listed}"


def save_order(order: dict) -> None:
    rows: list = []
    if ORDERS_FILE.exists():
        try:
            rows = json.loads(ORDERS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rows = []
    rows.append(order)
    ORDERS_FILE.write_text(json.dumps(rows[-300:], ensure_ascii=False, indent=2), encoding="utf-8")
