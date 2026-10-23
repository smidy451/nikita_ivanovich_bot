import os
import sys
import sqlite3
import hashlib
import secrets
import base64
import re
import html
import telebot

# 1. Чтение токена из файла
try:
    with open("token.txt", "r", encoding="utf-8") as file:
        BOT_TOKEN = file.read().strip()
except FileNotFoundError:
    print("Ошибка: Файл token.txt не найден в текущей директории!")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

# 2. Определение режима работы и путей
IS_DEBUG = "-debug" in sys.argv or "--debug" in sys.argv
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

if IS_DEBUG:
    # Тестовые данные постоянны между запусками и не очищаются автоматически.
    DB_PATH = os.path.join(PROJECT_DIR, "test_data", "test_students.db")
    BASE_FILES_DIR = os.path.join(PROJECT_DIR, "test_data", "subjects")
else:
    DB_PATH = os.path.join(PROJECT_DIR, "students.db")
    BASE_FILES_DIR = os.path.join(PROJECT_DIR, "uploaded_works")

os.makedirs(BASE_FILES_DIR, exist_ok=True)

def get_db_connection():
    # В обоих режимах используем файловую SQLite-БД.
    # В --debug это test_data/test_students.db, иначе students.db.
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
            status TEXT DEFAULT 'Отправлена на проверку',
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
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_works_work_code ON works(work_code)")
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
    """Читает ID единственной разрешённой группы из teacher_group.txt."""
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
        print("Первая непустая строка teacher_group.txt должна быть числовым ID группы.")
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
    return keyboard


def build_work_caption(row):
    # row соответствует get_work_by_code(): id, code, user_id, subject, name,
    # file_path, status, timestamp, full_name, student_group.
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
    """Публикует принятую работу с файлом в непрерывную ленту группы преподавателя."""
    teacher_chat_id = get_registered_teacher_group_id()
    if teacher_chat_id is None:
        print("Не отправлена работа преподавателю: не задан ID в teacher_group.txt")
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    row = get_work_by_code(cursor, work_code)
    conn.close()
    if not row:
        print(f"Не удалось найти работу {work_code} для публикации в группе преподавателя")
        return

    _, _, _, _, work_name, file_path, _, _, _, _ = row
    if not os.path.isfile(file_path):
        print(f"Файл работы {work_code} не найден: {file_path}")
        return

    try:
        with open(file_path, "rb") as document:
            bot.send_document(
                teacher_chat_id,
                document,
                caption=build_work_caption(row),
                parse_mode="HTML",
                reply_markup=build_work_keyboard(work_code),
            )
    except Exception as e:
        # Ошибка уведомления не отменяет уже сохранённую сдачу студента.
        print(f"Не удалось отправить работу {work_code} в группу преподавателя: {e}")


def clean_path(text):
    """Очищает имя для использования в пути файла."""
    cleaned = "".join(c for c in text if c.isalpha() or c.isdigit() or c in (" ", "_", "-")).strip()
    return cleaned or "Без названия"


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
        SELECT w.id, w.work_code, w.user_id, w.subject, w.work_name, w.file_path,
               w.status, w.timestamp, s.full_name, s.student_group
        FROM works w JOIN students s ON s.user_id = w.user_id
        WHERE UPPER(w.work_code) = UPPER(?)
    """, (work_code,))
    return cursor.fetchone()


# Назначаем коды ранее сохранённым работам, созданным старой версией бота.
def backfill_work_codes():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT w.id, w.subject, w.work_name, s.full_name, s.student_group
        FROM works w JOIN students s ON s.user_id = w.user_id
        WHERE w.work_code IS NULL OR w.work_code = ''
    """)
    old_works = cursor.fetchall()
    for row_id, subject, work_name, full_name, student_group in old_works:
        code = create_unique_work_code(cursor, subject or "", student_group or "", full_name or "", work_name or "")
        cursor.execute("UPDATE works SET work_code = ? WHERE id = ?", (code, row_id))
    conn.commit()
    conn.close()


backfill_work_codes()

# Вспомогательная функция проверки регистрации
def get_student(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT full_name, student_group FROM students WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

# --- ЛОГИКА РЕГИСТРАЦИИ СТУДЕНТА ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Здравствуйте! Это Никита_Иванович - Бот для приёма практических работ.\n"
                          "Доступные команды:\n/login - Регистрация\n/new_work - Сдать работу")

@bot.message_handler(commands=['login'])
def start_login(message):
    msg = bot.send_message(message.chat.id, "Введите фамилию и имя студента в формате \"Иванов Иван\":")
    bot.register_next_step_handler(msg, process_name_step)

def process_name_step(message):
    full_name = message.text
    msg = bot.send_message(message.chat.id, "Введите группу. Примеры: *ПЕ-22б*, *ИТЕ-11б*")
    bot.register_next_step_handler(msg, process_group_step, full_name)

def process_group_step(message, full_name):
    try:
        chat_id = message.chat.id
        student_group = message.text

        if not student_group or student_group.startswith('/'):
            bot.send_message(chat_id, "Ввод отменен.")
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO students (user_id, username, full_name, student_group)
            VALUES (?, ?, ?, ?)
        """, (chat_id, message.from_user.username, full_name, student_group))
        conn.commit()
        conn.close()

        bot.send_message(chat_id, f"Успешно зарегистрированы!")
    except Exception as e:
        print(f"Ошибка БД (регистрация): {e}")
        bot.reply_to(message, "Ошибка при сохранении данных.")


# --- ЛОГИКА: СДАЧА РАБОТЫ (/new_work) ---
@bot.message_handler(commands=['new_work'])
def start_new_work(message):
    chat_id = message.chat.id
    student = get_student(chat_id)
    
    if not student:
        bot.send_message(chat_id, "Вы не зарегистрированы! Сначала введите команду /login")
        return
        
    msg = bot.send_message(chat_id, "Введите название дисциплины (например, Архитектура ЭВМ):")
    bot.register_next_step_handler(msg, process_subject_step)

def process_subject_step(message):
    subject = message.text.strip()
    if subject.startswith('/'):
        bot.send_message(message.chat.id, "Ввод отменен.")
        return
        
    msg = bot.send_message(message.chat.id, "Введите наименование работы (например, Практическая работа №1):")
    bot.register_next_step_handler(msg, process_work_name_step, subject)

def process_work_name_step(message, subject):
    work_name = message.text.strip()
    if work_name.startswith('/'):
        bot.send_message(message.chat.id, "Ввод отменен.")
        return
        
    msg = bot.send_message(message.chat.id, "📎 Теперь отправьте файл отчёта (в формате .doc, .docx, .pdf и т.д.):")
    bot.register_next_step_handler(msg, process_file_step, subject, work_name)

def process_file_step(message, subject, work_name):
    chat_id = message.chat.id
    
    if message.content_type != 'document':
        bot.send_message(chat_id, "Это не файл. Пожалуйста, начните заново с команды /new_work и прикрепите документ.")
        return

    try:
        student = get_student(chat_id)
        full_name, student_group = student[0], student[1]
        
        document = message.document
        old_file_name = document.file_name
        
        # 1. Извлекаем расширение оригинального файла
        _, file_extension = os.path.splitext(old_file_name)
        
        # 2. Формируем новое имя файла строго по ТЗ
        new_file_name = f"{clean_path(work_name)}{file_extension}"
        
        folder_path = os.path.join(
            BASE_FILES_DIR, 
            clean_path(subject), 
            clean_path(student_group), 
            clean_path(full_name)
        )
        os.makedirs(folder_path, exist_ok=True)
        full_file_path = os.path.join(folder_path, new_file_name)

        # В тестовом режиме не перезаписываем подготовленные тестовые файлы.
        if IS_DEBUG and os.path.exists(full_file_path):
            stem, extension = os.path.splitext(new_file_name)
            suffix = 2
            while os.path.exists(os.path.join(folder_path, f"{stem} ({suffix}){extension}")):
                suffix += 1
            new_file_name = f"{stem} ({suffix}){extension}"
            full_file_path = os.path.join(folder_path, new_file_name)
        
        # Скачиваем файл
        file_info = bot.get_file(document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        with open(full_file_path, 'wb') as new_file:
            new_file.write(downloaded_file)
            
        # Записываем информацию в БД
        conn = get_db_connection()
        cursor = conn.cursor()
        work_code = create_unique_work_code(cursor, subject, student_group, full_name, work_name)
        cursor.execute("""
            INSERT INTO works (user_id, subject, work_name, file_path, status, timestamp, work_code)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
        """, (chat_id, subject, work_name, full_file_path, "НЕПРОВЕРЕНО", work_code))
        conn.commit()
        cursor.execute("SELECT timestamp FROM works WHERE work_code = ?", (work_code,))
        submitted_at = cursor.fetchone()[0]
        conn.close()
        
        bot.send_message(
            chat_id,
            f"Работа успешно принята!\n\n"
            f"Дисциплина: {subject}\n"
            f"Работа: {work_name}\n"
            f"Файл: {new_file_name}\n"
            f"ID работы: {work_code}\n"
            f"Дата отправки: {submitted_at}\n"
            f"Статус: НЕПРОВЕРЕНО\n\n"
            f"Сохраните ID работы — он нужен для получения файла и информации о работе."
        )

        # Каждая новая сдача сразу появляется в группе преподавателя вместе с файлом.
        notify_teacher_group(work_code)
        
    except Exception as e:
        print(f"Ошибка при сохранении файла/БД: {e}")
        bot.send_message(chat_id, "Произошла ошибка на сервере при сохранении файла. Попробуйте повторить отправку.")

# Вспомогательная команда для первоначальной настройки реестра группы.
@bot.message_handler(commands=['group_id'])
def show_group_id(message):
    if message.chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Добавьте бота в нужную группу и выполните /group_id внутри неё.")
        return
    bot.reply_to(message, f"ID этой группы: {message.chat.id}\nНазвание: {message.chat.title or 'без названия'}")


# --- ЛОГИКА ПРЕПОДАВАТЕЛЯ: СЕКРЕТНЫЕ КОМАНДЫ ---

# 1. Поиск работ студента по формату: /check Дисциплина/Группа/ФИО
@bot.message_handler(regexp=r'^/check\s.+')
def check_student_works(message):
    if not authorize_teacher_command(message):
        return
    try:
        # Убираем саму команду /check и лишние пробелы
        raw_args = message.text[6:].strip()
        
        # Разделяем аргументы по слэшу
        parts = raw_args.split('/')
        if len(parts) != 3:
            bot.reply_to(
                message, 
                "Неверный формат команды!\n"
                "Используйте: `/check Дисциплина/Группа/Фамилия Имя`"
            )
            return
            
        subject, student_group, full_name = parts[0].strip(), parts[1].strip(), parts[2].strip()
        
        # Ищем работы студента, связывая таблицы по user_id
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT w.work_code, w.work_name, w.status, w.timestamp
            FROM works w
            JOIN students s ON w.user_id = s.user_id
            WHERE LOWER(w.subject) = LOWER(?)
              AND LOWER(s.student_group) = LOWER(?)
              AND LOWER(s.full_name) = LOWER(?)
            ORDER BY w.timestamp DESC
        """, (subject, student_group, full_name))

        works = cursor.fetchall()
        conn.close()

        if not works:
            bot.reply_to(
                message,
                f"Работы студента {full_name} ({student_group}) по дисциплине {subject} не найдены."
            )
            return

        response = (
            "СДАННЫЕ РАБОТЫ СТУДЕНТА\n"
            f"Студент: {full_name}\n"
            f"Группа: {student_group}\n"
            f"Предмет: {subject}\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        for idx, (work_code, work_name, status, timestamp) in enumerate(works, start=1):
            response += (
                f"{idx}. {work_name}\n"
                f"   ID работы: {work_code}\n"
                f"   Статус: {status}\n"
                f"   Дата отправки: {timestamp}\n"
                f"   Скачать: /get_work {work_code}\n\n"
            )

        bot.send_message(message.chat.id, response)
        
    except Exception as e:
        print(f"Ошибка при выполнении /check: {e}")
        bot.reply_to(message, "Произошла ошибка при поиске данных.")


# 2. Список работ для преподавателя: /pull [all|N|СТАТУС]
# /pull             — 10 последних работ со статусом НЕПРОВЕРЕНО
# /pull 25          — 25 последних непроверенных работ
# /pull all         — все работы
# /pull ПРОВЕРЕНО   — проверенные работы и работы с оценками
@bot.message_handler(regexp=r'^/pull(?:\s+.*)?$')
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
                f"{idx}. {work_name or 'Без названия'}\n"
                f"   ID: {code or 'нет ID'}\n"
                f"   Дисциплина: {subject or '—'}\n"
                f"   Студент: {full_name or '—'} ({student_group or '—'})\n"
                f"   Статус: {status or 'НЕПРОВЕРЕНО'}\n"
                f"   Дата отправки: {timestamp or '—'}\n"
                f"   Скачать: /get_work {code}\n\n"
            )
            if len(current) + len(item) > 3800:
                chunks.append(current)
                current = title + " (продолжение)\n\n"
            current += item
        if current.strip():
            chunks.append(current)
        for chunk in chunks:
            bot.send_message(message.chat.id, chunk)

    except Exception as e:
        print(f"Ошибка при выполнении /pull: {e}")
        bot.reply_to(message, "Не удалось получить список работ.")


# 2. Получение файла по человекочитаемому ID работы
@bot.message_handler(regexp=r"^/get_work\s+[A-Za-z0-9-]+$")
def send_work_file(message):
    try:
        work_code = message.text.split(maxsplit=1)[1].strip().upper()
        conn = get_db_connection()
        cursor = conn.cursor()
        row = get_work_by_code(cursor, work_code)
        conn.close()

        if not row:
            bot.reply_to(message, "Работа с таким ID не найдена.")
            return

        _, saved_code, _, subject, work_name, file_path, status, timestamp, full_name, student_group = row
        if not os.path.isfile(file_path):
            bot.reply_to(message, "Файл работы не найден в хранилище.")
            return

        bot.send_chat_action(message.chat.id, "upload_document")
        with open(file_path, "rb") as doc:
            bot.send_document(
                message.chat.id,
                doc,
                caption=(
                    f"Работа: {work_name}\nID: {saved_code}\nПредмет: {subject}\n"
                    f"Студент: {full_name} ({student_group})\n"
                    f"Дата отправки: {timestamp}\nСтатус: {status}"
                )
            )
    except Exception as e:
        print(f"Ошибка при отправке файла преподавателю: {e}")
        bot.reply_to(message, "Не удалось отправить файл из хранилища.")


# Кнопки под каждой работой: статус может менять только администратор
# зарегистрированной группы преподавателя.
@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("ws|"))
def handle_work_status_button(call):
    try:
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
        cursor.execute("UPDATE works SET status = ? WHERE UPPER(work_code) = UPPER(?)", (status, work_code))
        updated = cursor.rowcount
        conn.commit()
        row = get_work_by_code(cursor, work_code) if updated else None
        conn.close()

        if not updated or not row:
            bot.answer_callback_query(call.id, "Работа не найдена.", show_alert=True)
            return

        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption=build_work_caption(row),
            parse_mode="HTML",
            reply_markup=build_work_keyboard(work_code),
        )
        bot.answer_callback_query(call.id, f"Статус изменён: {status}")
    except Exception as e:
        print(f"Ошибка обработки кнопки статуса: {e}")
        try:
            bot.answer_callback_query(call.id, "Не удалось изменить статус работы.", show_alert=True)
        except Exception:
            pass


# 3. Временная команда преподавателя для изменения статуса:
# /set_status WRK-ABCD-EFGH ОТЛИЧНО
@bot.message_handler(regexp=r"^/set_status\s+\S+\s+.+$")
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
        conn.commit()
        conn.close()

        if not updated:
            bot.reply_to(message, "Работа с таким ID не найдена.")
            return
        bot.reply_to(message, f"Статус работы {work_code} изменён на: {status}")
    except Exception as e:
        print(f"Ошибка при изменении статуса работы: {e}")
        bot.reply_to(message, "Не удалось изменить статус работы.")


print(f"Бот запущен. Режим базы данных: {DB_PATH}")
print(f"Путь хранения файлов: {BASE_FILES_DIR}")
bot.infinity_polling()

