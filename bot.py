"""
Strelka Mini App: bot + admin panel + API (bitta fayl).

Ishga tushirish:  python bot.py
.env:  BOT_TOKEN, ADMIN_ID, WEBAPP_URL (https), APP_SHORT (ixtiyoriy), HOST, PORT
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, BotCommandScopeChat, CallbackQuery,
                           ChatJoinRequest, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           WebAppInfo)
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
APP_SHORT = os.getenv("APP_SHORT", "").strip()      # BotFather /newapp dagi short name (bo'sh bo'lsa ?start= havola ishlatiladi)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"        # faqat brauzerda sinash uchun
BASE_DIR = Path(__file__).parent
WEBAPP_DIR = BASE_DIR / "webapp"
DB_PATH = BASE_DIR / os.getenv("DB_FILE", "miniapp.db")

if not BOT_TOKEN:
    raise SystemExit(".env faylida BOT_TOKEN yo'q")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("strelka")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
BOT_USERNAME = ""

# ----------------------------------------------------------------- DB
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

DEFAULTS = {"reward": 1000, "ref_reward": 1000, "min_withdraw": 300000,
            "need_friends": 15,   # har bir yechib olish uchun kerak bo'lgan faol do'stlar (0 = talab yo'q)
            "wd_chat": 0}         # arizalar tushadigan kanal ID si (0 = ulanmagan, arizalar adminga keladi)


def init_db():
    db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, username TEXT, name TEXT,
        balance INTEGER NOT NULL DEFAULT 0, friends INTEGER NOT NULL DEFAULT 0,
        solved INTEGER NOT NULL DEFAULT 0, ref_by INTEGER,
        created INTEGER NOT NULL, last_seen INTEGER NOT NULL,
        blocked INTEGER NOT NULL DEFAULT 0,
        lv_token TEXT, lv_left INTEGER NOT NULL DEFAULT 0, lv_ts REAL NOT NULL DEFAULT 0,
        game_level INTEGER NOT NULL DEFAULT 1, game_state TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS withdrawals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        card TEXT NOT NULL, amount INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', created INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS channels(
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,   -- 'plain' | 'request'
        title TEXT NOT NULL, link TEXT NOT NULL, chat_id INTEGER,
        auto_approve INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS join_requests(
        user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, ts INTEGER NOT NULL,
        PRIMARY KEY(user_id, chat_id)
    );
    CREATE TABLE IF NOT EXISTS user_subs(
        user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
        PRIMARY KEY(user_id, channel_id)
    );
    """)
    # eski bazalar uchun yangi ustunlar
    ucols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
    if "friends_used" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN friends_used INTEGER NOT NULL DEFAULT 0")
    if "game_level" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN game_level INTEGER NOT NULL DEFAULT 1")
    if "game_state" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN game_state TEXT NOT NULL DEFAULT ''")
    wcols = {r["name"] for r in db.execute("PRAGMA table_info(withdrawals)")}
    if "used" not in wcols:
        db.execute("ALTER TABLE withdrawals ADD COLUMN used INTEGER NOT NULL DEFAULT 0")
    db.commit()


def get_setting(key: str) -> int:
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return int(r["value"]) if r else DEFAULTS[key]


def set_setting(key: str, value: int):
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    db.commit()


def get_user(uid: int):
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def upsert_user(uid: int, username, name):
    now = int(time.time())
    db.execute(
        "INSERT INTO users(id,username,name,created,last_seen) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET username=excluded.username, name=excluded.name, "
        "last_seen=excluded.last_seen, blocked=0",
        (uid, username, name, now, now))
    db.commit()
    return get_user(uid)


def try_referral(uid: int, inviter: int) -> bool:
    """Yangi foydalanuvchi uchun bir marta, taklif qilgan odamga mukofot."""
    if uid == inviter:
        return False
    u, inv = get_user(uid), get_user(inviter)
    if not u or not inv or u["ref_by"] is not None:
        return False
    if time.time() - u["created"] > 3600:        # faqat yangi kelganlar uchun
        return False
    db.execute("UPDATE users SET ref_by=? WHERE id=?", (inviter, uid))
    db.execute("UPDATE users SET friends=friends+1, balance=balance+? WHERE id=?",
               (get_setting("ref_reward"), inviter))
    db.commit()
    return True


def one(sql: str, *args):
    return db.execute(sql, args).fetchone()[0]


# Faol do'st: sizning havolangiz orqali kirgan va barcha majburiy obuna kanallaridan o'tgan odam
ACTIVE_SQL = ("SELECT COUNT(*) FROM users u WHERE u.ref_by=? AND NOT EXISTS ("
              "SELECT 1 FROM channels c WHERE NOT EXISTS ("
              "SELECT 1 FROM user_subs s WHERE s.user_id=u.id AND s.channel_id=c.id))")


def friend_stats(uid: int) -> dict:
    total = one("SELECT COUNT(*) FROM users WHERE ref_by=?", uid)
    active = one(ACTIVE_SQL, uid)
    u = get_user(uid)
    used = u["friends_used"] if u else 0          # oldingi yechib olishlarga sarflangan do'stlar
    return {"friends_total": total, "friends_active": active,
            "friends_inactive": total - active,
            "friends_free": max(0, active - used),
            "need_friends": get_setting("need_friends")}


def fmt(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def luhn_ok(card: str) -> bool:
    s, alt = 0, False
    for ch in reversed(card):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        s += d
        alt = not alt
    return s % 10 == 0


def esc(s) -> str:
    return html.escape(str(s or ""))


def play_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎮 O'ynash", web_app=WebAppInfo(url=WEBAPP_URL))]])


async def notify_referral(inviter: int, name: str):
    try:
        await bot.send_message(
            inviter, f"🎉 <b>{esc(name)}</b> havolangiz orqali qo'shildi!\n"
                     f"Sizga <b>{fmt(get_setting('ref_reward'))} so'm</b> berildi.")
    except Exception:
        pass


# ----------------------------------------------------------------- Majburiy obuna
def gate_item(ch) -> dict:
    return {"id": ch["id"], "title": ch["title"], "link": ch["link"], "kind": ch["kind"]}


def add_channel(kind: str, title: str, link: str, chat_id=None):
    db.execute("INSERT INTO channels(kind,title,link,chat_id,created) VALUES(?,?,?,?,?)",
               (kind, title, link, chat_id, int(time.time())))
    db.commit()


async def is_member(chat_id: int, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, uid)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False


async def compute_gate(uid: int, verify: bool = False) -> list:
    """Foydalanuvchi hali o'tmagan majburiy obuna kanallari ro'yxati.

    - zayavka havola: zayavka tashlagan (bazada yozilgan) yoki kanal a'zosi bo'lsa o'tgan hisoblanadi;
    - oddiy havola: tekshirilmaydi, «Tekshirish» bosilganda (verify=True) o'tgan hisoblanadi;
    - bir marta o'tgan kanal qayta ko'rsatilmaydi.
    """
    unmet = []
    passed = {r[0] for r in db.execute("SELECT channel_id FROM user_subs WHERE user_id=?", (uid,))}
    for ch in db.execute("SELECT * FROM channels ORDER BY id").fetchall():
        if ch["id"] in passed:
            continue
        ok = False
        if ch["kind"] == "request":
            ok = db.execute("SELECT 1 FROM join_requests WHERE user_id=? AND chat_id=?",
                            (uid, ch["chat_id"])).fetchone() is not None
            if not ok and verify:
                ok = await is_member(ch["chat_id"], uid)
        elif verify:
            ok = True
        if ok:
            db.execute("INSERT OR IGNORE INTO user_subs(user_id,channel_id) VALUES(?,?)", (uid, ch["id"]))
        else:
            unmet.append(ch)
    db.commit()
    return unmet


@dp.chat_join_request()
async def on_join_request(r: ChatJoinRequest):
    """Kanalga zayavka tashlanganda (bot kanalda admin bo'lishi kerak)."""
    ch = db.execute("SELECT * FROM channels WHERE kind='request' AND chat_id=?", (r.chat.id,)).fetchone()
    if not ch:
        return
    db.execute("INSERT OR IGNORE INTO join_requests(user_id,chat_id,ts) VALUES(?,?,?)",
               (r.from_user.id, r.chat.id, int(time.time())))
    db.execute("INSERT OR IGNORE INTO user_subs(user_id,channel_id) VALUES(?,?)", (r.from_user.id, ch["id"]))
    db.commit()
    if ch["auto_approve"]:
        try:
            await r.approve()
        except Exception as e:
            log.warning("zayavkani qabul qilib bo'lmadi: %s", e)


# ----------------------------------------------------------------- Foydalanuvchi: /start
@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    u = m.from_user
    upsert_user(u.id, u.username, u.full_name)
    arg = command.args or ""
    if arg.startswith("ref_") and arg[4:].isdigit():
        if try_referral(u.id, int(arg[4:])):
            await notify_referral(int(arg[4:]), u.full_name)
    text = (f"Salom, <b>{esc(u.first_name)}</b>! 👋\n\n"
            f"🎯 Strelkalar labirintini yeching: yo'li ochiq strelkani bosing, u chiqib ketadi.\n"
            f"💰 Har bir strelka uchun <b>{fmt(get_setting('reward'))} so'm</b>.\n"
            f"👥 Do'st taklif qilsangiz, har biri uchun <b>{fmt(get_setting('ref_reward'))} so'm</b>.\n"
            f"💳 Minimal chiqarish: <b>{fmt(get_setting('min_withdraw'))} so'm</b>.\n\n"
            f"Boshlash uchun pastdagi tugmani bosing 👇")
    await m.answer(text, reply_markup=play_kb())


# ----------------------------------------------------------------- Admin panel
admin = Router()
admin.message.filter(F.from_user.id == ADMIN_ID)
admin.callback_query.filter(F.from_user.id == ADMIN_ID)


class Ads(StatesGroup):
    waiting = State()
    confirm = State()


class SetVal(StatesGroup):
    waiting = State()


SET_NAMES = {"reward": "🎯 Strelka mukofoti",
             "ref_reward": "👥 Referal mukofoti",
             "min_withdraw": "💳 Minimal chiqarish"}


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika", callback_data="adm:stats"),
         InlineKeyboardButton(text="📢 Reklama", callback_data="adm:ads")],
        [InlineKeyboardButton(text="⚙️ Sozlamalar", callback_data="adm:set"),
         InlineKeyboardButton(text="💸 To'lov so'rovlari", callback_data="adm:wd")],
        [InlineKeyboardButton(text="📣 Majburiy obuna", callback_data="adm:subs")],
        [InlineKeyboardButton(text="📤 Yechib olish arizasi", callback_data="adm:wdch")]])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:home")]])


async def safe_edit(c: CallbackQuery, text: str, kb=None):
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


@admin.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=admin_menu_kb())


@admin.callback_query(F.data == "adm:home")
async def adm_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c, "🛠 <b>Admin panel</b>", admin_menu_kb())
    await c.answer()


# ---- statistika
@admin.callback_query(F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    day = int(time.time()) - 86400
    week = int(time.time()) - 7 * 86400
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👤 Jami foydalanuvchi: <b>{fmt(one('SELECT COUNT(*) FROM users'))}</b>\n"
        f"🆕 Oxirgi 24 soatda yangi: <b>{fmt(one('SELECT COUNT(*) FROM users WHERE created>?', day))}</b>\n"
        f"🔥 24 soatda faol: <b>{fmt(one('SELECT COUNT(*) FROM users WHERE last_seen>?', day))}</b>\n"
        f"📅 7 kunda faol: <b>{fmt(one('SELECT COUNT(*) FROM users WHERE last_seen>?', week))}</b>\n"
        f"🚫 Botni bloklagan: <b>{fmt(one('SELECT COUNT(*) FROM users WHERE blocked=1'))}</b>\n\n"
        f"🎯 Yechilgan strelkalar: <b>{fmt(one('SELECT COALESCE(SUM(solved),0) FROM users'))}</b>\n"
        f"👥 Referallar: <b>{fmt(one('SELECT COALESCE(SUM(friends),0) FROM users'))}</b>\n"
        f"💰 Foydalanuvchilar balansi: <b>{fmt(one('SELECT COALESCE(SUM(balance),0) FROM users'))} so'm</b>\n\n"
        f"⏳ Kutilayotgan so'rovlar: <b>{fmt(one('SELECT COUNT(*) FROM withdrawals WHERE status=?', 'pending'))}</b> "
        f"({fmt(one('SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status=?', 'pending'))} so'm)\n"
        f"✅ To'langan: <b>{fmt(one('SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status=?', 'paid'))} so'm</b>"
    )
    await safe_edit(c, text, back_kb())
    await c.answer()


# ---- reklama
@admin.callback_query(F.data == "adm:ads")
async def adm_ads(c: CallbackQuery, state: FSMContext):
    await state.set_state(Ads.waiting)
    await safe_edit(
        c, "📢 <b>Reklama</b>\n\nYubormoqchi bo'lgan xabarni yuboring: matn, rasm, video, GIF, "
           "fayl, audio, ovozli xabar, stiker, so'rovnoma. Istalgan turda bo'ladi (bitta xabar).\n\n"
           "Bekor qilish: /admin", back_kb())
    await c.answer()


@admin.message(Ads.waiting)
async def ads_got(m: Message, state: FSMContext):
    await state.update_data(chat_id=m.chat.id, msg_id=m.message_id)
    await state.set_state(Ads.confirm)
    total = one("SELECT COUNT(*) FROM users WHERE blocked=0")
    await m.answer("👆 Reklama shunday ko'rinadi.\n"
                   f"Qabul qiluvchilar: <b>{fmt(total)}</b> ta foydalanuvchi.",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                       [InlineKeyboardButton(text="✅ «O'ynash» tugmasi bilan yuborish", callback_data="ads:go:1")],
                       [InlineKeyboardButton(text="📤 Tugmasiz yuborish", callback_data="ads:go:0")],
                       [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm:home")]]),
                   reply_to_message_id=m.message_id)


async def copy_to(uid: int, src_chat: int, src_msg: int, kb):
    while True:
        try:
            await bot.copy_message(uid, src_chat, src_msg, reply_markup=kb)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)


async def run_broadcast(src_chat: int, src_msg: int, with_button: bool):
    ids = [r[0] for r in db.execute("SELECT id FROM users WHERE blocked=0")]
    kb = play_kb() if with_button else None
    ok = fail = 0
    for uid in ids:
        try:
            await copy_to(uid, src_chat, src_msg, kb)
            ok += 1
        except TelegramForbiddenError:
            db.execute("UPDATE users SET blocked=1 WHERE id=?", (uid,))
            db.commit()
            fail += 1
        except Exception as e:
            log.warning("broadcast %s: %s", uid, e)
            fail += 1
        await asyncio.sleep(0.05)      # ~20 xabar/soniya, Telegram limitidan past
    await bot.send_message(src_chat, f"📢 Reklama yuborildi.\n✅ Yetkazildi: <b>{ok}</b>\n❌ Xato/bloklagan: <b>{fail}</b>")


@admin.callback_query(Ads.confirm, F.data.startswith("ads:go:"))
async def ads_go(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    with_button = c.data.endswith(":1")
    await safe_edit(c, "⏳ Reklama yuborilmoqda... Tugagach xabar beraman.")
    asyncio.create_task(run_broadcast(data["chat_id"], data["msg_id"], with_button))
    await c.answer()


# ---- sozlamalar
def settings_text() -> str:
    return ("⚙️ <b>Sozlamalar</b>\n\n" + "\n".join(
        f"{SET_NAMES[k]}: <b>{fmt(get_setting(k))} so'm</b>" for k in SET_NAMES))


def settings_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"✏️ {SET_NAMES[k]}", callback_data=f"set:{k}")] for k in SET_NAMES]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin.callback_query(F.data == "adm:set")
async def adm_set(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c, settings_text(), settings_kb())
    await c.answer()


@admin.callback_query(F.data.startswith("set:"))
async def set_pick(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 1)[1]
    if key not in SET_NAMES:
        return await c.answer()
    await state.set_state(SetVal.waiting)
    await state.update_data(key=key)
    await safe_edit(c, f"{SET_NAMES[key]}\nHozirgi qiymat: <b>{fmt(get_setting(key))} so'm</b>\n\n"
                       "Yangi qiymatni raqam bilan yuboring (so'mda). Bekor qilish: /admin")
    await c.answer()


@admin.message(SetVal.waiting)
async def set_value(m: Message, state: FSMContext):
    digits = "".join(ch for ch in (m.text or "") if ch.isdigit())
    if not digits:
        return await m.answer("Faqat raqam yuboring, masalan: 1500")
    val = int(digits)
    key = (await state.get_data())["key"]
    if val > 1_000_000_000 or (key == "min_withdraw" and val < 1):
        return await m.answer("Qiymat noto'g'ri. Qaytadan yuboring.")
    set_setting(key, val)
    await state.clear()
    await m.answer(f"✅ Saqlandi: {SET_NAMES[key]} = <b>{fmt(val)} so'm</b>")
    await m.answer(settings_text(), reply_markup=settings_kb())


# ---- to'lov so'rovlari
def wd_text(r) -> str:
    u = get_user(r["user_id"])
    name = esc(u["name"]) if u else str(r["user_id"])
    card = " ".join(r["card"][i:i + 4] for i in range(0, 16, 4))
    st = friend_stats(r["user_id"])
    return (f"💸 <b>So'rov #{r['id']}</b>\n"
            f"👤 <a href=\"tg://user?id={r['user_id']}\">{name}</a> (<code>{r['user_id']}</code>)\n"
            f"💳 <code>{card}</code>\n"
            f"💰 <b>{fmt(r['amount'])} so'm</b>\n"
            f"👥 Do'stlar: {st['friends_total']} (faol: {st['friends_active']})")


def wd_kb(wid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ To'landi", callback_data=f"wd:ok:{wid}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"wd:no:{wid}")]])


@admin.callback_query(F.data == "adm:wd")
async def adm_wd(c: CallbackQuery):
    rows = db.execute("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id LIMIT 10").fetchall()
    total = one("SELECT COUNT(*) FROM withdrawals WHERE status='pending'")
    await safe_edit(c, f"💸 Kutilayotgan so'rovlar: <b>{total}</b>" +
                    ("" if total else "\n\nHozircha yo'q."), back_kb())
    for r in rows:
        await c.message.answer(wd_text(r), reply_markup=wd_kb(r["id"]))
    if total > len(rows):
        await c.message.answer(f"Yana {total - len(rows)} ta so'rov bor. Bularni hal qilgach qayta oching.")
    await c.answer()


@admin.callback_query(F.data.startswith("wd:"))
async def wd_decide(c: CallbackQuery):
    _, action, wid = c.data.split(":")
    wid = int(wid)
    r = db.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if not r:
        return await c.answer("Topilmadi", show_alert=True)
    new_status = "paid" if action == "ok" else "rejected"
    cur = db.execute("UPDATE withdrawals SET status=? WHERE id=? AND status='pending'", (new_status, wid))
    if cur.rowcount == 0:
        db.commit()
        return await c.answer("Bu so'rov allaqachon hal qilingan", show_alert=True)
    if new_status == "rejected":
        # pul ham, do'stlar ham qaytariladi
        db.execute("UPDATE users SET balance=balance+?, friends_used=MAX(0, friends_used-?) WHERE id=?",
                   (r["amount"], r["used"], r["user_id"]))
    db.commit()
    mark = "✅ To'langan" if new_status == "paid" else "❌ Rad etilgan (pul balansga qaytarildi)"
    await safe_edit(c, wd_text(r) + f"\n\n<b>{mark}</b>")
    try:
        if new_status == "paid":
            await bot.send_message(r["user_id"], f"✅ So'rovingiz bajarildi: <b>{fmt(r['amount'])} so'm</b> kartangizga o'tkazildi.")
        else:
            await bot.send_message(r["user_id"], f"❌ <b>{fmt(r['amount'])} so'm</b> chiqarish so'rovi rad etildi. Mablag' balansingizga qaytarildi.")
    except Exception:
        pass
    await c.answer()


# ---- majburiy obuna (kanallar)
class AddSub(StatesGroup):
    link = State()
    title = State()
    chat = State()
    reqlink = State()


def norm_link(s):
    s = (s or "").strip()
    if s.startswith("t.me/"):
        s = "https://" + s
    if s.startswith(("https://t.me/", "http://t.me/", "https://telegram.me/")) and " " not in s:
        return s
    return None


def subs_text() -> str:
    n = one("SELECT COUNT(*) FROM channels")
    head = "Hozircha kanal yo'q." if not n else f"Kanallar soni: <b>{n}</b>"
    return ("📣 <b>Majburiy obuna</b>\n\n" + head + "\n\n"
            "Foydalanuvchi «Hisobim» bo'limini ochganda shu kanallar ko'rsatiladi.\n"
            "🔗 Oddiy havola: obuna tekshirilmaydi.\n"
            "📝 Zayavka havola: zayavka tashlagani tekshiriladi, bir marta tashlagan odamga qayta ko'rsatilmaydi.")


def subs_kb() -> InlineKeyboardMarkup:
    rows = []
    for ch in db.execute("SELECT * FROM channels ORDER BY id").fetchall():
        icon = "📝" if ch["kind"] == "request" else "🔗"
        rows.append([InlineKeyboardButton(text=f"{icon} {ch['title']}", callback_data=f"sub:view:{ch['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="sub:add")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sub_view(ch):
    is_req = ch["kind"] == "request"
    kind = "📝 Zayavka havola" if is_req else "🔗 Oddiy havola"
    passed = fmt(one("SELECT COUNT(*) FROM user_subs WHERE channel_id=?", ch["id"]))
    text = f"<b>{esc(ch['title'])}</b>\n{kind}\n{esc(ch['link'])}\n\n✅ O'tgan foydalanuvchilar: <b>{passed}</b>"
    rows = []
    if is_req:
        reqs = fmt(one("SELECT COUNT(*) FROM join_requests WHERE chat_id=?", ch["chat_id"]))
        auto_on = bool(ch["auto_approve"])
        state_txt = "yoqilgan" if auto_on else "o'chiq"
        text += f"\n📝 Zayavkalar: <b>{reqs}</b>\n🤖 Avto-qabul: <b>{state_txt}</b>"
        btn_txt = "🤖 Avto-qabulni o'chirish" if auto_on else "🤖 Avto-qabulni yoqish"
        rows.append([InlineKeyboardButton(text=btn_txt, callback_data=f"sub:auto:{ch['id']}")])
    rows.append([InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"sub:del:{ch['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:subs")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@admin.callback_query(F.data == "adm:subs")
async def adm_subs(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c, subs_text(), subs_kb())
    await c.answer()


@admin.callback_query(F.data.startswith("sub:view:"))
async def sub_view_cb(c: CallbackQuery):
    ch = db.execute("SELECT * FROM channels WHERE id=?", (int(c.data.split(":")[2]),)).fetchone()
    if not ch:
        return await c.answer("Topilmadi", show_alert=True)
    text, kb = sub_view(ch)
    await safe_edit(c, text, kb)
    await c.answer()


@admin.callback_query(F.data.startswith("sub:auto:"))
async def sub_auto(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    db.execute("UPDATE channels SET auto_approve=1-auto_approve WHERE id=?", (cid,))
    db.commit()
    ch = db.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    if ch:
        text, kb = sub_view(ch)
        await safe_edit(c, text, kb)
    await c.answer("Saqlandi")


@admin.callback_query(F.data.startswith("sub:del:"))
async def sub_del(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    db.execute("DELETE FROM channels WHERE id=?", (cid,))
    db.execute("DELETE FROM user_subs WHERE channel_id=?", (cid,))
    db.commit()
    await safe_edit(c, subs_text(), subs_kb())
    await c.answer("O'chirildi")


@admin.callback_query(F.data == "sub:add")
async def sub_add(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c, "Qanday havola qo'shamiz?\n\n"
                       "🔗 <b>Oddiy havola</b>: obuna tekshirilmaydi.\n"
                       "📝 <b>Zayavka havola</b>: foydalanuvchi zayavka tashlagani tekshiriladi "
                       "(bot kanalda admin bo'lishi kerak).",
                    InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔗 Oddiy havola", callback_data="sub:kind:plain")],
                        [InlineKeyboardButton(text="📝 Zayavka havola", callback_data="sub:kind:request")],
                        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:subs")]]))
    await c.answer()


@admin.callback_query(F.data.startswith("sub:kind:"))
async def sub_kind(c: CallbackQuery, state: FSMContext):
    kind = c.data.split(":")[2]
    if kind == "plain":
        await state.set_state(AddSub.link)
        await safe_edit(c, "🔗 Kanal havolasini yuboring (masalan: https://t.me/kanal_nomi).\nBekor qilish: /admin")
    else:
        await state.set_state(AddSub.chat)
        await safe_edit(c, "📝 Avval botni kanalga <b>admin</b> qiling («Foydalanuvchilarni taklif qilish» huquqi bilan).\n\n"
                           "Keyin kanaldan istalgan xabarni shu yerga <b>forward</b> qiling yoki "
                           "kanal @username / ID sini (-100...) yuboring.\nBekor qilish: /admin")
    await c.answer()


@admin.message(AddSub.link)
async def sub_link(m: Message, state: FSMContext):
    link = norm_link(m.text)
    if not link:
        return await m.answer("Havola noto'g'ri. https://t.me/... ko'rinishida yuboring.")
    await state.update_data(link=link)
    await state.set_state(AddSub.title)
    await m.answer("Kanal nomini yuboring (tugmada shunday ko'rinadi):")


@admin.message(AddSub.title)
async def sub_title(m: Message, state: FSMContext):
    title = (m.text or "").strip()
    if not title or len(title) > 40:
        return await m.answer("Nom 1-40 belgidan iborat bo'lsin.")
    data = await state.get_data()
    add_channel("plain", title, data["link"])
    await state.clear()
    await m.answer("✅ Kanal qo'shildi.")
    await m.answer(subs_text(), reply_markup=subs_kb())


@admin.message(AddSub.chat)
async def sub_chat(m: Message, state: FSMContext):
    target = None
    fo = getattr(m, "forward_origin", None)
    if fo is not None and getattr(fo, "chat", None) is not None:
        target = fo.chat.id
    elif m.text and (m.text.startswith("@") or m.text.strip().lstrip("-").isdigit()):
        target = m.text.strip()
        if target.lstrip("-").isdigit():
            target = int(target)
    if target is None:
        return await m.answer("Kanaldan xabar forward qiling yoki @username / ID yuboring.")
    try:
        chat = await bot.get_chat(target)
        me_member = await bot.get_chat_member(chat.id, bot.id)
    except Exception:
        return await m.answer("Kanal topilmadi yoki bot kanalda yo'q. Botni kanalga admin qilib, qayta yuboring.")
    if me_member.status != "administrator":
        return await m.answer("Bot bu kanalda admin emas. Admin qilib, qayta yuboring.")
    await state.update_data(chat_id=chat.id, title=(chat.title or str(chat.id))[:40])
    await state.set_state(AddSub.reqlink)
    await m.answer(f"✅ Kanal: <b>{esc(chat.title)}</b>\n\n"
                   "Endi zayavka havolasini yuboring (kanalda «Zayavka bilan qo'shilish» yoqilgan havola).\n"
                   "Yoki <code>avto</code> deb yozing: bot o'zi zayavkali havola yaratadi.")


@admin.message(AddSub.reqlink)
async def sub_reqlink(m: Message, state: FSMContext):
    data = await state.get_data()
    txt = (m.text or "").strip()
    if txt.lower() in ("avto", "auto"):
        try:
            inv = await bot.create_chat_invite_link(data["chat_id"], name="MiniApp", creates_join_request=True)
            link = inv.invite_link
        except Exception:
            return await m.answer("Havola yaratib bo'lmadi. Botda «Foydalanuvchilarni taklif qilish» huquqi bormi?")
    else:
        link = norm_link(txt)
        if not link:
            return await m.answer("Havola noto'g'ri. https://t.me/+... yuboring yoki <code>avto</code> deb yozing.")
    add_channel("request", data["title"], link, data["chat_id"])
    await state.clear()
    await m.answer("✅ Kanal qo'shildi.")
    await m.answer(subs_text(), reply_markup=subs_kb())


# ---- yechib olish arizasi: kanal va do'st talabi
class SetWd(StatesGroup):
    chat = State()
    need = State()


async def wdch_text() -> str:
    chat_id = get_setting("wd_chat")
    if chat_id:
        try:
            title = esc((await bot.get_chat(chat_id)).title)
        except Exception:
            title = str(chat_id)
        ch = f"<b>{title}</b>"
    else:
        ch = "ulanmagan (arizalar botda, sizga keladi)"
    need = get_setting("need_friends")
    need_txt = f"<b>{need}</b> ta faol do'st" if need else "<b>o'chirilgan</b>"
    return ("📤 <b>Yechib olish arizasi</b>\n\n"
            f"📡 Ariza kanali: {ch}\n"
            f"👥 Har bir yechib olish uchun talab: {need_txt}\n\n"
            "Faol do'st: sizning havolangiz orqali kirgan va majburiy obuna kanallaridan o'tgan odam.\n"
            "⚠️ Arizada karta raqami bor, kanal yopiq (private) bo'lsin.")


def wdch_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📡 Kanalni ulash", callback_data="wdch:set")]]
    if get_setting("wd_chat"):
        rows.append([InlineKeyboardButton(text="🔌 Kanalni uzish", callback_data="wdch:off")])
    rows.append([InlineKeyboardButton(text="👥 Do'st talabini o'zgartirish", callback_data="wdch:need")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin.callback_query(F.data == "adm:wdch")
async def adm_wdch(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c, await wdch_text(), wdch_kb())
    await c.answer()


@admin.callback_query(F.data == "wdch:set")
async def wdch_set(c: CallbackQuery, state: FSMContext):
    await state.set_state(SetWd.chat)
    await safe_edit(c, "📡 Botni kanalga <b>admin</b> qiling (xabar yuborish huquqi bilan).\n\n"
                       "Keyin kanaldan istalgan xabarni shu yerga <b>forward</b> qiling yoki "
                       "kanal @username / ID sini (-100...) yuboring.\nBekor qilish: /admin")
    await c.answer()


@admin.callback_query(F.data == "wdch:off")
async def wdch_off(c: CallbackQuery):
    set_setting("wd_chat", 0)
    await safe_edit(c, await wdch_text(), wdch_kb())
    await c.answer("Uzildi")


@admin.callback_query(F.data == "wdch:need")
async def wdch_need(c: CallbackQuery, state: FSMContext):
    await state.set_state(SetWd.need)
    await safe_edit(c, f"Hozirgi talab: <b>{get_setting('need_friends')}</b> ta faol do'st.\n\n"
                       "Yangi sonni yuboring (0 = talab yo'q). Bekor qilish: /admin")
    await c.answer()


@admin.message(SetWd.need)
async def wdch_need_val(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    if not t.isdigit() or int(t) > 1000:
        return await m.answer("0 dan 1000 gacha raqam yuboring.")
    set_setting("need_friends", int(t))
    await state.clear()
    await m.answer("✅ Saqlandi.")
    await m.answer(await wdch_text(), reply_markup=wdch_kb())


@admin.message(SetWd.chat)
async def wdch_chat(m: Message, state: FSMContext):
    target = None
    fo = getattr(m, "forward_origin", None)
    if fo is not None and getattr(fo, "chat", None) is not None:
        target = fo.chat.id
    elif m.text and (m.text.startswith("@") or m.text.strip().lstrip("-").isdigit()):
        target = m.text.strip()
        if target.lstrip("-").isdigit():
            target = int(target)
    if target is None:
        return await m.answer("Kanaldan xabar forward qiling yoki @username / ID yuboring.")
    try:
        chat = await bot.get_chat(target)
        me_member = await bot.get_chat_member(chat.id, bot.id)
        if me_member.status != "administrator":
            return await m.answer("Bot bu kanalda admin emas. Admin qilib, qayta yuboring.")
        await bot.send_message(chat.id, "✅ Yechib olish arizalari shu kanalga tushadi.")
    except Exception:
        return await m.answer("Kanalga xabar yuborib bo'lmadi. Bot kanalda admin va xabar yuborish huquqi bormi?")
    set_setting("wd_chat", chat.id)
    await state.clear()
    await m.answer("✅ Kanal ulandi.")
    await m.answer(await wdch_text(), reply_markup=wdch_kb())


dp.include_router(admin)


@dp.callback_query(F.data.startswith("wd:"), F.from_user.id != ADMIN_ID)
async def wd_not_admin(c: CallbackQuery):
    """Ariza kanalidagi tugmalarni faqat admin bosa oladi."""
    await c.answer("Faqat admin", show_alert=True)


# ----------------------------------------------------------------- Web API (Mini App uchun)
def validate_init_data(init_data: str):
    """Telegram WebApp initData imzosini tekshiradi."""
    if not init_data:
        return None
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        recv_hash = data.pop("hash", None)
        if not recv_hash:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash):
            return None
        if time.time() - int(data.get("auth_date", "0")) > 3 * 86400:
            return None
        return json.loads(data["user"])
    except Exception:
        return None


def jerr(msg: str, status: int = 400):
    return web.json_response({"ok": False, "error": msg}, status=status)


def public_cfg() -> dict:
    return {"reward": get_setting("reward"), "ref_reward": get_setting("ref_reward"),
            "min_withdraw": get_setting("min_withdraw"),
            "need_friends": get_setting("need_friends"),
            "bot_username": BOT_USERNAME, "app_short": APP_SHORT}


async def read(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    tg_user = validate_init_data(data.get("initData", ""))
    if tg_user is None and DEV_MODE:
        tg_user = {"id": 1, "first_name": "Dev"}
    if not tg_user:
        return data, None
    name = f"{tg_user.get('first_name', '')} {tg_user.get('last_name', '')}".strip()
    return data, upsert_user(int(tg_user["id"]), tg_user.get("username"), name)


async def h_index(request):
    return web.FileResponse(WEBAPP_DIR / "index.html",
                            headers={"Cache-Control": "no-cache"})


async def h_config(request):
    return web.json_response({"ok": True, **public_cfg()})


async def h_me(request):
    _, u = await read(request)
    if not u:
        return jerr("auth", 401)
    wds = [{"card": r["card"][-4:], "amount": r["amount"], "status": r["status"]}
           for r in db.execute("SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 10", (u["id"],))]
    wds.reverse()
    gate = [gate_item(c) for c in await compute_gate(u["id"])]
    return web.json_response({"ok": True, "balance": u["balance"], "friends": u["friends"],
                              "withdrawals": wds, "gate": gate, **public_cfg(), **friend_stats(u["id"])})


async def h_subcheck(request):
    _, u = await read(request)
    if not u:
        return jerr("auth", 401)
    gate = await compute_gate(u["id"], verify=True)
    return web.json_response({"ok": True, "gate": [gate_item(c) for c in gate]})


async def h_level(request):
    """Har bir foydalanuvchining bosqichi va strelkalari serverda saqlanadi."""
    data, u = await read(request)
    if not u:
        return jerr("auth", 401)
    try:
        requested_level = max(1, min(int(data.get("level", u["game_level"] or 1)), 10000))
    except (TypeError, ValueError):
        requested_level = int(u["game_level"] or 1)

    current_level = int(u["game_level"] or 1)
    try:
        saved = json.loads(u["game_state"] or "null")
    except (TypeError, ValueError):
        saved = None

    # Avvaldan boshlangan raund bo'lsa, yangi qurilma/oyna ochilganda o'shani qaytaramiz.
    if isinstance(saved, list) and saved and requested_level <= current_level:
        token = u["lv_token"] or secrets.token_urlsafe(18)
        if not u["lv_token"]:
            db.execute("UPDATE users SET lv_token=? WHERE id=?", (token, u["id"]))
            db.commit()
        return web.json_response({"ok": True, "token": token, "level": current_level,
                                  "board": saved, "balance": u["balance"], "resumed": True})

    # Yangi raund boshlanayotgan bo'lsa, avval front-end taxtani yaratib yuboradi.
    board = data.get("board")
    if not isinstance(board, list):
        return web.json_response({"ok": True, "needs_board": True,
                                  "level": max(current_level, requested_level),
                                  "balance": u["balance"]})
    if not board or len(board) > 300:
        return jerr("board")

    clean = []
    seen_ids = set()
    for item in board:
        if not isinstance(item, dict):
            return jerr("board")
        try:
            aid = int(item["id"])
            cells = item["cells"]
            direction = item["dir"]
            if aid in seen_ids or not isinstance(cells, list) or not (2 <= len(cells) <= 300):
                return jerr("board")
            if not isinstance(direction, list) or len(direction) != 2:
                return jerr("board")
            clean_cells = []
            for c in cells:
                if not isinstance(c, list) or len(c) != 2:
                    return jerr("board")
                x, y = int(c[0]), int(c[1])
                if not (0 <= x < 20 and 0 <= y < 25):
                    return jerr("board")
                clean_cells.append([x, y])
            dx, dy = int(direction[0]), int(direction[1])
            if (dx, dy) not in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                return jerr("board")
            seen_ids.add(aid)
            clean.append({"id": aid, "cells": clean_cells, "dir": [dx, dy], "removed": False})
        except (KeyError, TypeError, ValueError):
            return jerr("board")

    token = secrets.token_urlsafe(18)
    left = len(clean)
    db.execute("UPDATE users SET game_level=?, game_state=?, lv_token=?, lv_left=?, lv_ts=0 WHERE id=?",
               (requested_level, json.dumps(clean, separators=(",", ":")), token, left, u["id"]))
    db.commit()
    return web.json_response({"ok": True, "token": token, "level": requested_level,
                              "board": clean, "balance": u["balance"], "resumed": False})


async def h_solve(request):
    data, u = await read(request)
    if not u:
        return jerr("auth", 401)
    if not u["lv_token"] or data.get("token") != u["lv_token"] or u["lv_left"] <= 0:
        return jerr("level")
    try:
        arrow_id = int(data.get("arrow_id"))
        board = json.loads(u["game_state"] or "[]")
    except (TypeError, ValueError):
        return jerr("arrow")
    if not isinstance(board, list):
        return jerr("level")
    target = next((a for a in board if a.get("id") == arrow_id), None)
    if target is None or target.get("removed"):
        return jerr("already_solved")
    now = time.time()
    if now - u["lv_ts"] < 0.12:
        return jerr("tez")
    target["removed"] = True
    left = sum(1 for a in board if not a.get("removed"))
    db.execute("UPDATE users SET balance=balance+?, solved=solved+1, lv_left=?, lv_ts=?, game_state=? WHERE id=?",
               (get_setting("reward"), left, now, json.dumps(board, separators=(",", ":")), u["id"]))
    db.commit()
    updated = get_user(u["id"])
    return web.json_response({"ok": True, "balance": updated["balance"],
                              "remaining": left, "level": updated["game_level"]})


async def h_referral(request):
    data, u = await read(request)
    if not u:
        return jerr("auth", 401)
    inviter = str(data.get("inviter", ""))
    if inviter.isdigit() and try_referral(u["id"], int(inviter)):
        asyncio.create_task(notify_referral(int(inviter), u["name"] or "Do'st"))
    return web.json_response({"ok": True})


async def h_withdraw(request):
    data, u = await read(request)
    if not u:
        return jerr("auth", 401)
    unmet = await compute_gate(u["id"])
    if unmet:
        return web.json_response({"ok": False, "error": "Avval majburiy obunadan o'ting",
                                  "gate": [gate_item(c) for c in unmet]}, status=403)
    st = friend_stats(u["id"])
    need = st["need_friends"]
    if need > 0 and st["friends_free"] < need:
        return web.json_response({"ok": False, **st, "error":
                                  f"Pulni chiqarish uchun yana {need - st['friends_free']} ta faol do'st kerak"},
                                 status=403)
    card = "".join(ch for ch in str(data.get("card", "")) if ch.isdigit())
    try:
        amount = int(data.get("amount", 0))
    except (TypeError, ValueError):
        return jerr("Summa noto'g'ri")
    if len(card) != 16 or not luhn_ok(card):
        return jerr("Karta raqami noto'g'ri")
    min_wd = get_setting("min_withdraw")
    if amount < min_wd:
        return jerr(f"Minimal chiqarish {fmt(min_wd)} so'm")
    cur = db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (amount, u["id"], amount))
    if cur.rowcount == 0:
        db.commit()
        return jerr("Balansda mablag' yetarli emas")
    cur = db.execute("INSERT INTO withdrawals(user_id,card,amount,created,used) VALUES(?,?,?,?,?)",
                     (u["id"], card, amount, int(time.time()), need))
    db.execute("UPDATE users SET friends_used=friends_used+? WHERE id=?", (need, u["id"]))
    db.commit()
    row = db.execute("SELECT * FROM withdrawals WHERE id=?", (cur.lastrowid,)).fetchone()
    text = "🔔 Yangi so'rov!\n\n" + wd_text(row)
    dest = get_setting("wd_chat") or ADMIN_ID          # kanal ulangan bo'lsa kanalga, aks holda adminga
    sent = False
    if dest:
        try:
            await bot.send_message(dest, text, reply_markup=wd_kb(row["id"]))
            sent = True
        except Exception as e:
            log.warning("arizani kanalga yuborib bo'lmadi: %s", e)
    if not sent and ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, text, reply_markup=wd_kb(row["id"]))
        except Exception as e:
            log.warning("adminga yuborib bo'lmadi: %s", e)
    return web.json_response({"ok": True, "balance": get_user(u["id"])["balance"]})


# ----------------------------------------------------------------- Ishga tushirish
async def main():
    global BOT_USERNAME
    init_db()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    if not WEBAPP_URL.startswith("https://"):
        log.warning("WEBAPP_URL https:// bilan boshlanishi kerak (hozir: %r)", WEBAPP_URL)
    if not ADMIN_ID:
        log.warning("ADMIN_ID .env da yo'q: /admin ishlamaydi")

    try:
        await bot.set_my_commands([BotCommand(command="start", description="Boshlash")])
        if ADMIN_ID:
            await bot.set_my_commands(
                [BotCommand(command="start", description="Boshlash"),
                 BotCommand(command="admin", description="Admin panel")],
                scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except Exception as e:
        log.warning("buyruqlar o'rnatilmadi: %s", e)

    app = web.Application()
    app.router.add_get("/", h_index)
    app.router.add_get("/config", h_config)
    for path, h in (("/me", h_me), ("/level", h_level), ("/solve", h_solve),
                    ("/referral", h_referral), ("/withdraw", h_withdraw),
                    ("/subcheck", h_subcheck)):
        app.router.add_post(path, h)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    log.info("Server: http://%s:%s | bot: @%s", HOST, PORT, BOT_USERNAME)

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
