import telegram
from telegram.ext import Application, CommandHandler, ContextTypes
import requests
import time
import os
from pathlib import Path

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / '.env'
    load_dotenv(dotenv_path=env_path, override=True)
except ImportError:
    print("⚠️  Warning: python-dotenv not installed. Trying to read .env manually.")

# --- Configuration ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
CHAT_ID_STR = os.getenv('TELEGRAM_CHAT_ID', '').strip()

# Validate and parse CHAT_ID
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN is not set in .env file!")
if not CHAT_ID_STR:
    raise ValueError("❌ TELEGRAM_CHAT_ID is not set in .env file!")

try:
    CHAT_ID = int(CHAT_ID_STR)
except ValueError:
    raise ValueError(f"❌ TELEGRAM_CHAT_ID must be a valid integer, got: {CHAT_ID_STR}")

API_URL = 'http://127.0.0.1:5000'  # สำหรับทดสอบในเครื่อง

# --- Commands ---

async def start_command(update, context):
    """Handles /start command to activate the trading bot."""
    if update.effective_chat.id != CHAT_ID: return # Security check
    
    # 🛑 (Optional Debug): ส่งข้อความทันทีเพื่อยืนยันการรับคำสั่ง
    await update.message.reply_text("⏳ Requesting OBot START Command...", parse_mode='Markdown')
    
    response = requests.post(f'{API_URL}/command', json={'command': 'START'})
    if response.status_code == 200:
        message = "🟢 **OBot Started!**\nMT5 Bot is instructed to start trading. \n use /help to see commands"
    else:
        try:
            error_msg = response.json().get('message', 'API Error')
        except:
            error_msg = f"API Connection Error (Code {response.status_code})"
        message = f"❌ **Error Starting OBot**\n{error_msg}"
        
    await update.message.reply_text(message, parse_mode='Markdown')

async def stop_command(update, context):
    """Handles /stop command to halt the trading bot."""
    if update.effective_chat.id != CHAT_ID: return 
    
    await update.message.reply_text("⏳ Requesting OBot STOP Command...", parse_mode='Markdown')
    
    response = requests.post(f'{API_URL}/command', json={'command': 'STOP'})
    if response.status_code == 200:
        message = "🔴 **OBot Stopped!**\nMT5 Bot is instructed to stop trading.\n use /help to see commands"
    else:
        try:
            error_msg = response.json().get('message', 'API Error')
        except:
            error_msg = f"API Connection Error (Code {response.status_code})"
        message = f"❌ **Error Stopping OBot**\n{error_msg}"
        
    await update.message.reply_text(message, parse_mode='Markdown')

async def status_command(update, context):
    """Handles /status command to check account and bot status."""
    if update.effective_chat.id != CHAT_ID: return 

    try:
        response = requests.get(f'{API_URL}/status')
        if response.status_code == 200:
            status_data = response.json()
            news_status = status_data.get('news_status', 'Unknown')
            # ⬇️ [ใหม่] อ่านสถานะโมเดล (ปรับแต่งจาก API)
            v6_loaded = status_data.get('v6_model_loaded', False)
            trend_loaded = status_data.get('trend_model_loaded', False)
            sideway_loaded = status_data.get('sideway_model_loaded', False)
            models_ok = "✅" if (v6_loaded and trend_loaded and sideway_loaded) else "❌"
            
            message = (
                f"📊 **OBOT STATUS REPORT** 📊\n"
                f"------------------------------------\n"
                f"**Bot State:** `{status_data.get('bot_status')}`\n"
                f"**Last Regime:** `{status_data.get('last_regime', 'N/A')}`\n" # ⬅️ [ใหม่]
                f"**Last Signal:** `{status_data.get('last_signal')}`\n"
                f"**News Filter:** `{news_status}`\n" # ⬅️ เพิ่มบรรทัดนี้
                f"**Models Loaded:** `{models_ok}`\n" # ⬅️ [ใหม่]
                f"------------------------------------\n"
                f"**Balance:** ${status_data.get('balance'):,.2f}\n"
                f"**Equity:** ${status_data.get('equity'):,.2f}\n"
                f"**Free Margin:** ${status_data.get('margin_free'):,.2f}\n"
                f"**Open Trades:** {status_data.get('open_trades')}\n"
                f"------------------------------------\n"
                f"**Last Updated:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}\n"
                f"**use /help to see commands**\n"
                f"------------------------------------"
            )
        else:
            message = f"❌ **Error retrieving status:** API returned {response.status_code}"
    except requests.exceptions.ConnectionError:
        message = "❌ **API Connection Error:** Flask API is not running or ngrok URL is wrong."
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def retrain_command(update, context):
    """Handles /retrain command to manually trigger model retraining."""
    if update.effective_chat.id != CHAT_ID: return 
    
    await update.message.reply_text("⏳ Requesting Model Retraining... (This may take a while)", parse_mode='Markdown')
    
    response = requests.post(f'{API_URL}/retrain')
    if response.status_code == 200:
        message = f"**use /help to see commands **\n{response.json().get('message')}"
    else:
        try:
            error_msg = response.json().get('message', 'API Error')
        except:
            error_msg = f"API Connection Error (Code {response.status_code})"
        message = f"❌ **Error Triggering Retrain**\n{error_msg}"
        
    await update.message.reply_text(message, parse_mode='Markdown')

# 🆕 เพิ่ม Command /help
async def help_command(update, context):
    """Handles /help command to show available commands."""
    if update.effective_chat.id != CHAT_ID: return 
    
    help_message = (
        "🤖 **Command Control OBot** 🤖\n"
        "------------------------------------\n"
        "/status - Check account and bot status\n"
        "/start - Start OBot (RUNNING)\n"
        "/stop - Stop OBot (STOPPED)\n"
        "/fix - Download and reload system files\n"
        "/retrain - Retrain in background\n"
        "/update - Updating EA\n"
        "/restart - Restarting All System\n"
        "/help - Show CCOBOT\n"
        "------------------------------------"
    )
    await update.message.reply_text(help_message, parse_mode='Markdown')

# 🆕 ฟังก์ชันสำหรับส่งข้อความตอนเริ่มต้น (ต้องเป็น async)
async def send_startup_message(token, chat_id): 
    """Sends a welcome message to the specified chat ID.""" 
    try: 
        # ใช้ Bot object เพื่อส่งข้อความ 
        bot = telegram.Bot(token) 
        await bot.send_message( 
            chat_id=chat_id, 
            text="✅ **OBot** online and ready for commands. Use /help to see commands.", 
            parse_mode='Markdown' 
        ) 
    except Exception as e: 
        print(f"❌ Failed to send startup message to CHAT_ID {chat_id}: {e}") 
        print("💡 Ensure CHAT_ID is correct and you have started a conversation with the bot.") 

# 🆕 Define the post_init function
async def post_init_callback(application: Application):
    """Callback function executed after the Application is initialized."""
    print("Executing post_init callback...")
    # The application is ready, now we can send the message safely within the event loop
    await send_startup_message(TELEGRAM_TOKEN, CHAT_ID)
    print("Startup notification sent.")

async def update_command(update, context):
    """Handles /update command to update and recompile the EA."""
    if update.effective_chat.id != CHAT_ID: return 
    
    await update.message.reply_text("⏳ Requesting EA Update & Recompile... (This may take a moment)", parse_mode='Markdown')
    
    response = requests.post(f'{API_URL}/update_ea') # เรียก Endpoint ใหม่
    
    if response.status_code == 200:
        message = f"**{response.json().get('message')}**\n use /help to see commands"
    else:
        try:
            error_msg = response.json().get('message', 'API Error')
        except:
            error_msg = f"API Connection Error (Code {response.status_code})"
        message = f"❌ **Error Updating EA**\n{error_msg}"
        
    await update.message.reply_text(message, parse_mode='Markdown')

async def restart_command(update, context):
    """Handles /restart command to restart the API service."""
    if update.effective_chat.id != CHAT_ID: return 
    
    await update.message.reply_text("⏳ Requesting Service RESTART...", parse_mode='Markdown')
    
    response = requests.post(f'{API_URL}/restart')
    if response.status_code == 200:
        message = "✅ **The Service Restarted!**\nService is restarting in the background."
    else:
        message = f"❌ **Error Restarting**"
        
    await update.message.reply_text(message, parse_mode='Markdown')
    
# 🆕 เพิ่ม Command /fix
async def fix_command(update, context):
    """Handles /fix command to download and reload system files from GitHub."""
    if update.effective_chat.id != CHAT_ID: return 
    
    await update.message.reply_text("⏳ Requesting System FIX (Download & Reload files)... (Requires server restart after success)", parse_mode='Markdown')
    
    response = requests.post(f'{API_URL}/fix') 
    
    if response.status_code == 200:
        message = f"**{response.json().get('message')}**\n use /help to see commands"
    else:
        try:
            error_msg = response.json().get('message', 'API Error')
        except:
            error_msg = f"API Connection Error (Code {response.status_code})"
        message = f"❌ **Error Triggering FIX**\n{error_msg}"
        
    await update.message.reply_text(message, parse_mode='Markdown')

def main(): 
    """Start the Telegram Bot.""" 
    
    print(f"🔐 Using Telegram Token: {TELEGRAM_TOKEN[:10]}...")
    print(f"📱 Using Chat ID: {CHAT_ID}")

    # 1. สร้าง Application และกำหนด post_init callback
    try:
        application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init_callback).build()
    except telegram.error.InvalidToken as e:
        print(f"❌ Invalid Telegram Token: {e}")
        print("💡 Check that TELEGRAM_BOT_TOKEN in .env is correct.")
        raise 
    

    # 2. Add command handlers 
    application.add_handler(CommandHandler("start", start_command)) 
    application.add_handler(CommandHandler("stop", stop_command)) 
    application.add_handler(CommandHandler("status", status_command)) 
    application.add_handler(CommandHandler("retrain", retrain_command))  
    application.add_handler(CommandHandler("update", update_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("fix", fix_command))
    application.add_handler(CommandHandler("help", help_command))
    # Start the Bot 
    print("🚀 Starting Telegram Bot Polling...") 

    # The post_init_callback will be executed before polling begins
    application.run_polling(allowed_updates=telegram.Update.ALL_TYPES) 

if __name__ == '__main__': 
    main()
