import hashlib
import hmac
import os
import secrets
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request
from app.config import SECRET_KEY

serializer = URLSafeTimedSerializer(SECRET_KEY)


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, expected = stored_hash.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
        return hmac.compare_digest(dk.hex(), expected)
    except Exception:
        return False


def generate_referral_code(name: str) -> str:
    prefix = "".join(c for c in name.upper() if c.isalnum())[:4] or "INF"
    return f"{prefix}{secrets.token_hex(3).upper()}"


def create_session_token(user_id: int) -> str:
    return serializer.dumps({"uid": user_id})


def decode_session_token(token: str) -> int | None:
    try:
        data = serializer.loads(token, max_age=60 * 60 * 24 * 14)
        return int(data.get("uid"))
    except (BadSignature, SignatureExpired, Exception):
        return None


def set_flash(request: Request, message: str, category: str = "success"):
    if "flash_queue" not in request.session:
        request.session["flash_queue"] = []
    request.session["flash_queue"].append({"message": message, "category": category})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("flash_queue", [])
