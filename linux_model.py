import pandas as pd
import numpy as np
import talib
from sklearn.preprocessing import RobustScaler

# ==============================================================================
# CONFIG: Feature List
# ==============================================================================
REQUIRED_FEATURES = [
    'log_ret_1', 'log_ret_5', 'dist_ema50', 'dist_h1_ema',
    'body_pct', 'upper_wick_pct', 'lower_wick_pct',
    'vol_force',
    'dist_pivot', 'dist_r1', 'dist_s1',
    'atr_14', 'atr_pct', 'rsi_14',
    'hour_sin', 'hour_cos',
    'usd_ret_5', 'usd_corr'
]

# ==============================================================================
# PART 1: FEATURE ENGINEERING
# ==============================================================================

def compute_features_lite(df_m5, df_usd=None):
    """
    สร้าง 18 Features สำหรับ Lite Model (M5 Scalping)
    รองรับ Intermarket Analysis (USD)
    """
    # 1. Prepare Data
    df = df_m5.sort_index().copy()
    
    # แปลงเป็น Float64 ให้หมดเพื่อความแม่นยำของ TA-Lib
    # (ป้องกัน Error ใน Linux บาง Environment)
    try:
        open_p = df['open'].astype(float).values
        high_p = df['high'].astype(float).values
        low_p  = df['low'].astype(float).values
        close_p = df['close'].astype(float).values
        volume = df['tick_volume'].astype(float).values
    except KeyError as e:
        print(f"❌ Error: Missing columns in DataFrame. {e}")
        return pd.DataFrame()

    # --- 1. Basic Momentum & Trend ---
    # Log Returns
    df['log_ret_1'] = np.log(df['close'] / df['close'].shift(1))
    df['log_ret_5'] = np.log(df['close'] / df['close'].shift(5))
    
    # EMA 50 Dist (Trend M5)
    ema50 = talib.EMA(close_p, timeperiod=50)
    df['dist_ema50'] = (close_p - ema50) / close_p
    
    # H1 Trend Context (สร้าง H1 จาก M5)
    # Resample 1H เพื่อดูเทรนด์ใหญ่
    df_h1 = df.resample('1H').agg({'close': 'last'}).dropna()
    df_h1['ema50_h1'] = talib.EMA(df_h1['close'].astype(float).values, timeperiod=50)
    
    # Map H1 EMA กลับมาใส่ M5 (ffill = ใช้ค่าล่าสุดที่มีอยู่)
    df['ema50_h1'] = df_h1['ema50_h1'].reindex(df.index, method='ffill')
    df['dist_h1_ema'] = (df['close'] - df['ema50_h1']) / df['close']
    
    # --- 2. Candle Psychology (Price Action) ---
    candle_range = (high_p - low_p) + 1e-9
    body_size = np.abs(close_p - open_p)
    upper_wick = high_p - np.maximum(close_p, open_p)
    lower_wick = np.minimum(close_p, open_p) - low_p
    
    df['body_pct'] = body_size / candle_range
    df['upper_wick_pct'] = upper_wick / candle_range
    df['lower_wick_pct'] = lower_wick / candle_range
    
    # --- 3. Volume Force ---
    vol_sma = talib.SMA(volume, timeperiod=20) + 1e-9
    df['vol_force'] = (volume * np.sign(close_p - open_p)) / vol_sma

    # --- 4. Daily Pivots (Support/Resistance) ---
    # Resample เป็นรายวัน (D)
    # shift(1) สำคัญมาก! เพื่อใช้ราคาปิดเมื่อวานคำนวณ Pivot วันนี้
    df_day = df.resample('D').agg({
        'high': 'max', 'low': 'min', 'close': 'last'
    }).shift(1).dropna()
    
    df_day['Pivot'] = (df_day['high'] + df_day['low'] + df_day['close']) / 3
    df_day['R1'] = (2 * df_day['Pivot']) - df_day['low']
    df_day['S1'] = (2 * df_day['Pivot']) - df_day['high']
    
    # Map กลับมาที่ M5
    df['Pivot'] = df_day['Pivot'].reindex(df.index, method='ffill')
    df['R1']    = df_day['R1'].reindex(df.index, method='ffill')
    df['S1']    = df_day['S1'].reindex(df.index, method='ffill')
    
    # Calculate Distances
    df['dist_pivot'] = (close_p - df['Pivot']) / close_p
    df['dist_r1']    = (close_p - df['R1']) / close_p
    df['dist_s1']    = (close_p - df['S1']) / close_p

    # --- 5. Volatility & Time ---
    df['atr_14'] = talib.ATR(high_p, low_p, close_p, timeperiod=14)
    df['atr_pct'] = df['atr_14'] / close_p
    df['rsi_14'] = talib.RSI(close_p, timeperiod=14)
    
    # Time Encoding
    df['hour_sin'] = np.sin(2 * np.pi * df.index.hour / 24.0)
    df['hour_cos'] = np.cos(2 * np.pi * df.index.hour / 24.0)

    # --- 6. Intermarket Analysis (USD) ---
    if df_usd is not None and not df_usd.empty:
        # Align Data: บังคับให้ USD มาเกาะเวลาเดียวกับ Gold
        usd_close = df_usd['close'].reindex(df.index, method='ffill').fillna(method='bfill')
        
        usd_vals = usd_close.astype(float).values
        gold_vals = df['close'].astype(float).values
        
        # USD Momentum
        df['usd_ret_5'] = np.log(usd_vals / (pd.Series(usd_vals).shift(5).values + 1e-9))
        
        # Correlation (12 bars window)
        df['usd_corr'] = df['close'].rolling(12).corr(usd_close)
    else:
        # Fallback กรณีไม่มีข้อมูล USD
        df['usd_ret_5'] = 0.0
        df['usd_corr'] = -1.0 # สมมติว่าสวนทางปกติ

    # --- Final Selection ---
    # เลือกเฉพาะ Column ที่ต้องใช้
    available_cols = [c for c in REQUIRED_FEATURES if c in df.columns]
    
    # ถ้ามี column ไหนหายไป ให้เติม 0 (Fail-safe)
    for col in REQUIRED_FEATURES:
        if col not in df.columns:
            df[col] = 0.0
            
    df = df[REQUIRED_FEATURES].copy()
    
    # Clean NaN/Inf
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    df.dropna(inplace=True)
    
    print(f"✅ Features computed (Lite). Rows: {len(df)}")
    return df

# ==============================================================================
# PART 2: SCALING (RobustScaler)
# ==============================================================================

def scale_features(df_features, scaler):
    """
    Scale ข้อมูลโดยใช้ Scaler ที่โหลดมาจากไฟล์ Pickle (.pkl)
    """
    if scaler is None:
        raise ValueError("Scaler is None. Cannot transform features.")
    
    # ตรวจสอบว่า Feature ครบไหม
    if list(df_features.columns) != REQUIRED_FEATURES:
        # พยายาม Reorder ให้ตรง
        try:
            df_features = df_features[REQUIRED_FEATURES]
        except KeyError as e:
             print(f"❌ Scaling Error: Missing columns {e}")
             return None

    # Transform
    # Scaler ถูก Fit มาแบบ 2D (n_samples, n_features)
    # เราส่ง DataFrame เข้าไป มันจะคืนค่าเป็น Numpy Array
    try:
        scaled_array = scaler.transform(df_features)
        
        # แปลงกลับเป็น DataFrame เพื่อความง่ายในการ Debug (Optional)
        # หรือส่งกลับเป็น Array เลยก็ได้ แต่ API มักจะเอา Array ไปเข้า Model ต่อ
        return scaled_array
        
    except Exception as e:
        print(f"❌ Error during scaling: {e}")
        return None