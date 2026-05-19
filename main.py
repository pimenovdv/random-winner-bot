import logging
import random
import re
import asyncio
from collections import Counter
from telegram import Update, MessageEntity
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest, TelegramError, RetryAfter
from config import settings

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
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет сообщение при команде /start"""
    logger.info(f"Команда /start от пользователя {update.effective_user.id}")
    await send_message_with_retry(context, update.effective_chat.id, "Привет! Я бот для выбора победителя. Тегните меня или ответьте на моё сообщение!")

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

    sim_results = [random.choice(participants) for _ in range(1000)]
    stats = Counter(sim_results)
    final_winner, winner_score = stats.most_common(1)[0]

    try:
        await context.bot.send_dice(chat_id=chat_id)
    except Exception as e:
        logger.error(f"Error sending dice: {e}")
    
    await asyncio.sleep(4)

    losers = [p for p in participants if p != final_winner]
    if losers:
        num_teases = random.randint(1, len(losers))
        tease_targets = random.sample(losers, num_teases)
        
        tease_phrases = [
            "Может это @{name}... нет ❌",
            "Хмм, @{name} был близок... но не в этот раз! 🧐",
            "Почти победа для @{name}... но нет! 🙊",
            "Смотрим на @{name}... мимо! 💨"
        ]

        for name in tease_targets:
            phrase = random.choice(tease_phrases).format(name=name)
            await send_message_with_retry(context, chat_id, phrase)
            await asyncio.sleep(random.uniform(1.0, 2.0))

    safe_prize = escape_markdown(prize_text)
    stats_text = "📊 *Результаты 1000 бросков:*\n"
    for user, count in stats.most_common():
        percentage = (count / 1000) * 100
        stats_text += f"@{user}: {count} побед ({percentage:.1f}%)\n"
    
    response_text = f"{stats_text}\n"
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
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен с использованием dynaconf.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
