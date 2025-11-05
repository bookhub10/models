#import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz
import talib
import pickle
import os
from sklearn.preprocessing import MinMaxScaler
from sklearn.utils import class_weight
from tensorflow.keras.models import Sequential, load_model
from tensorflow.keras.layers import GRU, Dense, Dropout, Input
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.regularizers import l2
from tensorflow.keras.callbacks import EarlyStopping, ModelCheckpoint
import tensorflow as tf
import talib

# ==============================================================================
# PART 1: DATA COLLECTION AND PREPROCESSING
# ==============================================================================

# def initialize_mt5(login=0, password="", server=""):
#     """Initialize connection to MetaTrader 5"""
#     if not mt5.initialize():
#         print("initialize() failed, retrying...")
#         if not mt5.initialize():
#             print("initialize() failed finally.")
#             return False
    
#     if login != 0:
#         if not mt5.login(login, password=password, server=server):
#             print("Failed to connect to trade account", login)
#             mt5.shutdown()
#             return False
#     return True

# def get_xauusd_data(days=180, timeframe=mt5.TIMEFRAME_M5):
#     """Get XAUUSD data FOR M5 (Base)"""
#     # (ฟังก์ชันนี้เหมือนเดิมทุกประการ)
#     timezone = pytz.timezone("Etc/UTC")
#     time_from = datetime.now(timezone) - timedelta(days=days)
#     rates = mt5.copy_rates_from(
#         "XAUUSD", 
#         timeframe, 
#         time_from, 
#         30000 
#     )
#     if rates is None or len(rates) == 0:
#         print("Failed to get XAUUSD data.")
#         return pd.DataFrame()
#     df = pd.DataFrame(rates)
#     df['time'] = pd.to_datetime(df['time'], unit='s')
#     df.set_index('time', inplace=True)
#     df.drop(columns=['spread', 'real_volume'], inplace=True)
#     return df

# def get_other_timeframe_data(days, timeframe):
#     """🆕 ฟังก์ชันใหม่สำหรับดึงข้อมูล TF อื่น"""
#     timezone = pytz.timezone("Etc/UTC")
#     time_from = datetime.now(timezone) - timedelta(days=days)
#     rates = mt5.copy_rates_from("XAUUSD", timeframe, time_from, 30000)
#     if rates is None or len(rates) == 0:
#         return pd.DataFrame()
#     df = pd.DataFrame(rates)
#     df['time'] = pd.to_datetime(df['time'], unit='s')
#     df.set_index('time', inplace=True)
#     df = df[['close']] # เราต้องการแค่ราคาปิด
#     return df

# 🛑 A. ADD TECHNICAL INDICATORS (ตาม Obot_model) 🛑
# 🛑 [แทนที่ฟังก์ชันนี้] ใน model.py (Windows) 🛑

def add_technical_indicators(df_m5, df_m30, df_h1):
    """
    เวอร์ชันอัปเกรด: เพิ่มฟีเจอร์จาก M5, M30, H1 และ ATR (12 Features)
    """
    
    # === ส่วนที่ 1: คำนวณฟีเจอร์ M5 ===
    df_m5 = df_m5.copy()
    close_prices_m5 = df_m5['close'].values.astype(np.float64)
    high_prices_m5 = df_m5['high'].values.astype(np.float64) # ⬅️ เพิ่ม
    low_prices_m5 = df_m5['low'].values.astype(np.float64)   # ⬅️ เพิ่ม
    
    df_m5['SMA_10'] = talib.SMA(close_prices_m5, timeperiod=10)
    df_m5['SMA_50'] = talib.SMA(close_prices_m5, timeperiod=50)
    df_m5['Momentum_1'] = talib.MOM(close_prices_m5, timeperiod=1)
    df_m5['High_Low'] = df_m5['high'] - df_m5['low']
    
    # --- ⬇️ 1. [เพิ่มใหม่] ⬇️ ---
    # คำนวณ ATR (Average True Range) บน M5
    df_m5['ATR_14'] = talib.ATR(high_prices_m5, low_prices_m5, close_prices_m5, timeperiod=14)
    # --- ⬆️ [เพิ่มใหม่] ⬆️ ---

    # === ส่วนที่ 2: คำนวณฟีเจอร์ Multi-Timeframe (using TA-Lib) ===
    close_prices_m30 = df_m30['close'].values.astype(np.float64)
    df_m30['M30_RSI'] = talib.RSI(close_prices_m30, timeperiod=14)
    
    close_prices_h1 = df_h1['close'].values.astype(np.float64)
    df_h1['H1_MA_200'] = talib.SMA(close_prices_h1, timeperiod=200)
    df_h1['H1_MA_Trend'] = np.where(df_h1['close'] > df_h1['H1_MA_200'], 1, 0)
    
    # === ส่วนที่ 3: "การจัดเรียง" (Alignment) ===
    # (ส่วนนี้เหมือนเดิม 100%)
    print("Aligning M30 features to M5 timeline...")
    df_combined = pd.merge_asof(
        df_m5.sort_index(), 
        df_m30[['M30_RSI']].sort_index(), 
        left_index=True, 
        right_index=True, 
        direction='backward'
    )
    print("Aligning H1 features to M5 timeline...")
    df_final = pd.merge_asof(
        df_combined.sort_index(),
        df_h1[['H1_MA_Trend']].sort_index(),
        left_index=True,
        right_index=True,
        direction='backward'
    )

    # Feature 13 & 14: RSI Zones (ใช้ M30_RSI ที่ merge แล้ว)
    df_final['RSI_Overbought'] = np.where(df_final['M30_RSI'] > 70, 1, 0)
    df_final['RSI_Oversold'] = np.where(df_final['M30_RSI'] < 30, 1, 0)
    
    # Feature 15: SMA Crossover (ใช้ SMA_10 และ SMA_50 ที่ merge แล้ว)
    df_final['SMA_Cross'] = np.where(df_final['SMA_10'] > df_final['SMA_50'], 1, 0)
    
    # === ส่วนที่ 4: สรุปผล ===
    df_final.dropna(inplace=True)
    df_final.reset_index(drop=True, inplace=True)

    # --- ⬇️ 3. [แก้ไข] ⬇️ ---
    # 🛑 เลือกฟีเจอร์ทั้งหมด (12 เดิม + 3 ใหม่ = 15 ฟีเจอร์)
    feature_cols = [
        'open', 'high', 'low', 'close', 'tick_volume', 
        'SMA_10', 'SMA_50', 'Momentum_1', 'High_Low',
        'M30_RSI', 'H1_MA_Trend',
        'ATR_14',
        'RSI_Overbought', # ⬅️ เพิ่ม
        'RSI_Oversold',   # ⬅️ เพิ่ม
        'SMA_Cross'       # ⬅️ เพิ่ม
    ]
    # --- ⬆️ [แก้ไข] ⬆️ ---
    
    final_cols = [col for col in feature_cols if col in df_final.columns]
    if len(final_cols) != len(feature_cols):
        print(f"Warning: Missing columns! Expected {len(feature_cols)}, found {len(final_cols)}")
        
    df_final = df_final[final_cols].copy()
    
    print(f"Total features created: {len(df_final.columns)}")
    return df_final

# 🛑 B. CREATE SEQUENCES AND LABELS (ปรับให้สอดคล้องกับการเทรด) 🛑
# --- 🛑 [แทนที่ฟังก์ชันนี้] (เวอร์ชัน 3-Class Fix) 🛑 ---

def create_sequences_and_labels(df_features_unscaled, sequence_length=100, lookahead_bars=1, hold_threshold_pct=0.0005):
    """
    รับ Unscaled Features (12 cols), สร้าง Unscaled X (3D) และ Labels y (1D)
    """
    X, y = [], []
    
    # 1. 🛑 [FIX] คัดลอก DF เพื่อป้องกัน Warning
    df = df_features_unscaled.copy() 
    
    # 2. 🛑 [FIX] เก็บชื่อ 12 features ไว้ก่อน
    feature_cols = list(df.columns)
    
    # 3. คำนวณ % การเปลี่ยนแปลง (จาก 'close' ที่ยังไม่ scale)
    future_price = df['close'].shift(-lookahead_bars)
    current_price = df['close']
    df['pct_change'] = (future_price - current_price) / current_price

    # 4. สร้าง Labels
    def labeler(pct):
        if pct > hold_threshold_pct:
            return 1 # BUY
        elif pct < -hold_threshold_pct:
            return 2 # SELL
        else:
            return 0 # HOLD
    df['Target'] = df['pct_change'].apply(labeler)
    
    # 5. ลบแถว NaN (สำคัญมาก)
    df.dropna(inplace=True) 
    
    # 6. สร้าง Sequences
    for i in range(len(df) - sequence_length):
        # 🛑 [FIX] เลือกเฉพาะ 12 features (โดยใช้ชื่อ)
        X.append(df.iloc[i:i+sequence_length][feature_cols].values) 
        
        # 🛑 [FIX] y คือ Target ของแท่ง "ปัจจุบัน" (i + sequence_length - 1)
        y.append(df.iloc[i+sequence_length-1]['Target']) 

    return np.array(X), np.array(y)

# 🛑 C. SCALING (ใช้ MinMaxScaler) 🛑
def scale_features(train_df, test_df=None, scaler=None):
    """Scales data using MinMaxScaler and returns the scaler."""
    if scaler is None:
        scaler = MinMaxScaler(feature_range=(0, 1))
        train_scaled = scaler.fit_transform(train_df)
    else:
        # ใช้ Scaler ที่มีอยู่แล้วในการ transform
        train_scaled = scaler.transform(train_df)
    
    train_scaled_df = pd.DataFrame(train_scaled, columns=train_df.columns)
    
    if test_df is not None:
        test_scaled = scaler.transform(test_df)
        test_scaled_df = pd.DataFrame(test_scaled, columns=test_df.columns)
        return scaler, train_scaled_df, test_scaled_df
        
    return scaler, train_scaled_df, None

# def collect_and_scale_data(days=180):
#     """
#     เวอร์ชันอัปเกรด: ดึงข้อมูล 3 Timeframes และรวมเข้าด้วยกัน
#     """
#     print("Fetching M5 data...")
#     df_m5_raw = get_xauusd_data(days, mt5.TIMEFRAME_M5)
#     print("Fetching M30 data...")
#     df_m30_raw = get_other_timeframe_data(days, mt5.TIMEFRAME_M30)
#     print("Fetching H1 data...")
#     df_h1_raw = get_other_timeframe_data(days, mt5.TIMEFRAME_H1)
    
#     if df_m5_raw.empty or df_m30_raw.empty or df_h1_raw.empty:
#         print("Data collection failed for one or more timeframes.")
#         return None, None, None, None, None

#     # ส่ง DF ทั้ง 3 เข้าไปประมวลผล
#     df_features = add_technical_indicators(df_m5_raw, df_m30_raw, df_h1_raw)

#     if df_features.empty:
#         print("Failed to create features or data was insufficient.")
#         return None, None, None, None, None

#     # Split: 80% train, 20% test
#     train_size = int(len(df_features) * 0.8)
#     train_df = df_features.iloc[:train_size]
#     test_df = df_features.iloc[train_size:]
    
#     # Scale: Fit only on training data
#     scaler, train_scaled_df, test_scaled_df = scale_features(train_df, test_df)

#     print(f"Total features scaled: {train_scaled_df.shape[1]}") 
    
#     return scaler, train_scaled_df, test_scaled_df, train_df, test_df

# ==============================================================================
# PART 2: MODEL ARCHITECTURE AND TRAINING
# ==============================================================================

# 🛑 D. BUILD GRU MODEL (โครงสร้างมาตรฐาน) 🛑
# 🛑 D. BUILD GRU MODEL (โครงสร้างมาตรฐาน) 🛑
def build_gru_model(input_shape):
    """
    [Simplified Model] ลดความซับซ้อนเพื่อสู้กับ Overfitting
    """
    print(f"Building 3-Class (Simplified) model with Input Shape: {input_shape}")
    
    model = Sequential([
        Input(shape=input_shape), 
        
        # ⬇️ ลด Units จาก 128 -> 64
        # ⬇️ เพิ่ม Dropout จาก 0.3 -> 0.4
        GRU(units=64, return_sequences=True, activation='tanh', kernel_regularizer=l2(0.001)),
        Dropout(0.4), 
        
        # ⬇️ ลด Units จาก 64 -> 32
        # ⬇️ เพิ่ม Dropout จาก 0.3 -> 0.4
        GRU(units=32, return_sequences=False, activation='tanh', kernel_regularizer=l2(0.001)),
        Dropout(0.4),
        
        # (Output Layer เหมือนเดิม)
        Dense(units=3, activation='softmax')
    ])
    
    optimizer = Adam(learning_rate=0.001)
    
    model.compile(optimizer=optimizer, loss='sparse_categorical_crossentropy', metrics=['accuracy'])
    
    return model

# ... (ส่วน backtest_and_evaluate_model และ train_rnn_model_main เดิม) ...
# =============================================================================
# PART 3: TRAINING MAIN FUNCTION (Keep Existing Logic)
# =============================================================================

# def train_rnn_model_main(
#     sequence_length=100, 
#     lookahead_bars=1,
#     epochs=50, 
#     batch_size=32, 
#     days=180
#     # timeframe ถูกลบออก เพราะเราดึง 3 TFs
# ):
#     """
#     Main function to run the full training process.
#     """
#     print(f"Starting model training for XAUUSD (Multi-Timeframe)...")

#     # 1. Connect and Collect Data
#     if not initialize_mt5():
#         print("Cannot connect to MT5.")
#         return None
    
#     # 🛑 แก้ไขการเรียก: ไม่ต้องส่ง timeframe
#     scaler, train_scaled_df, test_scaled_df, train_df, test_df = \
#         collect_and_scale_data(days=days)
    
#     # ... (ส่วนที่เหลือของฟังก์ชัน train_rnn_model_main เหมือนเดิมทั้งหมด) ...
#     # (เช่น create_sequences_and_labels, Handle Class Imbalance,
#     #  Build Model, Callbacks, Train Model, Save model/scaler, Upload)
    
#     if train_scaled_df is None or test_scaled_df is None:
#         print("Data collection failed or returned empty dataframes.")
#         mt5.shutdown()
#         return None
    
#     print(f"Train/Test split: {len(train_scaled_df)} / {len(test_scaled_df)} bars.")

#     # 2. Create Sequences and Labels (ฟังก์ชันนี้ยังใช้ได้เหมือนเดิม)
#     X_train, y_train = create_sequences_and_labels(
#         train_scaled_df, 
#         sequence_length=sequence_length,
#         lookahead_bars=lookahead_bars
#     )
#     X_test, y_test = create_sequences_and_labels(
#         test_scaled_df, 
#         sequence_length=sequence_length,
#         lookahead_bars=lookahead_bars
#     )
    
#     if X_train.shape[0] == 0:
#          print("Not enough data to create training sequences.")
#          mt5.shutdown()
#          return None

#     # (ตรวจสอบว่า Input Shape ถูกต้อง)
#     print(f"X_train shape: {X_train.shape}, y_train shape: {y_train.shape}")
#     print(f"X_test shape: {X_test.shape}, y_test shape: {y_test.shape}")
    
#     # 3. Handle Class Imbalance (เหมือนเดิม)
#     unique_classes, counts = np.unique(y_train, return_counts=True)
#     if len(unique_classes) > 1:
#         class_weights = class_weight.compute_class_weight(
#             class_weight='balanced', 
#             classes=unique_classes, 
#             y=y_train
#         )
#         class_weight_dict = dict(zip(unique_classes, class_weights))
#         print(f"Class Weights: {class_weight_dict}")
#     else:
#         class_weight_dict = {unique_classes[0]: 1.0}

#     # 4. Build Model (โมเดลจะรับ input_shape ใหม่โดยอัตโนมัติ)
#     input_shape = (sequence_length, X_train.shape[2]) # X_train.shape[2] ตอนนี้จะเป็น 11
#     model = build_gru_model(input_shape)
#     print("Model built and compiled.")

#     # 5. Define Callbacks (เหมือนเดิม)
#     model_checkpoint_callback = ModelCheckpoint(
#         filepath='models/gru_bot_best_M5.h5', 
#         monitor='val_accuracy', 
#         save_best_only=True, 
#         mode='max', 
#         verbose=1
#     )
#     early_stopping_callback = EarlyStopping(
#         monitor='val_loss', 
#         patience=10, 
#         mode='min', 
#         restore_best_weights=True,
#         verbose=1
#     )

#     # 6. Train Model (เหมือนเดิม)
#     print("Starting training...")
#     history = model.fit(
#         X_train, y_train,
#         epochs=epochs,
#         batch_size=batch_size,
#         validation_data=(X_test, y_test),
#         callbacks=[model_checkpoint_callback, early_stopping_callback],
#         class_weight=class_weight_dict if len(unique_classes) > 1 else None,
#         verbose=2
#     )
#     print("Training finished.")

#     # 7. Load best model (เหมือนเดิม)
#     best_model = load_model('models/gru_bot_best_M5.h5')
    
#     # 8. Save final model and scaler (เหมือนเดิม)
#     os.makedirs('models', exist_ok=True)
#     with open('models/scaler.pkl', 'wb') as f:
#         pickle.dump(scaler, f)
#     print("\nTraining completed. Model and Scaler saved locally.")  

#     # 9. 🚀 อัปโหลดขึ้น GitHub (เหมือนเดิม)
#     upload_to_github("models/gru_bot_best_M5.h5", "models/gru_bot_best_M5.h5")
#     upload_to_github("models/scaler.pkl", "models/scaler.pkl")

#     mt5.shutdown()
#     return best_model

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
if __name__ == "__main__":
    
    # 🚨 Configuration based on typical RNN setup
    FINAL_SEQUENCE_LENGTH = 100 # Lookback bars for prediction
    FINAL_LOOKAHEAD_BARS = 1   # Predict 1 bar ahead
    
    # train_rnn_model_main(
    #     sequence_length=FINAL_SEQUENCE_LENGTH,
    #     lookahead_bars=FINAL_LOOKAHEAD_BARS,
    #     epochs=50,
    #     batch_size=32,
    #     days=180 # ใช้ข้อมูล 180 วันเพื่อฝึก
    # )

# ... (End of model.py)