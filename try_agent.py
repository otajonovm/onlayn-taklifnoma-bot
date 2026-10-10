"""Agent uslubini Telegram'siz terminalda sinash: python try_agent.py"""
import asyncio
import sys

import agent

DEFAULT_SCRIPT = [
    "Assalomu alaykum",
    "Narxi qancha",
    "Video kerak",
    "1-chisini tanladim",
    "Nikoh to'yi",
    "Sardor va Dilnoza",
    "15 noyabr soat 18:00",
    "Grand Hall to'yxonasi, Toshkent Chilonzor",
    "Botmisiz?",
]


async def main() -> None:
    key = "test"
    lines = sys.argv[1:] or DEFAULT_SCRIPT
    for text in lines:
        reply = await agent.ask(key, text, "Jasur Karimov")
        print(f"\nMijoz: {text}")
        for msg in reply.messages:
            print(f"  Masudbek: {msg}")
        if reply.show_templates:
            print(f"  [shablonlar yuboriladi: {reply.template_kind or 'hammasi'}]")
            agent.add_event(key, "Mijozga shablonlar yuborildi: 1. Klassik (video, id 1); 2. Gold (video, id 2)")
        if reply.payment_shown:
            print("  [karta yuborildi -> adminga xabar]")


if __name__ == "__main__":
    asyncio.run(main())
