import os
import sys
import inspect
import pickle
import json
import threading
import traceback
import numpy as np
import pandas as pd
import requests 
from flask import Flask, request, jsonify
from tensorflow.keras.models import load_model
import warnings
import subprocess

# Suppress TensorFlow and other library warnings
warnings.filterwarnings("ignore")
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0' 

# --- Path Setup for External Modules ---
try:
    current_dir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
    root_dir = os.path.dirname(current_dir)
    if root_dir not in sys.path:
        sys.path.append(root_dir)
    
    # Import necessary functions from the external training script
    from linux_model import add_technical_indicators, scale_features#, train_rnn_model_main 
    
    print("✅ External model functions loaded successfully.")
except ImportError as e:
    print(f"❌ FATAL: Cannot import from models.model. Error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ FATAL: An unexpected error occurred during import: {e}")
    sys.exit(1)

# --- Configuration & Global State ---

class Config:
    MODEL_PATH = 'models/gru_bot_best_M5.h5' 
    SCALER_PATH = 'models/scaler.pkl'
    SEQUENCE_LENGTH = 100
    
    # --- ⬇️ [เพิ่มใหม่] ⬇️ ---
    # เกณฑ์ขั้นต่ำที่ "มั่นใจ" ถึงจะยอมเทรด
    # (ถ้าโมเดลมั่นใจแค่ 40% (0.4) เราจะบังคับให้เป็น HOLD)
    PREDICTION_THRESHOLD = 0.4 # ⬅️ คุณจูนค่านี้ได้ (เช่น 0.45 หรือ 0.55)

app = Flask(__name__)
rnn_model = None
scaler = None

# 🛑 รายชื่อคุณลักษณะ (15 Features)
REQUIRED_FEATURES = [
    'open', 'high', 'low', 'close', 'tick_volume', 
    'SMA_10', 'SMA_50', 'Momentum_1', 'High_Low',
    'M30_RSI', 'H1_MA_Trend',
    'ATR_14',
    'RSI_Overbought', # ⬅️ เพิ่ม
    'RSI_Oversold',   # ⬅️ เพิ่ม
    'SMA_Cross'       # ⬅️ เพิ่ม
]

account_status = {
    'bot_status': 'STOPPED', 
    'balance': 0.0,
    'equity': 0.0,
    'margin_free': 0.0,
    'open_trades': 0,
    'last_signal': 'NONE'
}

# --- Download Model & Scaler from GitHub --- 
def download_model_assets():
    """Download model and scaler from GitHub."""
    GITHUB_FILES = {
        'gru_model': {
            'url': 'https://raw.githubusercontent.com/bookhub10/models/main/models/gru_bot_best_M5.h5',
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
        # Assuming linux_model.py is in the root directory for simplicity.
        # If it's in a different path, adjust filename here.
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
            # ไม่ throw error เพื่อให้ลองดาวน์โหลดไฟล์อื่นต่อ
    return success

# --- Asset Management ---

def load_assets():
    """Load the Keras H5 model and MinMaxScaler."""
    global rnn_model, scaler
    print("--- Attempting to load Model and Scaler ---")
    try:
        rnn_model = load_model(Config.MODEL_PATH)
        with open(Config.SCALER_PATH, 'rb') as f:
            scaler = pickle.load(f)
        
        if hasattr(scaler, 'n_features_in_'):
            print(f"DEBUG: Scaler expects {scaler.n_features_in_} features.")
            
        print("✅ Model and Scaler loaded successfully.")
        return True
    except FileNotFoundError:
        print(f"❌ Error: Model or Scaler file not found. Check paths: {Config.MODEL_PATH}, {Config.SCALER_PATH}")
    except Exception as e:
        print(f"❌ Critical Error loading assets: {e}")
        traceback.print_exc()
    
    rnn_model = None
    scaler = None
    return False

# --- JSON Parsing Helper (Robust against MQL output issues) ---

def parse_mql_json(req):
    """Helper to safely parse JSON from MQL5 (which might contain trailing NULs)."""
    if req.data:
        try:
            # Decode using utf-8 and strip any non-printable chars
            raw_data = req.data.decode('utf-8', errors='ignore').strip('\x00').strip()
            return json.loads(raw_data)
        except json.JSONDecodeError as e:
            print(f"❌ JSON Decode Error: {e}")
            print(f"Raw data snippet (first 200 chars): {raw_data[:200]}")
            return None
    return None

# --- Core Prediction Logic ---

# --- 🛑 [แทนที่ฟังก์ชันนี้] (เวอร์ชัน 3-Class) 🛑 ---
def preprocess_and_predict(raw_data):
    """
    Processes 15 features, runs 3-CLASS prediction, and returns signal/prob/atr.
    """
    global rnn_model, scaler
    
    # 1. แปลง JSON (เหมือนเดิม)
    try:
        df_m5 = pd.DataFrame(raw_data['m5_data'])
        df_m30 = pd.DataFrame(raw_data['m30_data'])
        df_h1 = pd.DataFrame(raw_data['h1_data'])
        # (โค้ดแปลง time index เหมือนเดิม)
        df_m5['time'] = pd.to_datetime(df_m5['time'], unit='s')
        df_m5.set_index('time', inplace=True)
        df_m30['time'] = pd.to_datetime(df_m30['time'], unit='s')
        df_m30.set_index('time', inplace=True)
        df_m30 = df_m30[['close']] 
        df_h1['time'] = pd.to_datetime(df_h1['time'], unit='s')
        df_h1.set_index('time', inplace=True)
        df_h1 = df_h1[['close']] 
    except Exception as e:
        raise ValueError(f"Failed to parse Multi-Timeframe data. Error: {e}")

    # 2. เรียกใช้ฟังก์ชัน 15-feature (จาก linux_model.py)
    df_features = add_technical_indicators(df_m5, df_m30, df_h1)
    
    # 3. ตรวจสอบความยาว (เหมือนเดิม)
    if len(df_features) < Config.SEQUENCE_LENGTH:
        raise ValueError(f"Not enough valid bars after merging TFs ({len(df_features)} bars), expected at least {Config.SEQUENCE_LENGTH}.")

    # 4. ดึงค่า ATR ล่าสุด (เหมือนเดิม)
    latest_atr = df_features['ATR_14'].iloc[-1]

    # 5. เตรียม Sequence สำหรับ Scaling (เหมือนเดิม)
    df_for_scaling = df_features.iloc[-Config.SEQUENCE_LENGTH:].copy() 
    
    # 6. เลือก 15 ฟีเจอร์ (ใช้ List ใหม่)
    df_for_scaling_trimmed = df_for_scaling[REQUIRED_FEATURES]
    
    # 7. Scaling (เหมือนเดิม)
    try:
        _, test_scaled, _ = scale_features(
            df_for_scaling_trimmed, test_df=None, scaler=scaler
        )
    except Exception as e:
        raise ValueError(f"Scaling failed (check feature count: {len(df_for_scaling_trimmed.columns)}). Error: {e}")

    # 8. Prepare Sequence & Predict (เหมือนเดิม)
    X_pred_data = test_scaled.values 
    X_pred = np.array([X_pred_data]) 
    
    # 9. 🛑 [แก้ไข] 🛑 Predict แบบ 3-Class
    # prediction จะหน้าตาแบบนี้: [[0.7 (HOLD), 0.2 (BUY), 0.1 (SELL)]]
    prediction_array = rnn_model.predict(X_pred, verbose=0)[0]
    
    # 10. 🛑 [แก้ไข] 🛑 Determine Signal (หา Class ที่ชนะ)
    
    # ดึง Class ที่มี % ชนะสูงสุด (0, 1, หรือ 2)
    predicted_class = np.argmax(prediction_array) 
    
    # ดึง % ความน่าจะเป็นของ Class ที่ชนะ
    probability = np.max(prediction_array) 
    
    signal = 'NONE' # ค่าเริ่มต้น
    if predicted_class == 1: # 1 = BUY
        signal = 'BUY'
    elif predicted_class == 2: # 2 = SELL
        signal = 'SELL'
    elif predicted_class == 0: # 0 = HOLD
        signal = 'HOLD' # ⬅️ สัญญาณใหม่
    
    account_status['last_signal'] = signal
        
    # 11. คืนค่า ATR (เหมือนเดิม)
    # (เราจะส่ง ATR เสมอ เผื่อไว้)
    return signal, probability, latest_atr

# --- API Endpoints ---

# 🆕 เพิ่ม Endpoint /status เพื่อให้ MT5 EA ตรวจสอบสถานะ
@app.route('/status', methods=['GET']) 
def get_status():
    """Endpoint for MT5 EA to check the bot's current status and performance."""
    global account_status, rnn_model, scaler
    try:
        current_status = account_status.copy()
        current_status['model_loaded'] = (rnn_model is not None)
        current_status['scaler_loaded'] = (scaler is not None)
        return jsonify(current_status), 200
    except Exception as e:
        print(f"❌ Error fetching status: {e}")
        return jsonify({'bot_status': 'ERROR', 'message': f'Server internal error: {str(e)}'}), 500

# --- 🛑 [แทนที่ Endpoint นี้] (เวอร์ชัน 3-Class) 🛑 ---
@app.route('/predict', methods=['POST']) 
def predict_signal():
    if rnn_model is None or scaler is None:
        return jsonify({'signal': 'ERROR', 'probability': 0.0, 'message': 'Model not loaded.'}), 503
    if account_status['bot_status'] != 'RUNNING':
        return jsonify({'signal': 'NONE', 'probability': 0.0, 'message': f"Bot is {account_status['bot_status']}."}), 200

    try:
        data = parse_mql_json(request)
        if data is None:
            return jsonify({'signal': 'ERROR', 'probability': 0.0, 'message': 'Invalid JSON data received.'}), 400
        
        signal, probability, atr = preprocess_and_predict(data)
        
        # --- ⬇️ [เพิ่มใหม่] ⬇️ ---
        # 🛑 "ตัวกรองความมั่นใจ" 🛑
        # ถ้าความมั่นใจ (probability) ต่ำกว่าเกณฑ์ (0.50)...
        if probability < Config.PREDICTION_THRESHOLD:
            # บังคับให้เป็น HOLD (แม้ว่าโมเดลจะบอก BUY/SELL)
            signal = 'HOLD' 
        # --- ⬆️ [เพิ่มใหม่] ⬆️ ---
        
        return jsonify({
            'signal': signal, # ⬅️ ตอนนี้สามารถเป็น "HOLD" ได้แล้ว
            'probability': float(probability),
            'atr': float(atr),
            'message': 'Prediction successful.'
        }), 200
        
    except ValueError as ve:
        print(f"❌ Prediction validation error: {ve}")
        return jsonify({'signal': 'ERROR', 'probability': 0.0, 'message': str(ve)}), 400
    except Exception as e:
        print(f"❌ CRITICAL ERROR in /predict: {e}")
        traceback.print_exc()
        return jsonify({'signal': 'ERROR', 'probability': 0.0, 'message': 'Internal Server Error.'}), 500

@app.route('/update_status', methods=['POST']) 
def update_status():
    """Endpoint for MT5 to send updated account status and trade alerts."""
    try:
        # 🛑 FIX: ใช้ parse_mql_json เพื่อจัดการ Null bytes และ JSON format
        data = parse_mql_json(request)
        
        if data is None:
             return jsonify({'status': 'ERROR', 'message': 'Invalid JSON data received.'}), 400
             
        account_status.update({
            'balance': data.get('balance', 0.0),
            'equity': data.get('equity', 0.0),
            'margin_free': data.get('margin_free', 0.0),
            'open_trades': data.get('open_trades', 0),
        })

        alert_message = data.get('alert_message')
        if alert_message and alert_message.strip() != '': # ตรวจสอบ alert_message
            print(f"🚨 MQL ALERT: {alert_message}")

        return jsonify({'status': 'SUCCESS'})
    except Exception as e:
        print(f"❌ update_status exception: {e}")
        traceback.print_exc()
        return jsonify({'status': 'ERROR', 'message': str(e)}), 500

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
        # ดาวน์โหลดโมเดลและ scaler จาก Google Drive
        download_model_assets()

        # โหลดไฟล์เข้า memory
        if load_assets():
            return jsonify({'status': 'SUCCESS', 'message': '✅ Retraining completed and model loaded.'}), 200
        else:
            return jsonify({'status': 'FAIL', 'message': '⚠️ Model or scaler could not be loaded after download.'}), 500

    except Exception as e:
        print(f"❌ Error in retrain_model_async: {e}")
        traceback.print_exc()
        return jsonify({'status': 'FAIL', 'message': f'Error during retraining: {str(e)}'}), 500

@app.route('/update_ea', methods=['POST'])
def update_expert_advisor():
    """
    [NEW VERSION] Downloads the EA and creates a trigger file.
    The actual compile is handled by linux_compiler.py (GUI Watcher).
    """
    EA_URL = 'https://raw.githubusercontent.com/bookhub10/models/main/linux_OBot.mq5' 
    EA_PATH = "/home/hp/.mt5/drive_c/Program Files/MetaTrader 5/MQL5/Experts/OBotTrading.mq5"
    TRIGGER_FILE = "/home/hp/Downloads/bot/COMPILE_NOW.trigger" # ⬅️ ไฟล์สัญญาณ

    try:
        # 1. ดาวน์โหลดไฟล์ EA
        print(f"⬇️ Downloading new EA from {EA_URL}...")
        response = requests.get(EA_URL)
        response.raise_for_status()
        with open(EA_PATH, 'wb') as f:
            f.write(response.content)
        print("✅ EA Downloaded.")

        # 2. 🛑 [THE FIX] 🛑
        # สร้างไฟล์ Trigger เพื่อให้ "Watcher" ที่หน้าจอทำงาน
        with open(TRIGGER_FILE, 'w') as f:
            f.write('triggered') # เขียนอะไรก็ได้ลงไป
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
        # นี่คือคำสั่งที่ปลอดภัยกว่า
        # (เราจะตั้งค่า sudoers ในขั้นตอนถัดไป)
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


# 🆕 เพิ่ม Endpoint /fix
@app.route('/fix', methods=['POST'])
def fix_system_files():
    """Downloads updated Python scripts and reloads model assets."""
    # 1. ดาวน์โหลดไฟล์ Python ใหม่
    python_downloaded = download_python_files()

    # 2. ดาวน์โหลดโมเดลและ scaler ใหม่ (เหมือนกับการ retrain)
    try:
        download_model_assets()
    except Exception as e:
        return jsonify({'status': 'FAIL', 'message': f'❌ Failed to download model assets: {str(e)}. Python files may be updated.'}), 500
        
    # 3. โหลดโมเดลและ Scaler เข้า memory
    assets_loaded = load_assets()
    
    message = "✅ System files and assets updated successfully."
    
    if not python_downloaded:
        message = "⚠️ Python files update failed for one or more files. Assets reloaded."

    if not assets_loaded:
        return jsonify({'status': 'FAIL', 'message': '⚠️ Assets downloaded but failed to load into memory. System files updated. **Please manually restart the server.**'}), 500

    return jsonify({
        'status': 'SUCCESS', 
        'message': f'{message} **Requires Server Restart** for new Python files to take effect.'
    }), 200

# --- Server Run ---
if __name__ == '__main__':
    if load_assets():
        print("💡 NOTE: Remember to start the separate telegram_bot.py script.")
        app.run(host='0.0.0.0', port=5000)
