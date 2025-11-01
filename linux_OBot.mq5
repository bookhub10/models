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
            // 3. ดึงข้อมูล OHLCV ล่าสุด
            int requestBars = MathMax(LookbackBars, 100) + 50; 
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
                    if (LastProbability < ProbThreshold)
                    {
                        Print("DEBUG: Skipping ", signal, " - probability below threshold (", DoubleToString(LastProbability,6), ").");
                    }
                    else if (secondsSinceLast < MinTradeIntervalMins * 60)
                    {
                        Print("DEBUG: Skipping ", signal, " - within MinTradeInterval (", IntegerToString(secondsSinceLast), "s).");
                    }
                    else
                    {
                        Print("DEBUG: Conditions met - attempting ExecuteTrade(\"", signal, "\").");
                        ExecuteTrade(signal);
                        // 🚨 หมายเหตุ: LastSignalTime จะถูกอัปเดตใน ExecuteTrade หากส่งคำสั่งสำเร็จ 
                        // หากต้องการความรวดเร็วในการป้องกันการส่งคำสั่งซ้ำให้ย้ายมาอัปเดตตรงนี้:
                        // LastSignalTime = now; 
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
string GetXAUUSDDataJSON(int bars)
{
    MqlRates rates[];
    int copied = CopyRates(_Symbol, PERIOD_M5, 0, bars, rates);
    if (copied <= 0)
    {
        Print("❌ GetXAUUSDDataJSON: CopyRates failed, copied=", copied);
        return "{}"; // Error
    }

    // Log how many bars were actually copied (helps debug server-side insufficient-data issues)
    Print("DEBUG: GetXAUUSDDataJSON requested=", bars, " copied=", copied);

    string json_array = "[";
    // CopyRates returns bars with index 0 = most recent. Python expects oldest-first (time increasing).
    // Iterate from the oldest available element (copied-1) down to 0 and append so the JSON array is oldest->newest.
    for(int idx = copied - 1, j = 0; idx >= 0; idx--, j++)
    {
        // สร้าง JSON object สำหรับแต่ละแท่งเทียน (เรียงจากเก่->ใหม่)
        string item = StringFormat(
            "{\"time\":%d, \"open\":%.5f, \"high\":%.5f, \"low\":%.5f, \"close\":%.5f, \"tick_volume\":%d}",
            (long)rates[idx].time, rates[idx].open, rates[idx].high, rates[idx].low, rates[idx].close, rates[idx].tick_volume);

        json_array += item;
        if (j < copied - 1) json_array += ",";
    }
    json_array += "]";
    
    // รวมให้อยู่ในรูปแบบที่ API ต้องการ: {"ohlcv_data": [...]}
    string final_json = "{\"ohlcv_data\":" + json_array + "}";
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
//| (D) Execute Trade - FINAL REVISED & CORRECTED VERSION            |
//+------------------------------------------------------------------+
void ExecuteTrade(string signal)
{
    // Block if terminal or account doesn't allow trading
    if (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) == 0 || AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) == 0)
    {
        Print("❌ Trading not allowed by terminal or account settings. Skipping trade.");
        return;
    }
    Print("DEBUG: SYMBOL_TRADE_STOPS_LEVEL = ", SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL));
    Print("DEBUG: ACCOUNT_TRADE_ALLOWED = ", AccountInfoInteger(ACCOUNT_TRADE_ALLOWED));
    Print("DEBUG: TERMINAL_TRADE_ALLOWED = ", TerminalInfoInteger(TERMINAL_TRADE_ALLOWED));


    // Safety check: Skip if there's already a position for this symbol
    if (PositionSelect(_Symbol))
    {
        Print("⚠️ Existing position detected for symbol ", _Symbol, " - skipping open inside ExecuteTrade.");
        return;
    }

    // --- 1. Volume Normalization & Setup ---
    MqlTradeRequest request;
    MqlTradeResult  result;
    
    // ... (โค้ด Volume Normalization เดิม) ...
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
    request.type_filling = ORDER_FILLING_FOK;    // Immediate-Or-Cancel (ปลอดภัยสำหรับ market orders)
    request.type_time    = ORDER_TIME_GTC;       // Good Till Canceled (ไม่มีวันหมดอายุ)
    
    int sl_points = 1500;
    int tp_points = 3000;

    int min_stop_points = (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
    // 🛑 แก้ไข: ใช้ MathMax(1, ...) เพื่อรับประกันว่า min_stop_points เป็นอย่างน้อย 1
    if (min_stop_points <= 0) min_stop_points = 1; 
    
    // 🛑 แก้ไข: กำหนด min_stop_price_dist ขั้นต่ำเผื่อไว้เสมอ (เช่น 10 points)
    double min_stop_price_dist = MathMax(min_stop_points * _Point, 10 * _Point); 
    
    MqlTick tick;
    if(!SymbolInfoTick(_Symbol, tick))
    {
        Print("❌ Failed to get tick");
        return;
    }
   
    if (TimeCurrent() - tick.time > 10) // ถ้า tick เก่าเกิน 10 วินาที
    {
        Print("⚠️ Tick data is stale: ", TimeToString(tick.time, TIME_DATE|TIME_SECONDS));
        return;
    }

    // --- 3. BUY/SELL Logic (Cleaned and Completed) ---
    if (signal == "BUY")
    {
        request.type    = ORDER_TYPE_BUY;
        request.comment = "RNN_BOT_BUY";

        if(!SymbolInfoTick(_Symbol, tick))
        {
            Print("❌ BUY failed: Could not get fresh tick price.");
            return;
        }
        request.price = tick.ask; // ✅ ถูกต้อง: ใช้ ASK สำหรับ BUY
        
        // Calculate SL/TP for BUY
        request.sl = NormalizeDouble(request.price - (sl_points * _Point), _Digits);
        request.tp = NormalizeDouble(request.price + (tp_points * _Point), _Digits);
        
        // Adjust SL/TP
        if (request.price - request.sl < min_stop_price_dist)
        {
             request.sl = NormalizeDouble(request.price - min_stop_price_dist, _Digits);
             Print("DEBUG: Adjusted BUY SL to meet min_stop: ", DoubleToString(request.sl, _Digits));
        }
        if (request.tp - request.price < min_stop_price_dist)
        {
             request.tp = NormalizeDouble(request.price + min_stop_price_dist * 2, _Digits); 
             Print("DEBUG: Adjusted BUY TP to meet min_stop: ", DoubleToString(request.tp, _Digits));
        }
    }
    else if (signal == "SELL")
    {
        request.type    = ORDER_TYPE_SELL;
        request.comment = "RNN_BOT_SELL";
        
        if(!SymbolInfoTick(_Symbol, tick))
        {
            Print("❌ SELL failed: Could not get fresh tick price."); // ✅ แก้ไข Print Message
            return;
        }
        request.price = tick.bid; // ✅ แก้ไข: ใช้ BID สำหรับ SELL
        
        // Calculate SL/TP for SELL
        request.sl = NormalizeDouble(request.price + (sl_points * _Point), _Digits);
        request.tp = NormalizeDouble(request.price - (tp_points * _Point), _Digits);
        
        // Adjust SL/TP
        if (request.sl - request.price < min_stop_price_dist)
        {
             request.sl = NormalizeDouble(request.price + min_stop_price_dist, _Digits);
             Print("DEBUG: Adjusted SELL SL to meet min_stop: ", DoubleToString(request.sl, _Digits));
        }
        if (request.price - request.tp < min_stop_price_dist)
        {
             request.tp = NormalizeDouble(request.price - min_stop_price_dist * 2, _Digits);
             Print("DEBUG: Adjusted SELL TP to meet min_stop: ", DoubleToString(request.tp, _Digits));
        }
    }
    else
    {
        Print("DEBUG: ExecuteTrade received unhandled signal: ", signal);
        return;
    }

    // --- 4. Final Diagnostics and Order Send (เหมือนเดิม) ---
    Print("DEBUG: Order preflight ", signal, ": price=", DoubleToString(request.price,_Digits),
          " sl=", DoubleToString(request.sl,_Digits), " tp=", DoubleToString(request.tp,_Digits),
          " volume=", DoubleToString(volume,2));

    Print("INFO: Attempting OrderSend for ", signal);
    
    double MinStopLevelPoints = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
    Print("DEBUG: Symbol Digits=", _Digits, ", Symbol Point=", _Point);
    Print("DEBUG: Broker's Min Stops Level (Points)=", MinStopLevelPoints);

    bool sent = OrderSend(request, result);

      Print("DEBUG: OrderSend returned sent=", sent,
            " retcode=", result.retcode,
            " comment=", result.comment,
            " deal=", result.deal,
            " order=", result.order);
      
      if (sent && (result.retcode == TRADE_RETCODE_DONE || result.retcode == TRADE_RETCODE_PLACED))
      {
          string alert_msg = StringFormat("✅ %s Order Opened: Price %.5f, Lots %.2f", signal, request.price, volume);
          SendTradeAlert(alert_msg);
          LastSignalTime = TimeCurrent(); 
      }
      else
      {
          Print("❌ ", signal, " failed: retcode=", result.retcode, " result_comment=", result.comment);
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

void SendTradeAlert(string alert_message)
{
    SendAccountStatusToAPI(alert_message);
}
