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

# Suppress TensorFlow and other library warnings
warnings.filterwarnings("ignore")
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0' 

# --- Path Setup for External Modules ---
try:
    current_dir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
    root_dir = os.path.dirname(current_dir)
    if root_dir not in sys.path:
        sys.path.append(root_dir)
    
    from linux_model import add_technical_indicators, scale_features
    
    print("✅ External model functions (19 Features - v6.1) loaded successfully.")
except ImportError as e:
    print(f"❌ FATAL: Cannot import from linux_model.py. Error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ FATAL: An unexpected error occurred during import: {e}")
    sys.exit(1)

# --- [v6] อัปเดต Configuration & Global State ---

class Config:
    # ⬇️ [ใหม่] อัปเดต Path ทั้งหมด
    V6_MODEL_PATH = 'models/v6_stable_backtest.h5'
    TREND_MODEL_PATH = 'models/v6_trend_detector.h5'
    SIDEWAY_MODEL_PATH = 'models/v6_sideway_model.h5'
    SCALER_PATH = 'models/scaler_v6.pkl'
    # ⬆️ [ใหม่]
    
    SEQUENCE_LENGTH = 120
    PREDICTION_THRESHOLD = 0.45 
    DB_PATH = 'obot_history.db'
    NEWS_LOCKDOWN_MINUTES = 30

app = Flask(__name__)

# ⬇️ [ใหม่] เราต้องการ 3 โมเดล + 1 Scaler
v6_model = None
trend_model = None
sideway_model = None
scaler = None

# --- [v6.1] รายชื่อคุณลักษณะ (19 Features) ---
REQUIRED_FEATURES = [
    'high', 'low', 'close', 'tick_volume',
    'ATR_14', 'Stoch_K', 'MACD_Hist', 'ADX_14',
    'M30_RSI', 'H1_Dist_MA200','H4_Dist_MA50',
    'ret_1', 'ret_5', 'ret_10', 'vol_rolling',
    'hour', 'dow',
    'k_upper', 'k_lower'
]

account_status = {
    'bot_status': 'STOPPED', 'balance': 0.0, 'equity': 0.0,
    'margin_free': 0.0, 'open_trades': 0, 'last_signal': 'NONE',
    'last_regime': 'NONE' # ⬅️ [ใหม่]
}

news_lockdown = {'active': False, 'message': 'News filter starting...'}
news_lock = threading.Lock() 

# --- 🛑 [v6] News Filter Functions (แก้ไขสำหรับ Playwright + FXVerify) ---

def fetch_html_with_playwright(url):
    """
    [v6 ใหม่] ใช้ Playwright เพื่อเปิดเว็บ, รอข้อมูลโหลด, แล้วคืนค่า HTML
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
    [v6 อัปเดต] ดึงปฏิทินข่าวจาก FXVerify
    (ใช้ Selectors ที่ถูกต้องจากไฟล์ HTML ที่คุณให้มา)
    """
    global news_lockdown
    
    url = "https://fxverify.com/tools/economic-calendar#popout" 
    
    try:
        # 1. ⬅️ [v6 ใหม่] ใช้ Playwright ดึง HTML ที่โหลดแล้ว
        html_text = fetch_html_with_playwright(url)
        
        if not html_text:
            raise Exception("Playwright failed to fetch HTML (content is None).")

        soup = BeautifulSoup(html_text, 'html.parser')
        
        # --- 🛑 [แก้ไข] ใช้ Selectors ที่ถูกต้อง ---
        
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
    """
    (เหมือนเดิม) รัน fetch_ff_news() ทุก 1 ชั่วโมง
    (ตอนนี้จะเรียกใช้เวอร์ชัน Playwright ที่แก้ไขแล้ว)
    """
    fetch_ff_news() # (รันครั้งแรก)
    while True:
        time.sleep(3600) # (รอ 1 ชั่วโมง)
        fetch_ff_news()

# --- Download Model & Scaler from GitHub --- 
def download_model_assets():
    """Download model and scaler from GitHub."""
    GITHUB_FILES = {
        'v6_model': {
            'url': 'https://raw.githubusercontent.com/bookhub10/models/main/models/v6_stable_backtest.h5', 
            'filename': Config.V6_MODEL_PATH
        },
        'trend_model': {
            'url': 'https://raw.githubusercontent.com/bookhub10/models/main/models/v6_trend_detector.h5', 
            'filename': Config.TREND_MODEL_PATH
        },
        'sideway_model': {
            'url': 'https://raw.githubusercontent.com/bookhub10/models/main/models/v6_sideway_model.h5', 
            'filename': Config.SIDEWAY_MODEL_PATH
        },
        'scaler': {
            'url': 'https://raw.githubusercontent.com/bookhub10/models/main/models/scaler_v6.pkl', 
            'filename': Config.SCALER_PATH
        }
    }

    os.makedirs(os.path.dirname(Config.V6_MODEL_PATH), exist_ok=True)

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

# --- [ Database Functions (ไม่เปลี่ยนแปลง) ] ---
def init_db():
    """สร้างตารางฐานข้อมูลถ้ายังไม่มี"""
    print(f"Initializing database at {Config.DB_PATH}...")
    try:
        conn = sqlite3.connect(Config.DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS account_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            balance REAL, equity REAL, margin_free REAL, open_trades INTEGER
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            signal TEXT, probability REAL, atr REAL, dynamic_risk REAL
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            event_message TEXT
        )
        ''')
        
        conn.commit()
        print("✅ Database tables initialized successfully.")
    except Exception as e:
        print(f"❌ FATAL: Failed to initialize database: {e}")
    finally:
        if conn:
            conn.close()

def log_to_db(query, params=()):
    """ฟังก์ชันช่วยสำหรับ INSERT ข้อมูลลง DB (ป้องกัน DB locked)"""
    try:
        conn = sqlite3.connect(Config.DB_PATH, timeout=10) 
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
    except Exception as e:
        print(f"❌ DB Log Error: {e}")
    finally:
        if conn:
            conn.close()

# --- [ใหม่] Asset Management (โหลด 3 โมเดล) ---

def load_assets():
    """Load the Keras H5 models and MinMaxScaler."""
    global v6_model, trend_model, sideway_model, scaler
    print("--- Attempting to load v6 Multi-Model System ---")
    try:
        v6_model = load_model(Config.V6_MODEL_PATH)
        print(f"✅ Loaded Main Model: {Config.V6_MODEL_PATH}")
        
        trend_model = load_model(Config.TREND_MODEL_PATH)
        print(f"✅ Loaded Trend Detector: {Config.TREND_MODEL_PATH}")
        
        sideway_model = load_model(Config.SIDEWAY_MODEL_PATH)
        print(f"✅ Loaded Sideway Model: {Config.SIDEWAY_MODEL_PATH}")
        
        with open(Config.SCALER_PATH, 'rb') as f:
            scaler = pickle.load(f)
        print(f"✅ Loaded Scaler: {Config.SCALER_PATH}")
        
        if hasattr(scaler, 'n_features_in_'):
            print(f"DEBUG: Scaler expects {scaler.n_features_in_} features.")
            if scaler.n_features_in_ != len(REQUIRED_FEATURES):
                print(f"⚠️ WARNING: Scaler/Config mismatch. Scaler needs {scaler.n_features_in_}, Config has {len(REQUIRED_FEATURES)}")

        print("✅ All v6 assets loaded successfully.")
        return True
        
    except FileNotFoundError as e:
        print(f"❌ Error: Model or Scaler file not found. {e}")
    except Exception as e:
        print(f"❌ Critical Error loading assets: {e}")
        traceback.print_exc()
    
    v6_model = None
    trend_model = None
    sideway_model = None
    scaler = None
    return False

# --- JSON Parsing Helper (ไม่เปลี่ยนแปลง) ---
def parse_mql_json(req):
    """Helper to safely parse JSON from MQL5 (which might contain trailing NULs)."""
    if req.data:
        try:
            raw_data = req.data.decode('utf-8', errors='ignore').strip('\x00').strip()
            return json.loads(raw_data)
        except json.JSONDecodeError as e:
            print(f"❌ JSON Decode Error: {e}")
            print(f"Raw data snippet (first 200 chars): {raw_data[:200]}")
            return None
    return None

# --- [ใหม่] Core Prediction Logic (v6 Regime-Switching) ---

def preprocess_and_predict(raw_data):
    """
    v6 (แก้ไข): 
    1. ใช้ Trend Detector ตัดสินใจ
    2. ถ้า Trend -> ใช้ v6_model
    3. ถ้า Sideway -> ใช้ sideway_model
    """
    global v6_model, trend_model, sideway_model, scaler
    
    # ... (ส่วนการ Parse MQL JSON เหมือนเดิม) ...
    try:
        df_m5 = pd.DataFrame(raw_data['m5_data'])
        df_m30 = pd.DataFrame(raw_data['m30_data'])
        df_h1 = pd.DataFrame(raw_data['h1_data'])
        df_h4 = pd.DataFrame(raw_data['h4_data'])
        
        if df_m5.empty or df_m30.empty or df_h1.empty or df_h4.empty:
            raise ValueError(f"One or more timeframes returned empty data.")

        df_m5['time'] = pd.to_datetime(df_m5['time'], unit='s'); df_m5.set_index('time', inplace=True)
        df_m30['time'] = pd.to_datetime(df_m30['time'], unit='s'); df_m30.set_index('time', inplace=True); df_m30 = df_m30[['close']] 
        df_h1['time'] = pd.to_datetime(df_h1['time'], unit='s'); df_h1.set_index('time', inplace=True); df_h1 = df_h1[['close']] 
        df_h4['time'] = pd.to_datetime(df_h4['time'], unit='s'); df_h4.set_index('time', inplace=True); df_h4 = df_h4[['close']]

    except Exception as e:
        raise ValueError(f"Failed to parse Multi-Timeframe data. Error: {e}")

    # 1. คำนวณ 19 ฟีเจอร์ (จาก linux_model_11.py)
    df_features = add_technical_indicators(
        df_m5, df_m30, df_h1, 
        df_h4
    )
    
    if len(df_features) < Config.SEQUENCE_LENGTH:
        raise ValueError(f"Not enough valid bars ({len(df_features)}), expected {Config.SEQUENCE_LENGTH}.")
    
    latest_atr = df_features['ATR_14'].iloc[-1]
    df_for_scaling = df_features.iloc[-Config.SEQUENCE_LENGTH:].copy() 
    
    # 2. ตรวจสอบ 19 ฟีเจอร์
    final_features = [col for col in REQUIRED_FEATURES if col in df_for_scaling.columns]
    if len(final_features) != len(REQUIRED_FEATURES):
        missing = set(REQUIRED_FEATURES) - set(final_features)
        raise ValueError(f"Feature Mismatch: Expected {len(REQUIRED_FEATURES)} features (v6). Missing: {missing}")
    
    df_for_scaling_trimmed = df_for_scaling[final_features]
    
    # 3. Scale ข้อมูล
    try:
        _, test_scaled, _ = scale_features(
            df_for_scaling_trimmed, test_df=None, scaler=scaler 
        )
    except Exception as e:
        raise ValueError(f"Scaling failed (check feature count: {len(df_for_scaling_trimmed.columns)}). Error: {e}")

    X_pred_data = test_scaled.values 
    X_pred = np.array([X_pred_data]) 
    
    # --- 4. 🧠 ตรรกะ Regime-Switching ---
    
    # 4.1 รัน Trend Detector (โมเดล Gatekeeper)
    # (Trend Detector เป็น binary sigmoid )
    trend_prob = trend_model.predict(X_pred, verbose=0)[0][0] 
    print(f"DEBUG: Trend Probability: {trend_prob:.4f}")
    signal = 'NONE'
    probability = 0.0
    regime = 'NONE'

    if trend_prob >= 0.50:
        # 4.2 ตลาดเป็น Trend -> ใช้ v6 (Trend-Following)
        regime = "TREND"
        prediction_array = v6_model.predict(X_pred, verbose=0)[0]
        predicted_class = np.argmax(prediction_array)
        probability = np.max(prediction_array)
        
        if predicted_class == 1: signal = 'BUY'
        elif predicted_class == 2: signal = 'SELL'
        elif predicted_class == 0: signal = 'HOLD'
        
    else:
        # 4.3 ตลาดเป็น Sideway -> ใช้ Sideway (Reversion)
        regime = "SIDEWAY"
        prediction_array = sideway_model.predict(X_pred, verbose=0)[0]
        predicted_class = np.argmax(prediction_array)
        probability = np.max(prediction_array)
        
        # (อ้างอิงจาก label_sideway_reversion )
        if predicted_class == 1: signal = 'BUY' # (Reversion Long)
        elif predicted_class == 2: signal = 'SELL' # (Reversion Short)
        elif predicted_class == 0: signal = 'HOLD'

    account_status['last_signal'] = signal
    account_status['last_regime'] = regime # ⬅️ [ใหม่]
    
    # คืนค่า regime กลับไปด้วย
    return signal, probability, latest_atr, regime

# --- [ v6 Dynamic Risk Manager (ต้องปรับค่า) ] ---
def calculate_dynamic_risk(probability):
    if probability > 0.85: # ⬅️ (ต้องปรับใหม่)
        return 2.0  
    elif probability > 0.65: # ⬅️ (ต้องปรับใหม่)
        return 1.5  
    elif probability > Config.PREDICTION_THRESHOLD: 
        return 1.0  
    else:
        return 0.5 

# --- API Endpoints ---

@app.route('/status', methods=['GET']) 
def get_status():
    """Endpoint for MT5 EA to check the bot's current status and performance."""
    global account_status, v6_model, trend_model, sideway_model, scaler # ⬅️ อัปเดตตัวแปร
    try:
        current_status = account_status.copy()
        
        # ⬇️ [ใหม่] รายงานสถานะของทุกโมเดล
        current_status['v6_model_loaded'] = (v6_model is not None)
        current_status['trend_model_loaded'] = (trend_model is not None)
        current_status['sideway_model_loaded'] = (sideway_model is not None)
        current_status['scaler_loaded'] = (scaler is not None)
        
        with news_lock:
            current_status['news_status'] = news_lockdown['message']

        return jsonify(current_status), 200
    except Exception as e:
        print(f"❌ Error fetching status: {e}")
        return jsonify({'bot_status': 'ERROR', 'message': f'Server internal error: {str(e)}'}), 500

@app.route('/predict', methods=['POST']) 
def predict_signal():
    with news_lock:
        if news_lockdown['active']:
            return jsonify({
                'signal': 'HOLD', 'probability': 0.0, 'atr': 0.0,
                'dynamic_risk': 0.0, 'regime': 'NEWS_LOCKDOWN', 'message': news_lockdown['message']
            }), 200

    if v6_model is None or trend_model is None or sideway_model is None or scaler is None:
        return jsonify({'signal': 'ERROR', 'probability': 0.0, 'message': 'One or more models not loaded.'}), 503
    if account_status['bot_status'] != 'RUNNING':
        return jsonify({'signal': 'NONE', 'probability': 0.0, 'message': f"Bot is {account_status['bot_status']}."}), 200

    try:
        data = parse_mql_json(request)
        if data is None:
            return jsonify({'signal': 'ERROR', 'probability': 0.0, 'message': 'Invalid JSON data received.'}), 400
        
        # ⬇️ [ใหม่] รับ regime กลับมาด้วย
        signal, probability, atr, regime = preprocess_and_predict(data)
        
        dynamic_risk_pct = 0.5 

        if probability < Config.PREDICTION_THRESHOLD: 
            signal = 'HOLD' 
        else:
            dynamic_risk_pct = calculate_dynamic_risk(probability)

        log_query = "INSERT INTO signal_history (signal, probability, atr, dynamic_risk) VALUES (?, ?, ?, ?)"
        log_params = (signal, probability, atr, dynamic_risk_pct) 
        log_to_db(log_query, log_params)
        
        return jsonify({
            'signal': signal, 
            'probability': float(probability),
            'atr': float(atr),
            'dynamic_risk': float(dynamic_risk_pct),
            'regime': regime, # ⬅️ [ใหม่]
            'message': 'Prediction successful.'
        }), 200
        
    except ValueError as ve:
        print(f"❌ Prediction validation error: {ve}")
        return jsonify({'signal': 'ERROR', 'probability': 0.0, 'message': str(ve)}), 400
    except Exception as e:
        print(f"❌ CRITICAL ERROR in /predict: {e}")
        traceback.print_exc()
        return jsonify({'signal': 'ERROR', 'probability': 0.0, 'message': 'Internal Server Error.'}), 500

# --- [ /update_status Endpoint (ไม่เปลี่ยนแปลง) ] ---
@app.route('/update_status', methods=['POST']) 
def update_status():
    """Endpoint for MT5 to send updated account status and trade alerts."""
    try:
        data = parse_mql_json(request)
        
        if data is None:
             return jsonify({'status': 'ERROR', 'message': 'Invalid JSON data received.'}), 400
             
        balance = data.get('balance', 0.0)
        equity = data.get('equity', 0.0)
        margin_free = data.get('margin_free', 0.0)
        open_trades = data.get('open_trades', 0)
        
        account_status.update({
            'balance': balance, 'equity': equity,
            'margin_free': margin_free, 'open_trades': open_trades,
        })

        log_query_ac = "INSERT INTO account_history (balance, equity, margin_free, open_trades) VALUES (?, ?, ?, ?)"
        log_params_ac = (balance, equity, margin_free, open_trades)
        log_to_db(log_query_ac, log_params_ac)

        alert_message = data.get('alert_message')
        if alert_message and alert_message.strip() != '': 
            print(f"🚨 MQL ALERT: {alert_message}")
            log_query_alert = "INSERT INTO trade_log (event_message) VALUES (?)"
            log_to_db(log_query_alert, (alert_message,))

        return jsonify({'status': 'SUCCESS'})
    except Exception as e:
        print(f"❌ update_status exception: {e}")
        traceback.print_exc()
        return jsonify({'status': 'ERROR', 'message': str(e)}), 500

# --- [ /command Endpoint (ไม่เปลี่ยนแปลง) ] ---
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

# --- [ /retrain Endpoint (ไม่เปลี่ยนแปลง) ] ---
@app.route('/retrain', methods=['POST'])
def retrain_model_async():
    if account_status['bot_status'] != 'STOPPED':
        return jsonify({'status': 'FAIL', 'message': '❌ This command requires the bot to be STOPPED.'}), 400

    try:
        download_model_assets() 
        if load_assets():
            return jsonify({'status': 'SUCCESS', 'message': '✅ Retraining completed and model (v6) loaded.'}), 200
        else:
            return jsonify({'status': 'FAIL', 'message': '⚠️ Model (v6) or scaler could not be loaded after download.'}), 500

    except Exception as e:
        print(f"❌ Error in retrain_model_async: {e}")
        traceback.print_exc()
        return jsonify({'status': 'FAIL', 'message': f'Error during retraining: {str(e)}'}), 500

# --- [ /update_ea Endpoint (ไม่เปลี่ยนแปลง) ] ---
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

# --- [ /restart Endpoint (ไม่เปลี่ยนแปลง) ] ---
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

# --- [ /fix Endpoint (ไม่เปลี่ยนแปลง) ] ---
@app.route('/fix', methods=['POST'])
def fix_system_files():
    """Downloads updated Python scripts and reloads model assets."""
    python_downloaded = download_python_files()

    try:
        download_model_assets()
    except Exception as e:
        return jsonify({'status': 'FAIL', 'message': f'❌ Failed to download model assets: {str(e)}. Python files may be updated.'}), 500
        
    assets_loaded = load_assets()
    
    message = "✅ System files and assets (v6) updated successfully."
    
    if not python_downloaded:
        message = "⚠️ Python files update failed for one or more files. Assets (v6) reloaded."

    if not assets_loaded:
        return jsonify({'status': 'FAIL', 'message': '⚠️ Assets (v6) downloaded but failed to load. System files updated. **Please manually restart.**'}), 500

    return jsonify({
        'status': 'SUCCESS', 
        'message': f'{message} **Requires Server Restart** for new Python files to take effect.'
    }), 200

# --- Server Run ---
if __name__ == '__main__':
    init_db() 
    if load_assets():
        print("Starting background news scheduler (Playwright + FXVerify Scraper)...") # ⬅️ [แก้ไข]
        scheduler_thread = threading.Thread(target=run_news_scheduler, daemon=True)
        scheduler_thread.start()

        print("💡 NOTE: Remember to start the separate telegram_bot.py script.")
        app.run(host='0.0.0.0', port=5000)
    else:

        print("❌ FATAL: Could not load v6 model/scaler. API not starting.")
