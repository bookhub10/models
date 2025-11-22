import pandas as pd
import numpy as np
import talib
from sklearn.preprocessing import MinMaxScaler

# ==============================================================================
# PART 1.5: FEATURE ENGINEERING (v6.1 - 19 Features)
# ==============================================================================

# --- 🛑 [อัปเดต] ฟังก์ชันนี้ (v6.1 - 19 Features) ---
def add_technical_indicators(df_m5, df_m30, df_h1, df_h4):
    """
    เวอร์ชัน v6.1 (แก้ไข): 19 Features
    (นำโค้ดมาจาก compute_features ของ model.py)
    """
    
    df_m5 = df_m5.sort_index().copy()
    df_m30 = df_m30.sort_index().copy()
    df_h1 = df_h1.sort_index().copy()
    df_h4 = df_h4.sort_index().copy()

    close = df_m5['close'].astype(float).values
    high = df_m5['high'].astype(float).values
    low = df_m5['low'].astype(float).values

    df = df_m5.copy()
    df['ret_1'] = df['close'].pct_change(1)
    df['ret_5'] = df['close'].pct_change(5)
    df['ret_10'] = df['close'].pct_change(10)
    df['vol_rolling'] = df['tick_volume'].rolling(20).std().fillna(0)

    df['ATR_14'] = talib.ATR(high, low, close, timeperiod=14)
    k, d = talib.STOCH(high, low, close, 14, 3, 3)
    df['Stoch_K'] = k
    macd, macdsig, macdh = talib.MACD(close, 12, 26, 9)
    df['MACD_Hist'] = macdh
    df['ADX_14'] = talib.ADX(high, low, close, timeperiod=14)

    df_m30['M30_RSI'] = talib.RSI(df_m30['close'].astype(float).values, timeperiod=14)
    df_h1['H1_MA_200'] = talib.SMA(df_h1['close'].astype(float).values, timeperiod=200)
    df_h1['H1_Dist_MA200'] = (df_h1['close'] - df_h1['H1_MA_200']) / df_h1['close']
    df_h4['H4_MA_50'] = talib.SMA(df_h4['close'].astype(float).values, timeperiod=50)
    df_h4['H4_Dist_MA50'] = (df_h4['close'] - df_h4['H4_MA_50']) / df_h4['close']

    atr = df['ATR_14'].ffill()
    ma20 = df['close'].rolling(20).mean()
    df['k_upper'] = ma20 + 1.5 * atr
    df['k_lower'] = ma20 - 1.5 * atr

    m30 = df_m30[['M30_RSI']].sort_index()
    h1 = df_h1[['H1_Dist_MA200']].sort_index()
    h4 = df_h4[['H4_Dist_MA50']].sort_index()

    merged = pd.merge_asof(df.sort_index(), m30, left_index=True, right_index=True, direction='backward')
    merged = pd.merge_asof(merged.sort_index(), h1, left_index=True, right_index=True, direction='backward')
    merged = pd.merge_asof(merged.sort_index(), h4, left_index=True, right_index=True, direction='backward')

    merged['hour'] = merged.index.hour
    merged['dow'] = merged.index.dayofweek

    # --- ⬇️ [อัปเดต v6.1] 19 Features ⬇️ ---
    cols = ['high','low','close','tick_volume',
            'ATR_14','Stoch_K','MACD_Hist','ADX_14',
            'M30_RSI','H1_Dist_MA200','H4_Dist_MA50',
            'ret_1','ret_5','ret_10','vol_rolling',
            'hour','dow',
            'k_upper','k_lower' # ⬅️ [ใหม่]
            ]
    
    merged = merged[cols].copy()
    
    merged.dropna(inplace=True)
    merged.reset_index(inplace=True) # คืน 'time' กลับมาเป็นคอลัมน์
    print(f"Features computed (v6.1 - 19f). Rows: {len(merged)}")
    
    return merged

# ==============================================================================
# PART 2: SCALING
# ==============================================================================
def scale_features(train_df, test_df=None, scaler=None):
    if scaler is None:
        scaler = MinMaxScaler(feature_range=(0, 1))
        train_scaled = scaler.fit_transform(train_df)
    else:
        train_scaled = scaler.transform(train_df)
    train_scaled_df = pd.DataFrame(train_scaled, columns=train_df.columns)
    if test_df is not None:
        test_scaled = scaler.transform(test_df)
        test_scaled_df = pd.DataFrame(test_scaled, columns=test_df.columns)
        return scaler, train_scaled_df, test_scaled_df
    return scaler, train_scaled_df, None