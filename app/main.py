import math
import struct
import zlib
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import (
    TEMPLATES_DIR,
    STATIC_DIR,
    SECRET_KEY,
    ADMIN_EMAIL,
    REFERRAL_NEW_USER_BONUS,
    REFERRAL_OWNER_BONUS,
    OFFICIAL_CHANNEL_URL,
    SUPPORT_URL,
)
from app.db import db
from app.imgbb import upload_with_load_balancer
from app.security import (
    hash_password,
    verify_password,
    generate_referral_code,
    create_session_token,
    decode_session_token,
    set_flash,
    pop_flashes,
)

app = FastAPI(title="InfinityWork")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DAILY_CUBE_LIMIT_SECONDS = 300  # 5 minutes per day

LEVEL_TIERS = [
    {"key": "beginner", "name": "Beginner", "min": 0, "next": 100, "icon": "fa-seedling"},
    {"key": "starter", "name": "Starter", "min": 100, "next": 500, "icon": "fa-rocket"},
    {"key": "legend", "name": "Legend", "min": 500, "next": 1500, "icon": "fa-crown"},
    {"key": "pro", "name": "Pro Earner", "min": 1500, "next": 5000, "icon": "fa-gem"},
    {"key": "hero", "name": "Infinity Hero", "min": 5000, "next": None, "icon": "fa-infinity"},
]


# ---------------- HELPERS ----------------

async def get_current_user(request: Request) -> dict | None:
    token = request.cookies.get("iw_auth")
    if not token:
        return None
    uid = decode_session_token(token)
    if not uid:
        return None
    try:
        return await db.select("users", filters={"id": uid}, single=True)
    except Exception:
        return None


def base_ctx(request: Request, user: dict | None = None, active_nav: str = "") -> dict:
    return {
        "request": request,
        "user": user,
        "active_nav": active_nav,
        "flashes": pop_flashes(request),
        "channel_url": OFFICIAL_CHANNEL_URL,
        "support_url": SUPPORT_URL,
    }


def redirect_with_flash(request: Request, url: str, message: str, category: str = "success"):
    set_flash(request, message, category)
    return RedirectResponse(url=url, status_code=303)


def get_bd_today_str() -> str:
    bd_tz = timezone(timedelta(hours=6))
    return datetime.now(bd_tz).strftime("%Y-%m-%d")


async def sync_cube_daily_limit(user: dict) -> int:
    today = get_bd_today_str()
    if user.get("cube_play_date") != today:
        await db.update(
            "users",
            {"id": user["id"]},
            {"cube_play_date": today, "cube_seconds_used": 0},
        )
        return DAILY_CUBE_LIMIT_SECONDS
    used = int(user.get("cube_seconds_used") or 0)
    return max(0, DAILY_CUBE_LIMIT_SECONDS - used)


def get_earning_level(total_earned: float) -> dict:
    current = LEVEL_TIERS[0]
    for tier in LEVEL_TIERS:
        if total_earned >= tier["min"]:
            current = tier
    if current["next"]:
        span = current["next"] - current["min"]
        prog = max(0, min(100, round(((total_earned - current["min"]) / span) * 100)))
        need = max(0.0, current["next"] - total_earned)
    else:
        prog = 100
        need = 0.0
    return {**current, "progress": prog, "need_more": need}


# ---------------- PUBLIC ROUTES ----------------

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    user = await get_current_user(request)
    return templates.TemplateResponse("landing.html", base_ctx(request, user, "landing"))


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, ref: str = ""):
    user = await get_current_user(request)
    if user:
        return RedirectResponse("/i", status_code=303)
    ctx = base_ctx(request, None, "signup")
    ctx["ref_code"] = ref
    return templates.TemplateResponse("signup.html", ctx)


@app.post("/signup")
async def signup_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    referral_code: str = Form(""),
    device_id: str = Form(""),
):
    email_clean = email.strip().lower()
    device_clean = device_id.strip() or request.cookies.get("iw_device") or ""

    if not device_clean or len(device_clean) < 8:
        return redirect_with_flash(request, "/signup", "ডিভাইস ভেরিফিকেশন ব্যর্থ হয়েছে। অনুগ্রহ করে পুনরায় চেষ্টা করুন।", "error")

    # 1 Device = 1 Account check
    if request.cookies.get("iw_registered") == "1":
        return redirect_with_flash(request, "/signup", "এই ডিভাইস থেকে ইতিমধ্যেই একটি অ্যাকাউন্ট খোলা হয়েছে!", "error")

    existing_device = await db.select("users", filters={"device_id": device_clean}, single=True)
    if existing_device:
        return redirect_with_flash(request, "/signup", "একটি ডিভাইসে একাধিক অ্যাকাউন্ট খোলা অনুমোদিত নয়!", "error")

    existing_email = await db.select("users", filters={"email": email_clean}, single=True)
    if existing_email:
        return redirect_with_flash(request, "/signup", "এই ইমেইল দিয়ে আগেই অ্যাকাউন্ট তৈরি করা হয়েছে।", "error")

    # Referral Logic: Referred User = 10৳, Referred By = 8৳
    ref_input = referral_code.strip().upper()
    initial_balance = 0.0
    valid_referrer = None

    if ref_input:
        valid_referrer = await db.select("users", filters={"referral_code": ref_input}, single=True)
        if not valid_referrer:
            return redirect_with_flash(request, "/signup", "ভুল রেফারেল কোড দেওয়া হয়েছে!", "error")
        initial_balance = REFERRAL_NEW_USER_BONUS

    new_ref_code = generate_referral_code(name)
    is_admin = email_clean == ADMIN_EMAIL

    new_user = await db.insert(
        "users",
        {
            "name": name.strip(),
            "email": email_clean,
            "password_hash": hash_password(password),
            "referral_code": new_ref_code,
            "referred_by": ref_input if valid_referrer else None,
            "device_id": device_clean,
            "balance": initial_balance,
            "total_earned": initial_balance,
            "is_admin": is_admin,
        },
    )

    if valid_referrer:
        new_ref_bal = float(valid_referrer.get("balance", 0)) + REFERRAL_OWNER_BONUS
        new_ref_total = float(valid_referrer.get("total_earned", 0)) + REFERRAL_OWNER_BONUS
        await db.update(
            "users",
            {"id": valid_referrer["id"]},
            {"balance": new_ref_bal, "total_earned": new_ref_total},
        )
        await db.insert(
            "work_logs",
            {
                "user_id": valid_referrer["id"],
                "work_type": "Referral Bonus",
                "details": f"রেফারেল বোনাস ({name.strip()})",
                "reward": REFERRAL_OWNER_BONUS,
            },
        )

    if initial_balance > 0 and new_user:
        await db.insert(
            "work_logs",
            {
                "user_id": new_user["id"],
                "work_type": "Welcome Referral Bonus",
                "details": f"রেফার কোড {ref_input} ব্যবহারের বোনাস",
                "reward": initial_balance,
            },
        )

    set_flash(
        request,
        f"অ্যাকাউন্ট সফলভাবে তৈরি হয়েছে! {'১০৳ রেফারেল বোনাস যোগ হয়েছে।' if initial_balance > 0 else 'স্বাগতম!'}",
        "success",
    )
    response = RedirectResponse("/i", status_code=303)
    token = create_session_token(new_user["id"])
    response.set_cookie("iw_auth", token, httponly=True, max_age=60 * 60 * 24 * 14, samesite="lax")
    response.set_cookie("iw_device", device_clean, max_age=60 * 60 * 24 * 365 * 5, samesite="lax")
    response.set_cookie("iw_registered", "1", max_age=60 * 60 * 24 * 365 * 5, samesite="lax")
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = await get_current_user(request)
    if user:
        return RedirectResponse("/i", status_code=303)
    return templates.TemplateResponse("login.html", base_ctx(request, None, "login"))


@app.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    email_clean = email.strip().lower()
    user = await db.select("users", filters={"email": email_clean}, single=True)
    if not user or not verify_password(password, user["password_hash"]):
        return redirect_with_flash(request, "/login", "ইমেইল অথবা পাসওয়ার্ড সঠিক নয়!", "error")

    set_flash(request, f"স্বাগতম, {user['name']}!", "success")
    response = RedirectResponse("/i", status_code=303)
    response.set_cookie("iw_auth", create_session_token(user["id"]), httponly=True, max_age=60 * 60 * 24 * 14, samesite="lax")
    return response


@app.get("/logout")
async def logout(request: Request):
    set_flash(request, "সফলভাবে লগআউট সম্পন্ন হয়েছে।", "info")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("iw_auth")
    return response


# ---------------- AUTHENTICATED APP ROUTES ----------------

@app.get("/i", response_class=HTMLResponse)
async def app_home(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    recent_logs = await db.select("work_logs", filters={"user_id": user["id"]}, order="created_at.desc", limit=6)
    referrals = await db.select("users", filters={"referred_by": user["referral_code"]})
    ctx = base_ctx(request, user, "home")
    ctx["recent_logs"] = recent_logs
    ctx["referral_count"] = len(referrals)
    return templates.TemplateResponse("home.html", ctx)


@app.get("/work", response_class=HTMLResponse)
async def work_hub(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")
    return templates.TemplateResponse("work.html", base_ctx(request, user, "work"))


@app.get("/task", response_class=HTMLResponse)
async def task_list(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    tasks = await db.select("tasks", filters={"is_active": "true"}, order="created_at.desc")
    submissions = await db.select("task_submissions", filters={"user_id": user["id"]}, order="created_at.desc")
    submitted_ids = {s["task_id"]: s["status"] for s in submissions}

    ctx = base_ctx(request, user, "work")
    ctx["tasks"] = tasks
    ctx["submitted_ids"] = submitted_ids
    return templates.TemplateResponse("tasks.html", ctx)


@app.get("/task/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: int):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    task = await db.select("tasks", filters={"id": task_id}, single=True)
    if not task:
        return redirect_with_flash(request, "/task", "টাস্কটি পাওয়া যায়নি!", "error")

    ctx = base_ctx(request, user, "work")
    ctx["task"] = task
    return templates.TemplateResponse("task_detail.html", ctx)


@app.post("/task/{task_id}/submit")
async def task_submit(
    request: Request,
    task_id: int,
    proof_note: str = Form(""),
    proof_image: UploadFile = File(None),
):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    task = await db.select("tasks", filters={"id": task_id}, single=True)
    if not task:
        return redirect_with_flash(request, "/task", "টাস্কটি পাওয়া যায়নি!", "error")

    image_bytes = await proof_image.read() if proof_image and proof_image.filename else None
    img_url, is_dummy = await upload_with_load_balancer(image_bytes)

    await db.insert(
        "task_submissions",
        {
            "user_id": user["id"],
            "task_id": task_id,
            "proof_note": proof_note.strip(),
            "proof_image_url": img_url,
            "is_dummy": is_dummy,
            "status": "pending",
            "reward": task["rate"],
        },
    )

    return redirect_with_flash(request, "/task", "আপনার কাজ সফলভাবে জমা হয়েছে! ভেরিফিকেশনের পর পেমেন্ট যুক্ত হবে।", "success")


# ---------------- SPECIALIZED WORK MODULES ----------------

@app.get("/work/typing", response_class=HTMLResponse)
async def work_typing(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")
    return templates.TemplateResponse("work_typing.html", base_ctx(request, user, "work"))


@app.post("/work/typing")
async def work_typing_submit(request: Request, typed_text: str = Form(...), target_text: str = Form(...)):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"ok": False, "msg": "Unauthorized"}, status_code=401)

    if typed_text.strip() != target_text.strip():
        return redirect_with_flash(request, "/work/typing", "লেখাটি হুবহু মিলেনি! আবার চেষ্টা করুন।", "error")

    reward = 2.50
    await db.update(
        "users",
        {"id": user["id"]},
        {"balance": float(user["balance"]) + reward, "total_earned": float(user["total_earned"]) + reward},
    )
    await db.insert("work_logs", {"user_id": user["id"], "work_type": "Typing Task", "details": "সফল টাইপিং সম্পন্ন", "reward": reward})
    return redirect_with_flash(request, "/work/typing", f"অভিনন্দন! {reward}৳ আপনার ব্যালেন্সে যোগ হয়েছে।", "success")


@app.get("/work/survey", response_class=HTMLResponse)
async def work_survey(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")
    return templates.TemplateResponse("work_survey.html", base_ctx(request, user, "work"))


@app.post("/work/survey")
async def work_survey_submit(request: Request, q1: str = Form(...), q2: str = Form(...)):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    reward = 5.00
    await db.update(
        "users",
        {"id": user["id"]},
        {"balance": float(user["balance"]) + reward, "total_earned": float(user["total_earned"]) + reward},
    )
    await db.insert("work_logs", {"user_id": user["id"], "work_type": "Survey", "details": f"{q1} | {q2}", "reward": reward})
    return redirect_with_flash(request, "/work/survey", f"সার্ভে সম্পন্ন! {reward}৳ বোনাস যোগ হয়েছে।", "success")

@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    user = await get_current_user(request)
    return templates.TemplateResponse("about.html", base_ctx(request, user, "about"))
    
@app.get("/work/ai-image", response_class=HTMLResponse)
async def work_ai_image(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    all_tasks = await db.select("ai_image_tasks", filters={"is_active": "true"}, order="id.desc")
    user_subs = await db.select("ai_image_submissions", filters={"user_id": user["id"]}, order="created_at.desc")

    # Hide tasks that have 'pending' or 'approved' submissions; keep 'rejected' tasks available to retry!
    blocked_task_ids = {s["ai_task_id"] for s in user_subs if s.get("status") in ("pending", "approved")}
    rejected_task_ids = {s["ai_task_id"] for s in user_subs if s.get("status") == "rejected"}

    available_tasks = []
    for t in all_tasks:
        if t["id"] not in blocked_task_ids:
            t["was_rejected"] = t["id"] in rejected_task_ids
            available_tasks.append(t)

    ctx = base_ctx(request, user, "work")
    ctx["tasks"] = available_tasks
    return templates.TemplateResponse("work_ai_image.html", ctx)


@app.get("/work/ai-image/{task_id}", response_class=HTMLResponse)
async def work_ai_image_detail(request: Request, task_id: int):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    task = await db.select("ai_image_tasks", filters={"id": task_id}, single=True)
    if not task or not task.get("is_active"):
        return redirect_with_flash(request, "/work/ai-image", "কাজটি পাওয়া যায়নি!", "error")

    user_subs = await db.select("ai_image_submissions", filters={"user_id": user["id"]})
    if any(s["ai_task_id"] == task_id and s.get("status") in ("pending", "approved") for s in user_subs):
        return redirect_with_flash(request, "/work/ai-image", "এই কাজটি ইতিমধ্যেই জমা দেওয়া হয়েছে!", "info")

    ctx = base_ctx(request, user, "work")
    ctx["task"] = task
    return templates.TemplateResponse("work_ai_detail.html", ctx)


@app.post("/work/ai-image/{task_id}/submit")
async def work_ai_image_submit(
    request: Request,
    task_id: int,
    ai_image: UploadFile = File(...),
):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    task = await db.select("ai_image_tasks", filters={"id": task_id}, single=True)
    if not task:
        return redirect_with_flash(request, "/work/ai-image", "কাজটি পাওয়া যায়নি!", "error")

    user_subs = await db.select("ai_image_submissions", filters={"user_id": user["id"]})
    if any(s["ai_task_id"] == task_id and s.get("status") in ("pending", "approved") for s in user_subs):
        return redirect_with_flash(request, "/work/ai-image", "এই কাজটি ইতিমধ্যেই জমা দেওয়া আছে!", "info")

    image_bytes = await ai_image.read() if ai_image and ai_image.filename else None
    if not image_bytes:
        return redirect_with_flash(request, f"/work/ai-image/{task_id}", "অনুগ্রহ করে তৈরি করা ছবিটি আপলোড করুন।", "error")

    img_url, is_dummy = await upload_with_load_balancer(image_bytes)

    await db.insert(
        "ai_image_submissions",
        {
            "user_id": user["id"],
            "ai_task_id": task_id,
            "image_url": img_url,
            "is_dummy": is_dummy,
            "status": "pending",
            "reward": task["reward"],
        },
    )
    return redirect_with_flash(request, "/work/ai-image", f"{task['title']} সফলভাবে জমা হয়েছে! ভেরিফিকেশনের পর টাকা যোগ হবে।", "success")


# ---------------- ADMIN PANEL: CHATGPT IMAGE (/admin/c) ----------------

@app.get("/admin/c", response_class=HTMLResponse)
async def admin_ai_panel(request: Request):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অ্যাডমিন অ্যাক্সেস অনুমোদিত নয়!", "error")

    tasks = await db.select("ai_image_tasks", order="id.desc")
    task_map = {t["id"]: t for t in tasks}
    submissions = await db.select("ai_image_submissions", order="created_at.desc", limit=50)

    for s in submissions:
        s["task_title"] = task_map.get(s["ai_task_id"], {}).get("title", f"Chatgpt Img #{s['ai_task_id']}")

    next_num = (max((t["id"] for t in tasks), default=0)) + 1
    ctx = base_ctx(request, user, "admin")
    ctx.update({"tasks": tasks, "submissions": submissions, "next_auto_title": f"Chatgpt Img #{next_num}"})
    return templates.TemplateResponse("admin_ai.html", ctx)


@app.post("/admin/c/task/add")
async def admin_ai_add_task(
    request: Request,
    title: str = Form(""),
    prompt: str = Form(...),
    reward: float = Form(...),
    description: str = Form(""),
):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")

    final_title = title.strip()
    if not final_title:
        existing = await db.select("ai_image_tasks", order="id.desc", limit=1)
        next_num = (existing[0]["id"] + 1) if existing else 1
        final_title = f"Chatgpt Img #{next_num}"

    await db.insert(
        "ai_image_tasks",
        {
            "title": final_title,
            "prompt": prompt.strip(),
            "description": description.strip(),
            "reward": reward,
            "is_active": True,
        },
    )
    return redirect_with_flash(request, "/admin/c", f"'{final_title}' সফলভাবে পাবলিশ হয়েছে!", "success")


@app.post("/admin/c/task/{task_id}/delete")
async def admin_ai_delete_task(request: Request, task_id: int):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")
    await db.delete("ai_image_tasks", {"id": task_id})
    return redirect_with_flash(request, "/admin/c", "ChatGPT টাস্ক ডিলিট করা হয়েছে।", "info")


@app.post("/admin/c/submission/{sub_id}/{action}")
async def admin_ai_review_submission(request: Request, sub_id: int, action: str):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")

    sub = await db.select("ai_image_submissions", filters={"id": sub_id}, single=True)
    if not sub or sub["status"] != "pending":
        return redirect_with_flash(request, "/admin/c", "সাবমিশনটি আগেই প্রসেস করা আছে।", "info")

    if action == "approve":
        await db.update("ai_image_submissions", {"id": sub_id}, {"status": "approved"})
        target_user = await db.select("users", filters={"id": sub["user_id"]}, single=True)
        if target_user:
            reward = float(sub["reward"])
            await db.update(
                "users",
                {"id": target_user["id"]},
                {
                    "balance": float(target_user["balance"]) + reward,
                    "total_earned": float(target_user["total_earned"]) + reward,
                },
            )
        return redirect_with_flash(request, "/admin/c", "ইমেজ অ্যাপ্রুভ হয়েছে ও ইউজারের ব্যালেন্সে টাকা যোগ হয়েছে!", "success")
    else:
        await db.update("ai_image_submissions", {"id": sub_id}, {"status": "rejected"})
        return redirect_with_flash(request, "/admin/c", "রিজেক্ট করা হয়েছে — ইউজার কাজটি আবার করতে পারবে।", "info")
    
@app.get("/work/cube-game", response_class=HTMLResponse)
async def work_cube(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    remaining_seconds = await sync_cube_daily_limit(user)
    ctx = base_ctx(request, user, "work")
    ctx["remaining_seconds"] = remaining_seconds
    return templates.TemplateResponse("work_cube.html", ctx)


@app.post("/work/cube-game/tick")
async def work_cube_tick(request: Request, seconds: int = Form(5)):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"ok": False, "remaining_seconds": 0}, status_code=401)

    remaining = await sync_cube_daily_limit(user)
    if remaining <= 0:
        return JSONResponse({"ok": True, "remaining_seconds": 0})

    step = max(1, min(int(seconds), 15))
    today = get_bd_today_str()
    used = int(user.get("cube_seconds_used") or 0) if user.get("cube_play_date") == today else 0
    new_used = min(DAILY_CUBE_LIMIT_SECONDS, used + step)

    await db.update(
        "users",
        {"id": user["id"]},
        {"cube_play_date": today, "cube_seconds_used": new_used},
    )
    return JSONResponse({"ok": True, "remaining_seconds": max(0, DAILY_CUBE_LIMIT_SECONDS - new_used)})


@app.post("/work/cube-game/claim")
async def work_cube_claim(request: Request, score: int = Form(...)):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"ok": False, "message": "লগইন প্রয়োজন"}, status_code=401)

    remaining = await sync_cube_daily_limit(user)
    if remaining <= 0:
        return JSONResponse({"ok": False, "message": "আজকের ৫ মিনিটের লিমিট শেষ হয়ে গেছে! আগামীকাল আবার খেলুন।"}, status_code=403)

    if score < 150:
        return JSONResponse({"ok": False, "message": "রিওয়ার্ড পেতে কমপক্ষে ১৫০ স্কোর করতে হবে!"}, status_code=400)

    reward = round(score / 150, 2)

    await db.update(
        "users",
        {"id": user["id"]},
        {
            "balance": float(user["balance"]) + reward,
            "total_earned": float(user["total_earned"]) + reward,
        },
    )
    await db.insert(
        "work_logs",
        {
            "user_id": user["id"],
            "work_type": "2,4,8 Cube Game",
            "details": f"Score: {score}",
            "reward": reward,
        },
    )
    return JSONResponse({"ok": True, "reward": reward, "message": f"দারুণ! ৳{reward:.2f} আপনার ব্যালেন্সে যোগ হয়েছে।"})


@app.get("/work/tiktok-sell", response_class=HTMLResponse)
async def work_tiktok(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")
    return templates.TemplateResponse("work_tiktok.html", base_ctx(request, user, "work"))


@app.post("/work/tiktok-sell")
async def work_tiktok_submit(
    request: Request,
    username: str = Form(...),
    followers: int = Form(...),
    asking_price: float = Form(...),
    contact_tg: str = Form(...),
):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    await db.insert(
        "work_logs",
        {
            "user_id": user["id"],
            "work_type": "TikTok Account Sell",
            "details": f"@{username} | {followers} followers | {asking_price}৳ | TG: {contact_tg}",
            "reward": 0.0,
            "status": "pending",
        },
    )
    return redirect_with_flash(request, "/work/tiktok-sell", "আপনার TikTok অ্যাকাউন্ট বিক্রয়ের আবেদন জমা হয়েছে! টিম যোগাযোগ করবে।", "success")


# ---------------- HISTORY, PAYOUT & PROFILE ----------------

@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, status: str = "all"):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    submissions = await db.select("task_submissions", filters={"user_id": user["id"]}, order="created_at.desc")
    all_tasks = await db.select("tasks")
    task_map = {t["id"]: t for t in all_tasks}
    work_logs = await db.select("work_logs", filters={"user_id": user["id"]}, order="created_at.desc")

    icon_map = {
        "Typing Task": ("⌨️", "Typing Task"),
        "Survey": ("📝", "Survey করে আয়"),
        "ChatGPT Image Gen": ("🎨", "ChatGPT Image Generate"),
        "2,4,8 Cube Game": ("🎲", "গেম খেলে আয় (2,4,8 Cube)"),
        "TikTok Account Sell": ("🎵", "TikTok Account Sell"),
        "Referral Bonus": ("🤝", "রেফারেল বোনাস"),
        "Welcome Referral Bonus": ("🎁", "ওয়েলকাম রেফার বোনাস"),
    }

    combined: list[dict] = []

    for sub in submissions:
        task_info = task_map.get(sub["task_id"], {})
        task_title = task_info.get("title") or f"Micro Task #{sub['task_id']}"
        raw_status = (sub.get("status") or "pending").lower()
        norm_status = "approved" if raw_status in ("approved", "completed") else ("rejected" if raw_status == "rejected" else "pending")
        status_label = {"approved": "Approved", "pending": "Pending", "rejected": "Rejected"}[norm_status]

        combined.append({
            "icon": "📋",
            "title": task_title,
            "subtitle": f"Micro Task • {sub.get('proof_note') or 'জমা দেওয়া হয়েছে'}",
            "amount": float(sub.get("reward") or 0),
            "norm_status": norm_status,
            "status_label": status_label,
            "created_at": sub.get("created_at") or "",
            "date": (sub.get("created_at") or "")[:16].replace("T", " "),
        })

    for log in work_logs:
        wtype = log.get("work_type") or "Work"
        icon, display_title = icon_map.get(wtype, ("💼", wtype))
        raw_status = (log.get("status") or "completed").lower()
        norm_status = "approved" if raw_status in ("completed", "approved") else ("rejected" if raw_status == "rejected" else "pending")
        status_label = {"approved": "Approved", "pending": "Pending", "rejected": "Rejected"}[norm_status]

        combined.append({
            "icon": icon,
            "title": display_title,
            "subtitle": log.get("details") or wtype,
            "amount": float(log.get("reward") or 0),
            "norm_status": norm_status,
            "status_label": status_label,
            "created_at": log.get("created_at") or "",
            "date": (log.get("created_at") or "")[:16].replace("T", " "),
        })

    combined.sort(key=lambda x: x["created_at"], reverse=True)

    counts = {
        "all": len(combined),
        "approved": sum(1 for x in combined if x["norm_status"] == "approved"),
        "pending": sum(1 for x in combined if x["norm_status"] == "pending"),
        "rejected": sum(1 for x in combined if x["norm_status"] == "rejected"),
    }

    status_filter = status if status in ("all", "approved", "pending", "rejected") else "all"
    filtered = combined if status_filter == "all" else [x for x in combined if x["norm_status"] == status_filter]

    ctx = base_ctx(request, user, "history")
    ctx.update({"items": filtered, "counts": counts, "status_filter": status_filter})
    return templates.TemplateResponse("history.html", ctx)


@app.get("/payout", response_class=HTMLResponse)
async def payout_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    payouts = await db.select("payouts", filters={"user_id": user["id"]}, order="created_at.desc")
    ctx = base_ctx(request, user, "payout")
    ctx["payouts"] = payouts
    return templates.TemplateResponse("payout.html", ctx)


@app.post("/payout")
async def payout_submit(
    request: Request,
    method: str = Form(...),
    account_number: str = Form(...),
    amount: float = Form(...),
):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    current_bal = float(user.get("balance", 0))
    if amount < 50:
        return redirect_with_flash(request, "/payout", "সর্বনিম্ন উইথড্র ৫০৳!", "error")
    if amount > current_bal:
        return redirect_with_flash(request, "/payout", "আপনার অ্যাকাউন্টে পর্যাপ্ত ব্যালেন্স নেই!", "error")

    await db.update("users", {"id": user["id"]}, {"balance": current_bal - amount})
    await db.insert(
        "payouts",
        {
            "user_id": user["id"],
            "method": method,
            "account_number": account_number.strip(),
            "amount": amount,
            "status": "pending",
        },
    )
    return redirect_with_flash(request, "/payout", "উইথড্র রিকুয়েস্ট সফলভাবে পাঠানো হয়েছে!", "success")


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    referrals = await db.select("users", filters={"referred_by": user["referral_code"]})
    total_earned = float(user.get("total_earned") or 0)
    level_info = get_earning_level(total_earned)

    ctx = base_ctx(request, user, "profile")
    ctx["referrals"] = referrals
    ctx["level"] = level_info
    ctx["all_levels"] = LEVEL_TIERS
    ctx["total_earned"] = total_earned
    return templates.TemplateResponse("profile.html", ctx)


@app.post("/profile/avatar")
async def profile_avatar_upload(request: Request, avatar: UploadFile = File(...)):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    image_bytes = await avatar.read() if avatar and avatar.filename else None
    if not image_bytes:
        return redirect_with_flash(request, "/profile", "অনুগ্রহ করে একটি সঠিক ছবি নির্বাচন করুন।", "error")

    img_url, is_dummy = await upload_with_load_balancer(image_bytes)
    if img_url and not is_dummy:
        await db.update("users", {"id": user["id"]}, {"avatar_url": img_url})
        return redirect_with_flash(request, "/profile", "প্রোফাইল ছবি সফলভাবে আপডেট হয়েছে!", "success")

    return redirect_with_flash(request, "/profile", "ছবি আপলোডের জন্য ImgBB API Key সক্রিয় নেই।", "info")


# ---------------- ADMIN PANEL ----------------

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অ্যাডমিন অ্যাক্সেস অনুমোদিত নয়!", "error")

    tasks = await db.select("tasks", order="created_at.desc")
    imgbb_keys = await db.select("imgbb_keys", order="id.asc")
    submissions = await db.select("task_submissions", order="created_at.desc", limit=30)
    payouts = await db.select("payouts", order="created_at.desc", limit=30)

    ctx = base_ctx(request, user, "admin")
    ctx.update({"tasks": tasks, "imgbb_keys": imgbb_keys, "submissions": submissions, "payouts": payouts})
    return templates.TemplateResponse("admin.html", ctx)


@app.post("/admin/task/add")
async def admin_add_task(
    request: Request,
    title: str = Form(...),
    category: str = Form(...),
    description: str = Form(...),
    rate: float = Form(...),
    task_link: str = Form(""),
):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")

    await db.insert(
        "tasks",
        {
            "title": title.strip(),
            "category": category.strip(),
            "description": description.strip(),
            "rate": rate,
            "task_link": task_link.strip(),
            "is_active": True,
        },
    )
    return redirect_with_flash(request, "/admin", "নতুন মাইক্রো টাস্ক যুক্ত হয়েছে!", "success")


@app.post("/admin/task/{task_id}/delete")
async def admin_delete_task(request: Request, task_id: int):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")
    await db.delete("tasks", {"id": task_id})
    return redirect_with_flash(request, "/admin", "টাস্ক মুছে ফেলা হয়েছে।", "info")


@app.post("/admin/imgbb/add")
async def admin_add_imgbb(request: Request, label: str = Form(...), api_key: str = Form(...)):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")
    await db.insert("imgbb_keys", {"label": label.strip(), "api_key": api_key.strip(), "is_active": True})
    return redirect_with_flash(request, "/admin", "ImgBB API Key লোড ব্যালেন্সারে যুক্ত হয়েছে!", "success")


@app.post("/admin/imgbb/{key_id}/delete")
async def admin_delete_imgbb(request: Request, key_id: int):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")
    await db.delete("imgbb_keys", {"id": key_id})
    return redirect_with_flash(request, "/admin", "API Key ডিলিট হয়েছে।", "info")


@app.post("/admin/submission/{sub_id}/{action}")
async def admin_review_submission(request: Request, sub_id: int, action: str):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")

    sub = await db.select("task_submissions", filters={"id": sub_id}, single=True)
    if not sub or sub["status"] != "pending":
        return redirect_with_flash(request, "/admin", "সাবমিশনটি প্রসেস করা আছে।", "info")

    if action == "approve":
        await db.update("task_submissions", {"id": sub_id}, {"status": "approved"})
        target_user = await db.select("users", filters={"id": sub["user_id"]}, single=True)
        if target_user:
            reward = float(sub["reward"])
            await db.update(
                "users",
                {"id": target_user["id"]},
                {
                    "balance": float(target_user["balance"]) + reward,
                    "total_earned": float(target_user["total_earned"]) + reward,
                },
            )
        return redirect_with_flash(request, "/admin", "টাস্ক অ্যাপ্রুভ হয়েছে ও টাকা যোগ হয়েছে!", "success")
    else:
        await db.update("task_submissions", {"id": sub_id}, {"status": "rejected"})
        return redirect_with_flash(request, "/admin", "টাস্ক রিজেক্ট করা হয়েছে।", "info")


@app.post("/admin/payout/{payout_id}/{action}")
async def admin_review_payout(request: Request, payout_id: int, action: str):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")

    p = await db.select("payouts", filters={"id": payout_id}, single=True)
    if not p or p["status"] != "pending":
        return redirect_with_flash(request, "/admin", "পেইমেন্ট আগেই আপডেট হয়েছে।", "info")

    if action == "paid":
        await db.update("payouts", {"id": payout_id}, {"status": "paid"})
        return redirect_with_flash(request, "/admin", "পেইমেন্ট Paid হিসেবে মার্ক করা হয়েছে!", "success")
    else:
        await db.update("payouts", {"id": payout_id}, {"status": "rejected"})
        target_user = await db.select("users", filters={"id": p["user_id"]}, single=True)
        if target_user:
            await db.update(
                "users",
                {"id": target_user["id"]},
                {"balance": float(target_user["balance"]) + float(p["amount"])},
            )
        return redirect_with_flash(request, "/admin", "উইথড্র বাতিল করে ব্যালেন্স ফেরত দেওয়া হয়েছে।", "info")


# ---------------- PWA ROUTES (FIXED INSTALLABLE) ----------------

_PWA_ICON_CACHE: dict[int, bytes] = {}


def _generate_infinity_png(size: int) -> bytes:
    if size in _PWA_ICON_CACHE:
        return _PWA_ICON_CACHE[size]

    scale = 4 if size >= 512 else 2
    base = size // scale
    cx, cy = base / 2.0, base / 2.0
    ring_offset = base * 0.165
    ring_r = base * 0.165
    stroke = base * 0.058

    raw_rows = bytearray()
    for y in range(base):
        t_grad = y / max(1, base - 1)
        bg_r = int(22 * (1 - t_grad) + 11 * t_grad)
        bg_g = int(130 * (1 - t_grad) + 74 * t_grad)
        bg_b = int(75 * (1 - t_grad) + 43 * t_grad)

        row_pixels = bytearray()
        dy = y - cy
        for x in range(base):
            d_l = abs(math.hypot(x - (cx - ring_offset), dy) - ring_r)
            d_r = abs(math.hypot(x - (cx + ring_offset), dy) - ring_r)
            d = min(d_l, d_r)

            if d <= stroke:
                alpha = 1.0
            elif d <= stroke + 1.2:
                alpha = max(0.0, 1.0 - (d - stroke) / 1.2)
            else:
                alpha = 0.0

            r = int(bg_r * (1 - alpha) + 255 * alpha)
            g = int(bg_g * (1 - alpha) + 255 * alpha)
            b = int(bg_b * (1 - alpha) + 255 * alpha)
            px = bytes((r, g, b, 255))
            row_pixels.extend(px * scale)

        for _ in range(scale):
            raw_rows.append(0)
            raw_rows.extend(row_pixels)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack("!IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw_rows), 6))
        + chunk(b"IEND", b"")
    )
    _PWA_ICON_CACHE[size] = png_bytes
    return png_bytes


@app.get("/pwa-icon.svg")
async def pwa_icon_svg():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#16824b"/><stop offset="100%" stop-color="#0b4a2b"/></linearGradient></defs>
  <rect width="128" height="128" rx="32" fill="url(#g)"/>
  <path d="M90.5 46c19 0 19 36 0 36-19 0-26.5-36-47.5-36-17 0-17 36 0 36 21 0 28.5-36 47.5-36z" fill="none" stroke="#ffffff" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""
    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/pwa-icon/{size}.png")
async def pwa_icon(size: int):
    valid_size = 512 if size >= 512 else 192
    return Response(
        content=_generate_infinity_png(valid_size),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/launch", response_class=HTMLResponse)
async def pwa_launch(request: Request):
    user = await get_current_user(request)
    target_url = "/i" if user else "/login"
    return templates.TemplateResponse("launch.html", {"request": request, "target_url": target_url})


@app.get("/manifest.json")
async def pwa_manifest():
    manifest = {
        "id": "/launch",
        "name": "InfinityWork",
        "short_name": "InfinityWork",
        "description": "কাজ করুন, আয় করুন - Trusted Online Income Platform",
        "start_url": "/launch",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "theme_color": "#effcf4",
        "background_color": "#effcf4",
        "icons": [
            {"src": "/pwa-icon/192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/pwa-icon/512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/pwa-icon/512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return JSONResponse(content=manifest, media_type="application/manifest+json")

@app.get("/favicon.ico")
async def favicon():
    return Response(content=_generate_infinity_png(192), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

@app.get("/sw.js")
async def pwa_service_worker():
    sw_code = """
const CACHE_NAME = "infinitywork-pwa-v2";
const CORE_ASSETS = ["/launch", "/manifest.json", "/pwa-icon/192.png", "/pwa-icon/512.png", "/static/js/app.js"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res.ok && (event.request.url.includes("/static/") || event.request.url.includes("/pwa-icon/"))) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then((c) => c.put(event.request, clone));
        }
        return res;
      })
      .catch(() => caches.match(event.request))
  );
});
"""
    return Response(
        content=sw_code.strip(),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )
