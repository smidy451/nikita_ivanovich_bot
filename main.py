import os
from pathlib import Path
import sys
import sqlite3
import shutil
import tempfile
import atexit
import hashlib
import secrets
import base64
import re
import html
import telebot

# Пути относительно каталога проекта, а не текущего рабочего каталога.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.getenv("API_TOKEN")
if not TOKEN:
    TOKEN = Path("token.txt").read_text().strip()
if not TOKEN:
    raise RuntimeError("Не задан токен бота")

bot = telebot.TeleBot(TOKEN)

# 2. Определение режима работы и путей
IS_DEBUG = "-debug" in sys.argv or "--debug" in sys.argv

if IS_DEBUG:
    TEST_DB_TEMPLATE = os.path.join(PROJECT_DIR, "test_data", "test_students.db")
    if not os.path.isfile(TEST_DB_TEMPLATE):
        print(f"Ошибка debug-режима: не найдена тестовая БД: {TEST_DB_TEMPLATE}")
        sys.exit(1)

    DEBUG_RUNTIME_DIR = tempfile.mkdtemp(prefix="nikita_ivanovich_debug_", dir="/tmp")
    DB_PATH = os.path.join(DEBUG_RUNTIME_DIR, "test_students.db")
    try:
        source_conn = sqlite3.connect(TEST_DB_TEMPLATE)
        runtime_conn = sqlite3.connect(DB_PATH)
        try:
            source_conn.backup(runtime_conn)
        finally:
            source_conn.close()
            runtime_conn.close()
        atexit.register(shutil.rmtree, DEBUG_RUNTIME_DIR, ignore_errors=True)
    except (OSError, sqlite3.Error) as e:
        shutil.rmtree(DEBUG_RUNTIME_DIR, ignore_errors=True)
        print(f"Не удалось подготовить изолированную тестовую БД в /tmp: {e}")
        sys.exit(1)
else:
    DB_PATH = os.path.join(PROJECT_DIR, "students.db")

def get_db_connection():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS students (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            student_group TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS works (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            subject TEXT,
            work_name TEXT,
            file_path TEXT,
            status TEXT DEFAULT 'НЕПРОВЕРЕНО',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            work_code TEXT UNIQUE,
            FOREIGN KEY (user_id) REFERENCES students (user_id)
        )
    """)

    # Миграция существующей базы: добавляем код работы, если его ещё нет.
    cursor.execute("PRAGMA table_info(works)")
    columns = {row[1] for row in cursor.fetchall()}
    if "work_code" not in columns:
        cursor.execute("ALTER TABLE works ADD COLUMN work_code TEXT")
    if "file_id" not in columns:
        cursor.execute("ALTER TABLE works ADD COLUMN file_id TEXT")
    if "teacher_chat_id" not in columns:
        cursor.execute("ALTER TABLE works ADD COLUMN teacher_chat_id INTEGER")
    if "teacher_message_id" not in columns:
        cursor.execute("ALTER TABLE works ADD COLUMN teacher_message_id INTEGER")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_works_work_code ON works(work_code)")
    # Нормализуем русские статусы в Python: SQLite UPPER/NOCASE не полностью
    # обрабатывают кириллицу, поэтому фильтры должны работать с единым форматом.
    allowed_statuses = {
        "НЕПРОВЕРЕНО", "ПРОВЕРЕНО", "ОТЛИЧНО", "ХОРОШО",
        "УДОВЛЕТВОРИТЕЛЬНО", "ВОЗВРАЩЕНО",
    }
    cursor.execute("SELECT id, status FROM works")
    for work_id, raw_status in cursor.fetchall():
        normalized_status = (raw_status or "").strip().upper()
        if normalized_status in ("ОТПРАВЛЕНА НА ПРОВЕРКУ", "ОТПРАВЛЕНО НА ПРОВЕРКУ") or normalized_status not in allowed_statuses:
            normalized_status = "НЕПРОВЕРЕНО"
        if raw_status != normalized_status:
            cursor.execute("UPDATE works SET status = ? WHERE id = ?", (normalized_status, work_id))
    conn.commit()
    conn.close()

init_db()

VALID_STATUSES = {
    "ПРОВЕРЕНО", "ОТЛИЧНО", "ХОРОШО", "УДОВЛЕТВОРИТЕЛЬНО",
    "ВОЗВРАЩЕНО", "НЕПРОВЕРЕНО"
}


# Реестр группы преподавателя хранится в обычном TXT-файле рядом с main.py.
# Первая непустая строка файла должна содержать числовой ID группы.
TEACHER_GROUP_REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "group_id.txt"
)

def get_registered_teacher_group_id():
    """Читает ID единственной разрешённой группы из group_id.txt."""
    try:
        with open(TEACHER_GROUP_REGISTRY_PATH, "r", encoding="utf-8") as registry_file:
            for line in registry_file:
                value = line.strip()
                if value:
                    return int(value)
        print(f"Реестр {TEACHER_GROUP_REGISTRY_PATH} пуст.")
    except FileNotFoundError:
        print(f"Реестр группы не найден: {TEACHER_GROUP_REGISTRY_PATH}")
    except ValueError:
        print("Первая непустая строка group_id.txt должна быть числовым ID группы.")
    except OSError as e:
        print(f"Не удалось прочитать реестр группы: {e}")
    return None


def authorize_teacher_command(message):
    """Разрешает команды преподавателя только администратору зарегистрированной группы."""
    if message.chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Эта команда доступна только администраторам зарегистрированной группы преподавателя.")
        return False

    registered_chat_id = get_registered_teacher_group_id()
    if registered_chat_id is None:
        bot.reply_to(message, "Группа преподавателя ещё не зарегистрирована. Обратитесь к владельцу бота.")
        return False

    if message.chat.id != registered_chat_id:
        bot.reply_to(message, "Эта группа не зарегистрирована для управления ботом.")
        return False

    if not message.from_user:
        bot.reply_to(message, "Не удалось определить отправителя команды.")
        return False

    try:
        member = bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status not in ("administrator", "creator"):
            bot.reply_to(message, "Команда доступна только администраторам этой группы.")
            return False
    except Exception as e:
        print(f"Не удалось проверить права администратора группы: {e}")
        bot.reply_to(message, "Не удалось проверить права доступа. Убедитесь, что бот является администратором группы.")
        return False

    return True


# Короткие коды статусов для callback_data Telegram (лимит 64 байта).
STATUS_CALLBACK_CODES = {
    "ПРОВЕРЕНО": "P",
    "ОТЛИЧНО": "O",
    "ХОРОШО": "H",
    "УДОВЛЕТВОРИТЕЛЬНО": "U",
    "ВОЗВРАЩЕНО": "V",
}
CALLBACK_CODE_STATUSES = {value: key for key, value in STATUS_CALLBACK_CODES.items()}


def add_copy_id_button(keyboard, work_code):
    """Добавляет нативную кнопку копирования, если установленная библиотека её поддерживает."""
    copy_type = getattr(telebot.types, "CopyTextButton", None)
    if copy_type is None:
        # Код уже показан в <code>; не выдаём обычную кнопку за копирование в буфер.
        return
    try:
        keyboard.add(telebot.types.InlineKeyboardButton(
            text="📋 Копировать ID",
            copy_text=copy_type(text=work_code),
        ))
    except (TypeError, AttributeError):
        # У некоторых старых версий pyTelegramBotAPI нет поддержки copy_text.
        return


def build_work_keyboard(work_code):
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        telebot.types.InlineKeyboardButton(
            text=status,
            callback_data=f"ws|{work_code}|{STATUS_CALLBACK_CODES[status]}"
        )
        for status in ("ПРОВЕРЕНО", "ОТЛИЧНО", "ХОРОШО", "УДОВЛЕТВОРИТЕЛЬНО", "ВОЗВРАЩЕНО")
    ]
    keyboard.add(*buttons)
    add_copy_id_button(keyboard, work_code)
    return keyboard


def build_student_work_keyboard(work_code):
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        telebot.types.InlineKeyboardButton("🔄 Статус", callback_data=f"sw|{work_code}|status")
    )
    add_copy_id_button(keyboard, work_code)
    return keyboard


def build_main_menu():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.row("📤 Сдать работу", "📚 Мои работы")
    keyboard.row("👤 Мой профиль", "✏️ Изменить профиль")
    keyboard.row("❌ Отмена")
    return keyboard


def build_choice_keyboard(choices):
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2, one_time_keyboard=True)
    for i in range(0, len(choices), 2):
        keyboard.row(*choices[i:i + 2])
    keyboard.row("✍️ Ввести вручную", "❌ Отмена")
    return keyboard


def is_private_chat(message):
    if message.chat.type != "private":
        bot.reply_to(message, "Студенческие действия выполняются в личном чате с ботом. Откройте бота и нажмите /start.")
        return False
    return True


def normalize_text(value):
    return " ".join((value or "").split())


def valid_short_text(value, max_length=120):
    return bool(value) and len(value) <= max_length and not value.startswith("/")


def recent_values(user_id, column, subject=None, limit=6):
    """Возвращает недавние уникальные значения только из собственных работ студента."""
    if column not in ("subject", "work_name"):
        return []
    conn = get_db_connection()
    cursor = conn.cursor()
    if column == "subject":
        cursor.execute(
            "SELECT subject FROM works WHERE user_id = ? AND COALESCE(subject, '') != '' ORDER BY timestamp DESC, id DESC",
            (user_id,),
        )
    else:
        cursor.execute(
            "SELECT work_name FROM works WHERE user_id = ? AND LOWER(subject) = LOWER(?) "
            "AND COALESCE(work_name, '') != '' ORDER BY timestamp DESC, id DESC",
            (user_id, subject or ""),
        )
    found, values = set(), []
    for (value,) in cursor.fetchall():
        value = normalize_text(value)
        key = value.casefold()
        if value and key not in found:
            found.add(key)
            values.append(value)
        if len(values) >= limit:
            break
    conn.close()
    return values


def build_work_caption(row):
    # row соответствует get_work_by_code(): id, code, user_id, subject, name,
    # file_id, status, timestamp, full_name, student_group.
    _, work_code, _, subject, work_name, _, status, timestamp, full_name, student_group = row
    return (
        f"<b>Новая работа</b>\n"
        f"Студент: {html.escape(full_name or '—')} ({html.escape(student_group or '—')})\n"
        f"Дисциплина: {html.escape(subject or '—')}\n"
        f"Работа: {html.escape(work_name or 'Без названия')}\n"
        f"Дата сдачи: {html.escape(str(timestamp or '—'))}\n"
        f"Статус: <b>{html.escape(status or 'НЕПРОВЕРЕНО')}</b>\n"
        f"ID: <code>{html.escape(work_code or '—')}</code>"
    )


def notify_teacher_group(work_code):
    """Публикует принятую работу с файлом в ленту преподавателя; возвращает факт доставки."""
    teacher_chat_id = get_registered_teacher_group_id()
    if teacher_chat_id is None:
        print("Не отправлена работа преподавателю: не задан ID в group_id.txt")
        return False

    conn = get_db_connection()
    cursor = conn.cursor()
    row = get_work_by_code(cursor, work_code)
    conn.close()
    if not row:
        print(f"Не удалось найти работу {work_code} для публикации в группе преподавателя")
        return False

    _, _, _, _, _, file_id, _, _, _, _ = row
    if not file_id:
        print(f"У работы {work_code} отсутствует Telegram file_id")
        return False

    try:
        sent_message = bot.send_document(
            teacher_chat_id,
            file_id,
            caption=build_work_caption(row),
            parse_mode="HTML",
            reply_markup=build_work_keyboard(work_code),
            )
        # Сначала сохраняем результат доставки. Если не удалось сохранить ссылку на сообщение,
        # сам факт отправки остаётся успешным и пользователь не будет введён в заблуждение.
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE works SET teacher_chat_id = ?, teacher_message_id = ? WHERE UPPER(work_code) = UPPER(?)",
                (teacher_chat_id, sent_message.message_id, work_code),
            )
            conn.commit()
        except Exception as metadata_error:
            print(f"Работа {work_code} отправлена, но не удалось сохранить ссылку на сообщение: {metadata_error}")
        finally:
            if conn is not None:
                conn.close()
        return True
    except Exception as e:
        # Сохранённую работу не удаляем: сбой уведомления не должен приводить к повторной сдаче.
        print(f"Не удалось отправить работу {work_code} в группу преподавателя: {e}")
        return False





def make_work_code(subject, student_group, full_name, work_name):
    """Создаёт короткий человекочитаемый код на основе метаданных и случайной соли."""
    metadata = "|".join((subject.casefold(), student_group.casefold(), full_name.casefold(), work_name.casefold()))
    # Соль обеспечивает разные коды даже для повторно сданных одинаковых работ.
    digest = hashlib.sha256((metadata + "|" + secrets.token_hex(8)).encode("utf-8")).digest()
    token = base64.b32encode(digest).decode("ascii").rstrip("=")[:8]
    return f"WRK-{token[:4]}-{token[4:8]}"


def create_unique_work_code(cursor, subject, student_group, full_name, work_name):
    for _ in range(20):
        code = make_work_code(subject, student_group, full_name, work_name)
        cursor.execute("SELECT 1 FROM works WHERE work_code = ?", (code,))
        if cursor.fetchone() is None:
            return code
    raise RuntimeError("Не удалось создать уникальный код работы")


def get_work_by_code(cursor, work_code):
    cursor.execute("""
        SELECT w.id, w.work_code, w.user_id, w.subject, w.work_name, w.file_id,
            w.status, w.timestamp, s.full_name, s.student_group
        FROM works w LEFT JOIN students s ON s.user_id = w.user_id
        WHERE UPPER(w.work_code) = UPPER(?)
    """, (work_code,))
    return cursor.fetchone()


# Назначаем коды ранее сохранённым работам, созданным старой версией бота.
def backfill_work_codes():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT w.id, w.subject, w.work_name, s.full_name, s.student_group
        FROM works w LEFT JOIN students s ON s.user_id = w.user_id
        WHERE w.work_code IS NULL OR w.work_code = ''
    """)
    old_works = cursor.fetchall()
    for row_id, subject, work_name, full_name, student_group in old_works:
        code = create_unique_work_code(cursor, subject or "", student_group or "", full_name or "", work_name or "")
        cursor.execute("UPDATE works SET work_code = ? WHERE id = ?", (code, row_id))
    conn.commit()
    conn.close()


backfill_work_codes()

# --- СТУДЕНЧЕСКОЕ МЕНЮ И РЕГИСТРАЦИЯ ---
def get_student(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT full_name, student_group FROM students WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def cancel_flow(message, notice="Действие отменено."):
    bot.send_message(message.chat.id, notice, reply_markup=build_main_menu())


def interrupt_flow(message):
    """Позволяет отменить текущий ввод или перейти к другому действию, не застревая в шаге."""
    text = normalize_text(getattr(message, "text", ""))
    if not text:
        return False
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    menu_actions = {
        "📤 Сдать работу": start_new_work,
        "📚 Мои работы": show_my_works,
        "👤 Мой профиль": profile_menu_action,
        "✏️ Изменить профиль": start_login,
        "❌ Отмена": cancel_flow,
    }
    if command == "/cancel" or text == "❌ Отмена":
        cancel_flow(message)
        return True
    dispatch = {
        "/start": send_welcome,
        "/help": send_welcome,
        "/login": start_login,
        "/new_work": start_new_work,
        "/my_works": show_my_works,
        "/profile": show_profile,
    }
    if command in dispatch:
        bot.send_message(message.chat.id, "Предыдущий ввод отменён.", reply_markup=build_main_menu())
        dispatch[command](message)
        return True
    if text in menu_actions:
        bot.send_message(message.chat.id, "Предыдущий ввод отменён.", reply_markup=build_main_menu())
        action = menu_actions[text]
        if action is cancel_flow:
            action(message)
        else:
            action(message)
        return True
    if text.startswith("/"):
        cancel_flow(message, "Текущий ввод отменён: эта команда здесь не ожидается. Выберите действие в меню.")
        return True
    return False


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    if message.chat.type != "private":
        bot.reply_to(message, "Для сдачи работ откройте личный чат с ботом и отправьте /start.")
        return
    bot.send_message(
        message.chat.id,
        "Здравствуйте! Это бот для приёма практических работ.\n\n"
        "Выберите действие кнопкой ниже. Во время ввода можно нажать «❌ Отмена» или отправить /cancel.\n"
        "Команды: /login — регистрация, /new_work — сдать работу, /my_works — мои работы.",
        reply_markup=build_main_menu(),
    )


@bot.message_handler(commands=["cancel"])
def cancel_command(message):
    if message.chat.type == "private":
        cancel_flow(message)
    else:
        bot.reply_to(message, "Отмена доступна в личном чате с ботом.")


@bot.message_handler(commands=["login"])
def start_login(message):
    if not is_private_chat(message):
        return
    msg = bot.send_message(
        message.chat.id,
        "Введите фамилию и имя (например, Иванов Иван).\nМожно отправить /cancel для отмены.",
        reply_markup=telebot.types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, process_name_step)


def process_name_step(message):
    if interrupt_flow(message):
        return
    full_name = normalize_text(getattr(message, "text", ""))
    parts = full_name.split()
    # Имя допускает буквы разных алфавитов, пробелы, дефис и апостроф.
    if len(parts) < 2 or len(full_name) > 100 or not all(re.fullmatch(r"[\wА-Яа-яЁё'’-]+", part, re.UNICODE) for part in parts):
        msg = bot.send_message(message.chat.id, "Введите фамилию и имя буквами, например: Иванов Иван. Попробуйте ещё раз или нажмите /cancel.")
        bot.register_next_step_handler(msg, process_name_step)
        return
    msg = bot.send_message(message.chat.id, "Введите учебную группу (например, ПЕ-22б).", reply_markup=telebot.types.ReplyKeyboardRemove())
    bot.register_next_step_handler(msg, process_group_step, full_name)


def process_group_step(message, full_name):
    if interrupt_flow(message):
        return
    student_group = normalize_text(getattr(message, "text", ""))
    if not valid_short_text(student_group, 40) or len(student_group) < 2:
        msg = bot.send_message(message.chat.id, "Укажите название группы (2–40 символов) или отправьте /cancel.")
        bot.register_next_step_handler(msg, process_group_step, full_name)
        return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO students (user_id, username, full_name, student_group)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                student_group = excluded.student_group
        """, (message.chat.id, message.from_user.username, full_name, student_group))
        conn.commit()
        conn.close()
        bot.send_message(
            message.chat.id,
            f"Регистрация сохранена.\nФИО: {html.escape(full_name)}\nГруппа: {html.escape(student_group)}",
            parse_mode="HTML",
            reply_markup=build_main_menu(),
        )
    except Exception as e:
        print(f"Ошибка БД (регистрация): {e}")
        bot.send_message(message.chat.id, "Не удалось сохранить регистрацию. Данные не изменены; попробуйте /login ещё раз.", reply_markup=build_main_menu())


def require_student(message):
    if not is_private_chat(message):
        return None
    student = get_student(message.chat.id)
    if not student:
        bot.send_message(message.chat.id, "Сначала зарегистрируйтесь: нажмите «👤 Мой профиль» или отправьте /login.", reply_markup=build_main_menu())
        return None
    return student


@bot.message_handler(commands=["profile"])
def show_profile(message):
    if not is_private_chat(message):
        return
    student = get_student(message.chat.id)
    if not student:
        bot.send_message(message.chat.id, "Вы ещё не зарегистрированы. Нажмите «👤 Мой профиль», затем выберите регистрацию — или отправьте /login.", reply_markup=build_main_menu())
        return
    bot.send_message(
        message.chat.id,
        f"Ваш профиль:\nФИО: {html.escape(student[0] or '—')}\nГруппа: {html.escape(student[1] or '—')}\n\n"
        "Чтобы исправить данные, нажмите «✏️ Изменить профиль».",
        parse_mode="HTML",
        reply_markup=build_main_menu(),
    )


def profile_menu_action(message):
    if get_student(message.chat.id):
        show_profile(message)
    else:
        start_login(message)


@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text == "👤 Мой профиль")
def profile_menu_button(message):
    profile_menu_action(message)


@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text == "✏️ Изменить профиль")
def edit_profile_menu_button(message):
    start_login(message)


@bot.message_handler(commands=["new_work"])
def start_new_work(message):
    student = require_student(message)
    if not student:
        return
    subjects = recent_values(message.chat.id, "subject")
    prompt = "Выберите прежнюю дисциплину или введите новую:"
    if subjects:
        msg = bot.send_message(message.chat.id, prompt, reply_markup=build_choice_keyboard(subjects))
        bot.register_next_step_handler(msg, process_subject_choice_step)
    else:
        msg = bot.send_message(message.chat.id, "Введите название дисциплины (например, История России, Социология). Можно отменить через /cancel.", reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, process_subject_step)


def prompt_work_name(message, subject):
    names = recent_values(message.chat.id, "work_name", subject=subject)
    if names:
        msg = bot.send_message(message.chat.id, f"Выберите прежнее название работы по дисциплине «{html.escape(subject)}» или введите новое:", parse_mode="HTML", reply_markup=build_choice_keyboard(names))
        bot.register_next_step_handler(msg, process_work_name_choice_step, subject)
    else:
        msg = bot.send_message(message.chat.id, "Введите название работы (например, Практическая работа №1). Можно отменить через /cancel.", reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, process_work_name_step, subject)


def process_subject_choice_step(message):
    if interrupt_flow(message):
        return
    value = normalize_text(getattr(message, "text", ""))
    if value == "✍️ Ввести вручную":
        msg = bot.send_message(message.chat.id, "Введите новую дисциплину:", reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, process_subject_step)
        return
    if not valid_short_text(value):
        msg = bot.send_message(message.chat.id, "Название дисциплины пустое или слишком длинное. Выберите кнопку или введите текст ещё раз.")
        bot.register_next_step_handler(msg, process_subject_choice_step)
        return
    prompt_work_name(message, value)


def process_subject_step(message):
    if interrupt_flow(message):
        return
    subject = normalize_text(getattr(message, "text", ""))
    if not valid_short_text(subject):
        msg = bot.send_message(message.chat.id, "Название дисциплины пустое или слишком длинное. Введите его ещё раз или /cancel.")
        bot.register_next_step_handler(msg, process_subject_step)
        return
    prompt_work_name(message, subject)


def process_work_name_choice_step(message, subject):
    if interrupt_flow(message):
        return
    value = normalize_text(getattr(message, "text", ""))
    if value == "✍️ Ввести вручную":
        msg = bot.send_message(message.chat.id, "Введите новое название работы:", reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, process_work_name_step, subject)
        return
    if not valid_short_text(value):
        msg = bot.send_message(message.chat.id, "Название работы пустое или слишком длинное. Введите его ещё раз или /cancel.")
        bot.register_next_step_handler(msg, process_work_name_choice_step, subject)
        return
    prompt_file(message, subject, value)


def process_work_name_step(message, subject):
    if interrupt_flow(message):
        return
    work_name = normalize_text(getattr(message, "text", ""))
    if not valid_short_text(work_name):
        msg = bot.send_message(message.chat.id, "Название работы пустое или слишком длинное. Введите его ещё раз или /cancel.")
        bot.register_next_step_handler(msg, process_work_name_step, subject)
        return
    prompt_file(message, subject, work_name)


def prompt_file(message, subject, work_name):
    msg = bot.send_message(
        message.chat.id,
        f"Прикрепите файл для работы «{html.escape(work_name)}» по дисциплине «{html.escape(subject)}».\n"
        "Поддерживается отправка документа. Если прикрепили не то, просто отправьте файл ещё раз. Для отмены — /cancel.",
        parse_mode="HTML",
        reply_markup=telebot.types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, process_file_step, subject, work_name)


def next_file_step(message, subject, work_name, text):
    msg = bot.send_message(message.chat.id, text)
    bot.register_next_step_handler(msg, process_file_step, subject, work_name)


def process_file_step(message, subject, work_name):
    if interrupt_flow(message):
        return
    chat_id = message.chat.id
    if message.content_type != "document" or not getattr(message, "document", None):
        next_file_step(message, subject, work_name, "Это не документ. Прикрепите файл именно как документ; текущая работа не потеряна. Или /cancel.")
        return

    document = message.document
    if not document.file_id:
        next_file_step(message, subject, work_name, "Telegram не передал идентификатор документа. Отправьте файл ещё раз или /cancel.")
        return

    db_saved = False
    work_code = None
    try:
        student = get_student(chat_id)
        if not student:
            cancel_flow(message, "Регистрация не найдена. Сначала выполните /login; файл не был принят.")
            return
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            work_code = create_unique_work_code(cursor, subject, student[1], student[0], work_name)
            cursor.execute("""
                INSERT INTO works (user_id, subject, work_name, file_id, status, timestamp, work_code)
                VALUES (?, ?, ?, ?, 'НЕПРОВЕРЕНО', CURRENT_TIMESTAMP, ?)
            """, (chat_id, subject, work_name, document.file_id, work_code))
            conn.commit()
            db_saved = True
            cursor.execute("SELECT timestamp FROM works WHERE work_code = ?", (work_code,))
            fetched = cursor.fetchone()
            submitted_at = fetched[0] if fetched else "—"
        finally:
            conn.close()

        try:
            group_notified = notify_teacher_group(work_code)
        except Exception as notify_error:
            print(f"Сдача {work_code} сохранена, но уведомление преподавателя завершилось ошибкой: {notify_error}")
            group_notified = False

        status_note = "Работа автоматически отправлена в группу преподавателя." if group_notified else (
            "Работа сохранена, но не удалось отправить её в группу преподавателя. "
            "Не отправляйте её повторно; сообщите преподавателю ID работы."
        )
        bot.send_message(
            chat_id,
            f"✅ Работа сохранена.\\n\\n"
            f"Дисциплина: {html.escape(subject)}\\n"
            f"Работа: {html.escape(work_name)}\\n"
            f"Файл: {html.escape(os.path.basename(document.file_name or 'Документ'))}\\n"
            f"ID: <code>{html.escape(work_code)}</code>\\n"
            f"Дата отправки: {html.escape(str(submitted_at))}\\n"
            f"Статус: <b>НЕПРОВЕРЕНО</b>\\n\\n{html.escape(status_note)}",
            parse_mode="HTML",
            reply_markup=build_main_menu(),
        )
    except Exception as e:
        print(f"Ошибка при сохранении работы: {e}")
        if db_saved and work_code:
            bot.send_message(
                chat_id,
                f"Работа сохранена под ID <code>{html.escape(work_code)}</code>, "
                "но не удалось завершить отправку уведомления. Не отправляйте её повторно.",
                parse_mode="HTML",
                reply_markup=build_main_menu(),
            )
        else:
            bot.send_message(
                chat_id,
                "Не удалось принять работу. Запись не подтверждена; попробуйте ещё раз или обратитесь к преподавателю.",
                reply_markup=build_main_menu(),
            )


@bot.message_handler(commands=["my_works"])
def show_my_works(message):
    if not is_private_chat(message):
        return
    if not get_student(message.chat.id):
        bot.send_message(message.chat.id, "Сначала зарегистрируйтесь через /login.", reply_markup=build_main_menu())
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT w.work_code, w.subject, w.work_name, w.status, w.timestamp
        FROM works w WHERE w.user_id = ? ORDER BY w.timestamp DESC, w.id DESC LIMIT 10
    """, (message.chat.id,))
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        bot.send_message(message.chat.id, "Пока нет сданных работ. Нажмите «📤 Сдать работу», чтобы отправить первую.", reply_markup=build_main_menu())
        return
    bot.send_message(message.chat.id, f"Ваши последние работы (показано {len(rows)}):", reply_markup=build_main_menu())
    for code, subject, work_name, status, timestamp in rows:
        text = (
            f"<b>{html.escape(work_name or 'Без названия')}</b>\n"
            f"Дисциплина: {html.escape(subject or '—')}\n"
            f"Статус: <b>{html.escape(status or 'НЕПРОВЕРЕНО')}</b>\n"
            f"Дата: {html.escape(str(timestamp or '—'))}\n"
            f"ID: <code>{html.escape(code or '—')}</code>"
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=build_student_work_keyboard(code))


@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text == "📤 Сдать работу")
def new_work_menu_button(message):
    start_new_work(message)


@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text == "📚 Мои работы")
def my_works_menu_button(message):
    show_my_works(message)


@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text == "❌ Отмена")
def cancel_menu_button(message):
    cancel_flow(message)


# Вспомогательная команда для первоначальной настройки реестра группы.
@bot.message_handler(commands=['group_id'])
def show_group_id(message):
    if message.chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Добавьте бота в нужную группу и выполните /group_id внутри неё.")
        return
    bot.reply_to(message, f"ID этой группы: {message.chat.id}\nНазвание: {message.chat.title or 'без названия'}")


# --- ЛОГИКА ПРЕПОДАВАТЕЛЯ: СЕКРЕТНЫЕ КОМАНДЫ ---

# 1. Поиск работ студента по формату: /check Дисциплина/Группа/ФИО
@bot.message_handler(regexp=r'^/check(?:@\w+)?\s.+$')
def check_student_works(message):
    if not authorize_teacher_command(message):
        return
    try:
        command_parts = (message.text or "").split(maxsplit=1)
        if len(command_parts) < 2:
            bot.reply_to(message, "Формат: <code>/check Дисциплина/Группа/Фамилия Имя</code>", parse_mode="HTML")
            return
        raw_args = command_parts[1].strip()
        parts = raw_args.split('/')
        if len(parts) != 3:
            bot.reply_to(message, "Формат: <code>/check Дисциплина/Группа/Фамилия Имя</code>", parse_mode="HTML")
            return
        subject, student_group, full_name = (part.strip() for part in parts)
        if not subject or not student_group or not full_name:
            bot.reply_to(message, "Все три поля обязательны: дисциплина, группа, ФИО.")
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT w.work_code, w.work_name, w.status, w.timestamp
            FROM works w JOIN students s ON w.user_id = s.user_id
            WHERE LOWER(w.subject) = LOWER(?) AND LOWER(s.student_group) = LOWER(?)
              AND LOWER(s.full_name) = LOWER(?)
            ORDER BY w.timestamp DESC, w.id DESC
        """, (subject, student_group, full_name))
        works = cursor.fetchall()
        conn.close()
        if not works:
            bot.reply_to(message, f"Работы студента {html.escape(full_name)} ({html.escape(student_group)}) по дисциплине {html.escape(subject)} не найдены.", parse_mode="HTML")
            return
        response = (
            "<b>СДАННЫЕ РАБОТЫ СТУДЕНТА</b>\n"
            f"Студент: {html.escape(full_name)}\nГруппа: {html.escape(student_group)}\n"
            f"Дисциплина: {html.escape(subject)}\n━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        for idx, (work_code, work_name, status, timestamp) in enumerate(works, start=1):
            response += (
                f"{idx}. {html.escape(work_name or 'Без названия')}\n"
                f"   ID: <code>{html.escape(work_code or '—')}</code>\n"
                f"   Статус: <b>{html.escape(status or 'НЕПРОВЕРЕНО')}</b>\n"
                f"   Дата: {html.escape(str(timestamp or '—'))}\n\n"
            )
            if len(response) > 3500:
                bot.send_message(message.chat.id, response, parse_mode="HTML")
                response = "<b>Продолжение списка</b>\n\n"
        if response.strip():
            bot.send_message(message.chat.id, response, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при выполнении /check: {e}")
        bot.reply_to(message, "Произошла ошибка при поиске данных.")


# 2. Список работ для преподавателя: /pull [all|N|СТАТУС]
# /pull             — 10 последних работ со статусом НЕПРОВЕРЕНО
# /pull 25          — 25 последних непроверенных работ
# /pull all         — все работы
# /pull ПРОВЕРЕНО   — проверенные работы и работы с оценками
@bot.message_handler(regexp=r'^/pull(?:@\w+)?(?:\s+.*)?$')
def pull_works(message):
    if not authorize_teacher_command(message):
        return
    try:
        parts = (message.text or "").split(maxsplit=1)
        argument = parts[1].strip() if len(parts) > 1 else ""
        conn = get_db_connection()
        cursor = conn.cursor()

        base_query = """
            SELECT w.work_code, w.work_name, w.subject, w.status, w.timestamp,
                   s.full_name, s.student_group
            FROM works w
            LEFT JOIN students s ON s.user_id = w.user_id
        """
        params = []
        title = "ПОСЛЕДНИЕ НЕПРОВЕРЕННЫЕ РАБОТЫ"

        if not argument:
            cursor.execute(base_query + " WHERE UPPER(COALESCE(w.status, 'НЕПРОВЕРЕНО')) = 'НЕПРОВЕРЕНО' ORDER BY w.timestamp DESC, w.id DESC LIMIT 10")
        elif argument.casefold() == "all":
            title = "ВСЕ РАБОТЫ"
            cursor.execute(base_query + " ORDER BY w.timestamp DESC, w.id DESC")
        elif argument.isdigit():
            limit = int(argument)
            if limit < 1 or limit > 500:
                conn.close()
                bot.reply_to(message, "Укажи число от 1 до 500. Например: /pull 25")
                return
            cursor.execute(base_query + " WHERE UPPER(COALESCE(w.status, 'НЕПРОВЕРЕНО')) = 'НЕПРОВЕРЕНО' ORDER BY w.timestamp DESC, w.id DESC LIMIT ?", (limit,))
            title = f"ПОСЛЕДНИЕ {limit} НЕПРОВЕРЕННЫХ РАБОТ"
        else:
            status_filter = argument.upper()
            if status_filter == "ПРОВЕРЕНО":
                # Включает ПРОВЕРЕНО и все оценки; НЕПРОВЕРЕНО исключается.
                cursor.execute(base_query + " WHERE UPPER(COALESCE(w.status, 'НЕПРОВЕРЕНО')) != 'НЕПРОВЕРЕНО' ORDER BY w.timestamp DESC, w.id DESC")
                title = "ПРОВЕРЕННЫЕ РАБОТЫ И РАБОТЫ С ОЦЕНКАМИ"
            elif status_filter in VALID_STATUSES:
                cursor.execute(base_query + " WHERE UPPER(w.status) = ? ORDER BY w.timestamp DESC, w.id DESC", (status_filter,))
                title = f"РАБОТЫ СО СТАТУСОМ: {status_filter}"
            else:
                conn.close()
                bot.reply_to(message, "Формат: /pull, /pull 20, /pull all, /pull ПРОВЕРЕНО или /pull ОТЛИЧНО")
                return

        rows = cursor.fetchall()
        conn.close()

        if not rows:
            bot.reply_to(message, "Работ по заданному условию не найдено.")
            return

        # Делим на отдельные сообщения, чтобы не превысить лимит Telegram.
        chunks = []
        current = title + f"\nНайдено: {len(rows)}\n━━━━━━━━━━━━━━━━━━━━\n\n"
        for idx, (code, work_name, subject, status, timestamp, full_name, student_group) in enumerate(rows, start=1):
            item = (
                f"{idx}. {html.escape(work_name or 'Без названия')}\n"
                f"   ID: <code>{html.escape(code or 'нет ID')}</code>\n"
                f"   Дисциплина: {html.escape(subject or '—')}\n"
                f"   Студент: {html.escape(full_name or '—')} ({html.escape(student_group or '—')})\n"
                f"   Статус: <b>{html.escape(status or 'НЕПРОВЕРЕНО')}</b>\n"
                f"   Дата отправки: {html.escape(str(timestamp or '—'))}\n\n"
            )
            if len(current) + len(item) > 3800:
                chunks.append(current)
                current = title + " (продолжение)\n\n"
            current += item
        if current.strip():
            chunks.append(current)
        for chunk in chunks:
            bot.send_message(message.chat.id, chunk, parse_mode="HTML")

    except Exception as e:
        print(f"Ошибка при выполнении /pull: {e}")
        bot.reply_to(message, "Не удалось получить список работ.")


# Информация о работе по её ID — без повторной отправки файла.
@bot.message_handler(regexp=r"^/get_work(?:@\\w+)?\\s+[A-Za-z0-9-]+$")
def send_work_file(message):
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            bot.reply_to(message, "Укажите ID работы: /get_work ID")
            return
        work_code = parts[1].strip().upper()
        conn = get_db_connection()
        try:
            row = get_work_by_code(conn.cursor(), work_code)
        finally:
            conn.close()
        if not row:
            bot.reply_to(message, "Работа с таким ID не найдена.")
            return
        _, saved_code, owner_id, subject, work_name, _, status, timestamp, full_name, student_group = row
        if message.chat.type in ("group", "supergroup"):
            if not authorize_teacher_command(message):
                return
        elif message.chat.type == "private":
            if not message.from_user or message.from_user.id != owner_id:
                bot.reply_to(message, "Вы можете просматривать только информацию о собственных работах.")
                return
        else:
            bot.reply_to(message, "Информация доступна владельцу работы или преподавателю в зарегистрированной группе.")
            return
        bot.reply_to(
            message,
            f"<b>Информация о работе</b>\\n"
            f"Дисциплина: {html.escape(subject or '—')}\\n"
            f"Работа: {html.escape(work_name or 'Без названия')}\\n"
            f"Студент: {html.escape(full_name or '—')} ({html.escape(student_group or '—')})\\n"
            f"Дата сдачи: {html.escape(str(timestamp or '—'))}\\n"
            f"Статус: <b>{html.escape(status or 'НЕПРОВЕРЕНО')}</b>\\n"
            f"ID: <code>{html.escape(saved_code)}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        print(f"Ошибка при получении информации о работе: {e}")
        bot.reply_to(message, "Не удалось получить информацию о работе. Попробуйте позже.")

def answer_student_work_button(call):
    """Обрабатывает приватные кнопки работы, предварительно проверяя владельца."""
    parts = call.data.split("|")
    if len(parts) != 3:
        bot.answer_callback_query(call.id, "Некорректная кнопка.", show_alert=True)
        return
    work_code, action = parts[1], parts[2]
    conn = get_db_connection()
    cursor = conn.cursor()
    row = get_work_by_code(cursor, work_code)
    conn.close()
    if not row:
        bot.answer_callback_query(call.id, "Работа не найдена.", show_alert=True)
        return
    _, saved_code, owner_id, subject, work_name, _, status, timestamp, _, _ = row
    if call.message.chat.type != "private" or not call.from_user or call.from_user.id != owner_id:
        bot.answer_callback_query(call.id, "Эта кнопка доступна только владельцу работы в личном чате.", show_alert=True)
        return
    if action == "status":
        bot.answer_callback_query(call.id, f"Статус: {status or 'НЕПРОВЕРЕНО'}", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Неизвестное действие.", show_alert=True)


# Кнопки под каждой работой: статус может менять только администратор
# зарегистрированной группы преподавателя.
@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith(("ws|", "sw|")))
def handle_work_status_button(call):
    try:
        if call.data.startswith("sw|"):
            answer_student_work_button(call)
            return
        if not call.message or call.message.chat.type not in ("group", "supergroup"):
            bot.answer_callback_query(call.id, "Кнопки доступны только в группе преподавателя.", show_alert=True)
            return

        registered_chat_id = get_registered_teacher_group_id()
        if registered_chat_id is None or call.message.chat.id != registered_chat_id:
            bot.answer_callback_query(call.id, "Эта группа не зарегистрирована для управления ботом.", show_alert=True)
            return

        member = bot.get_chat_member(call.message.chat.id, call.from_user.id)
        if member.status not in ("administrator", "creator"):
            bot.answer_callback_query(call.id, "Менять статус могут только администраторы группы.", show_alert=True)
            return

        parts = call.data.split("|")
        if len(parts) != 3:
            bot.answer_callback_query(call.id, "Некорректные данные кнопки.", show_alert=True)
            return
        work_code, status_code = parts[1], parts[2]
        status = CALLBACK_CODE_STATUSES.get(status_code)
        if not status:
            bot.answer_callback_query(call.id, "Неизвестный статус.", show_alert=True)
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        row_before = get_work_by_code(cursor, work_code)
        if not row_before:
            conn.close()
            bot.answer_callback_query(call.id, "Работа не найдена.", show_alert=True)
            return
        if (row_before[6] or "НЕПРОВЕРЕНО").upper() == status:
            conn.close()
            bot.answer_callback_query(call.id, f"Статус уже установлен: {status}")
            return
        cursor.execute("UPDATE works SET status = ? WHERE UPPER(work_code) = UPPER(?)", (status, work_code))
        conn.commit()
        row = get_work_by_code(cursor, work_code)
        conn.close()

        if not row:
            bot.answer_callback_query(call.id, "Работа не найдена.", show_alert=True)
            return

        try:
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption=build_work_caption(row),
                parse_mode="HTML",
                reply_markup=build_work_keyboard(work_code),
            )
        except Exception as edit_error:
            # Статус уже сохранён в SQLite. Не утверждаем, что он не поменялся только
            # потому что Telegram временно не смог обновить подпись.
            print(f"Статус {status} сохранён для {work_code}, но подпись сообщения не обновлена: {edit_error}")
            bot.answer_callback_query(
                call.id,
                f"Статус сохранён: {status}. Подпись сообщения не обновилась; проверьте /pull.",
                show_alert=True,
            )
            return
        bot.answer_callback_query(call.id, f"Статус изменён: {status}")
    except Exception as e:
        print(f"Ошибка обработки кнопки статуса: {e}")
        try:
            bot.answer_callback_query(call.id, "Не удалось изменить статус работы.", show_alert=True)
        except Exception:
            pass


# 3. Временная команда преподавателя для изменения статуса:
# /set_status WRK-ABCD-EFGH ОТЛИЧНО
@bot.message_handler(regexp=r"^/set_status(?:@\w+)?\s+\S+\s+.+$")
def set_work_status(message):
    if not authorize_teacher_command(message):
        return
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) != 3:
            bot.reply_to(message, "Формат: /set_status ID_РАБОТЫ СТАТУС")
            return

        work_code = parts[1].strip().upper()
        status = parts[2].strip().upper()
        if status not in VALID_STATUSES:
            bot.reply_to(
                message,
                "Неизвестный статус. Допустимые: " + ", ".join(sorted(VALID_STATUSES))
            )
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE works SET status = ? WHERE UPPER(work_code) = UPPER(?)", (status, work_code))
        updated = cursor.rowcount
        cursor.execute(
            "SELECT teacher_chat_id, teacher_message_id FROM works WHERE UPPER(work_code) = UPPER(?)",
            (work_code,),
        )
        message_ref = cursor.fetchone() if updated else None
        row = get_work_by_code(cursor, work_code) if updated else None
        conn.commit()
        conn.close()

        if not updated or not row:
            bot.reply_to(message, "Работа с таким ID не найдена.")
            return
        caption_updated = False
        if message_ref and message_ref[0] is not None and message_ref[1] is not None:
            try:
                bot.edit_message_caption(
                    chat_id=message_ref[0], message_id=message_ref[1],
                    caption=build_work_caption(row), parse_mode="HTML",
                    reply_markup=build_work_keyboard(work_code),
                )
                caption_updated = True
            except Exception as edit_error:
                print(f"Статус сохранён для {work_code}, но не удалось обновить подпись ленты: {edit_error}")
        outcome = "Статус изменён, сообщение в ленте обновлено." if caption_updated else "Статус изменён в базе данных. Сообщение ленты не связано с этой записью или не обновилось."
        bot.reply_to(message, f"{outcome}\nID: <code>{html.escape(work_code)}</code>\nСтатус: <b>{html.escape(status)}</b>", parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при изменении статуса работы: {e}")
        bot.reply_to(message, "Не удалось изменить статус работы.")


@bot.message_handler(regexp=r"^/retry_notify(?:@\w+)?\s+[A-Za-z0-9-]+$")
def retry_teacher_notification(message):
    if not authorize_teacher_command(message):
        return
    work_code = message.text.split(maxsplit=1)[1].strip().upper()
    conn = get_db_connection()
    cursor = conn.cursor()
    row = get_work_by_code(cursor, work_code)
    cursor.execute(
        "SELECT teacher_chat_id, teacher_message_id FROM works WHERE UPPER(work_code) = UPPER(?)",
        (work_code,),
    )
    message_ref = cursor.fetchone()
    conn.close()
    if not row:
        bot.reply_to(message, "Работа с таким ID не найдена.")
        return
    if message_ref and message_ref[0] is not None and message_ref[1] is not None:
        bot.reply_to(message, f"Работа <code>{html.escape(work_code)}</code> уже опубликована. Повторная отправка отменена, чтобы не создавать дубликат.", parse_mode="HTML")
        return
    if notify_teacher_group(work_code):
        bot.reply_to(message, f"Работа <code>{html.escape(work_code)}</code> повторно опубликована в ленте.", parse_mode="HTML")
    else:
        bot.reply_to(message, f"Не удалось опубликовать <code>{html.escape(work_code)}</code>. Запись в базе и файл не изменены; проверьте ID группы и права бота.", parse_mode="HTML")


print(f"Бот запущен. Режим: {'DEBUG (временная копия)' if IS_DEBUG else 'обычный'}; база данных: {DB_PATH}")
if IS_DEBUG:
    print("Исходные файлы в test_data не изменяются; временная копия удалится при завершении бота.")
bot.infinity_polling()

