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
input int    StopLossPoints = 1500; // SL (Points)
input int    TakeProfitPoints = 3000; // TP (Points)
//--- Global Variables
string BotStatus = "STOPPED"; // สถานะที่ควบคุมจาก API /command
string LastSignal = "NONE";
datetime LastSignalTime = 0;
double LastProbability = 0.0;

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

// (C) Get Signal from API (แก้ไขการจัดการ post_data size)
string GetSignalFromAPI(string data_json)
{
    string predict_url = "/predict";
    string headers = "Content-Type: application/json";
    uchar post_data[];
    uchar body[];
    uchar result[];
    string result_headers;
    int timeout = 10000;
    
    // **1. แปลง String เป็น Char Array และเก็บขนาดข้อมูล**
    int data_size = StringToCharArray(data_json, post_data, 0, WHOLE_ARRAY);

    // Copy into a trimmed body buffer of exact size to avoid trailing NULs
    ArrayResize(body, data_size);
    for (int i = 0; i < data_size; i++) body[i] = post_data[i];

    string full_url = APIServerURL + predict_url;
    // 🛑 แก้ไข: เปลี่ยน WebRequest method จาก "GET" เป็น "POST"
    int res = WebRequest("POST", full_url, headers, timeout, body, result, result_headers); 
    
    if (res == 200) 
    {
        string json_response = CharArrayToString(result);
        Print("DEBUG: /predict HTTP 200 raw_response: ", json_response);
        string signal = ExtractJsonString(json_response, "signal");
        double probability = ExtractJsonDouble(json_response, "probability");
        Print("DEBUG: Parsed signal=", signal, " probability=", DoubleToString(probability,6));
        // update globals used by decision logic
        LastProbability = probability;
        LastSignal = signal;
        return LastSignal;
    }
    else
    {
        Print("Error getting signal: HTTP " + IntegerToString(res));
        return "NONE";
    }
}


//+------------------------------------------------------------------+
//| (D) Execute Trade - [VERSION 3 - 2-Step Order]                   |
//+------------------------------------------------------------------+
void ExecuteTrade(string signal)
{
    // --- (ส่วนตรวจสอบ Trading Allowed และ Volume Normalization เหมือนเดิม) ---
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
    
    // --- 2. Trade Request Setup ---
    request.action    = TRADE_ACTION_DEAL;
    request.symbol    = _Symbol;
    request.volume    = volume;
    request.deviation = 50;
    request.magic     = MagicNumber;
    request.type_filling = ORDER_FILLING_IOC; // ⬅️ ใช้ IOC ที่เราแก้ไปครั้งก่อน
    request.type_time    = ORDER_TIME_GTC;
    
    // 🛑 [NEW LOGIC] 🛑
    // เราจะส่งออเดอร์โดยที่ SL/TP เป็น 0 ก่อน
    request.sl = 0.0;
    request.tp = 0.0;
    
    MqlTick tick;
    if(!SymbolInfoTick(_Symbol, tick))
    {
        Print("❌ Failed to get tick");
        return;
    }
   
    if (TimeCurrent() - tick.time > 10)
    {
        Print("⚠️ Tick data is stale: ", TimeToString(tick.time, TIME_DATE|TIME_SECONDS));
        return;
    }

    // --- 3. BUY/SELL Logic (ตั้งค่า Price เท่านั้น) ---
    if (signal == "BUY")
    {
        request.type    = ORDER_TYPE_BUY;
        request.comment = "RNN_BOT_BUY";
        request.price = tick.ask; // ✅ ถูกต้อง: ใช้ ASK สำหรับ BUY
    }
    else if (signal == "SELL")
    {
        request.type    = ORDER_TYPE_SELL;
        request.comment = "RNN_BOT_SELL";
        request.price = tick.bid; // ✅ แก้ไข: ใช้ BID สำหรับ SELL
    }
    else
    {
        Print("DEBUG: ExecuteTrade received unhandled signal: ", signal);
        return;
    }

    // --- 4. Final Diagnostics and Order Send ---
    Print("INFO: Attempting OrderSend (Step 1: Market Order) for ", signal);
    
    bool sent = OrderSend(request, result);

    Print("DEBUG: OrderSend (Market) returned sent=", sent,
          " retcode=", result.retcode,
          " comment=", result.comment,
          " deal=", result.deal,
          " order=", result.order);
          
    // --- 5. 🛑 [NEW LOGIC] Modify SL/TP AFTER order is open ---
    if (sent && (result.retcode == TRADE_RETCODE_DONE || result.retcode == TRADE_RETCODE_PLACED))
    {
        Print("✅ Order Opened. Deal ticket: ", (string)result.deal, ". Now attempting (Step 2: Set SL/TP)...");
        
        // เรียกฟังก์ชันใหม่เพื่อตั้งค่า SL/TP โดยใช้ Deal Ticket
        ModifyOrderSLTP(result.deal, signal); 
        
        // Send alert and update time (เหมือนเดิม)
        string alert_msg = StringFormat("✅ %s Order Opened: Price %.5f, Lots %.2f", signal, request.price, volume);
        SendTradeAlert(alert_msg);
        LastSignalTime = TimeCurrent(); 
    }
    else
    {
        // ถ้า Step 1 ล้มเหลว
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

//+------------------------------------------------------------------+
//| (F) Modify SL/TP for an open position                            |
//+------------------------------------------------------------------+
void ModifyOrderSLTP(ulong deal_ticket, string signal)
{
    // 1. เลือก Position โดยใช้ Deal Ticket ที่เพิ่งเปิด
    if (!PositionSelectByTicket(deal_ticket))
    {
        Print("❌ ModifyOrderSLTP Error: Could not select position by ticket ", (string)deal_ticket);
        return;
    }
    
    // 2. ดึงข้อมูล Position ที่เปิดอยู่
    double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
    ulong position_ticket = PositionGetInteger(POSITION_TICKET); // ⬅️ นี่คือ Ticket ของ Position จริงๆ
    
    // 3. ดึงค่า StopLevel (คัดลอกมาจาก ExecuteTrade)
    int min_stop_points = (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
    if (min_stop_points <= 0) min_stop_points = 1; 
    double min_stop_price_dist = MathMax(min_stop_points * _Point, 10 * _Point);
    
    // 4. เตรียมคำสั่ง Modify
    MqlTradeRequest request_mod;
    MqlTradeResult  result_mod;
    ZeroMemory(request_mod);
    ZeroMemory(result_mod);
    
    request_mod.action = TRADE_ACTION_SLTP; // ⬅️ Action คือการแก้ไข SL/TP
    request_mod.position = position_ticket; // ⬅️ ระบุ Position Ticket ที่จะแก้ไข
    request_mod.symbol = _Symbol;
    
    double sl_price = 0.0;
    double tp_price = 0.0;

    // 5. คำนวณ SL/TP (คัดลอกมาจาก ExecuteTrade)
    if (signal == "BUY")
    {
        sl_price = NormalizeDouble(open_price - (StopLossPoints * _Point), _Digits);
        tp_price = NormalizeDouble(open_price + (TakeProfitPoints * _Point), _Digits);
        
        // Adjust SL/TP
        if (open_price - sl_price < min_stop_price_dist)
        {
             sl_price = NormalizeDouble(open_price - min_stop_price_dist, _Digits);
        }
        if (tp_price - open_price < min_stop_price_dist)
        {
             tp_price = NormalizeDouble(open_price + min_stop_price_dist * 2, _Digits); 
        }
    }
    else if (signal == "SELL")
    {
        sl_price = NormalizeDouble(open_price + (StopLossPoints * _Point), _Digits);
        tp_price = NormalizeDouble(open_price - (TakeProfitPoints * _Point), _Digits);
        
        // Adjust SL/TP
        if (sl_price - open_price < min_stop_price_dist)
        {
             sl_price = NormalizeDouble(open_price + min_stop_price_dist, _Digits);
        }
        if (open_price - tp_price < min_stop_price_dist)
        {
             tp_price = NormalizeDouble(open_price - min_stop_price_dist * 2, _Digits);
        }
    }
    
    request_mod.sl = sl_price;
    request_mod.tp = tp_price;
    
    // 6. ส่งคำสั่ง Modify
    Print("DEBUG: Modifying position #", (string)position_ticket, " with SL=", DoubleToString(sl_price, _Digits), " TP=", DoubleToString(tp_price, _Digits));
    bool modified = OrderSend(request_mod, result_mod);
    
    if(modified && (result_mod.retcode == TRADE_RETCODE_DONE || result_mod.retcode == TRADE_RETCODE_PLACED))
    {
        Print("✅ SL/TP successfully set for position #", (string)position_ticket);
    }
    else
    {
        Print("❌ ModifyOrderSLTP failed (Step 2): retcode=", result_mod.retcode, " comment=", result_mod.comment);
    }
}

void SendTradeAlert(string alert_message)
{
    SendAccountStatusToAPI(alert_message);
}
