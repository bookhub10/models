import os
import subprocess
import time
from datetime import datetime

# --- ตั้งค่า ---
TRIGGER_FILE = "/home/hp/Downloads/bot/COMPILE_NOW.trigger"
EA_PATH_WINDOWS = "C:\\Program Files\\MetaTrader 5\\MQL5\\Experts\\OBotTrading.mq5"
METAEDITOR_PATH = "/home/hp/.mt5/drive_c/Program Files/MetaTrader 5/metaeditor64.exe"
WINEPREFIX_PATH = "/home/hp/.mt5"
COMPILE_LOG_PATH = "/home/hp/Downloads/bot/logs/compile.log"
LOG_DIR = os.path.dirname(COMPILE_LOG_PATH)

# Path ไปยัง venv Python (ถ้าจำเป็น, แต่ Watcher ไม่ต้องใช้ venv ก็ได้)
# venv_python = "/home/hp/Downloads/bot/venv/bin/python" 

print(f"Compiler Watcher started... Watching for {TRIGGER_FILE}")

def log(message):
    """Helper to log to console and file."""
    print(message)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(COMPILE_LOG_PATH, 'a') as f:
            f.write(f"[{datetime.now()}] {message}\n")
    except Exception as e:
        print(f"Failed to write to log file: {e}")

# --- ลูปหลัก ---
while True:
    if os.path.exists(TRIGGER_FILE):
        log("Trigger file detected. Starting compile...")

        # 1. ลบ Trigger ทิ้งทันที
        try:
            os.remove(TRIGGER_FILE)
        except Exception as e:
            log(f"Error removing trigger file: {e}")

        # 2. เตรียม Environment (เพื่อความชัวร์)
        env = os.environ.copy()
        env['WINEPREFIX'] = WINEPREFIX_PATH
        env['DISPLAY'] = ':0' 

        compile_command = [
            "wine", 
            METAEDITOR_PATH, 
            f'/compile:"{EA_PATH_WINDOWS}"'
        ]

        # 3. รันคำสั่ง Compile
        try:
            log(f"Executing: {' '.join(compile_command)}")
            # ใช้ .run (แบบรอให้เสร็จ) เพราะนี่คือ Background script
            result = subprocess.run(compile_command, env=env, capture_output=True, text=True, timeout=30)

            log("Compile process finished.")
            log(f"STDOUT: {result.stdout}")
            log(f"STDERR: {result.stderr}")

        except subprocess.TimeoutExpired:
            log("Compile command timed out.")
        except Exception as e:
            log(f"Compile subprocess failed: {e}")

    # ตรวจสอบทุก 3 วินาที
    time.sleep(3)