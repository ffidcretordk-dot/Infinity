import math
import struct
import zlib

import asyncio
import random
import httpx

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

async def get_global_notice() -> str:
    try:
        row = await db.select("site_settings", filters={"key": "global_notice"}, single=True)
        if row and row.get("value"):
            return row["value"]
    except Exception:
        pass
    try:
        notices = await db.select("notices", order="is_pinned.desc,created_at.desc", limit=1)
        if notices:
            return notices[0].get("content") or notices[0].get("title")
    except Exception:
        pass
    return "নতুন কাজ ও ইভেন্ট লাইভ হয়েছে! নিয়মিত কাজ করে দ্রুত পেমেন্ট নিন।"


@app.get("/i", response_class=HTMLResponse)
async def app_home(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    recent_logs = await db.select("work_logs", filters={"user_id": user["id"]}, order="created_at.desc", limit=5)
    referrals = await db.select("users", filters={"referred_by": user["referral_code"]})
    global_notice = await get_global_notice()
    level_info = get_earning_level(float(user.get("total_earned") or 0))

    ctx = base_ctx(request, user, "home")
    ctx["recent_logs"] = recent_logs
    ctx["referral_count"] = len(referrals)
    ctx["global_notice"] = global_notice
    ctx["level"] = level_info
    return templates.TemplateResponse("home.html", ctx)

@app.post("/admin/global-notice")
async def admin_update_global_notice(request: Request, global_notice: str = Form(...)):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")

    text = global_notice.strip()
    existing = await db.select("site_settings", filters={"key": "global_notice"}, single=True)
    if existing:
        await db.update("site_settings", {"key": "global_notice"}, {"value": text})
    else:
        await db.insert("site_settings", {"key": "global_notice", "value": text})

    return redirect_with_flash(request, "/admin", "Global Welcome Notice সফলভাবে আপডেট হয়েছে!", "success")

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

@app.get("/event", response_class=HTMLResponse)
async def refer_event_page(request: Request):
    user = await get_current_user(request)
    all_users = await db.select("users")

    ref_counts: dict[str, int] = {}
    active_ref_counts: dict[str, int] = {}

    for u in all_users:
        rb = u.get("referred_by")
        if rb:
            ref_counts[rb] = ref_counts.get(rb, 0) + 1
            # Active member = referred user who earned beyond the 10৳ welcome bonus (doing regular tasks/games)
            if float(u.get("total_earned") or 0) > REFERRAL_NEW_USER_BONUS:
                active_ref_counts[rb] = active_ref_counts.get(rb, 0) + 1

    leaderboard = []
    for u in all_users:
        code = u.get("referral_code")
        total_cnt = ref_counts.get(code, 0)
        act_cnt = active_ref_counts.get(code, 0)
        if total_cnt > 0:
            leaderboard.append({
                "name": u.get("name", "User"),
                "total": total_cnt,
                "active": act_cnt,
                "is_me": bool(user and u["id"] == user["id"]),
            })

    leaderboard.sort(key=lambda x: (x["active"], x["total"]), reverse=True)

    my_total_refs = ref_counts.get(user.get("referral_code"), 0) if user else 0
    my_active_refs = active_ref_counts.get(user.get("referral_code"), 0) if user else 0

    ctx = base_ctx(request, user, "refer")
    ctx.update({
        "leaderboard": leaderboard[:10],
        "my_total_refs": my_total_refs,
        "my_active_refs": my_active_refs,
    })
    return templates.TemplateResponse("event.html", ctx)
    
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



@app.get("/admin/submiton", response_class=HTMLResponse)
@app.get("/admin/submissions", response_class=HTMLResponse)
async def admin_submissions_page(request: Request, status: str = "pending"):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অ্যাডমিন অ্যাক্সেস অনুমোদিত নয়!", "error")

    all_subs = await db.select("task_submissions", order="created_at.desc", limit=300)
    all_tasks = await db.select("tasks")
    all_users = await db.select("users")

    task_map = {t["id"]: t for t in all_tasks}
    user_map = {u["id"]: u for u in all_users}

    counts = {
        "all": len(all_subs),
        "pending": sum(1 for s in all_subs if s.get("status") == "pending"),
        "approved": sum(1 for s in all_subs if s.get("status") == "approved"),
        "rejected": sum(1 for s in all_subs if s.get("status") == "rejected"),
    }

    status_filter = status if status in ("pending", "approved", "rejected", "all") else "pending"
    filtered = all_subs if status_filter == "all" else [s for s in all_subs if s.get("status") == status_filter]

    for s in filtered:
        t = task_map.get(s["task_id"], {})
        u = user_map.get(s["user_id"], {})
        s["task_title"] = t.get("title") or f"Task #{s['task_id']}"
        s["task_category"] = t.get("category") or "Micro Task"
        s["user_name"] = u.get("name") or f"User #{s['user_id']}"

    ctx = base_ctx(request, user, "admin")
    ctx.update({
        "submissions": filtered,
        "counts": counts,
        "status_filter": status_filter,
    })
    return templates.TemplateResponse("admin_submissions.html", ctx)


@app.post("/admin/submiton/bulk")
async def admin_submissions_bulk(
    request: Request,
    mode: str = Form("smart_bulk"),          # smart_bulk, approve_selected, reject_selected
    batch_limit: int = Form(60),             # 50+ batch size when no checkboxes picked
    random_reject: int = Form(5),            # Default: randomly reject 5
    selected_ids: list[int] = Form(default=[]),
):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")

    pending_subs = await db.select("task_submissions", filters={"status": "pending"}, order="created_at.asc", limit=500)
    if not pending_subs:
        return redirect_with_flash(request, "/admin/submiton", "প্রসেস করার মতো কোনো Pending সাবমিশন নেই।", "info")

    pending_map = {s["id"]: s for s in pending_subs}

    # Determine target batch (either checked IDs or top N pending up to batch_limit)
    if selected_ids:
        target_subs = [pending_map[sid] for sid in selected_ids if sid in pending_map]
    else:
        limit_n = max(1, min(int(batch_limit), 500))
        target_subs = pending_subs[:limit_n]

    if not target_subs:
        return redirect_with_flash(request, "/admin/submiton", "কোনো বৈধ Pending সাবমিশন সিলেক্ট করা হয়নি।", "error")

    if mode == "reject_selected":
        reject_subs = target_subs
        approve_subs = []
    elif mode == "approve_selected":
        approve_subs = target_subs
        reject_subs = []
    else:
        # Smart Bulk: Approve batch & randomly reject N (default 5)
        rj_n = max(0, int(random_reject))
        if rj_n > 0 and len(target_subs) > 1:
            actual_rj = min(rj_n, len(target_subs) - 1) if len(target_subs) <= rj_n else rj_n
            reject_subs = random.sample(target_subs, actual_rj)
            reject_id_set = {s["id"] for s in reject_subs}
            approve_subs = [s for s in target_subs if s["id"] not in reject_id_set]
        else:
            reject_subs = []
            approve_subs = target_subs

    approve_ids = [s["id"] for s in approve_subs]
    reject_ids = [s["id"] for s in reject_subs]

    # 1. Fast bulk status updates in Supabase PostgREST
    async with httpx.AsyncClient(timeout=25.0) as client:
        if approve_ids:
            await client.patch(
                f"{db.base_url}/task_submissions",
                headers=db.headers,
                params={"id": f"in.({','.join(map(str, approve_ids))})"},
                json={"status": "approved"},
            )
        if reject_ids:
            await client.patch(
                f"{db.base_url}/task_submissions",
                headers=db.headers,
                params={"id": f"in.({','.join(map(str, reject_ids))})"},
                json={"status": "rejected"},
            )

    # 2. Group rewards by user_id & credit balances concurrently
    user_rewards: dict[int, float] = {}
    for s in approve_subs:
        uid = s["user_id"]
        user_rewards[uid] = user_rewards.get(uid, 0.0) + float(s.get("reward") or 0)

    if user_rewards:
        all_users = await db.select("users")
        user_lookup = {u["id"]: u for u in all_users}

        async def credit_user(uid: int, add_amt: float):
            u = user_lookup.get(uid)
            if not u:
                return
            await db.update(
                "users",
                {"id": uid},
                {
                    "balance": float(u.get("balance") or 0) + add_amt,
                    "total_earned": float(u.get("total_earned") or 0) + add_amt,
                },
            )

        await asyncio.gather(*(credit_user(uid, amt) for uid, amt in user_rewards.items()))

    return redirect_with_flash(
        request,
        "/admin/submiton",
        f"Bulk সম্পন্ন! {len(approve_ids)}টি Approved ও {len(reject_ids)}টি Random Rejected হয়েছে।",
        "success",
    )


@app.post("/admin/submiton/{sub_id}/{action}")
async def admin_submissions_single(request: Request, sub_id: int, action: str):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")

    sub = await db.select("task_submissions", filters={"id": sub_id}, single=True)
    if not sub or sub["status"] != "pending":
        return redirect_with_flash(request, "/admin/submiton", "সাবমিশনটি আগেই প্রসেস করা আছে।", "info")

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
        return redirect_with_flash(request, "/admin/submiton", "সাবমিশন Approved হয়েছে ও টাকা যোগ হয়েছে!", "success")
    else:
        await db.update("task_submissions", {"id": sub_id}, {"status": "rejected"})
        return redirect_with_flash(request, "/admin/submiton", "সাবমিশন Rejected করা হয়েছে।", "info")
        
# ---------------- SPECIALIZED WORK MODULES ----------------

# ---------------- TYPING TASK SYSTEM (/work/typing & /admin/t) ----------------

@app.get("/work/typing", response_class=HTMLResponse)
async def work_typing(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    all_tasks = await db.select("typing_tasks", filters={"is_active": "true"}, order="id.desc")
    user_subs = await db.select("typing_submissions", filters={"user_id": user["id"]}, order="created_at.desc")

    blocked_ids = {s["typing_task_id"] for s in user_subs if s.get("status") in ("pending", "approved")}
    rejected_ids = {s["typing_task_id"] for s in user_subs if s.get("status") == "rejected"}

    available_tasks = []
    for t in all_tasks:
        if t["id"] not in blocked_ids:
            t["was_rejected"] = t["id"] in rejected_ids
            available_tasks.append(t)

    ctx = base_ctx(request, user, "work")
    ctx["tasks"] = available_tasks
    return templates.TemplateResponse("work_typing.html", ctx)


@app.get("/work/typing/{task_id}", response_class=HTMLResponse)
async def work_typing_detail(request: Request, task_id: int):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    task = await db.select("typing_tasks", filters={"id": task_id}, single=True)
    if not task or not task.get("is_active"):
        return redirect_with_flash(request, "/work/typing", "টাইপিং টাস্কটি পাওয়া যায়নি!", "error")

    user_subs = await db.select("typing_submissions", filters={"user_id": user["id"]})
    if any(s["typing_task_id"] == task_id and s.get("status") in ("pending", "approved") for s in user_subs):
        return redirect_with_flash(request, "/work/typing", "এই টাইপিং কাজটি ইতিমধ্যেই জমা দেওয়া হয়েছে!", "info")

    ctx = base_ctx(request, user, "work")
    ctx["task"] = task
    return templates.TemplateResponse("work_typing_detail.html", ctx)

@app.get("/refer", response_class=HTMLResponse)
@app.get("/reffer", response_class=HTMLResponse)
async def refer_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    referrals = await db.select("users", filters={"referred_by": user["referral_code"]}, order="created_at.desc")
    total_ref_earned = len(referrals) * REFERRAL_OWNER_BONUS

    ctx = base_ctx(request, user, "refer")
    ctx["referrals"] = referrals
    ctx["referral_count"] = len(referrals)
    ctx["total_ref_earned"] = total_ref_earned
    ctx["owner_bonus"] = REFERRAL_OWNER_BONUS
    ctx["friend_bonus"] = REFERRAL_NEW_USER_BONUS
    return templates.TemplateResponse("refer.html", ctx)


@app.post("/work/typing/{task_id}/submit")
async def work_typing_submit(
    request: Request,
    task_id: int,
    typed_text: str = Form(...),
):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    task = await db.select("typing_tasks", filters={"id": task_id}, single=True)
    if not task:
        return redirect_with_flash(request, "/work/typing", "টাইপিং টাস্কটি পাওয়া যায়নি!", "error")

    user_subs = await db.select("typing_submissions", filters={"user_id": user["id"]})
    if any(s["typing_task_id"] == task_id and s.get("status") in ("pending", "approved") for s in user_subs):
        return redirect_with_flash(request, "/work/typing", "এই কাজটি আগেই জমা দেওয়া আছে!", "info")

    if not typed_text.strip():
        return redirect_with_flash(request, f"/work/typing/{task_id}", "অনুগ্রহ করে ছবি দেখে টেক্সট টাইপ করুন।", "error")

    await db.insert(
        "typing_submissions",
        {
            "user_id": user["id"],
            "typing_task_id": task_id,
            "typed_text": typed_text.strip(),
            "status": "pending",
            "reward": task["reward"],
        },
    )
    return redirect_with_flash(request, "/work/typing", f"{task['title']} সফলভাবে জমা হয়েছে! অ্যাপ্রুভ হলে টাকা যোগ হবে।", "success")


# ---------------- ADMIN PANEL: TYPING (/admin/t) ----------------

@app.get("/admin/t", response_class=HTMLResponse)
async def admin_typing_panel(request: Request):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অ্যাডমিন অ্যাক্সেস অনুমোদিত নয়!", "error")

    tasks = await db.select("typing_tasks", order="id.desc")
    task_map = {t["id"]: t for t in tasks}
    submissions = await db.select("typing_submissions", order="created_at.desc", limit=50)

    for s in submissions:
        t = task_map.get(s["typing_task_id"], {})
        s["task_title"] = t.get("title", f"Typing #{s['typing_task_id']}")
        s["task_image"] = t.get("image_url")

    next_num = (max((t["id"] for t in tasks), default=0)) + 1
    ctx = base_ctx(request, user, "admin")
    ctx.update({"tasks": tasks, "submissions": submissions, "next_auto_title": f"Typing #{next_num}"})
    return templates.TemplateResponse("admin_typing.html", ctx)


@app.post("/admin/t/task/add")
async def admin_typing_add_task(
    request: Request,
    title: str = Form(""),
    reward: float = Form(...),
    typing_image: UploadFile = File(None),
    fallback_image_url: str = Form(""),
):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")

    image_bytes = await typing_image.read() if typing_image and typing_image.filename else None
    img_url = None
    if image_bytes:
        uploaded_url, is_dummy = await upload_with_load_balancer(image_bytes)
        if uploaded_url and not is_dummy:
            img_url = uploaded_url

    if not img_url and fallback_image_url.strip():
        img_url = fallback_image_url.strip()

    if not img_url:
        return redirect_with_flash(request, "/admin/t", "ImgBB API Key সক্রিয় করুন অথবা ইমেজ লিংক দিন!", "error")

    final_title = title.strip()
    if not final_title:
        existing = await db.select("typing_tasks", order="id.desc", limit=1)
        next_num = (existing[0]["id"] + 1) if existing else 1
        final_title = f"Typing #{next_num}"

    await db.insert(
        "typing_tasks",
        {
            "title": final_title,
            "image_url": img_url,
            "reward": reward,
            "is_active": True,
        },
    )
    return redirect_with_flash(request, "/admin/t", f"'{final_title}' সফলভাবে পাবলিশ হয়েছে!", "success")


@app.post("/admin/t/task/{task_id}/delete")
async def admin_typing_delete_task(request: Request, task_id: int):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")
    await db.delete("typing_tasks", {"id": task_id})
    return redirect_with_flash(request, "/admin/t", "টাইপিং টাস্ক ডিলিট করা হয়েছে।", "info")


@app.post("/admin/t/submission/{sub_id}/{action}")
async def admin_typing_review_submission(request: Request, sub_id: int, action: str):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")

    sub = await db.select("typing_submissions", filters={"id": sub_id}, single=True)
    if not sub or sub["status"] != "pending":
        return redirect_with_flash(request, "/admin/t", "সাবমিশনটি আগেই প্রসেস করা আছে।", "info")

    if action == "approve":
        await db.update("typing_submissions", {"id": sub_id}, {"status": "approved"})
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
        return redirect_with_flash(request, "/admin/t", "টাইপিং অ্যাপ্রুভ হয়েছে ও ইউজারের ব্যালেন্সে টাকা যোগ হয়েছে!", "success")
    else:
        await db.update("typing_submissions", {"id": sub_id}, {"status": "rejected"})
        return redirect_with_flash(request, "/admin/t", "রিজেক্ট করা হয়েছে — ইউজার আবার টাইপ করতে পারবে।", "info")
        
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

# ---------------- NOTICE BOARD (/notice & /admin/n) ----------------

@app.get("/notice", response_class=HTMLResponse)
async def notice_page(request: Request):
    user = await get_current_user(request)
    try:
        notices = await db.select("notices", order="is_pinned.desc,created_at.desc")
    except Exception:
        notices = []

    ctx = base_ctx(request, user, "notice")
    ctx["notices"] = notices
    return templates.TemplateResponse("notice.html", ctx)


@app.get("/admin/n", response_class=HTMLResponse)
async def admin_notice_panel(request: Request):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অ্যাডমিন অ্যাক্সেস অনুমোদিত নয়!", "error")

    try:
        notices = await db.select("notices", order="is_pinned.desc,created_at.desc")
    except Exception:
        notices = []

    ctx = base_ctx(request, user, "admin")
    ctx["notices"] = notices
    return templates.TemplateResponse("admin_notice.html", ctx)


@app.post("/admin/n/add")
async def admin_notice_add(
    request: Request,
    title: str = Form(...),
    category: str = Form("সাধারণ"),
    content: str = Form(...),
    is_pinned: str = Form(""),
    extra_image_urls: str = Form(""),
    images: list[UploadFile] = File(default=[]),
):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")

    uploaded_urls: list[str] = []

    # Upload 1+ selected image files via ImgBB Load Balancer
    for img in images:
        if img and img.filename:
            img_bytes = await img.read()
            if img_bytes:
                img_url, is_dummy = await upload_with_load_balancer(img_bytes)
                if img_url and not is_dummy:
                    uploaded_urls.append(img_url)

    # Optional manual URLs (comma or newline separated)
    if extra_image_urls.strip():
        for raw in extra_image_urls.replace(",", "\n").splitlines():
            u = raw.strip()
            if u.startswith("http"):
                uploaded_urls.append(u)

    await db.insert(
        "notices",
        {
            "title": title.strip(),
            "category": category.strip(),
            "content": content.strip(),
            "image_urls": uploaded_urls,
            "is_pinned": is_pinned == "on",
        },
    )
    return redirect_with_flash(request, "/admin/n", "নতুন নোটিশ সফলভাবে পাবলিশ হয়েছে!", "success")


@app.post("/admin/n/{notice_id}/delete")
async def admin_notice_delete(request: Request, notice_id: int):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")
    await db.delete("notices", {"id": notice_id})
    return redirect_with_flash(request, "/admin/n", "নোটিশটি ডিলিট করা হয়েছে।", "info")
    

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



@app.get("/payout", response_class=HTMLResponse)
async def payout_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    payouts = await db.select("payouts", filters={"user_id": user["id"]}, order="created_at.desc")
    referrals = await db.select("users", filters={"referred_by": user["referral_code"]})
    ctx = base_ctx(request, user, "payout")
    ctx["payouts"] = payouts
    ctx["referral_count"] = len(referrals)
    return templates.TemplateResponse("payout.html", ctx)




COMING_SOON_TASKS = {
    "gmail-sell": {
        "title": "Gmail Account Sell",
        "icon": "fa-solid fa-envelope",
        "color": "text-red-600 bg-red-100",
        "desc": "নতুন ও পুরোনো Gmail অ্যাকাউন্ট নিরাপদে বিক্রি করে সরাসরি টাকা আয় করুন।",
    },
    "fb-sell": {
        "title": "Facebook Account Sell",
        "icon": "fa-brands fa-facebook",
        "color": "text-blue-600 bg-blue-100",
        "desc": "ভেরিফায়েড ও পুরোনো Facebook অ্যাকাউন্ট উচ্চ মূল্যে বিক্রি করার সুবিধা আসছে।",
    },
    "ig-sell": {
        "title": "Instagram Account Sell",
        "icon": "fa-brands fa-instagram",
        "color": "text-pink-600 bg-pink-100",
        "desc": "আপনার Instagram অ্যাকাউন্ট ন্যায্য দামে আমাদের প্ল্যাটফর্মে বিক্রি করতে পারবেন।",
    },
    "tiktok-sell": {
        "title": "TikTok Account Sell",
        "icon": "fa-brands fa-tiktok",
        "color": "text-rose-600 bg-rose-100",
        "desc": "ফলোয়ারযুক্ত বা পুরোনো TikTok অ্যাকাউন্ট নিরাপদ ডিলের মাধ্যমে বিক্রি করুন।",
    },
    "survey": {
        "title": "Daily Survey",
        "icon": "fa-solid fa-square-poll-vertical",
        "color": "text-teal-700 bg-teal-100",
        "desc": "প্রতিদিন সহজ আন্তর্জাতিক সার্ভের উত্তর দিয়ে বাড়তি আয় করার সিস্টেম চালু হচ্ছে।",
    },
}


@app.get("/work/{slug}", response_class=HTMLResponse)
async def work_coming_soon(request: Request, slug: str):
    if slug not in COMING_SOON_TASKS:
        return redirect_with_flash(request, "/work", "কাজটি পাওয়া যায়নি!", "error")

    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "অনুগ্রহ করে প্রথমে লগইন করুন।", "error")

    ctx = base_ctx(request, user, "work")
    ctx["task_info"] = COMING_SOON_TASKS[slug]
    return templates.TemplateResponse("coming_soon.html", ctx)
    
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

@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(request: Request):
    user = await get_current_user(request)
    all_users = await db.select("users", order="total_earned.desc,id.asc")

    leaders = []
    my_rank = None
    for idx, u in enumerate(all_users, start=1):
        earned = float(u.get("total_earned") or 0)
        lvl = get_earning_level(earned)
        is_me = bool(user and u["id"] == user["id"])
        if is_me:
            my_rank = idx
        if idx <= 30:
            leaders.append({
                "rank": idx,
                "name": u.get("name") or "User",
                "avatar_url": u.get("avatar_url"),
                "amount": earned,
                "level": lvl,
                "is_me": is_me,
            })

    ctx = base_ctx(request, user, "leaderboard")
    ctx.update({
        "top3": leaders[:3],
        "leaders": leaders,
        "my_rank": my_rank,
        "my_earned": float(user.get("total_earned") or 0) if user else 0.0,
    })
    return templates.TemplateResponse("leaderboard.html", ctx)
    


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

    allowed_methods = {"bKash", "Nagad", "Rocket", "Binance"}
    if method not in allowed_methods:
        return redirect_with_flash(request, "/payout", "সঠিক পেমেন্ট মেথড নির্বাচন করুন!", "error")

    referrals = await db.select("users", filters={"referred_by": user["referral_code"]})
    ref_count = len(referrals)
    if ref_count < 4:
        return redirect_with_flash(
            request,
            "/payout",
            f"উইথড্র করতে কমপক্ষে ৪টি রেফার প্রয়োজন! (বর্তমান রেফার: {ref_count}/4)",
            "error",
        )

    current_bal = float(user.get("balance", 0))
    if amount < 350:
        return redirect_with_flash(request, "/payout", "সর্বনিম্ন উইথড্র ৩৫০৳!", "error")
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

# ---------------- REVIEW SYSTEM (/review & /admin/r) ----------------

@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request):
    user = await get_current_user(request)
    try:
        all_reviews = await db.select("reviews", order="display_date.desc,created_at.desc")
    except Exception:
        all_reviews = []

    # Show all Admin reviews to everyone + show a normal user's review ONLY to that same user
    visible_reviews = []
    for r in all_reviews:
        if r.get("is_admin_post"):
            visible_reviews.append(r)
        elif user and r.get("user_id") == user["id"]:
            visible_reviews.append(r)

    avg_rating = round(sum(int(r.get("rating") or 5) for r in visible_reviews) / len(visible_reviews), 1) if visible_reviews else 4.9

    ctx = base_ctx(request, user, "review")
    ctx.update({
        "reviews": visible_reviews,
        "avg_rating": avg_rating,
        "total_reviews": len(visible_reviews),
    })
    return templates.TemplateResponse("review.html", ctx)


@app.post("/review/add")
async def user_review_submit(
    request: Request,
    rating: int = Form(5),
    comment: str = Form(...),
    proof_file: UploadFile = File(None),
):
    user = await get_current_user(request)
    if not user:
        return redirect_with_flash(request, "/login", "রিভিউ দেওয়ার জন্য অনুগ্রহ করে লগইন করুন।", "error")

    clean_rating = max(1, min(5, int(rating)))
    proof_url = None
    if proof_file and proof_file.filename:
        img_bytes = await proof_file.read()
        if img_bytes:
            uploaded, is_dummy = await upload_with_load_balancer(img_bytes)
            if uploaded and not is_dummy:
                proof_url = uploaded

    await db.insert(
        "reviews",
        {
            "user_id": user["id"],
            "reviewer_name": user["name"],
            "reviewer_avatar": user.get("avatar_url"),
            "rating": clean_rating,
            "comment": comment.strip(),
            "proof_image": proof_url,
            "display_date": get_bd_today_str(),
            "is_admin_post": False,
        },
    )
    return redirect_with_flash(request, "/review", "আপনার মূল্যবান রিভিউয়ের জন্য ধন্যবাদ!", "success")


@app.get("/admin/r", response_class=HTMLResponse)
async def admin_review_panel(request: Request):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অ্যাডমিন অ্যাক্সেস অনুমোদিত নয়!", "error")

    try:
        reviews = await db.select("reviews", order="display_date.desc,created_at.desc")
    except Exception:
        reviews = []

    ctx = base_ctx(request, user, "admin")
    ctx.update({"reviews": reviews, "today_str": get_bd_today_str()})
    return templates.TemplateResponse("admin_review.html", ctx)


@app.post("/admin/r/add")
async def admin_review_add(
    request: Request,
    reviewer_name: str = Form(...),
    rating: int = Form(5),
    display_date: str = Form(""),
    comment: str = Form(...),
    avatar_url_input: str = Form(""),
    proof_url_input: str = Form(""),
    avatar_file: UploadFile = File(None),
    proof_file: UploadFile = File(None),
):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")

    # Fake Profile Pic (ImgBB upload or URL)
    final_avatar = avatar_url_input.strip() or None
    if avatar_file and avatar_file.filename:
        av_bytes = await avatar_file.read()
        if av_bytes:
            up_av, is_dummy = await upload_with_load_balancer(av_bytes)
            if up_av and not is_dummy:
                final_avatar = up_av

    # Proof Image (ImgBB upload or URL)
    final_proof = proof_url_input.strip() or None
    if proof_file and proof_file.filename:
        pr_bytes = await proof_file.read()
        if pr_bytes:
            up_pr, is_dummy = await upload_with_load_balancer(pr_bytes)
            if up_pr and not is_dummy:
                final_proof = up_pr

    final_date = display_date.strip() or get_bd_today_str()
    clean_rating = max(1, min(5, int(rating)))

    await db.insert(
        "reviews",
        {
            "user_id": user["id"],
            "reviewer_name": reviewer_name.strip(),
            "reviewer_avatar": final_avatar,
            "rating": clean_rating,
            "comment": comment.strip(),
            "proof_image": final_proof,
            "display_date": final_date,
            "is_admin_post": True,
        },
    )
    return redirect_with_flash(request, "/admin/r", "কাস্টম রিভিউ সফলভাবে পাবলিশ হয়েছে!", "success")


@app.post("/admin/r/{review_id}/delete")
async def admin_review_delete(request: Request, review_id: int):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")
    await db.delete("reviews", {"id": review_id})
    return redirect_with_flash(request, "/admin/r", "রিভিউটি ডিলিট করা হয়েছে।", "info")
    
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

# ---------------- ADMIN PANEL: IMGBB LOAD BALANCER (/admin/imgbb) ----------------

@app.get("/admin/imgbb", response_class=HTMLResponse)
async def admin_imgbb_page(request: Request):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অ্যাডমিন অ্যাক্সেস অনুমোদিত নয়!", "error")

    keys = await db.select("imgbb_keys", order="id.asc")
    active_count = sum(1 for k in keys if k.get("is_active"))
    total_uploads = sum(int(k.get("usage_count") or 0) for k in keys)
    total_fails = sum(int(k.get("fail_count") or 0) for k in keys)

    ctx = base_ctx(request, user, "admin")
    ctx.update({
        "imgbb_keys": keys,
        "active_count": active_count,
        "total_uploads": total_uploads,
        "total_fails": total_fails,
    })
    return templates.TemplateResponse("admin_imgbb.html", ctx)


@app.post("/admin/imgbb/add")
async def admin_add_imgbb(request: Request, label: str = Form(...), api_key: str = Form(...)):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")
    await db.insert("imgbb_keys", {"label": label.strip(), "api_key": api_key.strip(), "is_active": True})
    return redirect_with_flash(request, "/admin/imgbb", "নতুন ImgBB API Key লোড ব্যালেন্সারে যুক্ত হয়েছে!", "success")


@app.post("/admin/imgbb/{key_id}/toggle")
async def admin_toggle_imgbb(request: Request, key_id: int):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")
    row = await db.select("imgbb_keys", filters={"id": key_id}, single=True)
    if row:
        new_state = not bool(row.get("is_active"))
        await db.update("imgbb_keys", {"id": key_id}, {"is_active": new_state})
    return redirect_with_flash(request, "/admin/imgbb", "API Key স্ট্যাটাস পরিবর্তন হয়েছে।", "info")


@app.post("/admin/imgbb/{key_id}/delete")
async def admin_delete_imgbb(request: Request, key_id: int):
    user = await get_current_user(request)
    if not user or not user.get("is_admin"):
        return redirect_with_flash(request, "/i", "অনুমোদিত নয়!", "error")
    await db.delete("imgbb_keys", {"id": key_id})
    return redirect_with_flash(request, "/admin/imgbb", "API Key ডিলিট হয়েছে।", "info")
    
@app.get("/contact", response_class=HTMLResponse)
async def contact_page(request: Request):
    user = await get_current_user(request)
    return templates.TemplateResponse("contact.html", base_ctx(request, user, "contact"))


@app.post("/contact")
async def contact_submit(
    request: Request,
    name: str = Form(...),
    contact_info: str = Form(...),
    topic: str = Form(...),
    message: str = Form(...),
):
    try:
        await db.insert(
            "contact_messages",
            {
                "name": name.strip(),
                "contact_info": contact_info.strip(),
                "topic": topic.strip(),
                "message": message.strip(),
            },
        )
    except Exception:
        pass

    return redirect_with_flash(
        request,
        "/contact",
        "আপনার বার্তাটি সফলভাবে পাঠানো হয়েছে! আমাদের টিম শীঘ্রই যোগাযোগ করবে।",
        "success",
    )
    

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
