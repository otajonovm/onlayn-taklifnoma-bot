import asyncio
from aiogram import Bot
from config import BOT_TOKEN


async def main() -> None:
    bot = Bot(BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Webhook ochirildi")
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
