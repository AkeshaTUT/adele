import os
from dotenv import load_dotenv

load_dotenv()

# Конфигурация бота
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "YOUR_ADMIN_BOT_TOKEN_HERE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "123456789"))

# ID администраторов, которые могут пользоваться админ-ботом
admin_ids_str = os.getenv("ALLOWED_ADMIN_IDS", "123456789")
try:
    # Убираем возможные квадратные скобки и парсим
    admin_ids_str = admin_ids_str.strip('[]')
    ALLOWED_ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip()]
except (ValueError, AttributeError):
    # Если парсинг не удался, используем значение по умолчанию
    ALLOWED_ADMIN_IDS = [123456789]

# Конфигурация базы данных
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://username:password@localhost/cafe_bot"
)

# Настройки логирования
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
