import os
import logging
import sqlite3
import re
import random
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler, CallbackQueryHandler
from openai import OpenAI

# ---------- НАСТРОЙКИ ----------
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    TOKEN = "8960258146:AAEooW9g65ngBevd9lZYfJhSGA-qorb63lg"

GROUP_CHAT_ID = -1004462437609
ADMIN_IDS = [549890508]
BOT_USERNAME = "oz_support_bot"

MORNING_TIME_UTC = "03:00"  # 09:00 по Бишкеку

# ---------- СОВЕТЫ ----------
ADVICE_LIST = [
    "🔄 Попробуйте очистить кэш браузера и перезагрузить страницу.",
    "👤 Попробуйте выйти из профиля и зайти заново (перезайти).",
    "🌐 Проверьте интернет-соединение – возможно, проблемы с сетью.",
    "🧹 Очистите cookies и кэш, затем обновите страницу (Ctrl+F5).",
    "🔁 Попробуйте использовать другой браузер или режим инкогнито.",
    "⏳ Проверьте, не ведутся ли технические работы на сайте.",
    "📞 Если ничего не помогает, обратитесь к администратору системы.",
    "💾 Попробуйте перезагрузить устройство (компьютер/телефон)."
]

# ---------- ФРАЗЫ ДЛЯ ПРЯМОЙ ПОМОЩИ (расширенные) ----------
HELP_PHRASES = [
    "жардам керек",
    "жардам берличе",
    "нужна помощь",
    "требуется помощь",
    "помогите",
    "помоги",
    "help",
    "жардам",
    "жардамга",
    "жардам бергилечи",
    "сос",
    "помощь керек",
    "нужно помощь",
    "помощь"          # теперь слово "помощь" само по себе вызывает ответственного
]

# ---------- ФРАЗЫ ДЛЯ ТЕХНИЧЕСКИХ РАБОТ ----------
TECHNICAL_WORKS_PHRASES = [
    "техническая работа",
    "технические работы",
    "технические неполадки",
    "технические проблемы"
]

def get_random_advice():
    return random.choice(ADVICE_LIST)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- ИИ (для /ask) ----------
AI_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
ai_client = None
if AI_API_KEY:
    try:
        ai_client = OpenAI(api_key=AI_API_KEY, base_url="https://api.deepseek.com")
        logger.info("AI клиент инициализирован")
    except Exception as e:
        logger.error(f"Ошибка инициализации AI: {e}")
else:
    logger.warning("DEEPSEEK_API_KEY не задан, ИИ-функции отключены")

# ---------- БАЗА ДАННЫХ ----------
def init_db():
    conn = sqlite3.connect("support.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS keywords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT UNIQUE
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS responsible_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS banned_users (
        user_id INTEGER PRIMARY KEY,
        reason TEXT,
        banned_at TIMESTAMP
    )''')
    # Начальные ключевые слова (расширенные)
    initial_keywords = [
        "система не работает", "sanarip не работает", "санарип не работет",
        "база зависает", "база катып жатат", "база жай иштеп жатат",
        "база иштебей калды", "система медленно работает", "не работает",
        "ошибка", "баг", "глюк", "завис", "не открывается", "не грузит",
        "проблема", "система тутап калды", "система жай иштейт",
        "санприп иштебей калды", "санприп жай иштейт",
        "сайт не работает",          # новое
        "мис не работает",           # новое
        "а что с мис",               # новое
        "а что с мисс"               # новое
    ]
    for kw in initial_keywords:
        c.execute("INSERT OR IGNORE INTO keywords (word) VALUES (?)", (kw,))
    # Начальные ответственные
    default_responsible = ["analyst"]
    for user in default_responsible:
        c.execute("INSERT OR IGNORE INTO responsible_users (username) VALUES (?)", (user,))
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

# ---------- ФУНКЦИИ ЗАГРУЗКИ ----------
def load_keywords():
    conn = sqlite3.connect("support.db")
    c = conn.cursor()
    try:
        c.execute("SELECT word FROM keywords")
        rows = c.fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [row[0] for row in rows]

def load_responsible():
    conn = sqlite3.connect("support.db")
    c = conn.cursor()
    try:
        c.execute("SELECT username FROM responsible_users")
        rows = c.fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [row[0] for row in rows]

# ---------- РАБОТА С КЛЮЧЕВЫМИ СЛОВАМИ ----------
def add_keyword(word):
    conn = sqlite3.connect("support.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO keywords (word) VALUES (?)", (word,))
    conn.commit()
    conn.close()

def remove_keyword(word):
    conn = sqlite3.connect("support.db")
    c = conn.cursor()
    c.execute("DELETE FROM keywords WHERE word=?", (word,))
    conn.commit()
    conn.close()

# ---------- РАБОТА С ОТВЕТСТВЕННЫМИ ----------
def add_responsible(username):
    conn = sqlite3.connect("support.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO responsible_users (username) VALUES (?)", (username,))
    conn.commit()
    conn.close()

def remove_responsible(username):
    conn = sqlite3.connect("support.db")
    c = conn.cursor()
    c.execute("DELETE FROM responsible_users WHERE username=?", (username,))
    conn.commit()
    conn.close()

# ---------- БАНЫ ----------
def is_banned(user_id):
    conn = sqlite3.connect("support.db")
    c = conn.cursor()
    try:
        c.execute("SELECT 1 FROM banned_users WHERE user_id=?", (user_id,))
        row = c.fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    return row is not None

def ban_user(user_id, reason=""):
    conn = sqlite3.connect("support.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO banned_users (user_id, reason, banned_at) VALUES (?, ?, ?)",
              (user_id, reason, datetime.now()))
    conn.commit()
    conn.close()

def unban_user(user_id):
    conn = sqlite3.connect("support.db")
    c = conn.cursor()
    c.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# ---------- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ----------
KEYWORDS = []
RESPONSIBLE_LIST = []

def check_keywords(text: str) -> bool:
    lower = text.lower()
    for kw in KEYWORDS:
        if kw in lower:
            return True
    return False

def check_technical_works(text: str) -> bool:
    lower = text.lower()
    for phrase in TECHNICAL_WORKS_PHRASES:
        if phrase in lower:
            return True
    return False

# ---------- ОТВЕТ НА ВОПРОСЫ О БОТЕ ----------
async def reply_bot_info(update: Update):
    info_text = (
        "🤖 Мои возможности:\n\n"
        "🔹 Я помогаю сотрудникам ОЗ и консультантам по внедрению.\n"
        "🔹 Если вы напишете проблему (например, «система не работает»), я дам совет и спрошу, помог ли он.\n"
        "🔹 Если нажать «Не помогло», я отправлю уведомление ответственному.\n"
        "🔹 Если вы напишете «нужна помощь» или «жардам керек», я сразу передам сообщение ответственному (без совета).\n\n"
        "📋 Команды для всех:\n"
        "/help – показать это сообщение\n"
        "/ask <вопрос> – задать вопрос ИИ (если настроен)\n"
        "/list_keywords – список ключевых слов\n"
        "/list_responsible – список ответственных\n\n"
        "🔒 Админ-команды:\n"
        "/add_keyword <фраза> – добавить ключевую фразу\n"
        "/remove_keyword <фраза> – удалить ключевую фразу\n"
        "/add_responsible @username – добавить ответственного\n"
        "/remove_responsible @username – удалить ответственного\n"
        "/ban_user <id> – забанить пользователя\n"
        "/unban_user <id> – разбанить пользователя\n"
        "/list_banned – список забаненных\n\n"
        "Если у вас есть проблема, просто опишите её — я помогу!"
    )
    await update.message.reply_text(info_text)

def is_about_bot(text: str) -> bool:
    text_lower = text.lower()
    patterns = [
        r'\bты\s*(кто|чей|какой|как)\b',
        r'\b(что|как|зачем|для чего)\s*ты\b',
        r'\b(умеешь|можешь|делаешь|работаешь)\b',
        r'\b(твоя функция|твои возможности|о тебе|расскажи о себе)\b',
        r'\b(эмне кыла аласын|кантип иштейсин|сен ким|сен эмне кыласын)\b'
    ]
    for pattern in patterns:
        if re.search(pattern, text_lower):
            return True
    return False

# ---------- ОБРАБОТЧИК КНОПОК ----------
async def advice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "advice_helped":
        await query.edit_message_text("✅ Отлично! Рады, что помогли.")
    elif data == "advice_not_helped":
        await query.edit_message_text("🔄 Ваш запрос принят. Сообщение отправлено аналитику системы.")
        responsible_users = load_responsible()
        if responsible_users:
            mentions = " ".join([f"@{u}" for u in responsible_users])
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=f"⚠️ Пользователь @{query.from_user.username or 'без юзернейма'} не смог решить проблему.\n"
                     f"Сообщение: {context.user_data.get('last_problem_text', '')}\n"
                     f"Ответственные: {mentions}"
            )
        else:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text="⚠️ Нет назначенных ответственных. Сообщение не отправлено."
            )

# ---------- ОСНОВНЫЕ ОБРАБОТЧИКИ ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text
    logger.info(f"Получено: {text} от {msg.from_user.username} (chat_id: {msg.chat_id})")

    if is_banned(msg.from_user.id):
        await msg.reply_text("⛔ Вы забанены.")
        return

    if msg.reply_to_message:
        return

    if text.startswith('/'):
        return

    # Если упомянули бота или задали вопрос о нём
    if f"@{BOT_USERNAME}" in text or is_about_bot(text):
        await reply_bot_info(update)
        return

    # ---------- ПРОВЕРКА НА ТЕХНИЧЕСКИЕ РАБОТЫ ----------
    if check_technical_works(text):
        logger.info("Распознаны технические работы")
        await msg.reply_text(
            "🛠 Ведутся технические работы. Пожалуйста, подождите немного.\n"
            "Если проблема останется, обратитесь к ответственному."
        )
        return

    # ---------- ПРОВЕРКА НА ПРЯМОЙ ЗАПРОС ПОМОЩИ ----------
    lower_text = text.lower()
    if any(phrase in lower_text for phrase in HELP_PHRASES):
        logger.info("Распознан запрос помощи")
        await msg.reply_text(
            "🆘 Я вас понял! Сейчас передам сообщение ответственному.\n"
            "Пожалуйста, опишите проблему подробнее, если не сделали этого ранее."
        )
        responsible_users = load_responsible()
        if responsible_users:
            mentions = " ".join([f"@{u}" for u in responsible_users])
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=f"⚠️ Пользователь @{msg.from_user.username or 'без юзернейма'} запросил помощь.\n"
                     f"Сообщение: {text}\n"
                     f"Ответственные: {mentions}"
            )
        else:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text="⚠️ Нет назначенных ответственных. Сообщение не отправлено."
            )
        return

    # ---------- ПРОВЕРКА КЛЮЧЕВЫХ СЛОВ ----------
    if check_keywords(text):
        logger.info("Распознано по ключевым словам")
        advice = get_random_advice()
        context.user_data['last_problem_text'] = text
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
        return

# ---------- КОМАНДЫ ДЛЯ ВСЕХ ----------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_bot_info(update)

async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ai_client is None:
        await update.message.reply_text("❌ ИИ не настроен.")
        return
    if not context.args:
        await update.message.reply_text("❓ Напишите вопрос после команды: /ask ваш вопрос")
        return
    question = " ".join(context.args)
    if len(question) > 500:
        await update.message.reply_text("⚠️ Вопрос слишком длинный (макс. 500 символов).")
        return
    await update.message.reply_text("🤔 Думаю...")
    try:
        response = ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Ты — полезный и информативный ассистент."},
                {"role": "user", "content": question}
            ]
        )
        answer = response.choices[0].message.content
        # Очистка от Markdown
        cleaned = re.sub(r'\*\*(.*?)\*\*', r'\1', answer)
        cleaned = re.sub(r'\*(.*?)\*', r'\1', cleaned)
        cleaned = re.sub(r'_(.*?)_', r'\1', cleaned)
        cleaned = re.sub(r'#{1,6}\s?', '', cleaned)
        cleaned = re.sub(r'`(.*?)`', r'\1', cleaned)
        cleaned = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', cleaned)
        cleaned = cleaned.replace('*', '')
        await update.message.reply_text(cleaned)
    except Exception as e:
        logger.error(f"Ошибка /ask: {e}")
        await update.message.reply_text("❌ Извините, произошла ошибка.")

# ---------- АДМИН-КОМАНДЫ ----------
async def add_keyword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("Укажите ключевое слово: /add_keyword система не работает")
        return
    keyword = " ".join(context.args).strip().lower()
    if not keyword:
        await update.message.reply_text("Некорректное ключевое слово.")
        return
    add_keyword(keyword)
    global KEYWORDS
    KEYWORDS = load_keywords()
    await update.message.reply_text(f"✅ Ключевое слово «{keyword}» добавлено.")

async def remove_keyword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("Укажите ключевое слово: /remove_keyword система не работает")
        return
    keyword = " ".join(context.args).strip().lower()
    if not keyword:
        await update.message.reply_text("Некорректное ключевое слово.")
        return
    remove_keyword(keyword)
    global KEYWORDS
    KEYWORDS = load_keywords()
    await update.message.reply_text(f"✅ Ключевое слово «{keyword}» удалено.")

async def list_keywords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kw_list = load_keywords()
    if not kw_list:
        await update.message.reply_text("Список ключевых слов пуст.")
        return
    response = "📋 Ключевые слова:\n" + "\n".join([f"• {kw}" for kw in kw_list])
    await update.message.reply_text(response)

async def add_responsible_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("Укажите юзернейм: /add_responsible @username")
        return
    username = context.args[0].lstrip('@').strip()
    if not username:
        await update.message.reply_text("Некорректный юзернейм.")
        return
    add_responsible(username)
    global RESPONSIBLE_LIST
    RESPONSIBLE_LIST = load_responsible()
    await update.message.reply_text(f"✅ @{username} добавлен в список ответственных.")

async def remove_responsible_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("Укажите юзернейм: /remove_responsible @username")
        return
    username = context.args[0].lstrip('@').strip()
    if not username:
        await update.message.reply_text("Некорректный юзернейм.")
        return
    remove_responsible(username)
    global RESPONSIBLE_LIST
    RESPONSIBLE_LIST = load_responsible()
    await update.message.reply_text(f"✅ @{username} удалён из списка ответственных.")

async def list_responsible_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    resp_list = load_responsible()
    if not resp_list:
        await update.message.reply_text("Список ответственных пуст.")
        return
    response = "📋 Список ответственных:\n" + "\n".join([f"@{u}" for u in resp_list])
    await update.message.reply_text(response)

async def ban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("Укажите ID пользователя: /ban_user 123456789")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Некорректный ID.")
        return
    ban_user(user_id, "Забанен администратором")
    await update.message.reply_text(f"✅ Пользователь {user_id} забанен.")

async def unban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("Укажите ID пользователя: /unban_user 123456789")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Некорректный ID.")
        return
    unban_user(user_id)
    await update.message.reply_text(f"✅ Пользователь {user_id} разбанен.")

async def list_banned_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет прав.")
        return
    conn = sqlite3.connect("support.db")
    c = conn.cursor()
    c.execute("SELECT user_id, reason, banned_at FROM banned_users")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Забаненных пользователей нет.")
        return
    response = "🚫 Забаненные пользователи:\n"
    for uid, reason, banned_at in rows:
        response += f"ID: {uid} (причина: {reason or 'не указана'}, забанен: {banned_at[:16]})\n"
    await update.message.reply_text(response)

# ---------- УТРЕННЕЕ ПРИВЕТСТВИЕ ----------
async def morning_greeting(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text="🌞 Доброе утро, коллеги! Желаем продуктивного дня и поменьше проблем с системой! 😊"
    )

# ---------- ЗАПУСК ----------
def main():
    init_db()
    global KEYWORDS, RESPONSIBLE_LIST
    KEYWORDS = load_keywords()
    RESPONSIBLE_LIST = load_responsible()
    logger.info(f"Загружено {len(KEYWORDS)} ключевых слов и {len(RESPONSIBLE_LIST)} ответственных")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(advice_callback, pattern="^(advice_helped|advice_not_helped)$"))

    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("ask", ask_command))
    
    app.add_handler(CommandHandler("add_keyword", add_keyword_command))
    app.add_handler(CommandHandler("remove_keyword", remove_keyword_command))
    app.add_handler(CommandHandler("list_keywords", list_keywords_command))
    
    app.add_handler(CommandHandler("add_responsible", add_responsible_command))
    app.add_handler(CommandHandler("remove_responsible", remove_responsible_command))
    app.add_handler(CommandHandler("list_responsible", list_responsible_command))
    
    app.add_handler(CommandHandler("ban_user", ban_user_command))
    app.add_handler(CommandHandler("unban_user", unban_user_command))
    app.add_handler(CommandHandler("list_banned", list_banned_command))

    if app.job_queue:
        try:
            morning_time = datetime.strptime(MORNING_TIME_UTC, "%H:%M").time()
            app.job_queue.run_daily(morning_greeting, time=morning_time, days=tuple(range(7)))
            logger.info(f"Утреннее приветствие запланировано на {MORNING_TIME_UTC} UTC")
        except Exception as e:
            logger.error(f"Ошибка планирования: {e}")

    logger.info("Поддержка-бот (с расширенными фразами) запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
