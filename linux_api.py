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
    BUY_THRESHOLD = 0.50 
    SELL_THRESHOLD = 0.50 

app = Flask(__name__)
rnn_model = None
scaler = None

# 🛑 รายชื่อคุณลักษณะ (9 Features)
REQUIRED_FEATURES = [
    'open', 'high', 'low', 'close', 'tick_volume', 
    'SMA_10', 'SMA_50', 'Momentum_1', 'High_Low',
    'M30_RSI', 'H1_MA_Trend'
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

# --- Core Prediction Logic (เวอร์ชัน Multi-Timeframe) ---

def preprocess_and_predict(raw_data):
    """Processes 3 TFs, runs prediction, and returns signal/probability."""
    global rnn_model, scaler

    # 1. 🆕 แปลง JSON 3 ส่วน ให้เป็น DataFrames
    try:
        df_m5 = pd.DataFrame(raw_data['m5_data'])
        df_m30 = pd.DataFrame(raw_data['m30_data'])
        df_h1 = pd.DataFrame(raw_data['h1_data'])

        # ตั้งค่า Index (สำคัญมากสำหรับการ merge)
        df_m5['time'] = pd.to_datetime(df_m5['time'], unit='s')
        df_m5.set_index('time', inplace=True)

        df_m30['time'] = pd.to_datetime(df_m30['time'], unit='s')
        df_m30.set_index('time', inplace=True)
        # เราต้องการแค่ close จาก M30
        df_m30 = df_m30[['close']] 

        df_h1['time'] = pd.to_datetime(df_h1['time'], unit='s')
        df_h1.set_index('time', inplace=True)
        # เราต้องการแค่ close จาก H1
        df_h1 = df_h1[['close']] 

    except Exception as e:
        raise ValueError(f"Failed to parse Multi-Timeframe data. Error: {e}")

    # 2. 🆕 เรียกใช้ฟังก์ชัน add_technical_indicators เวอร์ชันใหม่
    # (ฟังก์ชันนี้ import มาจาก linux_model.py ที่เราเพิ่งอัปเกรด)
    df_features = add_technical_indicators(df_m5, df_m30, df_h1)

    # 3. ตรวจสอบความยาว (เหมือนเดิม)
    if len(df_features) < Config.SEQUENCE_LENGTH:
        raise ValueError(f"Not enough valid bars after merging TFs ({len(df_features)} bars), expected at least {Config.SEQUENCE_LENGTH}.")

    # 4. เตรียม Sequence สำหรับ Scaling (เหมือนเดิม)
    df_for_scaling = df_features.iloc[-Config.SEQUENCE_LENGTH:].copy() 

    # 5. 🛑 เลือก 11 ฟีเจอร์ (เหมือนเดิม แต่ตอนนี้ใช้ List ใหม่)
    final_features = [col for col in REQUIRED_FEATURES if col in df_for_scaling.columns]

    if len(final_features) != len(REQUIRED_FEATURES):
        missing = set(REQUIRED_FEATURES) - set(final_features)
        raise ValueError(f"Feature Mismatch: Expected {len(REQUIRED_FEATURES)} features. Missing: {missing}")

    df_for_scaling_trimmed = df_for_scaling[final_features]

    try:
        # 6. Scaling (เหมือนเดิม)
        _, test_scaled, _ = scale_features(
            df_for_scaling_trimmed,
            test_df=None,
            scaler=scaler
        )
    except Exception as e:
        raise ValueError(f"Scaling failed (check feature count: {len(final_features)}). Error: {e}")

    # 7. Prepare Sequence (เหมือนเดิม)
    X_pred_data = test_scaled.values 
    X_pred = np.array([X_pred_data]) 

    # 8. Predict (เหมือนเดิม)
    prediction = rnn_model.predict(X_pred, verbose=0)[0][0]

    # 9. Determine Signal (เหมือนเดิม)
    signal = 'NONE'
    if prediction >= Config.BUY_THRESHOLD:
        signal = 'BUY'
    elif (1 - prediction) >= Config.SELL_THRESHOLD:
        signal = 'SELL'

    account_status['last_signal'] = signal

    return signal, prediction

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

@app.route('/predict', methods=['POST']) 
def predict_signal():
    if rnn_model is None or scaler is None:
        return jsonify({'signal': 'ERROR', 'probability': 0.0, 'message': 'Model not loaded.'}), 503
    
    if account_status['bot_status'] != 'RUNNING':
        return jsonify({'signal': 'NONE', 'probability': 0.0, 'message': f"Bot is {account_status['bot_status']}."}), 200

    try:
        # ใช้ Helper Function เพื่อ parse JSON จาก MQL5
        data = parse_mql_json(request)
        if data is None:
            return jsonify({'signal': 'ERROR', 'probability': 0.0, 'message': 'Invalid JSON data received.'}), 400
        
        signal, probability = preprocess_and_predict(data)
        
        return jsonify({
            'signal': signal,
            'probability': float(probability),
            'message': 'Prediction successful.'
        }), 200
        
    except ValueError as ve:
        # Validation Errors (e.g., Not enough bars, Scaling failure)
        print(f"❌ Prediction validation error: {ve}")
        return jsonify({'signal': 'ERROR', 'probability': 0.0, 'message': str(ve)}), 400
    except Exception as e:
        # Other Internal Errors
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
    Downloads the latest .mq5 file and recompiles it ASYNCHRONOUSLY.
    """
    # 🛑 (ตรวจสอบ) Path เหล่านี้ของคุณถูกต้องหรือไม่
    EA_URL = 'https://raw.githubusercontent.com/bookhub10/models/main/linux_OBot.mq5' 
    EA_PATH = "/home/hp/.mt5/drive_c/Program Files/MetaTrader 5/MQL5/Experts/OBotTrading.mq5"
    METAEDITOR_PATH = "/home/hp/.mt5/drive_c/Program Files/MetaTrader 5/metaeditor64.exe"
    WINEPREFIX_PATH = "/home/hp/.mt5"
    
    # 🛑 [NEW] เราจะเก็บ Log การ Compile ไว้ที่นี่
    COMPILE_LOG_PATH = "/home/hp/Downloads/bot/logs/compile.log"

    try:
        # 1. ดาวน์โหลดไฟล์ EA (ยังเหมือนเดิม)
        print(f"⬇️ Downloading new EA from {EA_URL}...")
        response = requests.get(EA_URL)
        response.raise_for_status()

        with open(EA_PATH, 'wb') as f:
            f.write(response.content)
        print("✅ EA Downloaded.")

        # 2. เตรียมคำสั่ง Compile (ยังเหมือนเดิม)
        print(f"⚙️ Compiling {EA_PATH}...")
        env = os.environ.copy()
        env['WINEPREFIX'] = WINEPREFIX_PATH
        
        # 🛑 (ตรวจสอบ) ชื่อไฟล์ใน Path นี้ต้องตรงกับ EA_PATH 
        # C:\Program Files\MetaTrader 5\MQL5\Experts\OBotTrading.mq5
        wine_ea_path = "C:\\Program Files\\MetaTrader 5\\MQL5\\Experts\\OBotTrading.mq5"
        
        compile_command = [
            "wine", 
            METAEDITOR_PATH, 
            f'/compile:"{wine_ea_path}"'
        ]
        
        # 3. 🛑 [THE FIX] 🛑
        # ใช้ Popen (ยิงแล้วทิ้ง) แทน .run (รอให้เสร็จ)
        # เราจะส่ง Output (stdout/stderr) ไปที่ไฟล์ Log แทน
        print("✅ Issuing non-blocking compile command...")
        with open(COMPILE_LOG_PATH, 'w') as log_file:
            subprocess.Popen(compile_command, env=env, stdout=log_file, stderr=log_file)
        
        # 4. ตอบกลับ Telegram ทันที
        return jsonify({
            'status': 'SUCCESS', 
            'message': f'✅ Compile command issued! Check logs/compile.log for results.'
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
