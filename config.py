import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN")
XROCKET_API_KEY = os.getenv("XROCKET_API_KEY")
PORT = int(os.getenv("PORT", 8080))
DOMAIN = os.getenv("DOMAIN", "https://example.com")
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", 15))
MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", 5000))
WORK_HOURS_START = int(os.getenv("WORK_HOURS_START", 9))
WORK_HOURS_END = int(os.getenv("WORK_HOURS_END", 21))
