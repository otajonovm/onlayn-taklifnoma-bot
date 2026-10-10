import json
import mimetypes
import re
import uuid
from pathlib import Path

import httpx

from config import STAFF_IDS, SUPER_ADMIN_CHAT_ID, SUPABASE_BUCKET, SUPABASE_KEY, SUPABASE_URL

TABLE = "templates"
ADMINS = "admin_users"
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


def normalize_kind(value: str | None) -> str | None:
    v = (value or "").lower()
    if "combo" in v:
        return None
    if "video" in v:
        return "video"
    if "sayt" in v or "veb" in v or "web" in v:
        return "sayt"
    if "rasm" in v or "png" in v or "karta" in v:
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
    """Berilgan format bo'yicha shablonlar, bo'lmasa hammasi."""
    if kind:
        items = await list_templates(kind)
        if items:
            return items
    return await list_templates()


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
    payload = {k: v for k, v in payload.items() if v is not None}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/{TABLE}",
            json=payload,
            headers=_headers({"Prefer": "return=representation"}),
        )
        r.raise_for_status()
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


def caption(item: dict, index: int | None = None) -> str:
    head = f"{index}. {item['title']}" if index else item["title"]
    return f"{head}\n{item['url']}" if item.get("url") else head


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
