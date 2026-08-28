import os
import logging
import re
import sqlite3
import json
import random
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler, CallbackQueryHandler
from openai import OpenAI

# ---------- НАСТРОЙКИ ----------
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    # Для теста можно вставить жёстко, но лучше использовать переменную
    TOKEN = "8960258146:AAEooW9g65ngBevd9lZYfJhSGA-qorb63lg"

GROUP_CHAT_ID = -4462437609
DEFAULT_RESPONSIBLE = ["tunduk_dev", "tunduk_analyst"]
ADMIN_IDS = [549890508]  # ваш Telegram ID
BOT_USERNAME = "Jardam4y_bot"  # username вашего бота (без @)
RESPONSIBLE_USER = "@Bermet_Kadyrbekova"  # кому отправлять уведомления

MORNING_TIME_UTC = "03:00"  # 09:00 по Бишкеку (UTC+6)
TIMEZONE_OFFSET = 6

PRIORITY_REMINDER_MINUTES = {
    "high": 5,
    "medium": 15,
    "low": 30
}

# ---------- РАЗНООБРАЗНЫЕ СОВЕТЫ ----------
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

def get_random_advice():
    return random.choice(ADVICE_LIST)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- НАСТРОЙКА ИИ ----------
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
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER,
        chat_id INTEGER,
        author_id INTEGER,
        author_name TEXT,
        username TEXT,
        text TEXT,
        type TEXT,
        status TEXT DEFAULT 'open',
        priority TEXT DEFAULT 'low',
        tags TEXT,
        responsible TEXT,
        created_at TIMESTAMP,
        reminder_sent INTEGER DEFAULT 0,
        closed_by INTEGER,
        closed_at TIMESTAMP,
        file_id TEXT,
        file_url TEXT,
        title TEXT
    )''')
    c.execute("PRAGMA table_info(issues)")
    columns = [col[1] for col in c.fetchall()]
    for col in ["priority", "tags", "username", "responsible", "closed_by", "closed_at", "file_id", "file_url", "title"]:
        if col not in columns:
            c.execute(f"ALTER TABLE issues ADD COLUMN {col} TEXT")
    c.execute('''CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id INTEGER,
        user_id INTEGER,
        user_name TEXT,
        text TEXT,
        created_at TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS rating (
        user_id INTEGER PRIMARY KEY,
        points INTEGER DEFAULT 0,
        last_updated TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id INTEGER,
        user_id INTEGER,
        action TEXT,
        old_value TEXT,
        new_value TEXT,
        created_at TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS banned_users (
        user_id INTEGER PRIMARY KEY,
        reason TEXT,
        banned_at TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS responsible_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS keywords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT UNIQUE
    )''')
    # Добавляем начальные данные
    for username in DEFAULT_RESPONSIBLE:
        c.execute("INSERT OR IGNORE INTO responsible_users (username) VALUES (?)", (username,))
    initial_keywords = [
        "система не работает", "sanarip не работает", "санарип не работет",
        "база зависает", "база катып жатат", "база жай иштеп жатат", "база катып калды",
        "база иштебей калды", "система медленно работает", "не работает",
        "ошибка", "баг", "глюк", "завис", "не открывается", "не грузит",
        "проблема", "система токтом калды", "система жай иштейт",
        "санарип иштебей калды", "санарип жай иштеп калды", "санарип жай иштейт"
    ]
    for kw in initial_keywords:
        c.execute("INSERT OR IGNORE INTO keywords (word) VALUES (?)", (kw,))
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_close_days', '14')")
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

# ---------- ЗАГРУЗКА КЛЮЧЕВЫХ СЛОВ ----------
def load_keywords():
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT word FROM keywords")
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

KEYWORDS = load_keywords()

def check_keywords(text: str) -> bool:
    lower = text.lower()
    for kw in KEYWORDS:
        if kw in lower:
            return True
    return False

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def get_responsible_list():
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT username FROM responsible_users ORDER BY username")
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def add_responsible(username):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO responsible_users (username) VALUES (?)", (username,))
    conn.commit()
    conn.close()

def remove_responsible(username):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("DELETE FROM responsible_users WHERE username=?", (username,))
    conn.commit()
    conn.close()

def add_keyword(word):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO keywords (word) VALUES (?)", (word,))
    conn.commit()
    conn.close()
    global KEYWORDS
    KEYWORDS = load_keywords()

def remove_keyword(word):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("DELETE FROM keywords WHERE word=?", (word,))
    conn.commit()
    conn.close()
    global KEYWORDS
    KEYWORDS = load_keywords()

def list_keywords():
    return load_keywords()

def get_issue_by_id(issue_id):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT id, status, text, author_name, responsible, priority FROM issues WHERE id=?", (issue_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_issue_by_reply(reply_to_message_id):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT id, status FROM issues WHERE message_id=?", (reply_to_message_id,))
    row = c.fetchone()
    conn.close()
    return row

def add_issue(message, issue_type, tags, responsible, priority, file_id="", file_url="", title=""):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute('''INSERT INTO issues 
        (message_id, chat_id, author_id, author_name, username, text, type, tags, responsible, priority, created_at, file_id, file_url, title)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (message.message_id, message.chat_id, message.from_user.id,
         message.from_user.full_name, message.from_user.username or "",
         message.text, issue_type, tags, responsible, priority, datetime.now(),
         file_id, file_url, title))
    issue_id = c.lastrowid
    conn.commit()
    conn.close()
    add_audit_log(issue_id, message.from_user.id, "create", "", f"type={issue_type}, priority={priority}")
    return issue_id

def update_priority(issue_id, priority, user_id):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT priority FROM issues WHERE id=?", (issue_id,))
    old = c.fetchone()
    c.execute("UPDATE issues SET priority=? WHERE id=?", (priority, issue_id))
    conn.commit()
    conn.close()
    if old and old[0] != priority:
        add_audit_log(issue_id, user_id, "change_priority", old[0], priority)

def add_audit_log(issue_id, user_id, action, old_val="", new_val=""):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("INSERT INTO audit_log (issue_id, user_id, action, old_value, new_value, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (issue_id, user_id, action, old_val, new_val, datetime.now()))
    conn.commit()
    conn.close()

def add_points(user_id, points):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("INSERT INTO rating (user_id, points, last_updated) VALUES (?, ?, ?) "
              "ON CONFLICT(user_id) DO UPDATE SET points = points + ?, last_updated = ?",
              (user_id, points, datetime.now(), points, datetime.now()))
    conn.commit()
    conn.close()

def get_points(user_id):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT points FROM rating WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def get_top_users(limit=10):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT user_id, points FROM rating ORDER BY points DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def is_banned(user_id):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM banned_users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def ban_user(user_id, reason=""):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO banned_users (user_id, reason, banned_at) VALUES (?, ?, ?)",
              (user_id, reason, datetime.now()))
    conn.commit()
    conn.close()

def unban_user(user_id):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def extract_tags(text):
    return re.findall(r'#\w+', text)

def extract_mentions(text):
    return re.findall(r'@(\w+)', text)

def detect_priority(text):
    text_lower = text.lower()
    if re.search(r'критичн|срочн|high|critical', text_lower):
        return 'high'
    elif re.search(r'важн|medium|normal', text_lower):
        return 'medium'
    else:
        return 'low'

def is_issue_resolved(issue_id):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT status FROM issues WHERE id=?", (issue_id,))
    row = c.fetchone()
    conn.close()
    return row and row[0] == 'closed'

def mark_reminder_sent(issue_id):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("UPDATE issues SET reminder_sent=1 WHERE id=?", (issue_id,))
    conn.commit()
    conn.close()

def close_issue(issue_id, closer_id=None):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT status FROM issues WHERE id=?", (issue_id,))
    old = c.fetchone()
    if old and old[0] == 'closed':
        conn.close()
        return
    if closer_id:
        c.execute("UPDATE issues SET status='closed', closed_by=?, closed_at=? WHERE id=?", (closer_id, datetime.now(), issue_id))
    else:
        c.execute("UPDATE issues SET status='closed', closed_at=? WHERE id=?", (datetime.now(), issue_id))
    conn.commit()
    conn.close()
    add_audit_log(issue_id, closer_id or 0, "close", old[0] if old else "", "closed")
    if closer_id:
        add_points(closer_id, 2)

def reopen_issue(issue_id, user_id):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("UPDATE issues SET status='open', reminder_sent=0 WHERE id=?", (issue_id,))
    conn.commit()
    conn.close()

def add_comment(issue_id, user_id, user_name, text):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute('''INSERT INTO comments (issue_id, user_id, user_name, text, created_at)
                 VALUES (?, ?, ?, ?, ?)''',
              (issue_id, user_id, user_name, text, datetime.now()))
    conn.commit()
    conn.close()
    add_audit_log(issue_id, user_id, "comment", "", text)

async def generate_title(text: str) -> str:
    if ai_client is None:
        return text[:50] + "..."
    try:
        response = ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Сформулируй краткий заголовок для задачи (максимум 10 слов) на основе текста. Ответь только заголовком, без пояснений."},
                {"role": "user", "content": text}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return text[:50] + "..."

async def find_similar_issues(text: str, limit=3):
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT id, text, author_name FROM issues WHERE status='open' ORDER BY created_at DESC LIMIT 20")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return []
    words = set(re.findall(r'\w+', text.lower()))
    scores = []
    for row in rows:
        issue_id, issue_text, author = row
        common = len(words & set(re.findall(r'\w+', issue_text.lower())))
        if common > 1:
            scores.append((common, issue_id, issue_text[:50], author))
    scores.sort(reverse=True)
    return scores[:limit]

async def auto_tagging(text: str):
    if ai_client is None:
        return []
    try:
        response = ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Ты — система автоматического тегирования. Проанализируй текст и предложи 2-5 хэштегов (начинающихся с #) через запятую. Ответь только тегами, без пояснений."},
                {"role": "user", "content": text}
            ]
        )
        tags = response.choices[0].message.content.strip()
        return re.findall(r'#\w+', tags)
    except Exception:
        return []

async def smart_priority(text: str):
    if ai_client is None:
        return detect_priority(text)
    try:
        response = ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Определи приоритет задачи: high (критично), medium (важно), low (обычное). Ответь только одним словом."},
                {"role": "user", "content": text}
            ]
        )
        p = response.choices[0].message.content.strip().lower()
        if p in ['high', 'medium', 'low']:
            return p
        return detect_priority(text)
    except Exception:
        return detect_priority(text)

async def analyze_with_ai(text: str):
    if ai_client is None:
        return {"type": "other", "reason": "AI недоступен"}
    try:
        response = ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": """
                Ты — классификатор сообщений для IT-команды.
                Проанализируй сообщение и определи его тип:
                - "bug": если пользователь сообщает об ошибке, проблеме или неработающей функции.
                - "suggestion": если пользователь вносит предложение по улучшению, новую идею.
                - "other": если сообщение не относится к первым двум категориям.
                Ответь ТОЛЬКО в формате JSON: {"type": "тип", "reason": "краткая причина"}.
                """},
                {"role": "user", "content": text}
            ],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"AI ошибка: {e}")
        return {"type": "other", "reason": "Ошибка анализа"}

async def answer_with_ai(question: str) -> str:
    if ai_client is None:
        return "❌ AI не настроен."
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
        return "❌ Ошибка при обработке вопроса."

# ---------- СОЗДАНИЕ ЗАДАЧИ ----------
async def create_issue_from_message(msg, issue_type, context, responsible=None):
    text = msg.text
    title = await generate_title(text)
    tags_list = extract_tags(text)
    tags_str = ",".join(tags_list) if tags_list else ""

    if ai_client and not tags_list:
        ai_tags = await auto_tagging(text)
        if ai_tags:
            tags_str = ",".join(ai_tags)
            tags_list = ai_tags

    priority = await smart_priority(text)

    if responsible is None:
        mentions = extract_mentions(text)
        if mentions:
            responsible = mentions
        else:
            responsible = DEFAULT_RESPONSIBLE

    responsible_str = ",".join(responsible)
    file_id = ""
    file_url = ""
    if msg.document:
        file_id = msg.document.file_id
        file_url = msg.document.file_name or "документ"
    elif msg.photo:
        file_id = msg.photo[-1].file_id
        file_url = "фото"

    issue_id = add_issue(msg, issue_type, tags_str, responsible_str, priority, file_id, file_url, title)

    responsible_mentions = " ".join([f"@{u}" for u in responsible])
    tag_text = f"🏷️ Теги: {', '.join(tags_list)}" if tags_list else ""
    priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(priority, "")
    minutes = PRIORITY_REMINDER_MINUTES[priority]
    file_text = f"📎 Вложение: {file_url}" if file_url else ""

    similar = await find_similar_issues(text, limit=3)
    similar_text = ""
    if similar:
        similar_text = "\n⚠️ Похожие открытые задачи:\n"
        for _, sid, stext, sauthor in similar:
            similar_text += f"  #{sid} ({sauthor}): {stext}...\n"

    await msg.reply_text(
        f"🔔 Зарегистрирован {issue_type} #{issue_id}\n"
        f"Заголовок: {title}\n"
        f"Автор: {msg.from_user.full_name}\n"
        f"Текст: {text[:100]}...\n"
        f"{tag_text}\n"
        f"Приоритет: {priority_emoji} {priority}\n"
        f"Ответственные: {responsible_mentions}\n"
        f"⏳ Напоминание через {minutes} мин.\n"
        f"{file_text}\n"
        f"{similar_text}"
    )

    if priority == "high":
        for user in responsible:
            try:
                await context.bot.send_message(
                    chat_id=user,
                    text=f"🚨 Критичная задача #{issue_id}!\n{title}\n{text[:200]}...\nОтветственные: {responsible_mentions}"
                )
            except Exception:
                pass

    # Кнопки для изменения приоритета
    priority_keyboard = [
        [
            InlineKeyboardButton("🔴 Критичный", callback_data=f"set_priority_{issue_id}_high"),
            InlineKeyboardButton("🟡 Важный", callback_data=f"set_priority_{issue_id}_medium"),
            InlineKeyboardButton("🟢 Обычный", callback_data=f"set_priority_{issue_id}_low")
        ],
        [InlineKeyboardButton("⏩ Пропустить", callback_data=f"skip_priority_{issue_id}")]
    ]
    await msg.reply_text(
        "Выберите приоритет задачи или нажмите 'Пропустить':",
        reply_markup=InlineKeyboardMarkup(priority_keyboard)
    )

    if context.job_queue:
        delay = minutes * 60
        job = context.job_queue.run_once(send_reminder, when=delay, data={"issue_id": issue_id, "chat_id": msg.chat_id})
        if context.bot_data.get('reminder_jobs') is None:
            context.bot_data['reminder_jobs'] = {}
        context.bot_data['reminder_jobs'][issue_id] = job

    add_points(msg.from_user.id, 1)
    return issue_id

# ---------- ОБРАБОТЧИКИ КНОПОК ----------
async def priority_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    parts = data.split('_')
    if len(parts) < 3:
        await query.edit_message_text("❌ Неверный формат.")
        return
    action = parts[1]
    issue_id = int(parts[2])
    issue = get_issue_by_id(issue_id)
    if not issue:
        await query.edit_message_text("❌ Задача не найдена.")
        return
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT author_id FROM issues WHERE id=?", (issue_id,))
    row = c.fetchone()
    conn.close()
    author_id = row[0] if row else None
    if user_id != author_id and user_id not in ADMIN_IDS:
        await query.edit_message_text("⛔ Нет прав.")
        return
    if action == 'priority':
        new_priority = parts[3]
        update_priority(issue_id, new_priority, user_id)
        emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(new_priority, "")
        await query.edit_message_text(f"✅ Приоритет задачи #{issue_id} изменён на {emoji} {new_priority}.")
        if context.job_queue:
            jobs = context.bot_data.get('reminder_jobs', {})
            old = jobs.get(issue_id)
            if old:
                old.schedule_removal()
            minutes = PRIORITY_REMINDER_MINUTES[new_priority]
            new_job = context.job_queue.run_once(send_reminder, when=minutes*60, data={"issue_id": issue_id, "chat_id": GROUP_CHAT_ID})
            jobs[issue_id] = new_job
    elif action == 'skip':
        await query.edit_message_text(f"⏩ Приоритет задачи #{issue_id} оставлен как авто.")

async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if 'pending_issue' not in context.user_data:
        await query.edit_message_text("❌ Запрос устарел.")
        return
    pending = context.user_data['pending_issue']
    if query.from_user.id != pending['message'].from_user.id:
        await query.edit_message_text("⛔ Только автор может подтвердить.")
        return
    if query.data == "confirm_yes":
        await show_responsible_selection(context, query, pending, pending['issue_type'])
    else:
        await query.edit_message_text("❌ Создание отменено.")
        del context.user_data['pending_issue']

async def show_responsible_selection(context, query, pending, issue_type):
    responsible_list = get_responsible_list()
    if not responsible_list:
        responsible_list = DEFAULT_RESPONSIBLE
        for username in responsible_list:
            add_responsible(username)
    buttons = []
    row = []
    for i, username in enumerate(responsible_list):
        row.append(InlineKeyboardButton(f"@{username}", callback_data=f"resp_{username}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✏️ Другой (ввести @username)", callback_data="resp_other")])
    buttons.append([InlineKeyboardButton("⏩ Пропустить", callback_data="resp_skip")])
    context.user_data['pending_responsible'] = {'pending': pending, 'issue_type': issue_type, 'query': query}
    await query.edit_message_text(
        "Выберите ответственного за задачу:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def responsible_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if 'pending_responsible' not in context.user_data:
        await query.edit_message_text("❌ Сессия истекла.")
        return
    pending_data = context.user_data['pending_responsible']
    pending = pending_data['pending']
    issue_type = pending_data['issue_type']
    if pending['message'].from_user.id != user_id:
        await query.edit_message_text("⛔ Только автор может выбирать.")
        return
    if data == "resp_skip":
        msg = pending['message']
        text = msg.text
        mentions = extract_mentions(text)
        if mentions:
            responsible = mentions
        else:
            responsible_list = get_responsible_list()
            responsible = [responsible_list[0]] if responsible_list else DEFAULT_RESPONSIBLE
        await query.edit_message_text("✅ Создаю задачу...")
        await create_issue_from_message(msg, issue_type, context, responsible)
        del context.user_data['pending_responsible']
        del context.user_data['pending_issue']
        return
    elif data == "resp_other":
        await query.edit_message_text("✏️ Напишите в ответ на это сообщение @username ответственного.")
        context.user_data['awaiting_responsible'] = True
        context.user_data['pending_for_responsible'] = {'pending': pending, 'issue_type': issue_type, 'query': query}
        return
    elif data.startswith("resp_"):
        username = data.split("_")[1]
        responsible = [username]
        msg = pending['message']
        await query.edit_message_text(f"✅ Выбран @{username}. Создаю задачу...")
        await create_issue_from_message(msg, issue_type, context, responsible)
        del context.user_data['pending_responsible']
        del context.user_data['pending_issue']
        return

async def handle_manual_responsible(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_responsible'):
        return
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    mentions = extract_mentions(text)
    if not mentions:
        await msg.reply_text("⚠️ Не найдено @username. Попробуйте ещё раз.")
        return
    data = context.user_data.get('pending_for_responsible')
    if not data:
        return
    pending = data['pending']
    issue_type = data['issue_type']
    await msg.reply_text(f"✅ Выбраны: {' '.join([f'@{u}' for u in mentions])}. Создаю задачу...")
    await create_issue_from_message(pending['message'], issue_type, context, mentions)
    del context.user_data['awaiting_responsible']
    del context.user_data['pending_for_responsible']
    del context.user_data['pending_issue']

async def advice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "advice_helped":
        await query.edit_message_text("✅ Отлично! Рады, что помогли. Если будут ещё вопросы — обращайтесь.")
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

# ---------- КОМАНДЫ ДЛЯ ВСЕХ ----------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Бот для поддержки пользователей системы.\n\n"
        "Команды:\n"
        "/help – показать это сообщение\n"
        "/ask <вопрос> – задать вопрос ИИ\n"
        "/rating – ваш рейтинг\n"
        "/top – топ-10 пользователей по рейтингу\n"
        "/list_responsible – список ответственных\n"
        "/list_keywords – список ключевых слов\n\n"
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
    await update.message.reply_text("🤔 Думаю...")
    answer = await answer_with_ai(question)
    cleaned = re.sub(r'\*\*(.*?)\*\*', r'\1', answer)
    cleaned = re.sub(r'\*(.*?)\*', r'\1', cleaned)
    cleaned = re.sub(r'_(.*?)_', r'\1', cleaned)
    cleaned = re.sub(r'#{1,6}\s?', '', cleaned)
    cleaned = re.sub(r'`(.*?)`', r'\1', cleaned)
    cleaned = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', cleaned)
    cleaned = cleaned.replace('*', '')
    await update.message.reply_text(cleaned)

async def rating_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "без юзернейма"
    points = get_points(user_id)
    await update.message.reply_text(f"📊 Ваш рейтинг:\nПользователь: @{username}\nОчки: {points}")

async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_users = get_top_users(10)
    if not top_users:
        await update.message.reply_text("Пока нет рейтинга.")
        return
    response = "🏆 Топ-10 пользователей:\n"
    for i, (uid, pts) in enumerate(top_users, 1):
        conn = sqlite3.connect("issues.db")
        c = conn.cursor()
        c.execute("SELECT author_name FROM issues WHERE author_id=? LIMIT 1", (uid,))
        row = c.fetchone()
        conn.close()
        name = row[0] if row else f"user_{uid}"
        response += f"{i}. {name} – {pts} очков\n"
    await update.message.reply_text(response)

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    issue_id = data["issue_id"]
    chat_id = data["chat_id"]
    if is_issue_resolved(issue_id):
        return
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT reminder_sent, responsible FROM issues WHERE id=?", (issue_id,))
    row = c.fetchone()
    if row and row[0] == 1:
        conn.close()
        return
    conn.close()
    mark_reminder_sent(issue_id)
    resp = row[1] if row and row[1] else ""
    mentions = ""
    if resp:
        usernames = [u.strip() for u in resp.split(',') if u.strip()]
        if usernames:
            mentions = " " + " ".join([f"@{u}" for u in usernames])
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⚠️ Напоминание! Задача #{issue_id} без ответа.{mentions}\nПросьба ответить."
    )

# ---------- УТРЕННЕЕ ПРИВЕТСТВИЕ ----------
async def morning_greeting(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text="🌞 Доброе утро, коллеги! Желаем продуктивного дня!"
    )
    conn = sqlite3.connect("issues.db")
    c = conn.cursor()
    c.execute("SELECT id, author_name, text, type, priority, created_at FROM issues WHERE status='open' ORDER BY created_at DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if rows:
        response = "📋 Открытые задачи:\n"
        for row in rows:
            issue_id, author, text, issue_type, priority, created = row
            emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(priority, "")
            created_str = created[:16] if isinstance(created, str) else created.strftime("%Y-%m-%d %H:%M")
            response += f"#{issue_id} {issue_type} {emoji} от {author} ({created_str}): {text[:50]}...\n"
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=response)
    else:
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text="🎉 Нет открытых задач!")

# ---------- АДМИН-КОМАНДЫ ----------
async def add_responsible_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("Укажите юзернейм: /add_responsible @username")
        return
    username = context.args[0].lstrip('@')
    add_responsible(username)
    await update.message.reply_text(f"✅ @{username} добавлен в список ответственных.")

async def remove_responsible_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("Укажите юзернейм: /remove_responsible @username")
        return
    username = context.args[0].lstrip('@')
    remove_responsible(username)
    await update.message.reply_text(f"✅ @{username} удалён из списка ответственных.")

async def list_responsible_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    resp_list = get_responsible_list()
    if not resp_list:
        await update.message.reply_text("Список ответственных пуст.")
        return
    response = "📋 Список ответственных:\n" + "\n".join([f"@{u}" for u in resp_list])
    await update.message.reply_text(response)

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
    await update.message.reply_text(f"✅ Ключевое слово «{keyword}» удалено.")

async def list_keywords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kw_list = list_keywords()
    if not kw_list:
        await update.message.reply_text("Список ключевых слов пуст.")
        return
    response = "📋 Ключевые слова:\n" + "\n".join([f"• {kw}" for kw in kw_list])
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
    conn = sqlite3.connect("issues.db")
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

# ---------- ОСНОВНОЙ ОБРАБОТЧИК ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text
    logger.info(f"Получено сообщение: {text} от {msg.from_user.username} (chat_id: {msg.chat_id})")

    if is_banned(msg.from_user.id):
        await msg.reply_text("⛔ Вы забанены и не можете создавать задачи.")
        return

    if msg.reply_to_message:
        issue = get_issue_by_reply(msg.reply_to_message.message_id)
        if issue and issue[1] == 'open':
            close_issue(issue[0], msg.from_user.id)
            await msg.reply_text(f"✅ Задача #{issue[0]} закрыта.")
        return

    if text.startswith('/'):
        return

    if f"@{BOT_USERNAME}" in text or is_greeting_or_question(text):
        await msg.reply_text(
            "👋 Привет! Я бот для регистрации багов и предложений.\n"
            "Используйте /help для списка команд."
        )
        return

    # Проверка ключевых слов
    if check_keywords(text):
        logger.info("Распознано по ключевым словам")
        issue_type = "bug"
        # Берём случайный совет
        advice = get_random_advice()
        title = await generate_title(text)
        tags_list = extract_tags(text)
        tags_str = ",".join(tags_list) if tags_list else ""
        mentions = extract_mentions(text)
        if mentions:
            responsible = mentions
        else:
            responsible = DEFAULT_RESPONSIBLE
        responsible_str = ",".join(responsible)
        priority = detect_priority(text)
        file_id = ""
        file_url = ""
        if msg.document:
            file_id = msg.document.file_id
            file_url = msg.document.file_name or "документ"
        elif msg.photo:
            file_id = msg.photo[-1].file_id
            file_url = "фото"
        issue_id = add_issue(msg, issue_type, tags_str, responsible_str, priority, file_id, file_url, title)
        responsible_mentions = " ".join([f"@{u}" for u in responsible])
        tag_text = f"🏷️ Теги: {', '.join(tags_list)}" if tags_list else ""
        priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(priority, "")
        minutes = PRIORITY_REMINDER_MINUTES[priority]
        file_text = f"📎 Вложение: {file_url}" if file_url else ""
        keyboard = [
            [
                InlineKeyboardButton("✅ Помогло", callback_data="advice_helped"),
                InlineKeyboardButton("❌ Не помогло", callback_data="advice_not_helped")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await msg.reply_text(
            f"🔔 Зарегистрирован {issue_type} #{issue_id}\n"
            f"Заголовок: {title}\n"
            f"Автор: {msg.from_user.full_name}\n"
            f"Текст: {text[:100]}...\n"
            f"{tag_text}\n"
            f"Приоритет: {priority_emoji} {priority}\n"
            f"Ответственные: {responsible_mentions}\n"
            f"⏳ Напоминание через {minutes} мин.\n"
            f"{file_text}\n"
            f"🧠 Совет по решению:\n{advice}\n\n"
            "Если совет помог, нажмите «Помогло». Если нет — мы отправим запрос аналитику.",
            reply_markup=reply_markup
        )
        if context.job_queue:
            delay = minutes * 60
            job = context.job_queue.run_once(send_reminder, when=delay, data={"issue_id": issue_id, "chat_id": msg.chat_id})
            if context.bot_data.get('reminder_jobs') is None:
                context.bot_data['reminder_jobs'] = {}
            context.bot_data['reminder_jobs'][issue_id] = job
        add_points(msg.from_user.id, 1)
        return

    # AI-анализ
    if ai_client is not None:
        analysis = await analyze_with_ai(text)
        issue_type = analysis.get("type") if analysis else None
        if issue_type in ["bug", "suggestion"]:
            logger.info(f"AI определил как {issue_type}: {analysis.get('reason', '')}")
            neg_phrases = ["не баг", "не ошибка", "это не баг", "не предложение", "не улучшение"]
            if any(phrase in text.lower() for phrase in neg_phrases):
                return
            context.user_data['pending_issue'] = {'message': msg, 'text': text, 'issue_type': issue_type}
            keyboard = [[
                InlineKeyboardButton("✅ Да, создать", callback_data="confirm_yes"),
                InlineKeyboardButton("❌ Нет, отменить", callback_data="confirm_no")
            ]]
            await msg.reply_text(
                f"Я определил это как '{issue_type}'. Создать задачу?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

    # Старая логика (ключевые слова)
    bug_pattern = r'\b(баг|ошибка|bug|не работает|глюк)\b'
    suggest_pattern = r'\b(предложени|улучшени|suggest|идея|хотелось бы)\b'
    issue_type = None
    if re.search(bug_pattern, text, re.IGNORECASE):
        issue_type = "bug"
    elif re.search(suggest_pattern, text, re.IGNORECASE):
        issue_type = "suggestion"
    else:
        return

    neg_phrases = ["не баг", "не ошибка", "это не баг", "не предложение", "не улучшение", "поясню", "объясню", "просто хочу сказать", "к слову", "кстати"]
    if any(phrase in text.lower() for phrase in neg_phrases):
        return

    context.user_data['pending_issue'] = {'message': msg, 'text': text, 'issue_type': issue_type}
    keyboard = [[
        InlineKeyboardButton("✅ Да, создать", callback_data="confirm_yes"),
        InlineKeyboardButton("❌ Нет, отменить", callback_data="confirm_no")
    ]]
    await msg.reply_text(
        f"Вы упомянули '{issue_type}'. Создать задачу?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def is_greeting_or_question(text):
    text_lower = text.lower()
    patterns = [r'\bпривет\b', r'\bздравствуй[те]?\b', r'\bсалам\b', r'\bhello\b', r'\bhi\b',
                r'кто ты', r'откуда', r'зачем', r'для чего', r'что ты умеешь', r'расскажи о себе']
    for p in patterns:
        if re.search(p, text_lower):
            return True
    return False

# ---------- ЗАПУСК ----------
def main():
    init_db()
    global KEYWORDS
    KEYWORDS = load_keywords()
    logger.info(f"Загружено {len(KEYWORDS)} ключевых слов")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(priority_callback, pattern=r"^(set_priority_\d+_(high|medium|low)|skip_priority_\d+)$"))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern="^(confirm_yes|confirm_no)$"))
    app.add_handler(CallbackQueryHandler(responsible_callback, pattern=r"^(resp_.+|resp_skip|resp_other)$"))
    app.add_handler(CallbackQueryHandler(advice_callback, pattern="^(advice_helped|advice_not_helped)$"))

    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("ask", ask_command))
    app.add_handler(CommandHandler("rating", rating_command))
    app.add_handler(CommandHandler("top", top_command))

    app.add_handler(CommandHandler("add_responsible", add_responsible_command))
    app.add_handler(CommandHandler("remove_responsible", remove_responsible_command))
    app.add_handler(CommandHandler("list_responsible", list_responsible_command))
    app.add_handler(CommandHandler("add_keyword", add_keyword_command))
    app.add_handler(CommandHandler("remove_keyword", remove_keyword_command))
    app.add_handler(CommandHandler("list_keywords", list_keywords_command))

    app.add_handler(CommandHandler("ban_user", ban_user_command))
    app.add_handler(CommandHandler("unban_user", unban_user_command))
    app.add_handler(CommandHandler("list_banned", list_banned_command))

    # Утреннее приветствие
    if app.job_queue:
        try:
            morning_time = datetime.strptime(MORNING_TIME_UTC, "%H:%M").time()
            app.job_queue.run_daily(morning_greeting, time=morning_time, days=tuple(range(7)))
            logger.info(f"Утреннее приветствие запланировано на {MORNING_TIME_UTC} UTC")
        except Exception as e:
            logger.error(f"Ошибка планирования утреннего приветствия: {e}")

    logger.info("Второй бот (поддержка) с админ-функциями и разнообразными советами запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
