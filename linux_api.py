import os
import sys
import inspect
import pickle
import json
import traceback
import numpy as np
import pandas as pd
import os
import sys
import inspect
import pickle
import json
import traceback
import numpy as np
import pandas as pd
import requests 
from flask import Flask, request, jsonify
from tensorflow.keras.models import load_model
import warnings
import subprocess
import sqlite3
import threading 
import time      
import pytz
from datetime import datetime, timedelta 
from bs4 import BeautifulSoup 
from playwright.sync_api import sync_playwright
import talib

# Suppress TensorFlow and other library warnings
warnings.filterwarnings("ignore")
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0' 

# --- Path Setup for External Modules ---
try:
    current_dir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
    root_dir = os.path.dirname(current_dir)
    if root_dir not in sys.path:
        sys.path.append(root_dir)
    
    # [v7.0] Import from linux_model.py
    from deploy.linux_model import compute_features_lite, scale_features, REQUIRED_FEATURES
    
    print("✅ External model functions (18 Features - v7.0) loaded successfully.")
except ImportError as e:
    print(f"❌ FATAL: Cannot import from linux_model.py. Error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ FATAL: An unexpected error occurred during import: {e}")
    sys.exit(1)

# --- Configuration ---

class Config:
    # Path Config for LITE Model
    MODEL_PATH = 'models/model.h5'
    SCALER_PATH = 'models/scaler.pkl'

    SEQUENCE_LENGTH = 50
    PREDICTION_THRESHOLD = 0.75 
    NEWS_LOCKDOWN_MINUTES = 30
    MIN_ATR = 1.0       # ใส่ค่าตาม run_lite
    USE_EMA_FILTER = True

app = Flask(__name__)

# Global Variables
lite_model = None
scaler = None

REQUIRED_FEATURES = [
    'log_ret_1', 'log_ret_5', 'dist_ema50', 'dist_h1_ema',
    'body_pct', 'upper_wick_pct', 'lower_wick_pct',
    'vol_force',
    'dist_pivot', 'dist_r1', 'dist_s1',
    'atr_14', 'atr_pct', 'rsi_14',
    'hour_sin', 'hour_cos',
    'usd_ret_5', 'usd_corr'
]

account_status = {
    'bot_status': 'STOPPED', 'balance': 0.0, 'equity': 0.0,
    'margin_free': 0.0, 'open_trades': 0, 'last_signal': 'NONE',
    'last_regime': 'ACTIVE' 
}

news_lockdown = {'active': False, 'message': 'News filter starting...'}
news_lock = threading.Lock() 

# --- News Filter Functions (Playwright) ---
def fetch_html_with_playwright(url):
    """
    ใช้ Playwright เพื่อเปิดเว็บ, รอข้อมูลโหลด, แล้วคืนค่า HTML
    """
    html_content = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True) 
            page = browser.new_page()
            
            print(f"NEWS: Playwright accessing {url}...")
            page.goto(url, timeout=20000, wait_until='domcontentloaded')
            
            # (รอ 5 วินาทีให้ JS โหลดข้อมูลข่าว)
            print("NEWS: Waiting 5 seconds for dynamic content...")
            time.sleep(5) 

            html_content = page.content()
            browser.close()
            print("NEWS: Playwright successfully fetched HTML.")
            
    except Exception as e:
        print(f"NEWS: Playwright Error: {e}")
        if 'Target page, context or browser has been closed' in str(e):
             print("NEWS: (INFO) Browser closed as expected.")
        
    return html_content

def fetch_ff_news():
    """
    ดึงปฏิทินข่าวจาก FXVerify ด้วย Playwright
    """
    global news_lockdown
    
    url = "https://fxverify.com/tools/economic-calendar#popout" 
    
    try:
        # 1. ⬅️ ใช้ Playwright ดึง HTML ที่โหลดแล้ว
        html_text = fetch_html_with_playwright(url)
        
        if not html_text:
            raise Exception("Playwright failed to fetch HTML (content is None).")

        soup = BeautifulSoup(html_text, 'html.parser')
        
        # 2. ค้นหาตารางหลัก
        # (จาก HTML: <tbody id="eventDate_table_body">)
        table_body = soup.find('tbody', id='eventDate_table_body')
        if not table_body:
            print("NEWS: Could not find table body 'eventDate_table_body'. Page structure may have changed.")
            raise Exception("Scraper failed: table body not found.")
        
        # 3. ค้นหา "แถว" ของข่าวทั้งหมด
        # (จาก HTML: <tr ... class="ec-fx-table-event-row" ...>)
        rows = table_body.find_all('tr', class_='ec-fx-table-event-row')
        
        found_event = None
        now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)

        if not rows:
            print("NEWS: No event rows found with class 'ec-fx-table-event-row'.")

        for row in rows:
            
            # 4. หา Impact (ข่าวแดง)
            # (จาก HTML: <div class="row ec-fx-impact high" ...>)
            impact_div = row.find('div', class_='ec-fx-impact')
            if not impact_div or 'high' not in impact_div.get('class', []):
                continue # ข้าม ถ้าไม่ใช่ข่าวแดง

            # 5. หา สกุลเงิน
            # (เราจะหาช่อง <td> ที่อยู่ "ก่อนหน้า" ช่อง <td> ของ impact)
            currency_cell = impact_div.find_parent('td').find_previous_sibling('td')
            if not currency_cell:
                continue
                
            currency_tag = currency_cell.find('div')
            if not currency_tag or currency_tag.text.strip() != 'USD':
                continue # ข้าม ถ้าไม่ใช่ USD
                
            # 6. หาเวลา (ง่ายที่สุด)
            # (จาก HTML: <tr ... time="1763337000" ...>)
            timestamp_str = row.get('time')
            if not timestamp_str:
                continue
            
            try:
                # (แปลง Unix timestamp (ซึ่งเป็น UTC อยู่แล้ว)
                event_dt_utc = datetime.fromtimestamp(int(timestamp_str), tz=pytz.UTC)
                
                # 7. (เหมือนเดิม) คำนวณ Lockdown
                lockdown_start = event_dt_utc - timedelta(minutes=Config.NEWS_LOCKDOWN_MINUTES)
                lockdown_end = event_dt_utc + timedelta(minutes=Config.NEWS_LOCKDOWN_MINUTES)

                if lockdown_start <= now_utc <= lockdown_end:
                    # 8. หาชื่อข่าว
                    # (จาก HTML: <a class="event-name" ...>)
                    event_name_tag = row.find('a', class_='event-name')
                    found_event = event_name_tag.text.strip() if event_name_tag else "High Impact Event"
                    break # เจอข่าวแล้ว หยุดค้นหา

            except ValueError:
                print(f"NEWS: Could not parse timestamp '{timestamp_str}'")
                continue 

        # --- 🛑 [จบส่วนที่แก้ไข] ---
        
        # (อัปเดตสถานะ)
        with news_lock:
            if found_event:
                news_lockdown['active'] = True
                news_lockdown['message'] = f"LOCKDOWN: {found_event}"
            else:
                news_lockdown['active'] = False
                news_lockdown['message'] = 'No high-impact USD news.'
        
        print(f"NEWS: {news_lockdown['message']}")

    except Exception as e:
        print(f"NEWS: Error fetching FXVerify (Playwright): {e}")
        traceback.print_exc()
        with news_lock:
            news_lockdown = {'active': False, 'message': 'Error fetching news.'}

def run_news_scheduler():
    fetch_ff_news() # (รันครั้งแรก)
    while True:
        time.sleep(3600) # (รอ 1 ชั่วโมง)
        fetch_ff_news()

# --- Download Function (Placeholder URLs) ---
def download_model_assets():
    """
    Download Lite model/scaler from GitHub.
    """
    GITHUB_FILES = {
        'lite_model': {
            'url': 'https://raw.githubusercontent.com/bookhub10/models/main/models/model.h5', 
            'filename': Config.MODEL_PATH
        },
        'scaler': {
            'url': 'https://raw.githubusercontent.com/bookhub10/models/main/models/scaler.pkl', 
            'filename': Config.SCALER_PATH
        }
    }

    os.makedirs(os.path.dirname(Config.MODEL_PATH), exist_ok=True)

    for file_info in GITHUB_FILES.values():
        url = file_info['url']
        output_path = file_info['filename']

        try:
            print(f"⬇️ Downloading {output_path} from GitHub...")
            response = requests.get(url)
            response.raise_for_status()

            with open(output_path, 'wb') as f:
                f.write(response.content)

            print(f"✅ Downloaded: {output_path}")
        except Exception as e:
            print(f"❌ Failed to download {output_path}: {e}")
            raise

# --- Download Python Files from GitHub ---
def download_python_files():
    """Download the main Python scripts from GitHub."""
    GITHUB_PYTHON_FILES = {
        'linux_api': {
            'url': 'https://raw.githubusercontent.com/bookhub10/models/main/linux_api.py',
            'filename': 'linux_api.py'
        },
        'linux_telegram': {
            'url': 'https://raw.githubusercontent.com/bookhub10/models/main/linux_telegram.py',
            'filename': 'linux_telegram.py'
        },
        'linux_model': { 
            'url': 'https://raw.githubusercontent.com/bookhub10/models/main/linux_model.py',
            'filename': 'linux_model.py'
        }
    }

    success = True
    for file_info in GITHUB_PYTHON_FILES.values():
        url = file_info['url']
        output_path = file_info['filename']

        try:
            print(f"⬇️ Downloading {output_path} from GitHub...")
            response = requests.get(url)
            response.raise_for_status()

            with open(output_path, 'wb') as f:
                f.write(response.content)

            print(f"✅ Downloaded: {output_path}")
        except Exception as e:
            print(f"❌ Failed to download {output_path}: {e}")
            success = False
    return success

# --- Asset Management ---

def load_assets():
    """Load the Single Lite Model and Scaler."""
    global lite_model, scaler
    print("--- Attempting to load LITE Model System ---")
    try:
        lite_model = load_model(Config.MODEL_PATH)
        print(f"✅ Loaded Lite Model: {Config.MODEL_PATH}")
        
        with open(Config.SCALER_PATH, 'rb') as f:
            scaler = pickle.load(f)
        print(f"✅ Loaded Scaler: {Config.SCALER_PATH}")
        
        if hasattr(scaler, 'n_features_in_'):
            print(f"DEBUG: Scaler expects {scaler.n_features_in_} features.")
            if scaler.n_features_in_ != len(REQUIRED_FEATURES):
                print(f"⚠️ WARNING: Scaler/Config mismatch. Scaler needs {scaler.n_features_in_}, Config has {len(REQUIRED_FEATURES)}")

        print("✅ All v7 assets loaded successfully.")
        return True
    except FileNotFoundError as e:
        print(f"❌ Error: Model or Scaler file not found. {e}")
    except Exception as e:
        print(f"❌ Critical Error loading assets: {e}")
        traceback.print_exc()

    lite_model = None
    scaler = None
    return False

# --- JSON Parsing Helper ---
def parse_mql_json(req):
    """Helper to safely parse JSON from MQL5."""
    if req.data:
        try:
            raw_data = req.data.decode('utf-8', errors='ignore').strip('\x00').strip()
            return json.loads(raw_data)
        except json.JSONDecodeError as e:
            print(f"❌ JSON Decode Error: {e}")
            return None
    return None

def preprocess_and_predict(raw_data):
    """
    Lite Logic:
    1. Parse M5 & USD
    2. Compute 18 Features
    3. Scale & Predict (Single Model)
    """
    global lite_model, scaler
    
    try:
        # 1. Parse Data
        df_m5 = pd.DataFrame(raw_data['m5_data'])
        
        # USD Data Handling (Fail-safe)
        usd_raw = raw_data.get('usd_m5', [])
        df_usd = pd.DataFrame(usd_raw) if usd_raw else None
        
        if df_m5.empty: raise ValueError("Empty XAUUSD data")

        # Convert Time
        df_m5['time'] = pd.to_datetime(df_m5['time'], unit='s')
        df_m5.set_index('time', inplace=True)
        
        if df_usd is not None and not df_usd.empty:
            df_usd['time'] = pd.to_datetime(df_usd['time'], unit='s')
            df_usd.set_index('time', inplace=True)

        # 2. Compute Features (ใช้ฟังก์ชันจาก linux_model_lite)
        df_features = compute_features_lite(df_m5, df_usd=df_usd)
        
        # Check Length
        if len(df_features) < Config.SEQUENCE_LENGTH:
            raise ValueError(f"Not enough data: {len(df_features)}/{Config.SEQUENCE_LENGTH}")
            
        # 3. Prepare for Scaling
        # เลือกข้อมูลล่าสุดเท่ากับ Seq Length
        df_input = df_features.iloc[-Config.SEQUENCE_LENGTH:].copy()
        
        latest_atr = df_input['atr_14'].iloc[-1]

        # Feature Validation
        final_features = [col for col in REQUIRED_FEATURES if col in df_input.columns]
        if len(final_features) != len(REQUIRED_FEATURES):
            missing = set(REQUIRED_FEATURES) - set(final_features)
            # Fallback: ถ้าขาด Feature ใหม่ (เช่น USD ไม่มีข้อมูล) ให้เติม 0 เพื่อไม่ให้ระบบล่ม
            print(f"⚠️ Warning: Missing features {missing}. Filling with 0.")
            for col in missing:
                df_input[col] = 0.0
            df_input = df_input[REQUIRED_FEATURES] # Reorder
        
        # Scale (ใช้ฟังก์ชันจาก linux_model)
        X_scaled = scale_features(df_input, scaler)
        
        if X_scaled is None: raise ValueError("Scaling returned None")
        
        # Reshape for LSTM (1, 50, 18)
        X_pred = np.array([X_scaled])

        # 4. Predict
        probs = lite_model.predict(X_pred, verbose=0)[0]
        cls = np.argmax(probs)
        probability = np.max(probs)
        
        signal = 'NONE'
        if cls == 1: signal = 'BUY'
        elif cls == 2: signal = 'SELL'
        elif cls == 0: signal = 'HOLD'
        
        # A. ดึงราคาปัจจุบัน
        current_close = df_m5['close'].iloc[-1]
        
        # B. คำนวณ EMA 200 (ใช้ TA-Lib)
        # ต้องคำนวณจากข้อมูล M5 ที่ส่งมา
        real_close = df_m5['close'].astype(float).values
        ema200 = talib.EMA(real_close, timeperiod=200)[-1] # เอาค่าล่าสุด
        
        # C. กรอง Trend (EMA 200 Filter)
        if Config.USE_EMA_FILTER and not np.isnan(ema200):
            if signal == 'BUY' and current_close < ema200:
                print(f"filter: Blocked BUY (Price {current_close:.2f} < EMA {ema200:.2f})")
                signal = 'HOLD' # เปลี่ยนเป็น Hold
            elif signal == 'SELL' and current_close > ema200:
                print(f"filter: Blocked SELL (Price {current_close:.2f} > EMA {ema200:.2f})")
                signal = 'HOLD'

        # D. กรอง Min ATR
        if latest_atr < Config.MIN_ATR:
            print(f"filter: Low Volatility (ATR {latest_atr:.4f} < {Config.MIN_ATR})")
            signal = 'HOLD'

        return signal, probability, latest_atr, "ACTIVE"

    except Exception as e:
        raise ValueError(f"Preprocessing Error: {e}")
    
# --- [Dynamic Risk Manager] ---
def calculate_dynamic_risk(probability):
    if probability > 0.90: # ⬅️ (ต้องปรับใหม่)
        return 2.0  
    elif probability > 0.85: # ⬅️ (ต้องปรับใหม่)
        return 1.5  
    elif probability > Config.PREDICTION_THRESHOLD: 
        return 1.0  
    else:
        return 0.5 

# --- API Endpoints ---

@app.route('/status', methods=['GET']) 
def get_status():
    global account_status, lite_model, scaler

    current = account_status.copy()
    current['model_loaded'] = (lite_model is not None)
    current['scaler_loaded'] = (scaler is not None)
    with news_lock: current['news_status'] = news_lockdown['message']
    return jsonify(current), 200

@app.route('/predict', methods=['POST']) 
def predict_signal():
    # 1. News Check
    with news_lock:
        if news_lockdown['active']:
            return jsonify({
                'signal': 'HOLD', 'probability': 0.0, 'atr': 0.0,
                'dynamic_risk': 0.0, 'regime': 'NEWS_LOCKDOWN', 'message': news_lockdown['message']
            }), 200

    # 2. Bot Status Check
    if lite_model is None: return jsonify({'signal': 'ERROR', 'message': 'Model not loaded'}), 503
    if account_status['bot_status'] != 'RUNNING':
        return jsonify({'signal': 'NONE', 'message': 'Bot STOPPED'}), 200

    # 3. Process
    try:
        data = parse_mql_json(request)
        if not data: return jsonify({'signal': 'ERROR', 'probability': 0.0, 'message': 'Invalid JSON data received.'}), 400
        
        signal, prob, atr, regime = preprocess_and_predict(data)
        
        # Threshold Check
        dynamic_risk_pct = 0.5 

        if prob < Config.PREDICTION_THRESHOLD: 
            signal = 'HOLD' 
        else:
            dynamic_risk_pct = calculate_dynamic_risk(prob)
        
        return jsonify({
            'signal': signal,
            'probability': float(prob),
            'atr': float(atr),
            'dynamic_risk': float(dynamic_risk_pct),
            'regime': regime,
            'message': 'Prediction successful.'
        }), 200
        
    except Exception as e:
        print(f"❌ Predict Error: {e}")
        traceback.print_exc()
        return jsonify({'signal': 'ERROR', 'message': str(e)}), 500

@app.route('/update_status', methods=['POST'])
def update_status():
    try:
        data = parse_mql_json(request)
        if data:
            account_status.update({
                'balance': data.get('balance', 0),
                'equity': data.get('equity', 0),
                'margin_free': data.get('margin_free', 0),
                'open_trades': data.get('open_trades', 0)
            })

        return jsonify({'status': 'SUCCESS'})
    except: return jsonify({'status': 'ERROR'}), 500

@app.route('/command', methods=['POST'])
def execute_command():
    """Endpoint for Telegram Bot or external system to send START/STOP commands."""
    try:
        command = request.json.get('command')
        
        if command == 'START':
            account_status['bot_status'] = 'RUNNING'
            return jsonify({'status': 'SUCCESS', 'message': 'Bot set to RUNNING.'})
        
        elif command == 'STOP':
            account_status['bot_status'] = 'STOPPED'
            return jsonify({'status': 'SUCCESS', 'message': 'Bot set to STOPPED.'})
        
        else:
            return jsonify({'status': 'FAIL', 'message': 'Invalid command.'}), 400

    except Exception as e:
        return jsonify({'status': 'ERROR', 'message': str(e)}), 500

@app.route('/retrain', methods=['POST'])
def retrain_model_async():
    if account_status['bot_status'] != 'STOPPED':
        return jsonify({'status': 'FAIL', 'message': '❌ This command requires the bot to be STOPPED.'}), 400

    try:
        download_model_assets() 
        if load_assets():
            return jsonify({'status': 'SUCCESS', 'message': '✅ Retraining completed and model (v7) loaded.'}), 200
        else:
            return jsonify({'status': 'FAIL', 'message': '⚠️ Model (v7) or scaler could not be loaded after download.'}), 500

    except Exception as e:
        print(f"❌ Error in retrain_model_async: {e}")
        traceback.print_exc()
        return jsonify({'status': 'FAIL', 'message': f'Error during retraining: {str(e)}'}), 500

@app.route('/update_ea', methods=['POST'])
def update_expert_advisor():
    """
    [NEW VERSION] Downloads the EA and creates a trigger file.
    """
    EA_URL = 'https://raw.githubusercontent.com/bookhub10/models/main/linux_OBot.mq5' 
    EA_PATH = "/home/hp/.mt5/drive_c/Program Files/MetaTrader 5/MQL5/Experts/OBotTrading.mq5"
    TRIGGER_FILE = "/home/hp/Downloads/bot/COMPILE_NOW.trigger" 

    try:
        print(f"⬇️ Downloading new EA from {EA_URL}...")
        response = requests.get(EA_URL)
        response.raise_for_status()
        with open(EA_PATH, 'wb') as f:
            f.write(response.content)
        print("✅ EA Downloaded.")

        with open(TRIGGER_FILE, 'w') as f:
            f.write('triggered') 
        print(f"✅ Trigger file created at {TRIGGER_FILE}")

        return jsonify({
            'status': 'SUCCESS', 
            'message': f'✅ EA Downloaded. Compile trigger issued to GUI watcher.'
        }), 200

    except Exception as e:
        print(f"❌ Error in /update_ea: {e}")
        traceback.print_exc()
        return jsonify({'status': 'FAIL', 'message': f'Error during EA update: {str(e)}'}), 500
    
@app.route('/restart', methods=['POST'])
def restart_service():
    """Endpoint to restart the service via systemd."""
    try:
        command = ["sudo", "/bin/systemctl", "restart", "obot_api.service"]
        command2 = ["sudo", "/bin/systemctl", "restart", "obot_telegram.service"]
        command3 = ["sudo", "/bin/systemctl", "restart", "obot_mt5.service"]

        subprocess.run(command3)
        subprocess.run(command2)
        subprocess.run(command)
        
        return jsonify({'status': 'SUCCESS', 'message': 'The service restart command issued.'}), 200
    except Exception as e:
        print(f"❌ Error in /restart: {e}")
        return jsonify({'status': 'FAIL', 'message': str(e)}), 500

@app.route('/fix', methods=['POST'])
def fix_system_files():
    """Downloads updated Python scripts and reloads model assets."""
    python_downloaded = download_python_files()

    try:
        download_model_assets()
    except Exception as e:
        return jsonify({'status': 'FAIL', 'message': f'❌ Failed to download model assets: {str(e)}. Python files may be updated.'}), 500
        
    assets_loaded = load_assets()
    
    message = "✅ System files and assets (v7) updated successfully."
    
    if not python_downloaded:
        message = "⚠️ Python files update failed for one or more files. Assets (v7) reloaded."

    if not assets_loaded:
        return jsonify({'status': 'FAIL', 'message': '⚠️ Assets (v7) downloaded but failed to load. System files updated. **Please manually restart.**'}), 500

    return jsonify({
        'status': 'SUCCESS', 
        'message': f'{message} **Requires Server Restart** for new Python files to take effect.'
    }), 200

if __name__ == '__main__':
    if load_assets():
        print("Starting background news scheduler (Playwright + FXVerify Scraper)...") # ⬅️ [แก้ไข]
        scheduler_thread = threading.Thread(target=run_news_scheduler, daemon=True)
        scheduler_thread.start()

        print("💡 NOTE: Remember to start the separate telegram_bot.py script.")
        app.run(host='0.0.0.0', port=5000)
    else:

        print("❌ FATAL: Could not load v7 model/scaler. API not starting.")
