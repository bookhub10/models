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
#     """Get XAUUSD data"""
#     timezone = pytz.timezone("Etc/UTC")
#     time_from = datetime.now(timezone) - timedelta(days=days)
    
#     # ดึงข้อมูลจาก MT5
#     rates = mt5.copy_rates_from(
#         "XAUUSD", 
#         timeframe, 
#         time_from, 
#         30000 # ดึงข้อมูลสูงสุด 30000 แท่ง
#     )
    
#     if rates is None or len(rates) == 0:
#         print("Failed to get XAUUSD data.")
#         return pd.DataFrame()

#     df = pd.DataFrame(rates)
#     df['time'] = pd.to_datetime(df['time'], unit='s')
#     df.set_index('time', inplace=True)
#     df.drop(columns=['spread', 'real_volume'], inplace=True)
#     return df

# 🛑 A. ADD TECHNICAL INDICATORS (ตาม Obot_model) 🛑
def add_technical_indicators(df):
    """
    Adds necessary technical indicators (MUST match the final trained model: 9 features).
    """
    
    # 1. Simple Moving Average (SMA 10 and 50)
    df['SMA_10'] = df['close'].rolling(window=10).mean()
    df['SMA_50'] = df['close'].rolling(window=50).mean()

    # 2. Momentum / RSI Proxy
    df['Momentum_1'] = df['close'].diff(1)

    # 3. Price Range / ATR Proxy
    df['High_Low'] = df['high'] - df['low']

    # Remove rows with NaN created by rolling windows (e.g., first 50 rows)
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)

    # 🛑 Select only the 9 features (Base 5 + Indicators 4)
    feature_cols = ['open', 'high', 'low', 'close', 'tick_volume', 
                    'SMA_10', 'SMA_50', 'Momentum_1', 'High_Low']
    
    df = df[feature_cols].copy()
    
    return df

# 🛑 B. CREATE SEQUENCES AND LABELS (ปรับให้สอดคล้องกับการเทรด) 🛑
def create_sequences_and_labels(df, sequence_length=100, lookahead_bars=1):
    """
    Prepares data into sequences (X) and next-bar direction labels (Y).
    Labeling: 1 for Buy (price goes up), 0 for Sell/Hold (price stays same or drops).
    """
    X, y = [], []
    df_values = df.values
    
    # ตรรกะการสร้าง Label: Price Up vs. Price Down/Same
    # 🚨 NOTE: ต้องมี lookahead_bars เพื่อดูอนาคต 1 แท่ง
    
    # คำนวณ Label
    # y[i] = 1 ถ้า Close[i + lookahead_bars] > Close[i]
    # y[i] = 0 ถ้า Close[i + lookahead_bars] <= Close[i]
    
    df['Target'] = np.where(df['close'].shift(-lookahead_bars) > df['close'], 1, 0)
    
    # ลบแถวสุดท้ายที่ไม่มี Target
    df.dropna(subset=['Target'], inplace=True)
    
    # สร้าง Sequences
    for i in range(len(df) - sequence_length):
        X.append(df.iloc[i:i+sequence_length][df.columns[:-1]].values) # Features
        y.append(df.iloc[i+sequence_length-1]['Target']) # Target สำหรับแท่งสุดท้ายของ Sequence

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

# def collect_and_scale_data(days=180, timeframe=mt5.TIMEFRAME_M5):
#     """Collects data, adds indicators, and splits for training/testing."""
#     df_raw = get_xauusd_data(days, timeframe)
#     if df_raw.empty: return None, None, None, None, None

#     df_features = add_technical_indicators(df_raw.copy())

#     # Split: 80% train, 20% test
#     train_size = int(len(df_features) * 0.8)
#     train_df = df_features.iloc[:train_size]
#     test_df = df_features.iloc[train_size:]
    
#     # Scale: Fit only on training data
#     scaler, train_scaled_df, test_scaled_df = scale_features(train_df, test_df)

#     # Check the number of features after scaling
#     print(f"Total features used: {train_scaled_df.shape[1]}") 
    
#     return scaler, train_scaled_df, test_scaled_df, train_df, test_df

# ==============================================================================
# PART 2: MODEL ARCHITECTURE AND TRAINING
# ==============================================================================

# 🛑 D. BUILD GRU MODEL (โครงสร้างมาตรฐาน) 🛑
def build_gru_model(input_shape):
    """Defines the GRU-RNN model architecture."""
    
    model = Sequential([
        Input(shape=input_shape),
        # 1st GRU Layer
        GRU(units=128, return_sequences=True, activation='tanh', kernel_regularizer=l2(0.001)),
        Dropout(0.3),
        # 2nd GRU Layer
        GRU(units=64, return_sequences=False, activation='tanh', kernel_regularizer=l2(0.001)),
        Dropout(0.3),
        # Output Layer (Binary Classification: Buy=1, Sell/Hold=0)
        Dense(units=1, activation='sigmoid')
    ])
    
    # ใช้ Adam Optimizer
    optimizer = Adam(learning_rate=0.001)
    
    # Compile Model
    model.compile(optimizer=optimizer, loss='binary_crossentropy', metrics=['accuracy'])
    
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
#     days=180, 
#     timeframe=mt5.TIMEFRAME_M5
# ):
#     """
#     Main function to run the full training process.
#     """
#     print(f"Starting model training for XAUUSD on M5...")

#     # 1. Connect and Collect Data
#     if not initialize_mt5():
#         print("Cannot connect to MT5.")
#         return None
    
#     scaler, train_scaled_df, test_scaled_df, train_df, test_df = \
#         collect_and_scale_data(days=days, timeframe=timeframe)
    
#     if train_scaled_df is None or test_scaled_df is None:
#         print("Data collection failed or returned empty dataframes.")
#         mt5.shutdown()
#         return None
    
#     print(f"Train/Test split: {len(train_scaled_df)} / {len(test_scaled_df)} bars.")

#     # 2. Create Sequences and Labels
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

#     print(f"X_train shape: {X_train.shape}, y_train shape: {y_train.shape}")
#     print(f"X_test shape: {X_test.shape}, y_test shape: {y_test.shape}")
    
#     if X_train.shape[0] == 0:
#         print("Not enough data to create training sequences.")
#         mt5.shutdown()
#         return None

#     # 3. Handle Class Imbalance (if any)
#     # y_train = y_train.flatten() # y_train should already be 1D
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
#         # กรณีที่มีคลาสเดียว (ไม่น่าจะเกิดขึ้น)
#         class_weight_dict = {unique_classes[0]: 1.0}


#     # 4. Build Model
#     input_shape = (sequence_length, X_train.shape[2])
#     model = build_gru_model(input_shape)
#     print("Model built and compiled.")

#     # 5. Define Callbacks
#     model_checkpoint_callback = ModelCheckpoint(
#         filepath='models/gru_bot_best_M5.h5', # 🚨 Updated path for consistency
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

#     # 6. Train Model
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

#     # Load best model
#     best_model = load_model('models/gru_bot_best_M5.h5')
    
#     # 7. Evaluate and Backtest (Placeholder - Requires separate backtest logic)
#     # ... (Evaluation and Backtest steps are omitted for brevity/focus on deployment) ...
    
#     # 8. Save final model and scaler
#     os.makedirs('models', exist_ok=True)

#     with open('models/scaler.pkl', 'wb') as f:
#         pickle.dump(scaler, f)
        
#     print("\nTraining completed. Model and Scaler saved.")  
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