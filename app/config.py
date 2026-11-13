import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SECRET_KEY = os.getenv("SECRET_KEY", "infinitywork-super-secret-key-2026")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@infinitywork.com").lower()

REFERRAL_NEW_USER_BONUS = 10.0
REFERRAL_OWNER_BONUS = 8.0

OFFICIAL_CHANNEL_URL = "https://t.me/InfinityWork_Official"
SUPPORT_URL = "https://t.me/help_Infinity_work"
