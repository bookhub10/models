//+------------------------------------------------------------------+
//|                        XAUUSD_RNN_Bot.mq5                        |
//+------------------------------------------------------------------+
#property copyright "OakJkpG OBot Project"
#property version   "1.00"
#property description "RNN(GRU)-powered XAUUSD Trading Bot via Flask API"
//--- (ส่วน Input Parameters และ JSON Utilities ไม่มีการเปลี่ยนแปลง) ---
input string APIServerURL = "http://127.0.0.1:5000"; 
input int    LookbackBars = 50;  
input int    MagicNumber  = 12345;
input double Lots         = 0.01;
input double ProbThreshold = 0.50; // minimum probability to act on signal
input int    MinTradeIntervalMins = 1; // minimum minutes between trades

// --- ⬇️ เพิ่ม 2 บรรทัดนี้ ⬇️ ---
input double SL_Multiplier = 2.0; // SL = ATR * 2.0
input double TP_Multiplier = 3.0; // TP = ATR * 3.0

//--- Global Variables
string BotStatus = "STOPPED"; 
string LastSignal = "NONE";
datetime LastSignalTime = 0;
double LastProbability = 0.0;

double LastProbability = 0.0;

// --- ⬇️ แก้ไข 2 บรรทัดนี้ ⬇️ ---
double LastATR = 0.0; // ATR ที่ได้จาก API
// (ลบ LastSLPrice และ LastTPPrice ทิ้งไปเลย)

//--- MQL5 JSON Utilities (Basic Implementation)
// Function to safely extract a string value from a JSON response
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

// Function to safely extract a double/numeric value from a JSON response
double ExtractJsonDouble(string json_data, string key)
{
    string search = "\"" + key + "\":";
    int start_pos = StringFind(json_data, search);
    if (start_pos < 0) return 0.0;
    
    start_pos += StringLen(search);
    
    int end_pos_comma = StringFind(json_data, ",", start_pos);
    int end_pos_brace = StringFind(json_data, "}", start_pos);
    
    int end_pos = end_pos_comma;
    if (end_pos < 0 || (end_pos_brace > 0 && end_pos_brace < end_pos_comma))
    {
        end_pos = end_pos_brace;
    }
    
    if (end_pos < 0) return 0.0;
    
    return StringToDouble(StringSubstr(json_data, start_pos, end_pos - start_pos));
}

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
    // ตรวจสอบว่า URL ถูกเพิ่มแล้วหรือไม่ (ทำด้วยมือ)
    Print("🔔 INFO: Ensure " + APIServerURL + " is added to WebRequest allowed URLs.");
    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    // ... Deinitialization code ...
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
    // 1. ตรวจสอบแท่งเทียนใหม่ (สำคัญมากสำหรับ M5)
    static datetime prev_time = 0;
    MqlRates rates[];
    if (CopyRates(_Symbol, PERIOD_M5, 0, 1, rates) < 1) return;
    datetime current_time = rates[0].time;
    
    // ทำงานเมื่อมีแท่งเทียน M5 ใหม่เท่านั้น
    if (current_time > prev_time)
    {
        prev_time = current_time;
        
        // 2. ดึงสถานะ Bot จาก API (เพื่อรับคำสั่ง START/STOP จาก Telegram)
        CheckBotStatus(); 
        
        if (BotStatus == "RUNNING")
        {
            // โค้ดใหม่ (ขอเผื่อเป็น 200 แท่ง)
            int requestBars = MathMax(LookbackBars, 100) + 100; 
            string data_json = GetXAUUSDDataJSON(requestBars);
            
            // 4. ส่งข้อมูลไปยัง Flask API และรับสัญญาณ
            string signal = GetSignalFromAPI(data_json);

            // 5. Behavior: Execute trade if signal is strong AND there is NO existing position
            
            // 🛑 รวม Logic ของ BUY และ SELL เข้าด้วยกัน เพื่อให้ใช้เงื่อนไขเดียวกัน
            if (signal == "BUY" || signal == "SELL") 
            {
                int totalPositions = PositionsTotal();
                bool hasPosition = PositionSelect(_Symbol);
                datetime now = TimeCurrent();
                int secondsSinceLast = (int)(now - LastSignalTime);

                Print("DEBUG: Trade decision check for ", signal, ": LastProbability=", DoubleToString(LastProbability,6),
                      " ProbThreshold=", DoubleToString(ProbThreshold,2),
                      " SecondsSinceLast=", IntegerToString(secondsSinceLast),
                      " PositionsTotal=", IntegerToString(totalPositions));

                if (!hasPosition)
                {
                    // 1. ตรวจสอบ Probability ของ BUY
                    if (signal == "BUY" && LastProbability < ProbThreshold)
                    {
                        Print("DEBUG: Skipping BUY - probability (", DoubleToString(LastProbability,6), ") < threshold (", DoubleToString(ProbThreshold,2), ").");
                    }
                    // 2. ตรวจสอบ Probability ของ SELL (ต้องใช้ 1.0 ลบ)
                    else if (signal == "SELL" && (1.0 - LastProbability) < ProbThreshold)
                    {
                        // คำนวณ Prob ของ SELL เพื่อแสดงผล
                        double sell_prob = 1.0 - LastProbability;
                        Print("DEBUG: Skipping SELL - probability (", DoubleToString(sell_prob,6), ") < threshold (", DoubleToString(ProbThreshold,2), ").");
                    }
                    // 3. ตรวจสอบเงื่อนไขอื่นๆ (เหมือนเดิม)
                    else if (secondsSinceLast < MinTradeIntervalMins * 60)
                    {
                        Print("DEBUG: Skipping ", signal, " - within MinTradeInterval (", IntegerToString(secondsSinceLast), "s).");
                    }
                    // 4. ถ้าผ่านหมด ให้เทรด
                    else
                    {
                        Print("DEBUG: Conditions met - attempting ExecuteTrade(\"", signal, "\").");
                        ExecuteTrade(signal);
                    }
                }
                else
                {
                    Print("DEBUG: Received ", signal, " but existing position detected - skipping open.");
                }
            }
        }
        
        // 6. อัปเดตสถานะบัญชีไปยัง API ทุกๆ 1 แท่ง M5
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

// (A) Check Bot Status from API
void CheckBotStatus()
{
    // 🛑 แก้ไข: Endpoint ต้องเป็น /status ไม่ใช่ /update_status 
    string status_url = "/status"; 
    string full_url = APIServerURL + status_url;
    uchar post_data[]; // ต้องเป็น Array เปล่าสำหรับ GET
    string headers = "Content-Type: application/json";
    uchar result[];
    string result_headers;
    int timeout = 5000; 
    
    Print("DEBUG: Requesting status from " + full_url); 
    
    // WebRequest ต้องเป็น "GET" และใช้ post_data เปล่า
    int res = WebRequest("GET", full_url, headers, timeout, post_data, result, result_headers);
    
    if (res == 200) 
    {
        string json_response = CharArrayToString(result);
        BotStatus = ExtractJsonString(json_response, "bot_status");
        Print("Bot Status: " + BotStatus);
    }
    else
    {
        // ตอนนี้หากเกิด 404, 500 จะแสดง Error ที่ถูกต้อง
        Print("❌ API Error: CheckBotStatus failed. HTTP " + IntegerToString(res) + " on " + full_url); 
    }
}

// (B) Get XAUUSD Data in JSON format
// 🛑 [ฟังก์ชันใหม่ที่ 1] - ฟังก์ชันช่วยดึงข้อมูล
// เราสร้างฟังก์ชันนี้ขึ้นมาเพื่อลดการเขียนโค้ดซ้ำ
string GetRatesJSON(ENUM_TIMEFRAMES timeframe, int bars)
{
    MqlRates rates[];
    int copied = CopyRates(_Symbol, timeframe, 0, bars, rates);
    if (copied <= 0)
    {
        Print("❌ GetRatesJSON: CopyRates failed for ", EnumToString(timeframe));
        return "[]"; // ส่ง Array ว่างเปล่า
    }

    string json_array = "[";
    // เรียงข้อมูลจากเก่า -> ใหม่
    for(int idx = copied - 1, j = 0; idx >= 0; idx--, j++)
    {
        string item = StringFormat(
            "{\"time\":%d, \"open\":%.5f, \"high\":%.5f, \"low\":%.5f, \"close\":%.5f, \"tick_volume\":%d}",
            (long)rates[idx].time, rates[idx].open, rates[idx].high, rates[idx].low, rates[idx].close, rates[idx].tick_volume);

        json_array += item;
        if (j < copied - 1) json_array += ",";
    }
    json_array += "]";
    return json_array;
}


// 🛑 [ฟังก์ชันใหม่ที่ 2] - แทนที่ GetXAUUSDDataJSON เดิมทั้งหมด
// (B) Get XAUUSD Data in JSON format (Multi-Timeframe Version)
string GetXAUUSDDataJSON(int m5_bars)
{
    // 1. ดึงข้อมูล M5 (เหมือนเดิม 150 แท่ง)
    string m5_json = GetRatesJSON(PERIOD_M5, m5_bars);
    
    // 2. 🆕 ดึงข้อมูล M30 
    // โค้ดใหม่ (ขอเผื่อเป็น 70 แท่ง)
    string m30_json = GetRatesJSON(PERIOD_M30, 70); // ดึง M30 70 แท่งแท่ง
    
    // 3. 🆕 ดึงข้อมูล H1
    // เราต้องการข้อมูล H1 ย้อนหลังเพื่อให้แน่ใจว่า MA(200) คำนวณได้
    string h1_json = GetRatesJSON(PERIOD_H1, 250); // ดึง H1 250 แท่ง
    
    // 4. 🆕 ประกอบร่าง JSON ใหม่
    string final_json = StringFormat(
        "{\"m5_data\":%s, \"m30_data\":%s, \"h1_data\":%s}",
        m5_json,
        m30_json,
        h1_json
    );
    
    return final_json;
}

// --- 🛑 [แทนที่ฟังก์ชันนี้] (เวอร์ชันอ่าน ATR) 🛑 ---
// (C) Get Signal from API (เวอร์ชัน Dynamic SL/TP)
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
        Print("DEBUG: /predict HTTP 200 raw_response: ", json_response);
        
        string signal = ExtractJsonString(json_response, "signal");
        double probability = ExtractJsonDouble(json_response, "probability");
        
        // --- ⬇️ [แก้ไข] ⬇️ ---
        // ดึงค่า ATR ที่ API ส่งมาให้
        double atr_value = ExtractJsonDouble(json_response, "atr");
        // --- ⬆️ [แก้ไข] ⬆️ ---

        Print("DEBUG: Parsed signal=", signal, " probability=", DoubleToString(probability,6),
              " atr=", DoubleToString(atr_value, 4));
        
        // update globals
        LastProbability = probability;
        LastSignal = signal;
        LastATR = atr_value; // ⬅️ เก็บค่า ATR
        
        return LastSignal;
    }
    else
    {
        Print("Error getting signal: HTTP " + IntegerToString(res));
        LastATR = 0.0; // เคลียร์ค่าถ้า Error
        return "NONE";
    }
}


//+------------------------------------------------------------------+
//| (D) Execute Trade - [VERSION 2-Step]                             |
//+------------------------------------------------------------------+
// --- 🛑 [แทนที่ฟังก์ชันนี้] 🛑 ---
void ExecuteTrade(string signal)
{
    // ... (ส่วนตรวจสอบ Trading Allowed / Volume / Tick Data เหมือนเดิม) ...
    // --- ⬇️ (โค้ดส่วนนี้เหมือนเดิม) ⬇️ ---
    if (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) == 0 || AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) == 0)
    {
        Print("❌ Trading not allowed by terminal or account settings. Skipping trade.");
        return;
    }
    if (PositionSelect(_Symbol))
    {
        Print("⚠️ Existing position detected for symbol ", _Symbol, " - skipping open inside ExecuteTrade.");
        return;
    }
    MqlTradeRequest request;
    MqlTradeResult  result;
    ZeroMemory(request);
    ZeroMemory(result);
    double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
    double lotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
    double volume = Lots; 
    if (lotStep > 0) volume = MathFloor(volume / lotStep) * lotStep;
    volume = MathMax(minLot, MathMin(maxLot, volume));
    if (volume < minLot) { Print("❌ Computed volume below minimum. min=", minLot, " computed=", volume); return; }
    request.action    = TRADE_ACTION_DEAL;
    request.symbol    = _Symbol;
    request.volume    = volume;
    request.deviation = 50;
    request.magic     = MagicNumber;
    request.type_filling = ORDER_FILLING_IOC; 
    request.type_time    = ORDER_TIME_GTC;
    request.sl = 0.0; // ⬅️ ส่ง 0.0 ไปก่อน (เหมือนเดิม)
    request.tp = 0.0;
    MqlTick tick;
    if(!SymbolInfoTick(_Symbol, tick)) { Print("❌ Failed to get tick"); return; }
    if (TimeCurrent() - tick.time > 10) { Print("⚠️ Tick data is stale"); return; }
    // --- ⬆️ (โค้ดส่วนนี้เหมือนเดิม) ⬆️ ---

    // --- 3. BUY/SELL Logic (ตั้งค่า Price เท่านั้น) ---
    if (signal == "BUY")
    {
        request.type    = ORDER_TYPE_BUY;
        request.comment = "RNN_BOT_BUY";
        request.price = tick.ask; 
    }
    else if (signal == "SELL")
    {
        request.type    = ORDER_TYPE_SELL;
        request.comment = "RNN_BOT_SELL";
        request.price = tick.bid;
    }
    else { return; }

    // --- 4. Order Send (Step 1) ---
    Print("INFO: Attempting OrderSend (Step 1: Market Order) for ", signal);
    bool sent = OrderSend(request, result);
    Print("DEBUG: OrderSend (Market) returned sent=", sent, " retcode=", result.retcode, " deal=", result.deal);
          
    // --- 5. 🛑 [แก้ไข] 🛑 Modify SL/TP AFTER order is open ---
    if (sent && (result.retcode == TRADE_RETCODE_DONE || result.retcode == TRADE_RETCODE_PLACED))
    {
        Print("✅ Order Opened. Deal ticket: ", (string)result.deal, ". Now attempting (Step 2: Set Dynamic SL/TP)...");
        
        // --- ⬇️ [แก้ไข] ⬇️ ---
        // ส่งค่า ATR ที่ได้จาก API (Global Variable) เข้าไปในฟังก์ชัน Modify
        ModifyOrderSLTP(result.deal, signal, LastATR); 
        // --- ⬆️ [แก้ไข] ⬆️ ---
        
        string alert_msg = StringFormat("✅ %s Order Opened: Price %.5f, Lots %.2f", signal, request.price, volume);
        SendTradeAlert(alert_msg);
        LastSignalTime = TimeCurrent(); 
    }
    else
    {
        Print("❌ ", signal, " failed (Step 1): retcode=", result.retcode, " result_comment=", result.comment);
    }
}

// (E) Send Account Status/Alerts to API
void SendAccountStatusToAPI(string alert_message = "")
{
    string update_url = "/update_status";
    
    // ดึงข้อมูลบัญชี
    double balance = AccountInfoDouble(ACCOUNT_BALANCE);
    double equity = AccountInfoDouble(ACCOUNT_EQUITY);
    double margin_free = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
    int open_trades = PositionsTotal();
    
    // สร้าง JSON Payload
    string payload = StringFormat(
        "{\"balance\":%.2f, \"equity\":%.2f, \"margin_free\":%.2f, \"open_trades\":%d, \"alert_message\":\"%s\", \"account_type\":\"%s\"}", // <-- 🆕 เพิ่ม account_type
        balance, equity, margin_free, open_trades, alert_message); 
        
    string headers = "Content-Type: application/json";
    uchar post_data[];
    uchar body[];
    uchar result[];
    string result_headers;
    int timeout = 5000;
    
    int data_size = StringToCharArray(payload, post_data, 0, WHOLE_ARRAY);
    
    // Trim the post body to the actual data size to avoid sending trailing nulls
    ArrayResize(body, data_size);
    for (int i = 0; i < data_size; i++) body[i] = post_data[i];

    string full_url = APIServerURL + update_url;
    // 🛑 แก้ไข: เปลี่ยน WebRequest method จาก "GET" เป็น "POST"
    int res = WebRequest("POST", full_url, headers, timeout, body, result, result_headers);
    
    if (res != 200) 
    {
        Print("❌ API Error: SendAccountStatusToAPI failed. HTTP " + IntegerToString(res));
    }
}

// --- 🛑 [แทนที่ฟังก์ชันนี้] (เวอร์ชันคำนวณ ATR) 🛑 ---
// (F) Modify SL/TP [VERSION 4 - ATR Calculation]
// 🛑 [แก้ไข] รับ atr_value (ไม่ใช่ sl_price)
void ModifyOrderSLTP(ulong deal_ticket, string signal, double atr_value)
{
    // 1. ตรวจสอบว่าได้ค่า ATR มาจริง
    if(atr_value <= 0.0)
    {
        Print("❌ ModifyOrderSLTP Error: Invalid ATR value received from API (<= 0.0). Aborting modify.");
        return;
    }

    // 2. เลือก Position (เหมือนเดิม)
    if (!PositionSelect(_Symbol))
    {
        Print("❌ ModifyOrderSLTP Error: Could not select position by _Symbol after opening deal ", (string)deal_ticket);
        return;
    }
    
    // 3. ดึงข้อมูล Position (เหมือนเดิม)
    double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
    ulong position_ticket = PositionGetInteger(POSITION_TICKET); 
    
    // 4. ดึงค่า StopLevel (เหมือนเดิม)
    int min_stop_points = (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
    if (min_stop_points <= 0) min_stop_points = 1; 
    double min_stop_price_dist = MathMax(min_stop_points * _Point, 10 * _Point);
    
    // 5. เตรียมคำสั่ง Modify (เหมือนเดิม)
    MqlTradeRequest request_mod;
    MqlTradeResult  result_mod;
    ZeroMemory(request_mod);
    ZeroMemory(result_mod);
    request_mod.action = TRADE_ACTION_SLTP;
    request_mod.position = position_ticket;
    request_mod.symbol = _Symbol;
    
    // 6. 🛑 [แก้ไข] 🛑
    // คำนวณ SL/TP โดยใช้ ATR และ ตัวคูณ (Multiplier) ที่เราตั้งค่า Input ไว้
    
    // แปลง ATR (ที่เป็น "ราคา") ให้เป็น "Points"
    // (เช่น ATR 2.50 = 250 points ถ้า _Point = 0.01)
    double sl_points_dynamic = (atr_value * SL_Multiplier);
    double tp_points_dynamic = (atr_value * TP_Multiplier);

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
    
    // 7. ปรับค่า SL/TP ให้ตรงตามกฎโบรกเกอร์ (เหมือนเดิม)
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

    // 8. ส่งคำสั่ง Modify (เหมือนเดิม)
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

void SendTradeAlert(string alert_message)
{
    SendAccountStatusToAPI(alert_message);
}
