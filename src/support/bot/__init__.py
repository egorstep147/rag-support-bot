from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from support.config import settings

COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="operator", description="Позвать оператора"),
]


def create_bot() -> Bot:
    return Bot(settings.bot_token.get_secret_value())


def create_dispatcher() -> Dispatcher:
    from support.bot import admin, handlers

    dp = Dispatcher(storage=MemoryStorage())
    # Админка раньше: файл от админа — в базу знаний, а не вопрос клиента
    dp.include_routers(admin.router, handlers.router)
    return dp
