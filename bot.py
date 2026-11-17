"""
Универсальный Telegram-бот на базе aiogram v3, Webhook (Render), 
с ротацией ключей Gemini API, мультимодальным анализом фото/текста 
и оплатой через Telegram Stars (10 звезд)[cite: 1].
"""

import asyncio
import io
import os
import time
from PIL import Image

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    Message,
    PreCheckoutQuery,
    LabeledPrice,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from google import genai
from google.genai import types as genai_types

# ==================== КОНФИГУРАЦИЯ БОТА ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DEVELOPER_IDS = {8186927099, 1359780349, 9215066653}  # Бесплатные VIP ID[cite: 1]
STARS_PRICE = 10                                      # Стоимость запроса в Telegram Stars[cite: 1]

# Переменные для Webhook (Render автоматически подставляет RENDER_EXTERNAL_URL)[cite: 1]
WEBHOOK_HOST = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.environ.get("PORT", 10000))


# ==================== МЕХАНИЗМ РОТАЦИИ КЛЮЧЕЙ GEMINI ====================
class ApiKeyManager:
    """Менеджер для автоматического поиска и ротации ключей вида API_KEY1, API_KEY2...[cite: 1]"""
    def __init__(self):
        self.keys = []
        self.current_index = 0
        self.load_keys()

    def load_keys(self):
        self.keys = []
        i = 1
        while True:
            key_name = f"API_KEY{i}"
            val = os.environ.get(key_name)
            if val:
                self.keys.append(val.strip())
                i += 1
            else:
                found_any_ahead = False
                for j in range(1, 4):
                    if os.environ.get(f"API_KEY{i + j}"):
                        found_any_ahead = True
                        break
                if not found_any_ahead:
                    break
                i += 1
        if not self.keys and os.environ.get("GEMINI_API_KEY"):
            self.keys.append(os.environ.get("GEMINI_API_KEY").strip())
        print(f"🔄 Загружено API-ключей ротации Gemini: {len(self.keys)}")

    def get_current_key(self):
        if not self.keys:
            return None
        return self.keys[self.current_index]

    def rotate(self):
        if not self.keys:
            return
        old_index = self.current_index
        self.current_index = (self.current_index + 1) % len(self.keys)
        print(f"⚠️ Ротация API-ключа Gemini: переключение с индекса {old_index + 1} на {self.current_index + 1}")


api_manager = ApiKeyManager()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

# Хранилище оплативших пользователей (в памяти)
PAID_USERS = set()


def get_gemini_client():
    """Создает свежий экземпляр клиента Gemini с текущим ключом ротации."""
    current_key = api_manager.get_current_key()
    return genai.Client(api_key=current_key)


def has_access(user_id: int) -> bool:
    """Проверяет, есть ли у пользователя бесплатный VIP-доступ или куплена подписка[cite: 1]."""
    return (user_id in DEVELOPER_IDS) or (user_id in PAID_USERS)


async def send_paywall(message: Message, description: str = "Доступ к ИИ-ассистенту Gemini"):
    """Отправляет инвойс на оплату 10 Telegram Stars[cite: 1]."""
    prices = [LabeledPrice(label="Подписка / Запрос (10 ⭐)", amount=STARS_PRICE)]
    await message.answer(
        "🔒 **Доступ ограничен!**\n\n"
        "Для работы с ботом и анализа материалов требуется оплата.\n"
        f"Стоимость: **{STARS_PRICE} Telegram Stars ⭐**.",
    )
    await message.bot.send_invoice(
        chat_id=message.chat.id,
        title="ИИ-Ассистент Gemini",
        description=description,
        payload=f"stars_payment_{message.from_user.id}_{int(time.time())}",
        currency="XTR",
        prices=prices
    )


# ==================== РОУТЕР И ОБРАБОТЧИКИ (aiogram v3) ====================
router = Router()
router.message.filter(F.from_user.is_bot == False)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    welcome_text = (
        "👋 Привет! Я умный ассистент на базе **Gemini** с поддержкой мультимодальности.\n\n"
        "📸 **Что я умею:**\n"
        "• Принимать фото и скриншоты тестов, задач или чертежей на любых языках.\n"
        "• Распознавать текст (OCR), переводить его и давать точные правильные ответы.\n\n"
        "Просто отправь мне фото с вопросом или напиши текстовое сообщение!"
    )

    if user_id in DEVELOPER_IDS:
        welcome_text += "\n\n👑 Обнаружен VIP-статус: для вас все запросы бесплатны![cite: 1]"

    await message.answer(welcome_text)


@router.message(Command("reset"))
async def cmd_reset(message: Message):
    await message.answer("🔄 Готово! Отправьте новый вопрос или фото.")


# Обработка платежей Telegram Stars[cite: 1]
@router.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    user_id = message.from_user.id
    PAID_USERS.add(user_id)
    await message.answer(
        "🎉 **Оплата прошла успешно!** Спасибо.\n\n"
        "Теперь вам доступны все функции бота. Отправьте фото теста или задайте вопрос!"
    )


# Обработка фотографий (тесты, скриншоты, задачи)
@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    user_id = message.from_user.id

    if not has_access(user_id):
        return await send_paywall(message, description="Анализ изображения / теста")

    status_msg = await message.answer("⏳ Анализирую изображение и ищу ответ...")
    await bot.send_chat_action(message.chat.id, "upload_photo")

    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)
        image = Image.open(io.BytesIO(downloaded_file.read()))

        caption = (
            message.caption
            or "Внимательно проанализируй эту картинку, распознай текст/вопрос, "
               "переведи его при необходимости, найди правильный ответ и дай развернутое пояснение."
        )

        attempts = max(1, len(api_manager.keys)) if api_manager.keys else 1
        response_text = None

        for _ in range(attempts):
            try:
                client = get_gemini_client()
                response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=[image, caption],
                    config=genai_types.GenerateContentConfig(
                        system_instruction="Ты — эксперт по анализу тестов, документов и изображений. Дай точный и правильный ответ.",
                        temperature=0.3
                    )
                )
                response_text = response.text
                break
            except Exception as e:
                print(f"⚠️ Ошибка Gemini для фото, ротирую ключ: {e}")
                api_manager.rotate()
                await asyncio.sleep(0.3)

        if response_text:
            await message.reply(response_text)
        else:
            await message.reply("❌ Не удалось получить ответ от Gemini. Проверьте API-ключи.")

    except Exception as err:
        await message.reply(f"❌ Произошла ошибка при обработке изображения: {err}")
    finally:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
        except Exception:
            pass


# Обработка текстовых сообщений
@router.message(F.text)
async def handle_text(message: Message, bot: Bot):
    user_id = message.from_user.id

    if not has_access(user_id):
        return await send_paywall(message, description="Текстовый запрос к Gemini")

    await bot.send_chat_action(message.chat.id, "typing")

    attempts = max(1, len(api_manager.keys)) if api_manager.keys else 1
    for _ in range(attempts):
        try:
            client = get_gemini_client()
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=message.text,
                config=genai_types.GenerateContentConfig(
                    system_instruction="Ты — мощный и умный личный ИИ-ассистент и эксперт. Отвечай четко и по делу.",
                    temperature=0.3
                )
            )
            await message.reply(response.text)
            return
        except Exception as e:
            print(f"⚠️ Ошибка текстового запроса Gemini, ротирую ключ: {e}")
            api_manager.rotate()
            await asyncio.sleep(0.3)

    await message.reply("❌ Не удалось получить ответ от Gemini. Проверьте API-ключи.")


# ==================== WEBHOOK СЕРВЕР НА AIOHTTP (ДЛЯ RENDER) ====================
async def on_startup(bot: Bot):
    if WEBHOOK_HOST:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            print("🗑️ Предыдущий вебхук успешно удален.")
        except Exception as e:
            print(f"⚠️ Ошибка при удалении вебхука: {e}")

        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        print(f"🔗 Вебхук успешно зарегистрирован в Telegram: {WEBHOOK_URL}[cite: 1]")
    else:
        print("⚠️ Переменная RENDER_EXTERNAL_URL не найдена, проверьте настройки окружения![cite: 1]")


def main():
    if not BOT_TOKEN:
        print("❌ ОШИБКА: Переменная окружения BOT_TOKEN не задана!")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    dp.startup.register(on_startup)

    app = web.Application()

    async def health_check(request):
        return web.Response(text="Gemini Bot via Webhook is running!")

    app.router.add_get("/", health_check)

    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    print(f"🤖 Запуск Webhook-сервера на порту {WEBAPP_PORT}...")
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)


if __name__ == "__main__":
    main()
