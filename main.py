import logging
import random
import re
import asyncio
import httpx
from collections import Counter
from phrases import TEASE_PHRASES, WAITING_PHRASES, DUMB_MODELS
from telegram import Update, MessageEntity
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest, TelegramError, RetryAfter
from config import settings
from openai import AsyncOpenAI

# Настройка расширенного логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Настройки из dynaconf
TOKEN = settings.TOKEN
BOT_USERNAME = settings.BOT_USERNAME

# OpenAI клиенты
openrouter_client = AsyncOpenAI(
    api_key=settings.get("OPENROUTER_API_KEY", "dummy"),
    base_url="https://openrouter.ai/api/v1"
)

local_client = AsyncOpenAI(
    api_key="dummy",
    base_url="http://0.0.0.0:8080/v1"
)

# Список бесплатных моделей
free_openrouter_models = []
current_model_index = 0

async def update_openrouter_models(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обновляет список бесплатных моделей OpenRouter"""
    global free_openrouter_models
    logger.info("Старт обновления моделей openrouter")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://openrouter.ai/api/v1/models")
            response.raise_for_status()
            data = response.json()

            models = []
            for model in data.get("data", []):
                pricing = model.get("pricing", {})
                if pricing and pricing.get("prompt") == "0" and pricing.get("completion") == "0":
                    models.append(model.get("id"))

            free_openrouter_models = models
            logger.info(f"Обновлено {len(free_openrouter_models)} бесплатных моделей OpenRouter")
    except Exception as e:
        logger.error(f"Ошибка при обновлении моделей OpenRouter: {e}")

async def send_message_with_retry(context, chat_id, text, reply_to_message_id=None, parse_mode=None, retries=3):
    """Отправка сообщения с механизмом повторных попыток"""
    for i in range(retries):
        try:
            return await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
                parse_mode=parse_mode
            )
        except RetryAfter as e:
            logger.warning(f"Flood limit hit. Waiting {e.retry_after} seconds...")
            await asyncio.sleep(e.retry_after)
        except BadRequest as e:
            logger.error(f"BadRequest error: {e.message}")
            if "can't parse entities" in e.message.lower() and parse_mode == "Markdown":
                logger.info("Retrying without Markdown due to parsing error...")
                return await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_to_message_id=reply_to_message_id,
                    parse_mode=None
                )
            raise e
        except TelegramError as e:
            logger.error(f"Telegram error on attempt {i+1}: {e.message}")
            if i == retries - 1:
                raise e
            await asyncio.sleep(1)

def escape_markdown(text):
    """Экранирование специальных символов для Markdown"""
    return text #.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет сообщение при команде /start"""
    logger.info(f"Команда /start от пользователя {update.effective_user.id}")
    await send_message_with_retry(context, update.effective_chat.id, "Привет! Я бот для выбора победителя. Тегните меня или ответьте на моё сообщение!")


async def generate_battle_story(participants, final_winner, prize_text):
    global current_model_index
    await update_openrouter_models()
    
    participants_str = ", ".join([f"@{p}" for p in participants])
    
    prize_str = f" за главный приз: {prize_text}" if prize_text else ""

    system_prompt = "Ты креативный рассказчик, который описывает эпичные битвы."
    user_prompt = (
        f"Напиши короткий, но очень захватывающий рассказ об эпичной битве. "
        f"Участники битвы: {participants_str}. "
        f"Они сражаются{prize_str}. "
        f"В конце расскажи, как победил @{final_winner}. "
        f"Используй теги участников в тексте."
    )

    models_to_try = free_openrouter_models if free_openrouter_models else []
    num_models = len(models_to_try)
    attempts = 0

    if num_models > 0:
        for _ in range(num_models):
            model_to_use = models_to_try[current_model_index]
            attempts += 1
            try:
                logger.info(f"Trying openrouter model: {model_to_use}")
                response = await openrouter_client.chat.completions.create(
                    model=model_to_use,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    max_tokens=1000
                )
                story = response.choices[0].message.content.strip()
                current_model_index = (current_model_index + 1) % num_models
                return story, model_to_use, attempts
            except Exception as e:
                logger.warning(f"Error generating with {model_to_use}: {e}")
                current_model_index = (current_model_index + 1) % num_models

    # Fallback to local model
    attempts += 1
    try:
        logger.info("Falling back to local model")
        local_user_prompt = user_prompt + " Рассказ должен быть очень коротким, но очень крутым и эпичным! Напиши не больше 3-4 предложений."
        response = await local_client.chat.completions.create(
            model="local-model", # Model name is usually ignored by local backends, but required by SDK
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": local_user_prompt}
            ],
            max_tokens=400
        )
        return response.choices[0].message.content.strip(), random.choice(DUMB_MODELS), attempts
    except Exception as e:
        logger.error(f"Error generating with local model: {e}")
        return "⚔️ Битва была настолько эпичной, что летописцы не смогли описать её словами! Но победитель известен...", None, attempts

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает входящие сообщения"""
    if not update.message or not update.message.text:
        return

    message_text = update.message.text
    chat_id = update.message.chat_id
    
    is_tagged = f"@{BOT_USERNAME}" in message_text
    is_reply_to_bot = False
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        if update.message.reply_to_message.from_user.username == BOT_USERNAME:
            is_reply_to_bot = True

    if not (is_tagged or is_reply_to_bot):
        return

    all_mentions = re.findall(r"@(\w+)", message_text)
    if is_reply_to_bot and update.message.reply_to_message.text:
        parent_mentions = re.findall(r"@(\w+)", update.message.reply_to_message.text)
        all_mentions.extend(parent_mentions)

    participants = list(set([m for m in all_mentions if m.lower() != BOT_USERNAME.lower()]))
    
    if len(participants) == 0:
        if is_reply_to_bot:
            await send_message_with_retry(context, chat_id, "Для розыгрыша нужно тегнуть участников (например, @user1 @user2).", update.message.message_id)
        return

    logger.info(f"Запуск розыгрыша в чате {chat_id}. Участники: {participants}")

    lines = message_text.split('\n')
    prize_text = ""
    if len(lines) > 1:
        prize_text = '\n'.join(lines[1:]).strip()
    else:
        last_mention_match = list(re.finditer(r"@\w+", message_text))
        if last_mention_match:
            prize_text = message_text[last_mention_match[-1].end():].strip()

    sim_results = [random.choice(participants) for _ in range(10000)]
    stats = Counter(sim_results)
    final_winner, winner_score = stats.most_common(1)[0]

    try:
        await context.bot.send_dice(chat_id=chat_id)
    except Exception as e:
        logger.error(f"Error sending dice: {e}")

    # Запускаем генерацию рассказа в фоне
    story_task = asyncio.create_task(generate_battle_story(participants, final_winner, prize_text))

    messages_to_delete = []

    msg = await send_message_with_retry(context, chat_id, "⚔️ Начал моделировать битву...")
    if msg: messages_to_delete.append(msg)
    
    await asyncio.sleep(3)
    
    losers = [p for p in participants if p != final_winner]
    if losers:
        num_teases = random.randint(1, len(losers))
        tease_targets = random.sample(losers, num_teases)

        for name in tease_targets:
            phrase = random.choice(TEASE_PHRASES).format(name=name)
            msg = await send_message_with_retry(context, chat_id, phrase)
            if msg: messages_to_delete.append(msg)
            await asyncio.sleep(random.uniform(2.0, 4.0))

    # Ждем завершения генерации рассказа, если она еще идет
    while not story_task.done():
        phrase = random.choice(WAITING_PHRASES)
        msg = await send_message_with_retry(context, chat_id, phrase)
        if msg: messages_to_delete.append(msg)
        await asyncio.sleep(random.uniform(3.0, 6.0))

    battle_story, model_name, attempts = await story_task
    safe_battle_story = escape_markdown(battle_story)

    # Удаляем сообщения-тизеры и сообщения ожидания
    for msg in messages_to_delete:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
        except Exception as e:
            logger.warning(f"Не удалось удалить сообщение {msg.message_id}: {e}")

    safe_prize = escape_markdown(prize_text)
    stats_text = "📊 *Результаты 10000 бросков:*\n"
    for user, count in stats.most_common():
        percentage = (count / 10000) * 100
        stats_text += f"@{user}: {count} побед ({percentage:.1f}%)\n"
    
    response_text = f"📖 *Хроники Битвы:*\n{safe_battle_story}\n\n"
    if model_name:
        escaped_model_name = escape_markdown(model_name)
        response_text += f"📜 _{attempts} летописцев пытались описать эту битву, но только {escaped_model_name} смог это сделать._\n\n"
    response_text += f"{stats_text}\n"
    response_text += f"🏆 *Итог:* Поздравляю @{final_winner} с победой!\n"
    if prize_text:
        response_text += f"🎁 *Твой приз:* {safe_prize}"
    
    await send_message_with_retry(
        context, 
        chat_id, 
        response_text, 
        update.message.message_id,
        parse_mode="Markdown"
    )

def main() -> None:
    """Запуск бота"""
    application = Application.builder().token(TOKEN).build()

    # Регистрация планировщика для обновления моделей
    # application.job_queue.run_repeating(update_openrouter_models, interval=3600, first=0)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен с использованием dynaconf.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
