//+------------------------------------------------------------------+
//|                        OBotTrading_v6.2.mq5                      |
//+------------------------------------------------------------------+
#property copyright "OakJkpG OBot Project"
#property version   "7.00" 
#property description "RNN(GRU) v7.0"

// --- Inputs (v6.2) ---
input string APIServerURL = "http://127.0.0.1:5000";
input int    LookbackBars = 3100; // (ใช้ 120 แท่งฝั่ง api)
input int    MagicNumber  = 12345;
input double MaxLotSize  = 1.0;
input double ProbThreshold = 0.6; 
input double MinATR        = 0.5;
input int    MinTradeIntervalMins = 1;
input double SL_Multiplier = 1.0;
input double TP_Multiplier = 1.5;

// --- Trailing Stop Inputs ---
input bool   UseTrailingStop = true;
input double TrailingStart_ATR_Mult = 1.4;
input double TrailingDist_ATR_Mult = 0.9; 
input int    MaxHoldBars = 12;

// --- Time Filter Inputs ---
input bool   UseTimeFilter  = true;     // เปิดใช้งาน Filter
input int    TradeStartHour = 8;        // เริ่มเทรด 8 โมง (Broker Time)
input int    TradeEndHour   = 20;       // จบเทรด 20 โมง (Broker Time)  

// --- Cooldown Filter ---
input int    TradeCooldownBars = 3; 

// --- [v6.2] Intermarket Analysis Inputs ---
input string IntermarketSymbol = "UsDollar"; // ชื่อ Symbol ดอลลาร์ (ต้องตรงกับใน MT5)

// --- Fail-Safe Inputs (Circuit Breaker) ---
input int    MaxConsecutiveLosses = 3; // ขาดทุนติดกันได้สูงสุดกี่ครั้ง
input int    PenaltyPauseHours    = 1; // ถ้าครบกำหนด ให้หยุดพักกี่ชั่วโมง

//--- Global Variables
string BotStatus = "STOPPED";
string LastSignal = "NONE"; 
string LastRegime = "NONE"; 
datetime LastSignalTime = 0;
double LastProbability = 0.0;
double LastATR = 0.0;
int BarsSinceLastClose = 99;
double LastDynamicRisk = 1.0;

//--- MQL5 JSON Utilities (Basic Implementation)
string ExtractJsonString(string json_data, string key)
{
    string search = "\"" + key + "\":\"";
    int start_pos = StringFind(json_data, search);
    if (start_pos < 0) return "";
    start_pos += StringLen(search);
    int end_pos = StringFind(json_data, "\"", start_pos);
    if (end_pos < 0) return "";
    return StringSubstr(json_data, start_pos, end_pos - start_pos);
}

double ExtractJsonDouble(string json_data, string key)
{
    string search = "\"" + key + "\":";
    int start_pos = StringFind(json_data, search);
    if (start_pos < 0) return 0.0;
    start_pos += StringLen(search);
    int end_pos_comma = StringFind(json_data, ",", start_pos);
    int end_pos_brace = StringFind(json_data, "}", start_pos);
    int end_pos = end_pos_comma;
    if (end_pos < 0 || (end_pos_brace > 0 && end_pos_brace < end_pos_comma)) end_pos = end_pos_brace;
    if (end_pos < 0) return 0.0;
    return StringToDouble(StringSubstr(json_data, start_pos, end_pos - start_pos));
}

// --- OnTick ---
void OnTick()
{
    // --- ตรวจจับการปิดออเดอร์ ---
    static int prev_positions = 0;
    int current_positions = PositionsTotal();
    if (current_positions < prev_positions)
    {
        Print("INFO: Position closed (TP/SL/TS). Starting Cooldown.");
        BarsSinceLastClose = 0;
    }
    prev_positions = current_positions;

    // --- Circuit Breaker Check ---
    int consecutive_losses = 0;
    datetime last_loss_time = 0;
    CheckCircuitBreaker(consecutive_losses, last_loss_time);
    if(consecutive_losses >= MaxConsecutiveLosses)
    {
       long seconds_passed = TimeCurrent() - last_loss_time;
       long penalty_seconds = PenaltyPauseHours * 3600;
       if(seconds_passed < penalty_seconds)
       {
           string remaining = TimeToString((datetime)(penalty_seconds - seconds_passed), TIME_MINUTES|TIME_SECONDS);
           Comment("⛔ CIRCUIT BREAKER ACTIVE ⛔\nLosses: ", consecutive_losses, "\nWaiting: ", remaining);
           return;
       }
    }

    HandleTrailingStops();
    HandleTimeExit();

    // --- ตรรกะการตัดสินใจ ---
    static datetime prev_time = 0;
    MqlRates rates[];
    if (CopyRates(_Symbol, PERIOD_M5, 0, 1, rates) < 1) return;
    datetime current_time = rates[0].time;
    
    if (current_time > prev_time)
    {
        prev_time = current_time;
        BarsSinceLastClose++;
        CheckBotStatus();
        if (BotStatus != "RUNNING") return;

        // --- [แก้ไข] Time Filter Logic ใหม่ ---
        if (UseTimeFilter)
        {
            MqlDateTime dt;
            TimeToStruct(current_time, dt);
            
            // ถ้าชั่วโมงปัจจุบัน น้อยกว่า Start หรือ มากกว่า End -> ห้ามเทรด
            if (dt.hour < TradeStartHour || dt.hour > TradeEndHour)
            {
                // (Optional) Print บอกแค่ครั้งเดียวตอนเปลี่ยนชั่วโมงเพื่อไม่ให้รก Log
                static int last_print_hour = -1;
                if (dt.hour != last_print_hour) {
                    Print(StringFormat("INFO: Outside Trading Hours (%02d:00). Waiting for %02d:00.", dt.hour, TradeStartHour));
                    last_print_hour = dt.hour;
                }
                return; // ⛔ จบการทำงาน ไม่ส่งไปถาม Python
            }
        }
        
        // --- [v6.2] ส่งข้อมูล Multi-Asset ---
        int requestBars = LookbackBars;
        string data_json = GetMultiAssetDataJSON(requestBars);
        string ml_signal = GetSignalFromAPI(data_json);
            
        if (LastProbability < ProbThreshold) ml_signal = "HOLD";
        
        datetime now = TimeCurrent();
        int secondsSinceLast = (int)(now - LastSignalTime);
        if (secondsSinceLast < MinTradeIntervalMins * 60 && ml_signal != LastSignal) ml_signal = "HOLD";
        
        Print(StringFormat("OBot v6.2 (Intermarket): Signal=%s (Prob:%.2f), Cooldown: %d/%d", 
                ml_signal, LastProbability, BarsSinceLastClose, TradeCooldownBars));
        
        if (PositionSelect(_Symbol))
        {
            long position_type = PositionGetInteger(POSITION_TYPE);
            if (position_type == POSITION_TYPE_BUY && ml_signal == "SELL")
            {
                Print("❌ CONFLICT EXIT: ML Signal changed to SELL. Closing BUY position.");
                ClosePositionByConflict();
            }
            else if (position_type == POSITION_TYPE_SELL && ml_signal == "BUY")
            {
                Print("❌ CONFLICT EXIT: ML Signal changed to BUY. Closing SELL position.");
                ClosePositionByConflict();
            }
        }
        else
        {
            if (BarsSinceLastClose > TradeCooldownBars)
            {
                // --- 🔥 เพิ่มการเช็ค MinATR ตรงนี้ 🔥 ---
                if (LastATR < MinATR)
                {
                     // ถ้า ATR ต่ำกว่าเกณฑ์ ให้ข้าม ไม่ต้องเทรด
                     // Print("⚠️ Low Volatility (ATR: ", LastATR, " < ", MinATR, "). Skipping.");
                }
                else if (ml_signal == "BUY") // ถ้าผ่านเกณฑ์ค่อยเช็ค Signal
                {
             
                    Print("✅ GO: ML Signal is BUY. Executing BUY.");
                    ExecuteTrade("BUY", LastATR);
                }
                else if (ml_signal == "SELL")
                {
                    Print("✅ GO: ML Signal is SELL. Executing SELL.");
                    ExecuteTrade("SELL", LastATR);
                }
            }
        }
        
        static int update_counter = 0;
        update_counter++;
        if (update_counter >= 1)
        {
            SendAccountStatusToAPI();
            update_counter = 0;
        }
    }
}

//+------------------------------------------------------------------+
//| CUSTOM FUNCTIONS                                                 |
//+------------------------------------------------------------------+

void CheckBotStatus()
{
    string status_url = "/status";
    string full_url = APIServerURL + status_url;
    uchar post_data[]; 
    string headers = "Content-Type: application/json";
    uchar result[];
    string result_headers;
    int timeout = 5000; 
    
    Print("DEBUG: Requesting status from " + full_url);
    int res = WebRequest("GET", full_url, headers, timeout, post_data, result, result_headers);
    if (res == 200) 
    {
        string json_response = CharArrayToString(result);
        BotStatus = ExtractJsonString(json_response, "bot_status");
        Print("Bot Status: " + BotStatus);
    }
    else
    {
        Print("❌ API Error: CheckBotStatus failed. HTTP " + IntegerToString(res) + " on " + full_url);
    }
}

string GetRatesJSON(string symbol, ENUM_TIMEFRAMES timeframe, int bars)
{
    MqlRates rates[];
    int copied = CopyRates(symbol, timeframe, 0, bars, rates);
    if (copied <= 0)
    {
        Print("❌ GetRatesJSON: CopyRates failed for ", symbol, " ", EnumToString(timeframe));
        return "[]"; 
    }

    string json_array = "[";
    for(int idx = copied - 1, j = 0; idx >= 0; idx--, j++)
    {
        string item = StringFormat(
            "{\"time\":%d, \"open\":%.5f, \"high\":%.5f, \"low\":%.5f, \"close\":%.5f, \"tick_volume\":%d, \"real_volume\":%d}",
            (long)rates[idx].time, rates[idx].open, rates[idx].high, rates[idx].low, rates[idx].close, rates[idx].tick_volume, rates[idx].real_volume);
        json_array += item;
        if (j < copied - 1) json_array += ",";
    }
    json_array += "]";
    return json_array;
}

// 🛑 [v6.2] (ฟังก์ชันที่ 2) - ส่ง Multi-Asset (XAU + USD) 🛑
string GetMultiAssetDataJSON(int m5_bars)
{
    // 1. XAUUSD Data (M5 Only)
    string m5_json = GetRatesJSON(_Symbol, PERIOD_M5, m5_bars);
    
    // 2. Intermarket Data (UsDollar M5 Only)
    string usd_m5_json = "[]";
    if (SymbolSelect(IntermarketSymbol, true))
    {
        usd_m5_json = GetRatesJSON(IntermarketSymbol, PERIOD_M5, m5_bars);
    }
    else
    {
        Print("⚠️ Warning: Intermarket Symbol '", IntermarketSymbol, "' not found.");
    }

    // สร้าง JSON ที่เล็กลง (ส่ง field ว่างไปหลอก Python ในส่วนที่ไม่ใช้)
    string final_json = StringFormat(
        "{\"m5_data\":%s, \"usd_m5\":%s, \"m30_data\":[], \"h1_data\":[], \"h4_data\":[], \"usd_h1\":[]}",
        m5_json, usd_m5_json    
    );
    
    return final_json;
}

string GetSignalFromAPI(string data_json)
{
    string predict_url = "/predict";
    string headers = "Content-Type: application/json";
    uchar post_data[];
    uchar body[];
    uchar result[];
    string result_headers;
    int timeout = 10000;
    int data_size = StringToCharArray(data_json, post_data, 0, WHOLE_ARRAY);
    ArrayResize(body, data_size);
    for (int i = 0; i < data_size; i++) body[i] = post_data[i];
    string full_url = APIServerURL + predict_url;
    
    int res = WebRequest("POST", full_url, headers, timeout, body, result, result_headers);
    if (res == 200)
    {
        string json_response = CharArrayToString(result);
        string signal = ExtractJsonString(json_response, "signal");
        double probability = ExtractJsonDouble(json_response, "probability");
        double atr_value = ExtractJsonDouble(json_response, "atr");
        double dynamic_risk = ExtractJsonDouble(json_response, "dynamic_risk");
        string regime = ExtractJsonString(json_response, "regime"); 
        
        Print(StringFormat("DEBUG: Parsed Regime=%s, Signal=%s, Prob=%.4f, ATR=%.4f, DynRisk=%.1f%%",
              regime, signal, probability, atr_value, dynamic_risk));

        LastProbability = probability;
        LastSignal = signal;
        LastATR = atr_value;
        LastRegime = (regime == "") ? "NONE" : regime; 
        
        if (dynamic_risk > 0.0) LastDynamicRisk = dynamic_risk;
        else LastDynamicRisk = 1.0;
        
        return LastSignal;
    }
    else
    {
        Print("Error getting signal: HTTP " + IntegerToString(res));
        LastATR = 0.0;
        LastDynamicRisk = 1.0; 
        LastRegime = "NONE"; 
        return "NONE";
    }
}

void ExecuteTrade(string signal, double atr_value)
{
    if (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) == 0 || AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) == 0) return;
    if (PositionSelect(_Symbol)) return;

    if (atr_value <= 0.0) { Print("❌ ExecuteTrade Error: Invalid LastATR."); return; }
    
    double sl_distance = atr_value * SL_Multiplier;
    if (sl_distance <= 0.0) return;

    double risk_amount = AccountInfoDouble(ACCOUNT_BALANCE) * (LastDynamicRisk / 100.0);
    double contract_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
    double calculated_lots = risk_amount / (sl_distance * contract_size);

    MqlTradeRequest request;
    MqlTradeResult  result;
    ZeroMemory(request);
    ZeroMemory(result);
    
    double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
    double lotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
    double volume = calculated_lots;
    if (lotStep > 0) volume = MathFloor(volume / lotStep) * lotStep;
    volume = MathMax(minLot, MathMin(MaxLotSize, volume));
    if (volume < minLot) return;

    request.action    = TRADE_ACTION_DEAL;
    request.symbol    = _Symbol;
    request.volume    = volume;
    request.deviation = 50;
    request.magic     = MagicNumber;
    request.type_filling = ORDER_FILLING_IOC;
    request.type_time    = ORDER_TIME_GTC;
    request.sl = 0.0;
    request.tp = 0.0;
    
    MqlTick tick;
    if(!SymbolInfoTick(_Symbol, tick)) { Print("❌ Failed to get tick"); return; }
    if (TimeCurrent() - tick.time > 10) { Print("⚠️ Tick data is stale"); return; }

    if (signal == "BUY") {
        request.type = ORDER_TYPE_BUY;
        request.comment = "RNN_v6.2_BUY";
        request.price = tick.ask;
    } else if (signal == "SELL") {
        request.type = ORDER_TYPE_SELL;
        request.comment = "RNN_v6.2_SELL";
        request.price = tick.bid;
    } else return;

    bool sent = OrderSend(request, result);
    if (sent && (result.retcode == TRADE_RETCODE_DONE || result.retcode == TRADE_RETCODE_PLACED))
    {
        Print("✅ Order Opened. Deal: ", (string)result.deal, ". Setting Dynamic SL/TP...");
        ModifyOrderSLTP(result.deal, signal, atr_value);
        string alert_msg = StringFormat("✅ %s Order Opened: Price %.5f, Lots %.2f", signal, request.price, volume);
        SendTradeAlert(alert_msg);
        LastSignalTime = TimeCurrent();
    }
    else Print("❌ ", signal, " failed: ", result.retcode, " ", result.comment);
}

void SendAccountStatusToAPI(string alert_message = "")
{
    string update_url = "/update_status";
    double balance = AccountInfoDouble(ACCOUNT_BALANCE);
    double equity = AccountInfoDouble(ACCOUNT_EQUITY);
    double margin_free = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
    int open_trades = PositionsTotal();
    string payload = StringFormat(
        "{\"balance\":%.2f, \"equity\":%.2f, \"margin_free\":%.2f, \"open_trades\":%d, \"alert_message\":\"%s\", \"account_type\":\"%s\"}",
        balance, equity, margin_free, open_trades, alert_message, "DEMO");
    
    string headers = "Content-Type: application/json";
    uchar post_data[];
    uchar body[];
    uchar result[];
    string result_headers;
    int timeout = 5000;
    int data_size = StringToCharArray(payload, post_data, 0, WHOLE_ARRAY);
    ArrayResize(body, data_size);
    for (int i = 0; i < data_size; i++) body[i] = post_data[i];
    string full_url = APIServerURL + update_url;
    WebRequest("POST", full_url, headers, timeout, body, result, result_headers);
}

void ModifyOrderSLTP(ulong deal_ticket, string signal, double atr_value)
{
    if(atr_value <= 0.0)
    {
        Print("❌ ModifyOrderSLTP Error: Invalid ATR value received from API (<= 0.0). Aborting modify.");
        return;
    }

    if (!PositionSelect(_Symbol))
    {
        Print("❌ ModifyOrderSLTP Error: Could not select position by _Symbol after opening deal ", (string)deal_ticket);
        return;
    }
    
    double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
    ulong position_ticket = PositionGetInteger(POSITION_TICKET); 
    
    int min_stop_points = (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
    if (min_stop_points <= 0) min_stop_points = 1; 
    double min_stop_price_dist = MathMax(min_stop_points * _Point, 10 * _Point);
    
    MqlTradeRequest request_mod;
    MqlTradeResult  result_mod;
    ZeroMemory(request_mod);
    ZeroMemory(result_mod);
    request_mod.action = TRADE_ACTION_SLTP;
    request_mod.position = position_ticket;
    request_mod.symbol = _Symbol;
    
    double tp_mult = TP_Multiplier;
    
    double sl_points_dynamic = (atr_value * SL_Multiplier);
    double tp_points_dynamic = (atr_value * tp_mult); // ⬅️ [แก้ไข]

    double sl_price = 0.0;
    double tp_price = 0.0;
    if (signal == "BUY")
    {
        sl_price = NormalizeDouble(open_price - sl_points_dynamic, _Digits);
        tp_price = NormalizeDouble(open_price + tp_points_dynamic, _Digits);
    }
    else if (signal == "SELL")
    {
        sl_price = NormalizeDouble(open_price + sl_points_dynamic, _Digits);
        tp_price = NormalizeDouble(open_price - tp_points_dynamic, _Digits);
    }

    request_mod.sl = sl_price;
    request_mod.tp = tp_price;
    
    // (ปรับค่า SL/TP ให้ตรงตามกฎโบรกเกอร์ - เหมือนเดิม)
    if (signal == "BUY")
    {
        if (open_price - request_mod.sl < min_stop_price_dist)
        {
             request_mod.sl = NormalizeDouble(open_price - min_stop_price_dist, _Digits);
             Print("DEBUG: Adjusted BUY SL (ATR) to meet min_stop: ", DoubleToString(request_mod.sl, _Digits));
        }
        if (request_mod.tp - open_price < min_stop_price_dist)
        {
             request_mod.tp = NormalizeDouble(open_price + min_stop_price_dist, _Digits);
             Print("DEBUG: Adjusted BUY TP (ATR) to meet min_stop: ", DoubleToString(request_mod.tp, _Digits));
        }
    }
    else if (signal == "SELL")
    {
        if (request_mod.sl - open_price < min_stop_price_dist)
        {
             request_mod.sl = NormalizeDouble(open_price + min_stop_price_dist, _Digits);
             Print("DEBUG: Adjusted SELL SL (ATR) to meet min_stop: ", DoubleToString(request_mod.sl, _Digits));
        }
        if (open_price - request_mod.tp < min_stop_price_dist)
        {
             request_mod.tp = NormalizeDouble(open_price - min_stop_price_dist, _Digits);
             Print("DEBUG: Adjusted SELL TP (ATR) to meet min_stop: ", DoubleToString(request_mod.tp, _Digits));
        }
    }

    Print("DEBUG: Modifying position #", (string)position_ticket, " with [DYNAMIC ATR] SL=", DoubleToString(request_mod.sl, _Digits), " TP=", DoubleToString(request_mod.tp, _Digits));
    bool modified = OrderSend(request_mod, result_mod);
    
    if(modified && (result_mod.retcode == TRADE_RETCODE_DONE || result_mod.retcode == TRADE_RETCODE_PLACED))
    {
        Print("✅ Dynamic (ATR) SL/TP successfully set for position #", (string)position_ticket);
    }
    else
    {
        Print("❌ ModifyOrderSLTP failed (Dynamic ATR): retcode=", result_mod.retcode, " comment=", result_mod.comment);
    }
}

void HandleTrailingStops()
{
    if (!UseTrailingStop)
    {
        return;
    }
    if (LastATR <= 0.0) return;
    if (!PositionSelect(_Symbol))
    {
        return;
    }
    
    double TrailingStartPoints_Dynamic = (LastATR * TrailingStart_ATR_Mult) / _Point;
    double TrailingDistancePoints_Dynamic = (LastATR * TrailingDist_ATR_Mult) / _Point;

    ulong position_ticket = PositionGetInteger(POSITION_TICKET);
    long position_type = PositionGetInteger(POSITION_TYPE); 
    double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
    double current_sl = PositionGetDouble(POSITION_SL);
    double current_tp = PositionGetDouble(POSITION_TP); 
    
    MqlTick tick;
    if(!SymbolInfoTick(_Symbol, tick)) { return; } 

    double new_sl_price = 0.0;
    double profit_points = 0.0;
    
    if (position_type == POSITION_TYPE_BUY)
    {
        new_sl_price = NormalizeDouble(tick.bid - (TrailingDistancePoints_Dynamic * _Point), _Digits);
        profit_points = (tick.bid - open_price) / _Point;
        
        if (profit_points >= TrailingStartPoints_Dynamic && new_sl_price > current_sl)
        {
             if(new_sl_price >= tick.bid) return;
             Print("DEBUG: Trailing BUY Stop. Profit: ", profit_points, "p. Moving SL to: ", DoubleToString(new_sl_price, _Digits));
             SendModifySLTP(position_ticket, new_sl_price, current_tp);
        }
    }
    else if (position_type == POSITION_TYPE_SELL)
    {
        new_sl_price = NormalizeDouble(tick.ask + (TrailingDistancePoints_Dynamic * _Point), _Digits);
        profit_points = (open_price - tick.ask) / _Point;
        
        if (profit_points >= TrailingStartPoints_Dynamic && (new_sl_price < current_sl || current_sl == 0.0))
        {
             if(new_sl_price <= tick.ask) return;
             Print("DEBUG: Trailing SELL Stop. Profit: ", profit_points, "p. Moving SL to: ", DoubleToString(new_sl_price, _Digits));
             SendModifySLTP(position_ticket, new_sl_price, current_tp);
        }
    }
}

void SendModifySLTP(ulong position_ticket, double sl_price, double tp_price)
{
    MqlTradeRequest request_mod;
    MqlTradeResult  result_mod;
    ZeroMemory(request_mod);
    ZeroMemory(result_mod);
    
    request_mod.action = TRADE_ACTION_SLTP;
    request_mod.position = position_ticket;
    request_mod.symbol = _Symbol;
    request_mod.sl = sl_price;
    request_mod.tp = tp_price;
    
    bool modified = OrderSend(request_mod, result_mod);
    if(!modified)
    {
        Print("❌ SendModifySLTP failed: retcode=", result_mod.retcode, " comment=", result_mod.comment);
    }
}

void HandleTimeExit()
{
    if (MaxHoldBars <= 0) { return; }
    if (!PositionSelect(_Symbol)) { return; }
    
    long open_time = PositionGetInteger(POSITION_TIME);
    ulong position_ticket = PositionGetInteger(POSITION_TICKET);
    long position_type = PositionGetInteger(POSITION_TYPE);
    double position_volume = PositionGetDouble(POSITION_VOLUME);
    long seconds_held = TimeCurrent() - open_time;
    long m5_period_seconds = PeriodSeconds(PERIOD_M5);
    
    if (m5_period_seconds <= 0) return;
    int bars_held = (int)(seconds_held / m5_period_seconds);

    if (bars_held >= MaxHoldBars)
    {
        Print("❌ TIME EXIT: Position #", (string)position_ticket, " held for ", (string)bars_held, " M5 bars (>= Max ", (string)MaxHoldBars, "). Closing position.");
        MqlTradeRequest request;
        MqlTradeResult  result;
        ZeroMemory(request);
        ZeroMemory(result);
        request.action = TRADE_ACTION_DEAL;
        request.symbol = _Symbol;
        request.volume = position_volume;
        request.magic  = MagicNumber;
        request.position = position_ticket;
        request.type_filling = ORDER_FILLING_IOC;
        MqlTick tick;
        if(!SymbolInfoTick(_Symbol, tick)) { Print("❌ TimeExit: Failed to get tick"); return; }

        if (position_type == POSITION_TYPE_BUY)
        {
            request.type = ORDER_TYPE_SELL;
            request.price = tick.bid;
            request.comment = "RNN_BOT_TimeExit_CloseBUY";
        }
        else // ปิด SELL
        {
            request.type = ORDER_TYPE_BUY;
            request.price = tick.ask;
            request.comment = "RNN_BOT_TimeExit_CloseSELL";
        }

        if (OrderSend(request, result))
        {
            SendTradeAlert(StringFormat("⛔️ TIME EXIT: Closed position #%I64u at market", position_ticket));
            BarsSinceLastClose = 0;
        }
        else
        {
            Print("❌ TimeExit OrderSend failed: ", result.retcode, " ", result.comment);
        }
    }
}

// --- SendTradeAlert (ไม่เปลี่ยนแปลง) ---
void SendTradeAlert(string alert_message)
{
    SendAccountStatusToAPI(alert_message);
}

void ClosePositionByConflict()
{
    if (!PositionSelect(_Symbol))
    {
        Print("❌ ClosePositionByConflict: Failed to select position.");
        return;
    }
    
    ulong position_ticket = PositionGetInteger(POSITION_TICKET);
    long position_type = PositionGetInteger(POSITION_TYPE);
    double position_volume = PositionGetDouble(POSITION_VOLUME);
    
    MqlTradeRequest request;
    MqlTradeResult  result;
    ZeroMemory(request);
    ZeroMemory(result);
    
    request.action = TRADE_ACTION_DEAL;
    request.symbol = _Symbol;
    request.volume = position_volume;
    request.magic  = MagicNumber;
    request.position = position_ticket;
    request.type_filling = ORDER_FILLING_IOC;
    
    MqlTick tick;
    if(!SymbolInfoTick(_Symbol, tick)) { Print("❌ ConflictExit: Failed to get tick"); return; }

    if (position_type == POSITION_TYPE_BUY) // ปิด BUY
    {
        request.type = ORDER_TYPE_SELL;
        request.price = tick.bid;
        request.comment = "RNN_v6_Conflict_CloseBUY"; // [v6]
    }
    else // ปิด SELL
    {
        request.type = ORDER_TYPE_BUY;
        request.price = tick.ask;
        request.comment = "RNN_v6_Conflict_CloseSELL"; // [v6]
    }

    if (OrderSend(request, result))
    {
        SendTradeAlert(StringFormat("⛔️ CONFLICT EXIT: Closed position #%I64u at market due to ML signal change.", position_ticket));
        BarsSinceLastClose = 0;
    }
    else
    {
        Print("❌ ConflictExit OrderSend failed: ", result.retcode, " ", result.comment);
    }
}

void CheckCircuitBreaker(int &loss_count, datetime &last_loss_time)
{
   loss_count = 0;
   last_loss_time = 0;
   
   HistorySelect(0, TimeCurrent());
   int total_deals = HistoryDealsTotal();
   
   // วนลูปจากออเดอร์ล่าสุดย้อนหลัง
   for(int i = total_deals - 1; i >= 0; i--)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      
      long deal_entry = HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(deal_entry != DEAL_ENTRY_OUT) continue; // ดูเฉพาะตอนปิดออเดอร์
      
      string symbol = HistoryDealGetString(ticket, DEAL_SYMBOL);
      if(symbol != _Symbol) continue;
      
      long magic = HistoryDealGetInteger(ticket, DEAL_MAGIC);
      if(magic != MagicNumber) continue;
      
      double profit = HistoryDealGetDouble(ticket, DEAL_PROFIT);
      
      if(profit < 0)
      {
         loss_count++;
         if(last_loss_time == 0) last_loss_time = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);
      }
      else if(profit > 0)
      {
         // ถ้าเจอกำไร ให้หยุดนับทันที (เพราะ Chain ขาดทุนถูกตัดแล้ว)
         break;
      }
   }
}
//+------------------------------------------------------------------+