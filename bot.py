import os
import logging
import sqlite3
import json
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler, CallbackQueryHandler
from openai import OpenAI

# ---------- НАСТРОЙКИ ----------
TOKEN = "8960258146:AAEooW9g65ngBevd9lZYfJhSGA-qorb63lg"
if not TOKEN:
    raise ValueError("TOKEN не задан")

GROUP_CHAT_ID = -4462437609
RESPONSIBLE_USER = "@analyst"  # ← замените на реального ответственного

# DeepSeek API
AI_API_KEY = os.environ.get("AI_BOT")
ai_client = None
if AI_API_KEY:
    try:
        ai_client = OpenAI(api_key=AI_API_KEY, base_url="https://api.deepseek.com")
        logging.info("AI клиент инициализирован")
    except Exception as e:
        logging.error(f"Ошибка инициализации AI: {e}")
else:
    logging.warning("AI_BOT не задан, ИИ-функции отключены")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- КЛЮЧЕВЫЕ СЛОВА ----------
KEYWORDS = [
    "система не работает",
    "sanarip не работает",
    "санарип не работет",
    "база зависает",
    "база катып жатат",
    "база жай иштеп жатат",
    "база иштебей калды",
    "система медленно работает",
    "не работает",
    "ошибка",
    "баг",
    "глюк",
    "завис",
    "не открывается",
    "не грузит",
    "проблема",
    "система тутап калды",
    "система жай иштейт",
    "санприп иштебей калды",
    "санприп жай иштейт"
]

def check_keywords(text: str) -> bool:
    lower = text.lower()
    for kw in KEYWORDS:
        if kw in lower:
            return True
    return False

# ---------- БАЗА ДАННЫХ ----------
def init_db():
    conn = sqlite3.connect("issues2.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS problems (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        text TEXT,
        created_at TIMESTAMP,
        resolved BOOLEAN DEFAULT 0,
        notified BOOLEAN DEFAULT 0
    )''')
    conn.commit()
    conn.close()
    logger.info("База данных для второго бота инициализирована")

# ---------- ФУНКЦИИ ИИ ----------
async def analyze_with_ai(text: str):
    if ai_client is None:
        return {"is_problem": False, "advice": "ИИ недоступен"}
    try:
        response = ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": """
                Ты — ИИ-помощник для IT-поддержки.
                Если сообщение содержит жалобу на работу системы, ошибку или проблему, 
                предложи краткое практическое решение (1-2 предложения).
                Если сообщение не является проблемой, ответь: "is_problem": false.
                Верни JSON: {"is_problem": true/false, "advice": "текст совета"}.
                """},
                {"role": "user", "content": text}
            ],
            response_format={"type": "json_object"}
        )
        result = json.loads(response.choices[0].message.content)
        return result
    except Exception as e:
        logger.error(f"AI ошибка: {e}")
        return {"is_problem": False, "advice": "Ошибка анализа"}

async def answer_with_ai(question: str) -> str:
    if ai_client is None:
        return "❌ ИИ недоступен. Проверьте переменную DEEPSEEK_API_KEY."
    try:
        if len(question) > 500:
            return "⚠️ Вопрос слишком длинный (макс. 500 символов)."
        response = ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Ты — полезный и информативный ассистент."},
                {"role": "user", "content": question}
            ]
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"AI ответ ошибка: {e}")
        return "❌ Извините, произошла ошибка при обработке вопроса."

# ---------- ОБРАБОТЧИК КНОПОК ----------
async def advice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "advice_helped":
        await query.edit_message_text("✅ Отлично! Рады, что помогли. Если будут ещё вопросы — обращайтесь.")
        conn = sqlite3.connect("issues2.db")
        c = conn.cursor()
        c.execute("UPDATE problems SET resolved=1 WHERE id=(SELECT MAX(id) FROM problems WHERE user_id=?)", (query.from_user.id,))
        conn.commit()
        conn.close()

    elif data == "advice_not_helped":
        await query.edit_message_text("🔄 Ваш запрос принят. Сообщение отправлено аналитику системы.")
        responsible = RESPONSIBLE_USER
        if responsible.startswith('@'):
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=f"⚠️ Пользователь @{query.from_user.username or 'без юзернейма'} не смог решить проблему.\n"
                     f"Сообщение: {context.user_data.get('last_problem_text', '')}\n"
                     f"Ответственный: {responsible}"
            )
        else:
            try:
                await context.bot.send_message(
                    chat_id=int(responsible),
                    text=f"⚠️ Пользователь @{query.from_user.username or 'без юзернейма'} сообщил о проблеме:\n{context.user_data.get('last_problem_text', '')}"
                )
            except:
                pass
        conn = sqlite3.connect("issues2.db")
        c = conn.cursor()
        c.execute("UPDATE problems SET notified=1 WHERE id=(SELECT MAX(id) FROM problems WHERE user_id=?)", (query.from_user.id,))
        conn.commit()
        conn.close()

# ---------- ОСНОВНОЙ ОБРАБОТЧИК ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text or msg.chat_id != GROUP_CHAT_ID:
        return
    text = msg.text

    if msg.reply_to_message:
        return

    if text.startswith('/'):
        return

    logger.info(f"Получено сообщение: {text} от {msg.from_user.username}")

    # Проверяем ключевые слова
    if check_keywords(text):
        is_problem = True
        advice = "Попробуйте перезагрузить страницу или проверить соединение. Если не поможет, обратитесь к аналитику."
        logger.info("Распознано по ключевым словам")
    else:
        analysis = await analyze_with_ai(text)
        is_problem = analysis.get("is_problem", False)
        advice = analysis.get("advice", "")
        logger.info(f"AI анализ: is_problem={is_problem}, advice={advice[:50] if advice else 'нет'}")

    if not is_problem:
        logger.info("Сообщение не распознано как проблема")
        return

    context.user_data['last_problem_text'] = text

    conn = sqlite3.connect("issues2.db")
    c = conn.cursor()
    c.execute("INSERT INTO problems (user_id, username, text, created_at) VALUES (?, ?, ?, ?)",
              (msg.from_user.id, msg.from_user.username or "", text, datetime.now()))
    conn.commit()
    conn.close()

    keyboard = [
        [
            InlineKeyboardButton("✅ Помогло", callback_data="advice_helped"),
            InlineKeyboardButton("❌ Не помогло", callback_data="advice_not_helped")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await msg.reply_text(
        f"🧠 Совет по решению:\n{advice}\n\n"
        "Если совет помог, нажмите «Помогло». Если нет — мы отправим запрос аналитику.",
        reply_markup=reply_markup
    )

# ---------- КОМАНДЫ ----------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Бот для поддержки пользователей системы.\n\n"
        "Команды:\n"
        "/help – показать это сообщение\n"
        "/ask <вопрос> – задать вопрос ИИ\n\n"
        "Просто опишите проблему — я дам совет. Если не поможет, я передам сообщение аналитику."
    )

async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❓ Напишите вопрос после команды: /ask ваш вопрос")
        return
    question = " ".join(context.args)
    if len(question) > 500:
        await update.message.reply_text("⚠️ Вопрос слишком длинный (макс. 500 символов).")
        return
    await update.message.reply_text("🤔 Думаю над вашим вопросом...")
    answer = await answer_with_ai(question)
    cleaned = re.sub(r'\*\*(.*?)\*\*', r'\1', answer)
    cleaned = re.sub(r'\*(.*?)\*', r'\1', cleaned)
    cleaned = re.sub(r'_(.*?)_', r'\1', cleaned)
    cleaned = re.sub(r'#{1,6}\s?', '', cleaned)
    cleaned = re.sub(r'`(.*?)`', r'\1', cleaned)
    cleaned = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', cleaned)
    cleaned = cleaned.replace('*', '')
    await update.message.reply_text(cleaned)

# ---------- ЗАПУСК ----------
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(advice_callback, pattern="^(advice_helped|advice_not_helped)$"))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("ask", ask_command))

    # Удаление вебхука убрано — для polling это не требуется.
    # Библиотека сама управляет циклом событий.

    logger.info("Второй бот (поддержка) с ключевыми словами запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
