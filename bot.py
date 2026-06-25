import os
import random
import logging
import asyncio
import json
from datetime import datetime, timedelta
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, PreCheckoutQuery, LabeledPrice,
    ChatMemberLeft, ChatMemberBanned
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- ENV VARIABLES ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@doom_73")
CHANNEL_ID = "@fermadoom"
CHANNEL_URL = "https://t.me/fermadoom"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DB_NAME = "doom_farm.db"

# ============================================================
# --- MULTIPLAYER ROULETTE STATE (in-memory) ---
# ============================================================
active_roulette_rounds: dict[int, dict] = {}
ROULETTE_BET_SECONDS = 45
RED_NUMBERS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

ROULETTE_BET_TYPES = {
    "red":    ("🔴 Красное",   2),
    "black":  ("⚫ Чёрное",    2),
    "zero":   ("🟢 Зеро",      36),
    "1_12":   ("1️⃣–12",        3),
    "13_24":  ("13–24",        3),
    "25_36":  ("25–36",        3),
    "even":   ("2️⃣ Чётное",    2),
    "odd":    ("1️⃣ Нечётное",  2),
    "number": ("🎯 Число",     36),
}

def get_bet_color(landed: int) -> str:
    if landed == 0:
        return "zero"
    return "red" if landed in RED_NUMBERS else "black"

def check_bet_win(bet_type: str, bet_value, landed: int) -> bool:
    color = get_bet_color(landed)
    if bet_type == "red":    return color == "red"
    if bet_type == "black":  return color == "black"
    if bet_type == "zero":   return landed == 0
    if bet_type == "1_12":   return 1 <= landed <= 12
    if bet_type == "13_24":  return 13 <= landed <= 24
    if bet_type == "25_36":  return 25 <= landed <= 36
    if bet_type == "even":   return landed != 0 and landed % 2 == 0
    if bet_type == "odd":    return landed != 0 and landed % 2 == 1
    if bet_type == "number": return int(bet_value) == landed
    return False

# ============================================================
# --- SEASONS ---
# ============================================================
SEASONS = {
    "spring": {
        "name": "🌸 Весна",
        "mult": 1.2,
        "desc": "+20% к урожаю",
        "event_chance": 0.10,
    },
    "summer": {
        "name": "☀️ Лето",
        "mult": 1.0,
        "desc": "Стандартный сезон",
        "event_chance": 0.20,
    },
    "autumn": {
        "name": "🍂 Осень",
        "mult": 1.5,
        "desc": "+50% к урожаю, больше событий",
        "event_chance": 0.15,
    },
    "winter": {
        "name": "❄️ Зима",
        "mult": 0.6,
        "desc": "−40% к урожаю",
        "event_chance": 0.05,
    },
}

SEASON_ORDER = ["spring", "summer", "autumn", "winter"]

def get_current_season() -> str:
    # Меняем сезон каждые 7 дней от эпохи
    day_number = (datetime.now() - datetime(2024, 1, 1)).days
    idx = (day_number // 7) % 4
    return SEASON_ORDER[idx]

def get_season_info() -> dict:
    return SEASONS[get_current_season()]

# ============================================================
# --- RANDOM FARM EVENTS (server-wide, stored in memory) ---
# ============================================================
current_farm_event: dict | None = None

FARM_EVENTS = [
    {"id": "drought",     "name": "🌵 Засуха",          "mult": 0.5,  "duration_h": 3,  "desc": "Урожай −50% на 3 часа!"},
    {"id": "rain",        "name": "🌧 Проливной дождь",  "mult": 2.0,  "duration_h": 2,  "desc": "Урожай ×2 на 2 часа!"},
    {"id": "festival",    "name": "🎪 Фестиваль фермы",  "mult": 1.8,  "duration_h": 4,  "desc": "Урожай ×1.8 на 4 часа!"},
    {"id": "pest_plague", "name": "🐛 Нашествие вредителей", "mult": 0.3, "duration_h": 2, "desc": "Урожай −70% на 2 часа!"},
    {"id": "golden_hour", "name": "✨ Золотой час",       "mult": 3.0,  "duration_h": 1,  "desc": "Урожай ×3 на 1 час!"},
]

async def get_active_event() -> dict | None:
    global current_farm_event
    if current_farm_event and datetime.fromisoformat(current_farm_event["ends_at"]) > datetime.now():
        return current_farm_event
    current_farm_event = None
    return None

async def farm_event_task():
    """Случайное событие каждые 2–6 часов."""
    while True:
        wait_h = random.uniform(2, 6)
        await asyncio.sleep(wait_h * 3600)
        season = get_season_info()
        if random.random() < season["event_chance"] * 3:
            event = random.choice(FARM_EVENTS).copy()
            event["ends_at"] = (datetime.now() + timedelta(hours=event["duration_h"])).isoformat()
            global current_farm_event
            current_farm_event = event
            # Уведомить всех активных пользователей
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute("SELECT user_id FROM users WHERE has_started_bot=1 AND user_id!=0") as c:
                    rows = await c.fetchall()
            for (uid2,) in rows:
                try:
                    await bot.send_message(
                        uid2,
                        f"🌍 <b>Событие на сервере!</b>\n\n{event['name']}\n<i>{event['desc']}</i>",
                        parse_mode="HTML"
                    )
                    await asyncio.sleep(0.05)
                except Exception:
                    pass

# ============================================================
# --- RANKS ---
# ============================================================
RANKS = [
    (0,          "🌱 Новичок"),
    (5_000,      "👨‍🌾 Фермер"),
    (25_000,     "🚜 Агроном"),
    (100_000,    "🏡 Помещик"),
    (500_000,    "💼 Магнат"),
    (1_000_000,  "👑 Барон"),
    (5_000_000,  "💎 Лорд"),
    (20_000_000, "🌌 Легенда"),
]

def get_rank(balance: int) -> str:
    rank = RANKS[0][1]
    for threshold, name in RANKS:
        if balance >= threshold:
            rank = name
        else:
            break
    return rank

def get_next_rank(balance: int) -> tuple[str, int] | None:
    for i, (threshold, name) in enumerate(RANKS):
        if balance < threshold:
            return name, threshold
    return None

# ============================================================
# --- CLAN WARS SETTINGS ---
# ============================================================
WAR_DECLARE_COST = 2000
WAR_DURATION_HOURS = 24
WAR_ATTACK_COOLDOWN_MIN = 120          # 2 часа между атаками в войне
WAR_MIN_MEMBERS = 2                     # минимум участников в каждом клане
WAR_STEAL_MIN_PCT = 10
WAR_STEAL_MAX_PCT = 30
WAR_WINNER_TREASURY_PCT = 0.20          # сколько % казны проигравшего забирает победитель
WAR_WINNER_MEMBER_REWARD = 300          # бонус каждому участнику клана-победителя
WAR_WINNER_CLAN_POINTS = 100            # очки войны победителю (накопительно навсегда)
WAR_CHECK_INTERVAL_SEC = 300            # как часто проверяем не закончились ли войны

CLAN_WITHDRAW_COOLDOWN_MIN = 360        # 6 часов между выводами из казны клана

# ============================================================
# --- FSM STATES ---
# ============================================================
class GameStates(StatesGroup):
    waiting_for_deposit_stars = State()
    waiting_for_broadcast = State()
    waiting_for_admin_give_username = State()
    waiting_for_admin_give_amount = State()
    waiting_for_admin_take_username = State()
    waiting_for_admin_take_amount = State()
    waiting_for_admin_reset_user = State()
    waiting_for_duel_opponent = State()
    waiting_for_duel_amount = State()
    waiting_for_trade_item = State()
    waiting_for_trade_amount = State()
    waiting_for_trade_price = State()
    waiting_for_clan_name = State()
    waiting_for_clan_invite = State()
    waiting_for_bank_amount = State()
    waiting_for_transfer_user = State()
    waiting_for_transfer_amount = State()
    waiting_for_donate_amount = State()
    waiting_for_rob_target = State()
    waiting_for_sabotage_target = State()
    waiting_for_multi_roulette_number = State()
    waiting_for_multi_roulette_amount = State()
    # Биржа
    waiting_for_exchange_item = State()
    waiting_for_exchange_amount = State()
    waiting_for_exchange_price = State()
    waiting_for_exchange_buy_id = State()
    # Кредиты
    waiting_for_credit_amount = State()
    # Клановые войны
    waiting_for_war_target_clan = State()
    waiting_for_war_attack_target = State()
    waiting_for_clan_withdraw_amount = State()

# ============================================================
# --- DB INIT ---
# ============================================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # WAL-режим заметно снижает шанс "database is locked" при
        # параллельных коротких подключениях, которые использует бот.
        await db.execute("PRAGMA journal_mode=WAL")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                doom_balance INTEGER DEFAULT 500,
                farm_level INTEGER DEFAULT 1,
                last_farm_time TEXT,
                last_big_farm_time TEXT,
                last_rob_time TEXT,
                last_slot_time TEXT,
                shield_until TEXT,
                has_started_bot INTEGER DEFAULT 0,
                crop_potatoes INTEGER DEFAULT 0,
                crop_apples INTEGER DEFAULT 0,
                crop_pumpkins INTEGER DEFAULT 0,
                crop_watermelons INTEGER DEFAULT 0,
                crop_dumik INTEGER DEFAULT 0,
                clan_id INTEGER DEFAULT NULL,
                bank_balance INTEGER DEFAULT 0,
                last_bank_time TEXT,
                last_quest_time TEXT,
                quest_farm_count INTEGER DEFAULT 0,
                quest_slot_count INTEGER DEFAULT 0,
                quest_rob_count INTEGER DEFAULT 0,
                achievements TEXT DEFAULT '[]',
                total_robs_success INTEGER DEFAULT 0,
                total_earned INTEGER DEFAULT 0,
                referrer_id INTEGER DEFAULT NULL,
                referral_count INTEGER DEFAULT 0,
                sabotage_cooldown TEXT,
                dog_until TEXT,
                fertilizer_count INTEGER DEFAULT 0,
                magnifier_count INTEGER DEFAULT 0,
                dog_item_count INTEGER DEFAULT 0,
                pesticide_count INTEGER DEFAULT 0,
                last_daily_time TEXT,
                last_big_farm_time2 TEXT,
                credit_amount INTEGER DEFAULT 0,
                credit_due TEXT,
                credit_taken_at TEXT,
                total_robs_caught INTEGER DEFAULT 0,
                exchange_trades_count INTEGER DEFAULT 0,
                last_war_attack_time TEXT
            )
        """)

        await db.execute("""
            INSERT INTO users (user_id, username, first_name, doom_balance)
            VALUES (0, 'police_dep', 'Казна Полиции', 0)
            ON CONFLICT(user_id) DO NOTHING
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS clans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                owner_id INTEGER,
                bank INTEGER DEFAULT 0,
                created_at TEXT,
                last_withdraw_time TEXT,
                war_points INTEGER DEFAULT 0,
                wars_won INTEGER DEFAULT 0,
                wars_lost INTEGER DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS market (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER,
                item_type TEXT,
                amount INTEGER,
                price INTEGER,
                created_at TEXT
            )
        """)

        # Биржа (ордера)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS exchange (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                order_type TEXT,
                item_type TEXT,
                amount INTEGER,
                price_per_unit INTEGER,
                created_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS duels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                challenger_id INTEGER,
                opponent_id INTEGER,
                amount INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)

        # Клановые войны
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clan_wars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clan_a_id INTEGER,
                clan_b_id INTEGER,
                score_a INTEGER DEFAULT 0,
                score_b INTEGER DEFAULT 0,
                started_at TEXT,
                ends_at TEXT,
                status TEXT DEFAULT 'active'
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_stats (
                total_spins INTEGER DEFAULT 0,
                total_robs INTEGER DEFAULT 0,
                total_stars_deposited INTEGER DEFAULT 0,
                market_price_modifier REAL DEFAULT 1.0,
                last_price_update TEXT
            )
        """)

        async with db.execute("SELECT COUNT(*) FROM system_stats") as cursor:
            if (await cursor.fetchone())[0] == 0:
                await db.execute("INSERT INTO system_stats VALUES (0, 0, 0, 1.0, NULL)")

        new_cols = [
            ("clan_id", "INTEGER DEFAULT NULL"),
            ("bank_balance", "INTEGER DEFAULT 0"),
            ("last_bank_time", "TEXT"),
            ("last_quest_time", "TEXT"),
            ("quest_farm_count", "INTEGER DEFAULT 0"),
            ("quest_slot_count", "INTEGER DEFAULT 0"),
            ("quest_rob_count", "INTEGER DEFAULT 0"),
            ("achievements", "TEXT DEFAULT '[]'"),
            ("total_robs_success", "INTEGER DEFAULT 0"),
            ("total_earned", "INTEGER DEFAULT 0"),
            ("referrer_id", "INTEGER DEFAULT NULL"),
            ("referral_count", "INTEGER DEFAULT 0"),
            ("sabotage_cooldown", "TEXT"),
            ("dog_until", "TEXT"),
            ("fertilizer_count", "INTEGER DEFAULT 0"),
            ("magnifier_count", "INTEGER DEFAULT 0"),
            ("dog_item_count", "INTEGER DEFAULT 0"),
            ("last_big_farm_time", "TEXT"),
            ("last_slot_time", "TEXT"),
            ("pesticide_count", "INTEGER DEFAULT 0"),
            ("last_daily_time", "TEXT"),
            ("credit_amount", "INTEGER DEFAULT 0"),
            ("credit_due", "TEXT"),
            ("credit_taken_at", "TEXT"),
            ("total_robs_caught", "INTEGER DEFAULT 0"),
            ("exchange_trades_count", "INTEGER DEFAULT 0"),
            ("last_war_attack_time", "TEXT"),
        ]
        for col, typ in new_cols:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
            except aiosqlite.OperationalError:
                pass

        clan_new_cols = [
            ("last_withdraw_time", "TEXT"),
            ("war_points", "INTEGER DEFAULT 0"),
            ("wars_won", "INTEGER DEFAULT 0"),
            ("wars_lost", "INTEGER DEFAULT 0"),
        ]
        for col, typ in clan_new_cols:
            try:
                await db.execute(f"ALTER TABLE clans ADD COLUMN {col} {typ}")
            except aiosqlite.OperationalError:
                pass

        await db.commit()

# ============================================================
# --- SUBSCRIPTION CHECK ---
# ============================================================
async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return not isinstance(member, (ChatMemberLeft, ChatMemberBanned)) and member.status not in ("left", "kicked")
    except Exception:
        return False

def get_subscribe_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL))
    kb.row(InlineKeyboardButton(text="✅ Я подписался!", callback_data="check_subscription"))
    return kb.as_markup()

# ============================================================
# --- HELPERS ---
# ============================================================
async def register_user_chat(user_id: int, username: str, first_name: str, referrer_id: int = None):
    if user_id == 0:
        return
    safe_name_val = (first_name or "Игрок").replace("<", "&lt;").replace(">", "&gt;")
    safe_username = f"@{username}" if username else None
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, referrer_id FROM users WHERE user_id = ?", (user_id,)) as c:
            existing = await c.fetchone()
        if existing:
            await db.execute(
                "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                (safe_username, safe_name_val, user_id)
            )
        else:
            await db.execute("""
                INSERT INTO users (user_id, username, first_name, has_started_bot, referrer_id)
                VALUES (?, ?, ?, 1, ?)
            """, (user_id, safe_username, safe_name_val, referrer_id))
            if referrer_id and referrer_id != user_id:
                await db.execute(
                    "UPDATE users SET doom_balance=doom_balance+200, referral_count=referral_count+1 WHERE user_id=?",
                    (referrer_id,)
                )
                await db.execute(
                    "UPDATE users SET doom_balance=doom_balance+100 WHERE user_id=?",
                    (user_id,)
                )
                try:
                    await bot.send_message(referrer_id,
                        "🎉 По вашей реферальной ссылке зарегистрировался новый игрок! +200 DOOM!")
                except Exception:
                    pass
        await db.execute("UPDATE users SET has_started_bot=1 WHERE user_id=?", (user_id,))
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as c:
            return await c.fetchone()

async def get_user_by_username(username: str):
    clean = username.strip()
    if not clean.startswith("@"):
        clean = f"@{clean}"
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (clean,)) as c:
            return await c.fetchone()

async def update_balance(user_id: int, amount: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET doom_balance=doom_balance+? WHERE user_id=?", (amount, user_id))
        await db.commit()

async def try_deduct_balance(user_id: int, amount: int) -> bool:
    """Атомарно списывает amount DOOM у игрока. True если хватило средств."""
    if amount <= 0:
        return True
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "UPDATE users SET doom_balance=doom_balance-? WHERE user_id=? AND doom_balance>=?",
            (amount, user_id, amount)
        )
        await db.commit()
        return cursor.rowcount > 0

async def try_deduct_bank_balance(user_id: int, amount: int) -> bool:
    """Атомарно списывает amount DOOM с банковского счёта игрока."""
    if amount <= 0:
        return True
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "UPDATE users SET bank_balance=bank_balance-? WHERE user_id=? AND bank_balance>=?",
            (amount, user_id, amount)
        )
        await db.commit()
        return cursor.rowcount > 0

async def try_deduct_clan_bank(clan_id: int, amount: int) -> bool:
    """Атомарно списывает amount DOOM из казны клана."""
    if amount <= 0:
        return True
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "UPDATE clans SET bank=bank-? WHERE id=? AND bank>=?",
            (amount, clan_id, amount)
        )
        await db.commit()
        return cursor.rowcount > 0

async def increment_sys_stat(column: str, amount: int = 1):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f"UPDATE system_stats SET {column}={column}+?", (amount,))
        await db.commit()

def calc_crop_value(user, modifier: float = 1.0) -> int:
    return int((
        user['crop_potatoes'] * 25
        + user['crop_apples'] * 60
        + user['crop_pumpkins'] * 180
        + user['crop_watermelons'] * 600
        + user['crop_dumik'] * 3500
    ) * modifier)

def decode_slot_value(dice_value: int):
    val = dice_value - 1
    return val % 4, (val // 4) % 4, (val // 16) % 4

def check_cooldown(time_str: str, minutes: int) -> tuple[bool, int, int]:
    if not time_str:
        return False, 0, 0
    last = datetime.fromisoformat(time_str)
    end = last + timedelta(minutes=minutes)
    now = datetime.now()
    if now < end:
        rem = end - now
        return True, int(rem.total_seconds() // 60), int(rem.total_seconds() % 60)
    return False, 0, 0

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

async def get_market_modifier() -> float:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT market_price_modifier FROM system_stats LIMIT 1") as c:
            row = await c.fetchone()
    return row[0] if row else 1.0

async def update_market_prices():
    modifier = round(random.uniform(0.7, 1.4), 2)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE system_stats SET market_price_modifier=?, last_price_update=?",
            (modifier, datetime.now().isoformat())
        )
        await db.commit()

def safe_name(text: str) -> str:
    return (text or "Игрок").replace("<", "&lt;").replace(">", "&gt;")

# ============================================================
# --- CLAN HELPERS ---
# ============================================================
async def get_clan_by_id(clan_id: int):
    if not clan_id:
        return None
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM clans WHERE id=?", (clan_id,)) as c:
            return await c.fetchone()

async def get_clan_by_name(name: str):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM clans WHERE LOWER(name)=LOWER(?)", (name.strip(),)) as c:
            return await c.fetchone()

async def get_clan_members(clan_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE clan_id=?", (clan_id,)) as c:
            return await c.fetchall()

async def get_active_war_for_clan(clan_id: int):
    if not clan_id:
        return None
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM clan_wars WHERE status='active' AND (clan_a_id=? OR clan_b_id=?)",
            (clan_id, clan_id)
        ) as c:
            return await c.fetchone()

async def notify_users(user_ids, text: str):
    for uid2 in user_ids:
        try:
            await bot.send_message(uid2, text, parse_mode="HTML")
            await asyncio.sleep(0.05)
        except Exception:
            pass

# ============================================================
# --- ACHIEVEMENTS ---
# ============================================================
ACHIEVEMENTS_LIST = {
    "first_million": ("💎 Первый миллион", "Накопить 1 000 000 DOOM"),
    "robber_10": ("🥷 Рецидивист", "10 успешных ограблений"),
    "caught_10": ("🚓 Любимец Полиции", "Пойман полицией 10 раз"),
    "slot_100": ("🎰 Завсегдатай казино", "100 вращений слота"),
    "max_farm": ("🚜 Агрокороль", "Ферма 15 уровня"),
    "clan_owner": ("👑 Основатель клана", "Создать клан"),
    "referral_5": ("🤝 Вербовщик", "5 рефералов"),
    "lord_rank": ("💎 Лорд", "Достичь ранга Лорд"),
    "credit_repaid": ("🏦 Честный заёмщик", "Погасить кредит"),
    "exchange_trader": ("📊 Биржевик", "Совершить 10 сделок на бирже"),
    "war_winner": ("🛡 Полководец", "Победить в клановой войне"),
}

async def check_and_grant_achievements(user_id: int):
    user = await get_user(user_id)
    if not user:
        return
    try:
        current = json.loads(user['achievements'] or '[]')
    except Exception:
        current = []
    new_achievements = []
    if "first_million" not in current and user['doom_balance'] >= 1_000_000:
        new_achievements.append("first_million")
    if "robber_10" not in current and user['total_robs_success'] >= 10:
        new_achievements.append("robber_10")
    if "max_farm" not in current and user['farm_level'] >= 15:
        new_achievements.append("max_farm")
    if "referral_5" not in current and user['referral_count'] >= 5:
        new_achievements.append("referral_5")
    if "lord_rank" not in current and user['doom_balance'] >= 5_000_000:
        new_achievements.append("lord_rank")
    if "caught_10" not in current and (user['total_robs_caught'] or 0) >= 10:
        new_achievements.append("caught_10")
    if "exchange_trader" not in current and (user['exchange_trades_count'] or 0) >= 10:
        new_achievements.append("exchange_trader")
    if "clan_owner" not in current:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM clans WHERE owner_id=?", (user_id,)) as c:
                owns_clan = (await c.fetchone())[0]
        if owns_clan > 0:
            new_achievements.append("clan_owner")
    if new_achievements:
        for ach in new_achievements:
            current.append(ach)
            name, desc = ACHIEVEMENTS_LIST[ach]
            try:
                await bot.send_message(user_id,
                    f"🏆 <b>Новое достижение!</b>\n{name}\n<i>{desc}</i>", parse_mode="HTML")
            except Exception:
                pass
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "UPDATE users SET achievements=? WHERE user_id=?",
                (json.dumps(current, ensure_ascii=False), user_id)
            )
            await db.commit()

async def grant_achievement_directly(user_id: int, ach_key: str):
    """Выдать конкретное достижение без полной проверки всех условий (для войн и т.п.)."""
    user = await get_user(user_id)
    if not user:
        return
    try:
        current = json.loads(user['achievements'] or '[]')
    except Exception:
        current = []
    if ach_key in current:
        return
    current.append(ach_key)
    name, desc = ACHIEVEMENTS_LIST[ach_key]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET achievements=? WHERE user_id=?",
            (json.dumps(current, ensure_ascii=False), user_id)
        )
        await db.commit()
    try:
        await bot.send_message(user_id,
            f"🏆 <b>Новое достижение!</b>\n{name}\n<i>{desc}</i>", parse_mode="HTML")
    except Exception:
        pass

# ============================================================
# --- QUESTS ---
# ============================================================
DAILY_QUESTS = {
    "farm": ("🌾 Собери урожай 3 раза", 3, 300),
    "slot": ("🎰 Сыграй в слот 5 раз", 5, 500),
    "rob": ("🥷 Соверши 1 ограбление", 1, 400),
}

async def get_quest_status(user) -> dict:
    now = datetime.now()
    last = user['last_quest_time']
    reset = False
    if not last or datetime.fromisoformat(last).date() < now.date():
        reset = True
    return {
        "reset": reset,
        "farm": user['quest_farm_count'],
        "slot": user['quest_slot_count'],
        "rob": user['quest_rob_count'],
    }

async def increment_quest(user_id: int, quest_type: str):
    user = await get_user(user_id)
    if not user:
        return
    status = await get_quest_status(user)
    now = datetime.now()
    if status["reset"]:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "UPDATE users SET quest_farm_count=0, quest_slot_count=0, quest_rob_count=0, last_quest_time=? WHERE user_id=?",
                (now.isoformat(), user_id)
            )
            await db.commit()
        user = await get_user(user_id)

    col = f"quest_{quest_type}_count"
    _, target, reward = DAILY_QUESTS[quest_type]
    current_val = user[col]
    if current_val >= target:
        return
    new_val = current_val + 1
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f"UPDATE users SET {col}=? WHERE user_id=?", (new_val, user_id))
        await db.commit()
    if new_val >= target:
        await update_balance(user_id, reward)
        try:
            name, _, _ = DAILY_QUESTS[quest_type]
            await bot.send_message(user_id,
                f"✅ <b>Квест выполнен!</b>\n{name}\nНаграда: <b>+{reward} DOOM</b>!", parse_mode="HTML")
        except Exception:
            pass

# ============================================================
# --- KEYBOARDS ---
# ============================================================
def get_main_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🌾 Ферма", callback_data="cat_farm"),
        InlineKeyboardButton(text="⚔️ Бои", callback_data="cat_battle"),
        InlineKeyboardButton(text="💰 Финансы", callback_data="cat_finance"),
    )
    kb.row(
        InlineKeyboardButton(text="🏆 Рейтинг", callback_data="cat_rating"),
        InlineKeyboardButton(text="👤 Профиль", callback_data="cat_profile"),
    )
    return kb.as_markup()

def get_cat_farm_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🌾 Собрать (1ч)", callback_data="farm_action_normal"),
        InlineKeyboardButton(text="🧺 Большой (6ч)", callback_data="farm_action_big"),
    )
    kb.row(
        InlineKeyboardButton(text="🎒 Склад", callback_data="main_inventory"),
        InlineKeyboardButton(text="📈 Апгрейд", callback_data="main_upgrade"),
    )
    kb.row(
        InlineKeyboardButton(text="🌍 Событие", callback_data="farm_event_info"),
        InlineKeyboardButton(text="🗓 Сезон", callback_data="farm_season_info"),
    )
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return kb.as_markup()

def get_cat_battle_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="⚔️ Грабёж", callback_data="main_rob_menu"),
        InlineKeyboardButton(text="🤝 Дуэль", callback_data="main_duel_menu"),
    )
    kb.row(
        InlineKeyboardButton(text="🗡 Саботаж", callback_data="main_sabotage_menu"),
    )
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return kb.as_markup()

def get_cat_finance_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🏛 Банк", callback_data="main_bank"),
        InlineKeyboardButton(text="🏪 Рынок", callback_data="main_market"),
    )
    kb.row(
        InlineKeyboardButton(text="🛒 Магазин", callback_data="main_shop"),
        InlineKeyboardButton(text="🎁 Кейс", callback_data="main_case"),
    )
    kb.row(
        InlineKeyboardButton(text="🎲 Рулетка", callback_data="multi_roulette_menu"),
        InlineKeyboardButton(text="💳 Донат ⭐", callback_data="donate_menu"),
    )
    kb.row(
        InlineKeyboardButton(text="📊 Биржа", callback_data="exchange_menu"),
        InlineKeyboardButton(text="🏦 Кредит", callback_data="credit_menu"),
    )
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return kb.as_markup()

def get_cat_rating_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🏆 Топ", callback_data="main_top"),
        InlineKeyboardButton(text="🏰 Клан", callback_data="main_clan"),
    )
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return kb.as_markup()

def get_cat_profile_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="💰 Профиль", callback_data="main_balance"),
        InlineKeyboardButton(text="📋 Квесты", callback_data="main_quests"),
    )
    kb.row(
        InlineKeyboardButton(text="🏅 Ачивки", callback_data="main_achievements"),
        InlineKeyboardButton(text="🔗 Рефералы", callback_data="main_referral"),
    )
    kb.row(InlineKeyboardButton(text="ℹ️ Справка", callback_data="main_help"))
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return kb.as_markup()

def get_back_keyboard():
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return kb.as_markup()

def get_inventory_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="💰 Продать всё", callback_data="crop_sell_all"),
        InlineKeyboardButton(text="🏪 На рынок", callback_data="market_sell_open"),
    )
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return kb.as_markup()

def get_slot_keyboard(current_bet: int):
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="−50", callback_data="bet_change_-50"),
        InlineKeyboardButton(text="−500", callback_data="bet_change_-500"),
        InlineKeyboardButton(text="+500", callback_data="bet_change_500"),
        InlineKeyboardButton(text="+50", callback_data="bet_change_50"),
    )
    kb.row(InlineKeyboardButton(text=f"🎰 Крутить — {current_bet} DOOM", callback_data="slot_action_spin"))
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return kb.as_markup()

def get_slot_end_keyboard(bet: int):
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=f"🎰 Снова ({bet})", callback_data="slot_action_spin"),
        InlineKeyboardButton(text="« Меню", callback_data="main_root"),
    )
    return kb.as_markup()

def get_admin_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📊 Стат.", callback_data="adm_stats"),
        InlineKeyboardButton(text="👥 Игроки", callback_data="adm_players"),
        InlineKeyboardButton(text="🔍 Найти", callback_data="adm_find"),
    )
    kb.row(
        InlineKeyboardButton(text="💰 Начислить", callback_data="adm_give"),
        InlineKeyboardButton(text="💸 Снять", callback_data="adm_take"),
        InlineKeyboardButton(text="🗑 Сброс", callback_data="adm_reset"),
    )
    kb.row(
        InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast"),
        InlineKeyboardButton(text="🏛 Казна", callback_data="adm_police"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_refresh"),
    )
    kb.row(
        InlineKeyboardButton(text="🌍 Запустить событие", callback_data="adm_trigger_event"),
    )
    return kb.as_markup()

def get_admin_back_keyboard():
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="« Админ", callback_data="adm_back"))
    return kb.as_markup()

def get_broadcast_confirm_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Отправить", callback_data="adm_broadcast_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back")
    )
    return kb.as_markup()

def get_top_keyboard(tab: str = "balance"):
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="▶ 💰" if tab == "balance" else "💰", callback_data="top_tab_balance"),
        InlineKeyboardButton(text="▶ 🥷" if tab == "robs" else "🥷", callback_data="top_tab_robs"),
        InlineKeyboardButton(text="▶ 🏰" if tab == "clans" else "🏰", callback_data="top_tab_clans"),
        InlineKeyboardButton(text="▶ ⚔️" if tab == "war" else "⚔️", callback_data="top_tab_war"),
    )
    kb.row(
        InlineKeyboardButton(text="▶ 🚜" if tab == "farm" else "🚜", callback_data="top_tab_farm"),
        InlineKeyboardButton(text="▶ 🏅" if tab == "ach" else "🏅", callback_data="top_tab_ach"),
    )
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return kb.as_markup()

def get_multi_roulette_menu_keyboard(chat_id: int):
    kb = InlineKeyboardBuilder()
    if chat_id not in active_roulette_rounds:
        kb.row(InlineKeyboardButton(text="🎲 Запустить раунд", callback_data="mroul_start"))
    else:
        rd = active_roulette_rounds[chat_id]
        secs_left = max(0, int((rd['end_time'] - datetime.now()).total_seconds()))
        kb.row(InlineKeyboardButton(text=f"⏳ Приём ставок ({secs_left}с)", callback_data="mroul_noop"))
        kb.row(
            InlineKeyboardButton(text="🔴 Красное ×2", callback_data="mroul_bet_red"),
            InlineKeyboardButton(text="⚫ Чёрное ×2", callback_data="mroul_bet_black"),
        )
        kb.row(
            InlineKeyboardButton(text="🟢 Зеро ×36", callback_data="mroul_bet_zero"),
            InlineKeyboardButton(text="🎯 Число ×36", callback_data="mroul_bet_number"),
        )
        kb.row(
            InlineKeyboardButton(text="1–12 ×3", callback_data="mroul_bet_1_12"),
            InlineKeyboardButton(text="13–24 ×3", callback_data="mroul_bet_13_24"),
            InlineKeyboardButton(text="25–36 ×3", callback_data="mroul_bet_25_36"),
        )
        kb.row(
            InlineKeyboardButton(text="Чётное ×2", callback_data="mroul_bet_even"),
            InlineKeyboardButton(text="Нечётное ×2", callback_data="mroul_bet_odd"),
        )
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return kb.as_markup()

# ============================================================
# --- TOP TEXT ---
# ============================================================
async def get_top_text(tab: str = "balance") -> str:
    medals = ["🥇", "🥈", "🥉"]
    if tab == "balance":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT user_id,username,first_name,doom_balance,farm_level FROM users WHERE user_id!=0 ORDER BY doom_balance DESC LIMIT 10"
            ) as c:
                rows = await c.fetchall()
        text = "🏆 <b>ТОП-10 — Богатейшие</b>\n\n"
        for idx, row in enumerate(rows, 1):
            uid2, uname, fn, bal, lvl = row
            medal = medals[idx-1] if idx <= 3 else f"{idx}."
            name = uname if uname else fn
            rank = get_rank(bal)
            text += f"{medal} {name} {rank} — <b>{bal:,}</b> DOOM [🚜{lvl}]\n"
    elif tab == "robs":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT user_id,username,first_name,total_robs_success FROM users WHERE user_id!=0 ORDER BY total_robs_success DESC LIMIT 10"
            ) as c:
                rows = await c.fetchall()
        text = "🥷 <b>ТОП-10 — Грабители</b>\n\n"
        for idx, row in enumerate(rows, 1):
            uid2, uname, fn, robs = row
            medal = medals[idx-1] if idx <= 3 else f"{idx}."
            name = uname if uname else fn
            text += f"{medal} {name} — <b>{robs}</b> налётов\n"
    elif tab == "farm":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT user_id,username,first_name,farm_level,doom_balance FROM users WHERE user_id!=0 ORDER BY farm_level DESC, doom_balance DESC LIMIT 10"
            ) as c:
                rows = await c.fetchall()
        text = "🚜 <b>ТОП-10 — Фермеры</b>\n\n"
        for idx, row in enumerate(rows, 1):
            uid2, uname, fn, lvl, bal = row
            medal = medals[idx-1] if idx <= 3 else f"{idx}."
            name = uname if uname else fn
            text += f"{medal} {name} — Лвл <b>{lvl}</b> | {bal:,} DOOM\n"
    elif tab == "clans":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT c.name, c.bank, c.war_points, COUNT(u.user_id) as members "
                "FROM clans c LEFT JOIN users u ON u.clan_id=c.id GROUP BY c.id ORDER BY c.bank DESC LIMIT 10"
            ) as c:
                rows = await c.fetchall()
        text = "🏰 <b>ТОП-10 — Кланы</b>\n\n"
        for idx, row in enumerate(rows, 1):
            cname, cbank, cwar, members = row
            medal = medals[idx-1] if idx <= 3 else f"{idx}."
            text += f"{medal} {cname} — 💰<b>{cbank:,}</b> | ⚔️{cwar or 0:,} | 👥{members}\n"
        if not rows:
            text += "Кланов пока нет."
    elif tab == "war":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT name, war_points, wars_won, wars_lost FROM clans ORDER BY war_points DESC LIMIT 10"
            ) as c:
                rows = await c.fetchall()
        text = "⚔️ <b>ТОП-10 — Очки клановых войн</b>\n\n"
        for idx, row in enumerate(rows, 1):
            cname, points, won, lost = row
            medal = medals[idx-1] if idx <= 3 else f"{idx}."
            text += f"{medal} {cname} — <b>{points or 0:,}</b> очков | 🏆{won or 0} 💀{lost or 0}\n"
        if not rows:
            text += "Войн пока не было — объявите первую через 🏰 Клан → ⚔️ Война!"
    elif tab == "ach":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT user_id,username,first_name,achievements FROM users WHERE user_id!=0 AND achievements!='[]'"
            ) as c:
                rows = await c.fetchall()
        scored = []
        for uid2, uname, fn, achs_raw in rows:
            try:
                achs = json.loads(achs_raw or '[]')
            except Exception:
                achs = []
            scored.append((uname if uname else fn, len(achs)))
        scored.sort(key=lambda x: -x[1])
        text = "🏅 <b>ТОП-10 — По достижениям</b>\n\n"
        for idx, (name, cnt) in enumerate(scored[:10], 1):
            medal = medals[idx-1] if idx <= 3 else f"{idx}."
            text += f"{medal} {name} — <b>{cnt}</b> достижений\n"
        if not scored:
            text += "Достижений пока нет."
    else:
        text = "Неизвестная вкладка."
    return text

# ============================================================
# --- FORMAT HELPERS ---
# ============================================================
def format_user_stat_text(user, name_string):
    shield_status = (
        f"До {user['shield_until'][:16]}"
        if user['shield_until'] and datetime.fromisoformat(user['shield_until']) > datetime.now()
        else "Нет"
    )
    dog_status = (
        f"Активен до {user['dog_until'][:16]}"
        if user['dog_until'] and datetime.fromisoformat(user['dog_until']) > datetime.now()
        else "Нет"
    )
    total_price = calc_crop_value(user)
    try:
        achs = json.loads(user['achievements'] or '[]')
    except Exception:
        achs = []
    ach_str = " ".join(ACHIEVEMENTS_LIST[a][0] for a in achs if a in ACHIEVEMENTS_LIST) if achs else "Нет"
    pest = user['pesticide_count'] if 'pesticide_count' in user.keys() else 0
    rank = get_rank(user['doom_balance'])
    next_rank_info = get_next_rank(user['doom_balance'])
    next_rank_str = f"\n⬆️ До {next_rank_info[0]}: {(next_rank_info[1] - user['doom_balance']):,} DOOM" if next_rank_info else ""
    season = get_season_info()
    credit_str = ""
    try:
        if user['credit_amount'] and user['credit_amount'] > 0:
            due = datetime.fromisoformat(user['credit_due']) if user['credit_due'] else None
            due_str = due.strftime("%d.%m %H:%M") if due else "?"
            credit_str = f"\n💳 Долг: <b>{user['credit_amount']:,}</b> DOOM (до {due_str})"
    except Exception:
        pass
    return (
        f"💰 <b>ПРОФИЛЬ:</b> {name_string}\n"
        f"{rank}{next_rank_str}\n\n"
        f"💵 Баланс: <b>{user['doom_balance']:,}</b> DOOM\n"
        f"🏦 В банке: <b>{user['bank_balance']:,}</b> DOOM{credit_str}\n"
        f"🚜 Ферма: <b>{user['farm_level']}/15</b> [×{1+(user['farm_level']-1)*0.5:.1f}]\n"
        f"🛡 Щит: <i>{shield_status}</i>\n"
        f"🐕 Пёс: <i>{dog_status}</i>\n"
        f"🗓 Сезон: {season['name']} ({season['desc']})\n\n"
        f"🎒 <b>Склад:</b>\n"
        f"🥔 {user['crop_potatoes']}  🍎 {user['crop_apples']}  🎃 {user['crop_pumpkins']}  🍉 {user['crop_watermelons']}  🌀 {user['crop_dumik']}\n\n"
        f"📦 Стоимость: <b>{total_price:,}</b> DOOM\n"
        f"🧪 Удобрений: {user['fertilizer_count']} | 🐛 Пестицидов: {pest} | 🔍 Луп: {user['magnifier_count']}\n"
        f"🏅 {ach_str}"
    )

def get_help_text():
    return (
        "ℹ️ <b>СПРАВКА DOOM FERMA</b>\n\n"
        "<b>⚡️ Команды:</b>\n"
        "<code>ферма</code> / <code>/f</code> — обычный сбор\n"
        "<code>большой</code> / <code>/fb</code> — большой сбор (×5, кд 6ч)\n"
        "<code>продать</code> / <code>/sell</code> — продать весь урожай\n"
        "<code>баланс</code> / <code>/b</code> — профиль\n"
        "<code>топ</code> / <code>/top</code> — глобальный топ\n"
        "<code>слот 100</code> / <code>/slot 100</code> — слот (кд 10 мин)\n"
        "<code>рулетка</code> — 🎲 мультиплеерная рулетка\n"
        "<code>биржа</code> — 📊 биржа ресурсов\n"
        "<code>кредит</code> — 🏦 взять/погасить кредит\n"
        "<code>апгрейд</code> / <code>/up</code> — апгрейд фермы\n"
        "<code>квесты</code> / <code>/q</code> — квесты\n"
        "<code>реф</code> / <code>/ref</code> — реферальная ссылка\n"
        "<code>меню</code> / <code>/menu</code> — главное меню\n"
        "<code>кейс</code> — открыть кейс\n"
        "<code>магазин</code> — магазин\n"
        "<code>банк</code> — банк\n"
        "<code>бонус</code> / <code>/daily</code> — ежедневный бонус\n\n"
        "<b>⚔️ Бои (бесплатно):</b>\n"
        "<code>грабить @user</code> — ограбить (кд 8ч, шанс 40%)\n"
        "<code>саботаж @user</code> — саботаж (кд 12ч)\n"
        "<code>дуэль @user 500</code> — вызов на дуэль\n"
        "<code>лупа @user</code> — узнать баланс\n\n"
        "<b>🌾 Ферма:</b>\n"
        "Сезоны меняются каждые 7 дней и влияют на урожай.\n"
        "Случайные события (засуха, дождь, фестиваль) действуют на всех!\n\n"
        "<b>📊 Биржа:</b>\n"
        "Выставляй ордера на продажу ресурсов по своей цене.\n"
        "Другие игроки могут купить твои ордера.\n\n"
        "<b>🏦 Кредит:</b>\n"
        "Возьми до 10 000 DOOM под 20% на 24 часа.\n"
        "При просрочке — штраф 50% долга!\n\n"
        "<b>🏰 Клановые войны:</b>\n"
        f"Глава клана объявляет войну ({WAR_DECLARE_COST:,} DOOM из казны), {WAR_DURATION_HOURS}ч на бой.\n"
        "Атакуйте участников вражеского клана бесплатно (кд 2ч) — награбленное идёт в очки клана.\n"
        "Победитель забирает часть казны проигравшего и очки войны (топ ⚔️)!\n\n"
        "<b>🏛 Банк:</b> 5% в сутки, защищён от грабежа\n"
        "<b>🎁 Кейс:</b> от 1000 DOOM — DOOM или предмет\n"
        "<b>💳 Донат:</b> 1⭐=500 / 5⭐=2750 / 10⭐=6000"
    )

# ============================================================
# --- CASE LOGIC ---
# ============================================================
CASE_PRIZES = [
    (30, "doom", 500,   "💰 500 DOOM"),
    (25, "doom", 1000,  "💰 1 000 DOOM"),
    (15, "doom", 2500,  "💰 2 500 DOOM"),
    (8,  "doom", 5000,  "💰 5 000 DOOM"),
    (5,  "doom", 10000, "💰 10 000 DOOM"),
    (7,  "item", "fertilizer_count", "🧪 Удобрение"),
    (5,  "item", "pesticide_count",  "🐛 Пестицид"),
    (3,  "item", "dog_item_count",   "🐕 Пёс-охранник"),
    (2,  "item", "magnifier_count",  "🔍 Лупа"),
]

def roll_case_prize():
    weights = [p[0] for p in CASE_PRIZES]
    return random.choices(CASE_PRIZES, weights=weights, k=1)[0]

def get_case_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📦 1 000 DOOM", callback_data="case_open_1000"),
        InlineKeyboardButton(text="📦 2 500 DOOM", callback_data="case_open_2500"),
        InlineKeyboardButton(text="📦 5 000 DOOM", callback_data="case_open_5000"),
    )
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return kb.as_markup()

def get_donate_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="⭐ 1 = 500 DOOM", callback_data="donate_buy_1"),
        InlineKeyboardButton(text="⭐ 5 = 2 750 DOOM", callback_data="donate_buy_5"),
    )
    kb.row(
        InlineKeyboardButton(text="⭐ 10 = 6 000 DOOM", callback_data="donate_buy_10"),
        InlineKeyboardButton(text="✏️ Своя сумма", callback_data="donate_custom"),
    )
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return kb.as_markup()

# ============================================================
# --- MENU PAGE DATA ---
# ============================================================
async def get_menu_page_data(action: str, user_id: int, first_name: str):
    user = await get_user(user_id)
    if not user:
        return "❌ Не зарегистрированы. Напишите /menu в чат.", get_back_keyboard()
    fname = safe_name(first_name)

    if action == "root":
        rank = get_rank(user['doom_balance'])
        return (
            f"🛸 <b>DOOM Ферма</b> — {fname}\n"
            f"💰 <b>{user['doom_balance']:,}</b> DOOM | 🚜 Лвл <b>{user['farm_level']}/15</b> | {rank}\n\n"
            f"Выберите раздел:",
            get_main_keyboard()
        )
    elif action == "help":
        return get_help_text(), get_back_keyboard()
    elif action == "balance":
        return format_user_stat_text(user, fname), get_back_keyboard()
    elif action == "inventory":
        mod = await get_market_modifier()
        total_price = calc_crop_value(user, mod)
        text = (
            f"🎒 <b>Склад</b> — {fname}\n\n"
            f"🥔 Картошка: <b>{user['crop_potatoes']}</b>\n"
            f"🍎 Яблоки: <b>{user['crop_apples']}</b>\n"
            f"🎃 Тыквы: <b>{user['crop_pumpkins']}</b>\n"
            f"🍉 Арбузы: <b>{user['crop_watermelons']}</b>\n"
            f"🌀 Думик: <b>{user['crop_dumik']}</b>\n\n"
            f"📈 Рынок: <b>×{mod}</b>\n"
            f"💵 Стоимость: <b>{total_price:,}</b> DOOM"
        )
        return text, get_inventory_keyboard()
    elif action == "upgrade":
        lvl = user['farm_level']
        if lvl >= 15:
            return f"⚡️ {fname}, максимальный уровень (×8.0)!", get_back_keyboard()
        cost = 600 * (2 ** (lvl - 1))
        cur_mult = 1 + (lvl - 1) * 0.5
        nxt_mult = cur_mult + 0.5
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text=f"⚡️ Прокачать — {cost:,} DOOM", callback_data="action_buy_upgrade"))
        kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
        return (
            f"📈 <b>Апгрейд фермы</b> ({lvl}/15)\n\n"
            f"Множитель: <b>×{cur_mult}</b> → <b>×{nxt_mult}</b>\n"
            f"⚠️ С 5 лвл: Тыква | С 10 лвл: Арбуз\n\n"
            f"💰 Стоимость: <b>{cost:,}</b> DOOM\n"
            f"Баланс: <b>{user['doom_balance']:,}</b> DOOM",
            kb.as_markup()
        )
    elif action == "top":
        text = await get_top_text("balance")
        return text, get_top_keyboard("balance")
    elif action == "quests":
        status = await get_quest_status(user)
        reset_str = "завтра" if not status["reset"] else "обновлено сегодня"
        text = f"📋 <b>Ежедневные квесты</b> — {fname}\n\n"
        for qtype, (qdesc, qtarget, qreward) in DAILY_QUESTS.items():
            done = status.get(qtype, 0)
            bar = "✅" if done >= qtarget else f"{done}/{qtarget}"
            text += f"{qdesc}\n💰 +{qreward} DOOM | {bar}\n\n"
        text += f"🔄 Обновление: {reset_str}"
        return text, get_back_keyboard()
    elif action == "achievements":
        try:
            achs = json.loads(user['achievements'] or '[]')
        except Exception:
            achs = []
        text = f"🏅 <b>Достижения</b> — {fname}\n\n"
        for key, (name, desc) in ACHIEVEMENTS_LIST.items():
            mark = "✅" if key in achs else "🔒"
            text += f"{mark} {name}\n<i>{desc}</i>\n\n"
        return text, get_back_keyboard()
    elif action == "referral":
        me = await bot.get_me()
        ref_link = f"https://t.me/{me.username}?start={user_id}"
        text = (
            f"🔗 <b>Рефералы</b>\n\n"
            f"Приглашено: <b>{user['referral_count']}</b>\n"
            f"+200 DOOM за игрока | +100 новичку\n\n"
            f"<code>{ref_link}</code>"
        )
        return text, get_back_keyboard()
    elif action == "shop":
        pest = user['pesticide_count'] if 'pesticide_count' in user.keys() else 0
        text = (
            f"🛒 <b>Магазин</b> — {fname}\n\n"
            f"🧪 Удобрение — <b>300 DOOM</b>\n<i>+50% к урожаю на следующий сбор</i>\n\n"
            f"🐛 Пестицид — <b>400 DOOM</b>\n<i>Защита от вредителей на 2 сбора</i>\n\n"
            f"🐕 Пёс-охранник — <b>800 DOOM</b>\n<i>Блокирует саботаж 24ч</i>\n\n"
            f"🔍 Лупа — <b>200 DOOM</b>\n<i>Узнать баланс игрока</i>\n\n"
            f"Инвентарь: 🧪{user['fertilizer_count']} 🐛{pest} 🐕{user['dog_item_count']} 🔍{user['magnifier_count']}"
        )
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="🧪 Удобрение (300)", callback_data="shop_buy_fertilizer"),
            InlineKeyboardButton(text="🐛 Пестицид (400)", callback_data="shop_buy_pesticide"),
        )
        kb.row(
            InlineKeyboardButton(text="🐕 Пёс (800)", callback_data="shop_buy_dog"),
            InlineKeyboardButton(text="🔍 Лупа (200)", callback_data="shop_buy_magnifier"),
        )
        kb.row(InlineKeyboardButton(text="🐕 Активировать пса", callback_data="shop_use_dog"))
        kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
        return text, kb.as_markup()
    elif action == "case":
        text = (
            f"🎁 <b>Кейсы</b> — {fname}\n\n"
            f"Открой кейс и получи DOOM или предмет!\n\n"
            f"📦 <b>1 000 DOOM</b> — базовый\n"
            f"📦 <b>2 500 DOOM</b> — улучшенный\n"
            f"📦 <b>5 000 DOOM</b> — премиум\n\n"
            f"Возможные призы:\n"
            f"💰 500 – 10 000 DOOM\n"
            f"🧪 Удобрение | 🐛 Пестицид | 🐕 Пёс | 🔍 Лупа\n\n"
            f"Баланс: <b>{user['doom_balance']:,}</b> DOOM"
        )
        return text, get_case_keyboard()
    elif action == "bank":
        bank_bal = user['bank_balance']
        text = (
            f"🏛 <b>Банк DOOM</b> — {fname}\n\n"
            f"💵 На счёте: <b>{bank_bal:,}</b> DOOM\n"
            f"📊 5% в сутки\n"
            f"🔒 Защищён от грабежа и саботажа\n\n"
            f"Баланс: <b>{user['doom_balance']:,}</b> DOOM"
        )
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📥 Внести", callback_data="bank_deposit"),
            InlineKeyboardButton(text="📤 Снять", callback_data="bank_withdraw"),
            InlineKeyboardButton(text="💸 Перевести", callback_data="transfer_open"),
        )
        kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
        return text, kb.as_markup()
    elif action == "clan":
        if user['clan_id']:
            clan = await get_clan_by_id(user['clan_id'])
            if clan:
                async with aiosqlite.connect(DB_NAME) as db:
                    async with db.execute("SELECT COUNT(*) FROM users WHERE clan_id=?", (clan['id'],)) as c:
                        members = (await c.fetchone())[0]
                is_owner = clan['owner_id'] == user_id
                war = await get_active_war_for_clan(clan['id'])
                war_str = "⚔️ <b>Идёт война!</b>" if war else "Мир"
                text = (
                    f"🏰 <b>Клан: {clan['name']}</b>\n\n"
                    f"👑 Глава: {'вы' if is_owner else '...'}\n"
                    f"👥 Участников: <b>{members}</b>\n"
                    f"🏦 Казна: <b>{clan['bank']:,}</b> DOOM\n"
                    f"🏅 Очки войны: <b>{(clan['war_points'] or 0):,}</b> | 🏆{clan['wars_won'] or 0} 💀{clan['wars_lost'] or 0}\n"
                    f"🗡 Статус: {war_str}"
                )
                kb = InlineKeyboardBuilder()
                kb.row(
                    InlineKeyboardButton(text="📥 Внести в казну", callback_data="clan_donate"),
                    InlineKeyboardButton(text="⚔️ Война", callback_data="clan_war_menu"),
                )
                if is_owner:
                    kb.row(
                        InlineKeyboardButton(text="📨 Пригласить", callback_data="clan_invite"),
                        InlineKeyboardButton(text="📤 Снять из казны", callback_data="clan_withdraw_open"),
                    )
                    kb.row(InlineKeyboardButton(text="🗑 Распустить", callback_data="clan_disband"))
                else:
                    kb.row(InlineKeyboardButton(text="🚪 Покинуть", callback_data="clan_leave"))
                kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
                return text, kb.as_markup()
        text = (
            f"🏰 <b>Кланы</b> — {fname}\n\n"
            f"Вы не в клане.\nСоздать: <b>5000 DOOM</b>"
        )
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="⚔️ Создать (5000)", callback_data="clan_create"),
            InlineKeyboardButton(text="🏆 Топ кланов", callback_data="clan_top"),
        )
        kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
        return text, kb.as_markup()
    elif action == "market":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT m.id, u.username, u.first_name, m.item_type, m.amount, m.price FROM market m JOIN users u ON m.seller_id=u.user_id ORDER BY m.created_at DESC LIMIT 10"
            ) as c:
                listings = await c.fetchall()
        mod = await get_market_modifier()
        text = f"🏪 <b>Рынок</b> | ×{mod}\n\n"
        icons = {"potatoes": "🥔", "apples": "🍎", "pumpkins": "🎃", "watermelons": "🍉", "dumik": "🌀"}
        if listings:
            for lid, uname, fn, itype, amt, price in listings:
                seller = uname or fn
                text += f"<code>#{lid}</code> {icons.get(itype,'📦')}×{amt} | <b>{price}</b> DOOM | {seller}\n"
        else:
            text += "Лотов нет. Выставьте свои товары!"
        kb = InlineKeyboardBuilder()
        if listings:
            kb.row(InlineKeyboardButton(text="🛍 Купить лот (#ID)", callback_data="market_buy_open"))
        kb.row(InlineKeyboardButton(text="📦 Выставить товар", callback_data="market_sell_open"))
        kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
        return text, kb.as_markup()
    elif action == "rob_menu":
        text = (
            f"⚔️ <b>Грабёж</b> — {fname}\n\n"
            f"Бесплатно! КД: 8ч | Шанс: 40%\n\n"
            f"В группе: ответьте на сообщение словом <b>грабить</b>\n"
            f"В ЛС: <code>грабить @username</code>"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🥷 Топ грабителей", callback_data="rob_top"))
        kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
        return text, kb.as_markup()
    elif action == "sabotage_menu":
        text = (
            f"🗡 <b>Саботаж</b> — {fname}\n\n"
            f"Бесплатно! КД: 12ч\n"
            f"Уничтожает 20–40% склада цели\n\n"
            f"В группе: ответьте на сообщение словом <b>саботаж</b>\n"
            f"В ЛС: <code>саботаж @username</code>\n"
            f"⚠️ Пёс-охранник блокирует саботаж!"
        )
        return text, get_back_keyboard()
    elif action == "duel_menu":
        text = (
            f"🤝 <b>Дуэли</b> — {fname}\n\n"
            f"Бросок монеты на ставку!\n\n"
            f"Команда: <code>дуэль @username ставка</code>"
        )
        return text, get_back_keyboard()

    return "Страница не найдена", get_back_keyboard()

# ============================================================
# --- SUBSCRIPTION CHECK CALLBACK ---
# ============================================================
@dp.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    if await is_subscribed(uid):
        await register_user_chat(uid, callback.from_user.username, callback.from_user.first_name)
        await callback.message.edit_text(
            f"✅ Подписка подтверждена! Добро пожаловать, {callback.from_user.first_name}!\n\n"
            "Напишите <b>меню</b> или /menu",
            parse_mode="HTML"
        )
    else:
        await callback.answer("❌ Вы ещё не подписались на канал!", show_alert=True)

# ============================================================
# --- /start ---
# ============================================================
@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start_private(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    args = message.text.split()
    referrer_id = None
    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])
        if referrer_id == uid:
            referrer_id = None

    if not await is_subscribed(uid):
        await message.answer(
            "📢 <b>Для игры необходимо подписаться на канал DOOM Ferma!</b>\n\n"
            "Подпишитесь и нажмите «Я подписался!»",
            reply_markup=get_subscribe_keyboard(),
            parse_mode="HTML"
        )
        return

    await register_user_chat(uid, message.from_user.username, message.from_user.first_name, referrer_id)
    ref_link = f"https://t.me/{(await bot.get_me()).username}?start={uid}"
    await message.answer(
        f"🛸 Добро пожаловать, {message.from_user.first_name}!\n\n"
        "Напишите <b>меню</b> или /menu для открытия панели.\n\n"
        f"🔗 Реферальная ссылка:\n<code>{ref_link}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛸 Открыть меню", callback_data="main_root")],
        ]),
        parse_mode="HTML"
    )

# ============================================================
# --- SUBSCRIPTION GUARD ---
# ============================================================
async def require_subscription(callback: CallbackQuery) -> bool:
    if await is_subscribed(callback.from_user.id):
        return True
    await callback.answer("❌ Подпишитесь на @fermadoom для игры!", show_alert=True)
    return False

# ============================================================
# --- CATEGORY CALLBACKS ---
# ============================================================
@dp.callback_query(F.data == "cat_farm")
async def cat_farm_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    uid = callback.from_user.id
    user = await get_user(uid)
    fname = safe_name(callback.from_user.first_name)
    season = get_season_info()
    event = await get_active_event()
    event_str = f"\n🌍 Событие: <b>{event['name']}</b> (×{event['mult']})" if event else ""
    text = (
        f"🌾 <b>Ферма</b> — {fname}\n"
        f"🚜 Уровень: <b>{user['farm_level']}/15</b> [×{1+(user['farm_level']-1)*0.5:.1f}]\n"
        f"🗓 Сезон: {season['name']} ({season['desc']})"
        f"{event_str}\n"
        f"💰 Баланс: <b>{user['doom_balance']:,}</b> DOOM"
    )
    await callback.message.edit_text(text, reply_markup=get_cat_farm_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "farm_season_info")
async def farm_season_info_callback(callback: CallbackQuery):
    season_key = get_current_season()
    season = get_season_info()
    days_in_season = 7
    day_number = (datetime.now() - datetime(2024, 1, 1)).days
    days_left = days_in_season - (day_number % days_in_season)
    text = (
        f"🗓 <b>Текущий сезон: {season['name']}</b>\n\n"
        f"📊 Эффект: {season['desc']}\n"
        f"📅 До смены сезона: <b>{days_left}</b> дн.\n\n"
        f"<b>Все сезоны:</b>\n"
        f"🌸 Весна — +20% к урожаю\n"
        f"☀️ Лето — стандартный\n"
        f"🍂 Осень — +50% к урожаю\n"
        f"❄️ Зима — −40% к урожаю"
    )
    await callback.message.edit_text(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "farm_event_info")
async def farm_event_info_callback(callback: CallbackQuery):
    event = await get_active_event()
    if event:
        ends = datetime.fromisoformat(event["ends_at"])
        rem = ends - datetime.now()
        h = int(rem.total_seconds() // 3600)
        m = int((rem.total_seconds() % 3600) // 60)
        text = (
            f"🌍 <b>Активное событие!</b>\n\n"
            f"{event['name']}\n"
            f"<i>{event['desc']}</i>\n\n"
            f"⏳ Осталось: <b>{h}ч {m}мин.</b>"
        )
    else:
        text = (
            "🌍 <b>Событий нет</b>\n\n"
            "Случайные события происходят автоматически каждые 2–6 часов.\n"
            "Вы получите уведомление когда начнётся новое!"
        )
    await callback.message.edit_text(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "cat_battle")
async def cat_battle_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    await callback.message.edit_text(
        "⚔️ <b>Бои</b>\n\nВыберите действие:",
        reply_markup=get_cat_battle_keyboard(), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "cat_finance")
async def cat_finance_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    uid = callback.from_user.id
    user = await get_user(uid)
    fname = safe_name(callback.from_user.first_name)
    mod = await get_market_modifier()
    text = (
        f"💰 <b>Финансы</b> — {fname}\n\n"
        f"💵 Баланс: <b>{user['doom_balance']:,}</b> DOOM\n"
        f"🏦 Банк: <b>{user['bank_balance']:,}</b> DOOM\n"
        f"📈 Рынок: ×{mod}"
    )
    await callback.message.edit_text(text, reply_markup=get_cat_finance_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "cat_rating")
async def cat_rating_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    await callback.message.edit_text(
        "🏆 <b>Рейтинг</b>\n\nВыберите раздел:",
        reply_markup=get_cat_rating_keyboard(), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "cat_profile")
async def cat_profile_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    await callback.message.edit_text(
        "👤 <b>Профиль</b>\n\nВыберите раздел:",
        reply_markup=get_cat_profile_keyboard(), parse_mode="HTML"
    )
    await callback.answer()

# ============================================================
# --- /admin ---
# ============================================================
@dp.message(Command("admin"), F.chat.type == "private")
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа.")
        return
    await show_admin_panel(message)

async def show_admin_panel(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE user_id!=0") as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE has_started_bot=1 AND user_id!=0") as c:
            active = (await c.fetchone())[0]
        async with db.execute("SELECT * FROM system_stats LIMIT 1") as c:
            stats = await c.fetchone()
    season = get_season_info()
    event = await get_active_event()
    event_str = f"\n🌍 Событие: {event['name']}" if event else "\n🌍 Событий нет"
    text = (
        "🛠 <b>ПАНЕЛЬ АДМИНИСТРАТОРА</b>\n\n"
        f"👥 Всего: <b>{total}</b> | ✅ Активных: <b>{active}</b>\n"
        f"🎰 Спинов: <b>{stats[0] if stats else 0}</b>\n"
        f"🥷 Грабежей: <b>{stats[1] if stats else 0}</b>\n"
        f"⭐ Звёзд: <b>{stats[2] if stats else 0}</b>\n"
        f"🗓 Сезон: {season['name']}{event_str}"
    )
    await message.answer(text, reply_markup=get_admin_keyboard(), parse_mode="HTML")

# ============================================================
# --- ADMIN CALLBACKS ---
# ============================================================
@dp.callback_query(F.data.startswith("adm_"))
async def admin_callbacks(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа!", show_alert=True)
        return
    action = callback.data.removeprefix("adm_")

    if action in ("back", "refresh"):
        await state.clear()
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM users WHERE user_id!=0") as c:
                total = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM users WHERE has_started_bot=1 AND user_id!=0") as c:
                active = (await c.fetchone())[0]
            async with db.execute("SELECT * FROM system_stats LIMIT 1") as c:
                stats = await c.fetchone()
        season = get_season_info()
        text = (
            "🛠 <b>ПАНЕЛЬ АДМИНИСТРАТОРА</b>\n\n"
            f"👥 Всего: <b>{total}</b> | ✅ Активных: <b>{active}</b>\n"
            f"🎰 Спинов: <b>{stats[0] if stats else 0}</b>\n"
            f"🥷 Грабежей: <b>{stats[1] if stats else 0}</b>\n"
            f"⭐ Звёзд: <b>{stats[2] if stats else 0}</b>\n"
            f"🗓 Сезон: {season['name']}"
        )
        try:
            await callback.message.edit_text(text, reply_markup=get_admin_keyboard(), parse_mode="HTML")
        except Exception:
            pass
        await callback.answer("✅ Обновлено" if action == "refresh" else "")
        return

    if action == "trigger_event":
        event = random.choice(FARM_EVENTS).copy()
        event["ends_at"] = (datetime.now() + timedelta(hours=event["duration_h"])).isoformat()
        global current_farm_event
        current_farm_event = event
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT user_id FROM users WHERE has_started_bot=1 AND user_id!=0") as c:
                rows = await c.fetchall()
        sent = 0
        for (uid2,) in rows:
            try:
                await bot.send_message(uid2,
                    f"🌍 <b>Событие запущено администратором!</b>\n\n{event['name']}\n<i>{event['desc']}</i>",
                    parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await callback.answer(f"✅ Событие запущено! Уведомлено: {sent}", show_alert=True)
        return

    if action == "stats":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM users WHERE user_id!=0") as c:
                total = (await c.fetchone())[0]
            async with db.execute("SELECT SUM(doom_balance) FROM users WHERE user_id!=0") as c:
                total_doom = (await c.fetchone())[0] or 0
            async with db.execute("SELECT doom_balance FROM users WHERE user_id=0") as c:
                police_row = await c.fetchone()
            async with db.execute("SELECT * FROM system_stats LIMIT 1") as c:
                stats = await c.fetchone()
            async with db.execute("SELECT market_price_modifier FROM system_stats LIMIT 1") as c:
                mod_row = await c.fetchone()
            async with db.execute("SELECT COUNT(*) FROM exchange") as c:
                exchange_orders = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM clan_wars WHERE status='active'") as c:
                active_wars = (await c.fetchone())[0]
        police = police_row[0] if police_row else 0
        mod = mod_row[0] if mod_row else 1.0
        text = (
            f"📊 <b>СТАТИСТИКА</b>\n\n"
            f"👥 Игроков: <b>{total}</b>\n"
            f"💰 DOOM в обороте: <b>{total_doom:,}</b>\n"
            f"🏛 Казна Полиции: <b>{police:,}</b>\n"
            f"📈 Рынок: <b>×{mod}</b>\n"
            f"📊 Ордеров на бирже: <b>{exchange_orders}</b>\n"
            f"⚔️ Активных войн: <b>{active_wars}</b>\n\n"
            f"🎰 Спинов: <b>{stats[0] if stats else 0}</b>\n"
            f"🥷 Грабежей: <b>{stats[1] if stats else 0}</b>\n"
            f"⭐ Звёзд: <b>{stats[2] if stats else 0}</b>"
        )
        try:
            await callback.message.edit_text(text, reply_markup=get_admin_back_keyboard(), parse_mode="HTML")
        except Exception:
            pass
        await callback.answer()
        return

    if action == "players":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT user_id,username,first_name,doom_balance,farm_level FROM users WHERE user_id!=0 ORDER BY doom_balance DESC LIMIT 20"
            ) as c:
                rows = await c.fetchall()
        text = "👥 <b>ТОП-20 (баланс):</b>\n\n"
        for i, row in enumerate(rows, 1):
            uid2, uname, fname2, bal, lvl = row
            display = uname if uname else fname2
            text += f"{i}. {display} | <b>{bal:,}</b> | Лвл {lvl}\n"
        try:
            await callback.message.edit_text(text, reply_markup=get_admin_back_keyboard(), parse_mode="HTML")
        except Exception:
            pass
        await callback.answer()
        return

    if action == "police":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT doom_balance FROM users WHERE user_id=0") as c:
                row = await c.fetchone()
        bal = row[0] if row else 0
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🗑 Обнулить казну", callback_data="adm_police_reset"))
        kb.row(InlineKeyboardButton(text="« Назад", callback_data="adm_back"))
        try:
            await callback.message.edit_text(
                f"🏛 <b>Казна Полиции</b>\n\nБаланс: <b>{bal:,}</b> DOOM",
                reply_markup=kb.as_markup(), parse_mode="HTML"
            )
        except Exception:
            pass
        await callback.answer()
        return

    if action == "police_reset":
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET doom_balance=0 WHERE user_id=0")
            await db.commit()
        await callback.answer("✅ Казна обнулена!", show_alert=True)
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🗑 Обнулить казну", callback_data="adm_police_reset"))
        kb.row(InlineKeyboardButton(text="« Назад", callback_data="adm_back"))
        try:
            await callback.message.edit_text(
                "🏛 <b>Казна Полиции</b>\n\nБаланс: <b>0</b> DOOM",
                reply_markup=kb.as_markup(), parse_mode="HTML"
            )
        except Exception:
            pass
        return

    if action == "give":
        await state.set_state(GameStates.waiting_for_admin_give_username)
        try:
            await callback.message.edit_text(
                "💰 <b>Начисление</b>\n\nВведите @username или ID:\n/cancel — отмена",
                reply_markup=get_admin_back_keyboard(), parse_mode="HTML"
            )
        except Exception:
            pass
        await callback.answer()
        return

    if action == "take":
        await state.set_state(GameStates.waiting_for_admin_take_username)
        try:
            await callback.message.edit_text(
                "💸 <b>Снятие</b>\n\nВведите @username или ID:\n/cancel — отмена",
                reply_markup=get_admin_back_keyboard(), parse_mode="HTML"
            )
        except Exception:
            pass
        await callback.answer()
        return

    if action == "reset":
        await state.set_state(GameStates.waiting_for_admin_reset_user)
        try:
            await callback.message.edit_text(
                "🗑 <b>Сброс аккаунта</b>\n\nВведите @username или ID:\n/cancel — отмена",
                reply_markup=get_admin_back_keyboard(), parse_mode="HTML"
            )
        except Exception:
            pass
        await callback.answer()
        return

    if action == "find":
        await state.set_state(GameStates.waiting_for_admin_give_username)
        await state.update_data(admin_action="find")
        try:
            await callback.message.edit_text(
                "🔍 <b>Поиск</b>\n\nВведите @username или ID:\n/cancel — отмена",
                reply_markup=get_admin_back_keyboard(), parse_mode="HTML"
            )
        except Exception:
            pass
        await callback.answer()
        return

    if action == "broadcast":
        await state.set_state(GameStates.waiting_for_broadcast)
        try:
            await callback.message.edit_text(
                "📢 <b>Рассылка</b>\n\nОтправьте текст (HTML).\n/cancel — отмена",
                reply_markup=get_admin_back_keyboard(), parse_mode="HTML"
            )
        except Exception:
            pass
        await callback.answer()
        return

    if action == "broadcast_confirm":
        data = await state.get_data()
        broadcast_text = data.get("broadcast_text")
        if not broadcast_text:
            await callback.answer("❌ Текст не найден.", show_alert=True)
            return
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT user_id FROM users WHERE has_started_bot=1 AND user_id!=0") as c:
                all_users = await c.fetchall()
        await state.clear()
        sent = failed = 0
        try:
            await callback.message.edit_text(f"📢 Рассылка... ({len(all_users)} чел.)", parse_mode="HTML")
        except Exception:
            pass
        await callback.answer()
        for (uid2,) in all_users:
            try:
                await bot.send_message(uid2, broadcast_text, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
        try:
            await callback.message.edit_text(
                f"📢 <b>Готово!</b>\n✅ {sent} | ❌ {failed}",
                reply_markup=get_admin_back_keyboard(), parse_mode="HTML"
            )
        except Exception:
            pass
        return

    await callback.answer()

# ============================================================
# --- ADMIN FSM ---
# ============================================================
@dp.message(Command("cancel"), F.chat.type == "private")
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        return
    await state.clear()
    if is_admin(message.from_user.id):
        await show_admin_panel(message)
    else:
        await message.answer("❌ Действие отменено.")

@dp.message(GameStates.waiting_for_broadcast, F.chat.type == "private")
async def admin_receive_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(broadcast_text=message.text)
    await message.answer(
        f"📢 <b>Предпросмотр:</b>\n\n{'─'*20}\n{message.text}\n{'─'*20}\n\nОтправить?",
        reply_markup=get_broadcast_confirm_keyboard(), parse_mode="HTML"
    )

@dp.message(GameStates.waiting_for_admin_give_username, F.chat.type == "private")
async def admin_give_username(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    admin_action = data.get("admin_action", "give")
    arg = message.text.strip()
    target = await get_user_by_username(arg) if arg.startswith("@") else (await get_user(int(arg)) if arg.isdigit() else None)
    if not target or target['user_id'] == 0:
        await message.answer("❌ Не найден.", reply_markup=get_admin_back_keyboard())
        return
    if admin_action == "find":
        await state.clear()
        name = target['username'] or target['first_name']
        await message.answer(
            format_user_stat_text(target, name) + f"\n\n🆔 ID: <code>{target['user_id']}</code>",
            reply_markup=get_admin_back_keyboard(), parse_mode="HTML"
        )
        return
    name = target['username'] or target['first_name']
    await state.update_data(target_uid=target['user_id'], target_name=name)
    await state.set_state(GameStates.waiting_for_admin_give_amount)
    await message.answer(f"✅ {name} | Баланс: <b>{target['doom_balance']:,}</b>\n\nСумма:\n/cancel", parse_mode="HTML")

@dp.message(GameStates.waiting_for_admin_give_amount, F.chat.type == "private")
async def admin_give_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if not message.text.lstrip("-").isdigit():
        await message.answer("❌ Целое число!")
        return
    amount = int(message.text.strip())
    data = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET doom_balance=doom_balance+? WHERE user_id=?", (amount, data['target_uid']))
        await db.commit()
    updated = await get_user(data['target_uid'])
    await state.clear()
    await message.answer(
        f"✅ +{amount} → {data['target_name']}\nНовый баланс: <b>{updated['doom_balance']:,}</b>",
        reply_markup=get_admin_back_keyboard(), parse_mode="HTML"
    )
    try:
        await bot.send_message(data['target_uid'], f"💰 Администратор начислил <b>{amount} DOOM</b>!", parse_mode="HTML")
    except Exception:
        pass

@dp.message(GameStates.waiting_for_admin_take_username, F.chat.type == "private")
async def admin_take_username(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    arg = message.text.strip()
    target = await get_user_by_username(arg) if arg.startswith("@") else (await get_user(int(arg)) if arg.isdigit() else None)
    if not target or target['user_id'] == 0:
        await message.answer("❌ Не найден.", reply_markup=get_admin_back_keyboard())
        return
    name = target['username'] or target['first_name']
    await state.update_data(target_uid=target['user_id'], target_name=name)
    await state.set_state(GameStates.waiting_for_admin_take_amount)
    await message.answer(f"✅ {name} | Баланс: <b>{target['doom_balance']:,}</b>\n\nСумма:\n/cancel", parse_mode="HTML")

@dp.message(GameStates.waiting_for_admin_take_amount, F.chat.type == "private")
async def admin_take_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if not message.text.lstrip("-").isdigit():
        await message.answer("❌ Целое число!")
        return
    amount = int(message.text.strip())
    data = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET doom_balance=MAX(0,doom_balance-?) WHERE user_id=?", (amount, data['target_uid']))
        await db.commit()
    updated = await get_user(data['target_uid'])
    await state.clear()
    await message.answer(
        f"✅ −{amount} → {data['target_name']}\nНовый баланс: <b>{updated['doom_balance']:,}</b>",
        reply_markup=get_admin_back_keyboard(), parse_mode="HTML"
    )

@dp.message(GameStates.waiting_for_admin_reset_user, F.chat.type == "private")
async def admin_reset_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    arg = message.text.strip()
    target = await get_user_by_username(arg) if arg.startswith("@") else (await get_user(int(arg)) if arg.isdigit() else None)
    if not target or target['user_id'] == 0:
        await message.answer("❌ Не найден.", reply_markup=get_admin_back_keyboard())
        return
    name = target['username'] or target['first_name']
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            UPDATE users SET doom_balance=500, farm_level=1, last_farm_time=NULL, last_big_farm_time=NULL,
            last_rob_time=NULL, last_slot_time=NULL, shield_until=NULL,
            crop_potatoes=0, crop_apples=0, crop_pumpkins=0, crop_watermelons=0, crop_dumik=0,
            bank_balance=0, achievements='[]', total_robs_success=0,
            fertilizer_count=0, pesticide_count=0, dog_item_count=0, magnifier_count=0,
            credit_amount=0, credit_due=NULL, credit_taken_at=NULL,
            total_robs_caught=0, exchange_trades_count=0, last_war_attack_time=NULL
            WHERE user_id=?
        """, (target['user_id'],))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Аккаунт {name} сброшен.", reply_markup=get_admin_back_keyboard(), parse_mode="HTML")
    try:
        await bot.send_message(target['user_id'], "⚠️ Ваш аккаунт сброшен администратором. Начальный баланс: 500 DOOM.")
    except Exception:
        pass

# ============================================================
# --- MAIN MENU CALLBACKS ---
# ============================================================
@dp.callback_query(F.data.startswith("main_"))
async def main_menu_callbacks(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    await state.clear()
    action = callback.data.removeprefix("main_")
    uid = callback.from_user.id
    await register_user_chat(uid, callback.from_user.username, callback.from_user.first_name)

    if action == "root":
        user = await get_user(uid)
        fname = safe_name(callback.from_user.first_name)
        bal = user['doom_balance'] if user else 0
        lvl = user['farm_level'] if user else 1
        rank = get_rank(bal)
        text = (
            f"🛸 <b>DOOM Ферма</b> — {fname}\n"
            f"💰 <b>{bal:,}</b> DOOM | 🚜 Лвл <b>{lvl}/15</b> | {rank}\n\n"
            f"Выберите раздел:"
        )
        try:
            await callback.message.edit_text(text, reply_markup=get_main_keyboard(), parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=get_main_keyboard(), parse_mode="HTML")
        await callback.answer()
        return

    text, reply_markup = await get_menu_page_data(action, uid, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode="HTML", disable_web_page_preview=True)
    await callback.answer()

# ============================================================
# --- GLOBAL TOP TABS ---
# ============================================================
@dp.callback_query(F.data.startswith("top_tab_"))
async def top_tab_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    tab = callback.data.removeprefix("top_tab_")
    text = await get_top_text(tab)
    try:
        await callback.message.edit_text(text, reply_markup=get_top_keyboard(tab), parse_mode="HTML")
    except Exception:
        pass
    await callback.answer()

# ============================================================
# --- FARM ---
# ============================================================
@dp.callback_query(F.data.startswith("farm_action_"))
async def farm_actions_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    mode = callback.data.split("_")[2]
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user:
        return

    if mode == "normal":
        on_cd, m, s = check_cooldown(user['last_farm_time'], 60)
        if on_cd:
            kb = InlineKeyboardBuilder()
            kb.row(InlineKeyboardButton(text="⚡️ Пропустить за 5 ⭐", callback_data="skip_farm_normal"))
            kb.row(InlineKeyboardButton(text="« Назад", callback_data="cat_farm"))
            await callback.message.edit_text(
                f"⏳ Обычный сбор: ещё {m} мин. {s} сек.\nПропустить за 5 ⭐?",
                reply_markup=kb.as_markup()
            )
            await callback.answer()
            return
        await run_harvest_logic(callback.message, uid, callback.from_user.first_name, user['farm_level'],
                                is_big=False, fertilized=(user['fertilizer_count'] > 0),
                                has_pesticide=(user['pesticide_count'] > 0))
    elif mode == "big":
        on_cd, m, s = check_cooldown(user['last_big_farm_time'], 360)
        if on_cd:
            h, mins2 = m // 60, m % 60
            kb = InlineKeyboardBuilder()
            kb.row(InlineKeyboardButton(text="⚡️ Пропустить за 50 ⭐", callback_data="skip_farm_big"))
            kb.row(InlineKeyboardButton(text="« Назад", callback_data="cat_farm"))
            await callback.message.edit_text(
                f"⏳ Большой сбор: ещё {h}ч {mins2}мин.\nПропустить за 50 ⭐?",
                reply_markup=kb.as_markup()
            )
            await callback.answer()
            return
        await run_harvest_logic(callback.message, uid, callback.from_user.first_name, user['farm_level'],
                                is_big=True, fertilized=(user['fertilizer_count'] > 0),
                                has_pesticide=(user['pesticide_count'] > 0))

    await callback.answer()

async def run_harvest_logic(message: Message, uid: int, fname: str, lvl: int,
                             is_big: bool, fertilized: bool = False, has_pesticide: bool = False):
    now = datetime.now()
    season = get_season_info()
    event = await get_active_event()

    mode_mult = 5 if is_big else 1
    lvl_mult = 1 + (lvl - 1) * 0.5
    fert_mult = 1.5 if fertilized else 1.0
    season_mult = season["mult"]
    event_mult = event["mult"] if event else 1.0

    total_mult = lvl_mult * mode_mult * fert_mult * season_mult * event_mult

    potatoes = int(random.randint(3, 6) * total_mult)
    apples = int(random.randint(1, 4) * total_mult)
    pumpkins = int(random.randint(1, 2) * total_mult) if lvl >= 5 else 0
    watermelons = int(random.randint(1, 2) * total_mult) if lvl >= 10 else 0

    roll = random.uniform(0, 100)
    dumik = 2 if (roll <= 2.0 if is_big else roll <= 1.0) else (1 if (roll <= 12.0 if is_big else roll <= 6.0) else 0)

    pest_event = False
    pest_blocked = False
    pest_loss_text = ""
    if random.random() < 0.15:
        pest_event = True
        if has_pesticide:
            pest_blocked = True
        else:
            loss_pct = random.uniform(0.20, 0.40)
            potatoes = int(potatoes * (1 - loss_pct))
            apples = int(apples * (1 - loss_pct))
            pumpkins = int(pumpkins * (1 - loss_pct))
            watermelons = int(watermelons * (1 - loss_pct))
            dumik = int(dumik * (1 - loss_pct))
            pest_loss_text = f"\n🐛 <b>Вредители!</b> Потеряно {int(loss_pct*100)}% урожая!\n💡 Купи пестицид в 🛒 Магазине"

    async with aiosqlite.connect(DB_NAME) as db:
        field = "last_big_farm_time" if is_big else "last_farm_time"
        await db.execute(f"""
            UPDATE users SET
                crop_potatoes=crop_potatoes+?,
                crop_apples=crop_apples+?,
                crop_pumpkins=crop_pumpkins+?,
                crop_watermelons=crop_watermelons+?,
                crop_dumik=crop_dumik+?,
                {field}=?
            WHERE user_id=?
        """, (potatoes, apples, pumpkins, watermelons, dumik, now.isoformat(), uid))
        if fertilized:
            await db.execute("UPDATE users SET fertilizer_count=MAX(0,fertilizer_count-1) WHERE user_id=?", (uid,))
        if pest_blocked:
            await db.execute("UPDATE users SET pesticide_count=MAX(0,pesticide_count-1) WHERE user_id=?", (uid,))
        await db.commit()

    await increment_quest(uid, "farm")
    await check_and_grant_achievements(uid)

    prefix = "🧺 БОЛЬШОЙ СБОР" if is_big else "🌾 Сбор урожая"
    fert_note = "\n🧪 <b>+50% от удобрения!</b>" if fertilized else ""
    season_note = f"\n🗓 Сезон: {season['name']} (×{season['mult']})"
    event_note = f"\n🌍 {event['name']} (×{event['mult']})" if event else ""
    pest_note = "\n🐛 <b>Пестицид сработал!</b> Вредители отогнаны." if pest_blocked else pest_loss_text

    reward = f"🥔 +{potatoes}  🍎 +{apples}{fert_note}"
    if pumpkins:
        reward += f"  🎃 +{pumpkins}"
    if watermelons:
        reward += f"  🍉 +{watermelons}"
    if dumik:
        reward += f"\n🌀 <b>РЕДКОСТЬ! Думиков: +{dumik}!</b>"
    reward += pest_note + season_note + event_note

    sn = safe_name(fname)
    await message.answer(
        f"{prefix} | {sn} [Лвл {lvl}]\n\n{reward}",
        reply_markup=get_back_keyboard(), parse_mode="HTML"
    )

# ============================================================
# --- SKIP COOLDOWN (Stars) ---
# ============================================================
@dp.callback_query(F.data.startswith("skip_farm_"))
async def skip_farm_callback(callback: CallbackQuery):
    mode = callback.data.split("_")[2]
    stars = 50 if mode == "big" else 5
    await callback.message.answer_invoice(
        title="Сброс таймера фермы",
        description=f"Мгновенный сброс кд ({mode} сбор)",
        prices=[LabeledPrice(label="Пропуск", amount=stars)],
        provider_token="", currency="XTR",
        payload=f"skip_{mode}_{callback.from_user.id}"
    )
    await callback.answer()

@dp.message(F.successful_payment, F.successful_payment.invoice_payload.startswith("skip_"))
async def skip_payment_success(message: Message):
    try:
        parts = message.successful_payment.invoice_payload.split("_")
        mode, uid = parts[1], int(parts[2])
    except Exception:
        return
    user = await get_user(uid)
    if not user:
        return
    field = "last_big_farm_time" if mode == "big" else "last_farm_time"
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f"UPDATE users SET {field}=NULL WHERE user_id=?", (uid,))
        await db.commit()
    await increment_sys_stat("total_stars_deposited", 50 if mode == "big" else 5)
    await message.answer("⚡️ Кулдаун сброшен!")
    await run_harvest_logic(message, uid, user['first_name'], user['farm_level'],
                            is_big=(mode == "big"),
                            fertilized=(user['fertilizer_count'] > 0),
                            has_pesticide=(user['pesticide_count'] > 0))

# ============================================================
# --- SLOT MACHINE ---
# ============================================================
@dp.callback_query(F.data == "slot_menu_open")
async def slot_menu_open_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    data = await state.get_data()
    bet = data.get("current_bet", 100)
    await callback.message.edit_text(
        "🎰 <b>Слот-машина</b>\n\nКД: 10 минут между спинами.",
        reply_markup=get_slot_keyboard(bet), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("bet_change_"))
async def bet_change_callback(callback: CallbackQuery, state: FSMContext):
    change = int(callback.data.split("_")[2])
    data = await state.get_data()
    bet = data.get("current_bet", 100) + change
    if bet < 10:
        await callback.answer("❌ Минимальная ставка: 10 DOOM!", show_alert=True)
        return
    await state.update_data(current_bet=bet)
    try:
        await callback.message.edit_reply_markup(reply_markup=get_slot_keyboard(bet))
    except Exception:
        pass
    await callback.answer(f"Ставка: {bet} DOOM")

@dp.callback_query(F.data == "slot_action_spin")
async def slot_action_spin_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user:
        return
    on_cd, m, s = check_cooldown(user['last_slot_time'], 10)
    if on_cd:
        await callback.answer(f"⏳ Ещё {m} мин. {s} сек.", show_alert=True)
        return
    data = await state.get_data()
    bet = data.get("current_bet", 100)
    if user['doom_balance'] < bet:
        await callback.answer(f"❌ Не хватает DOOM для ставки {bet}!", show_alert=True)
        return
    await callback.answer("🎰 Крутим!")
    await run_slot_machine(callback.message, callback.from_user, bet)

async def run_slot_machine(message: Message, from_user, bet: int):
    now = datetime.now()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "UPDATE users SET doom_balance=doom_balance-?, last_slot_time=? WHERE user_id=? AND doom_balance>=?",
            (bet, now.isoformat(), from_user.id, bet)
        )
        await db.commit()
        if cursor.rowcount == 0:
            await message.answer("❌ Недостаточно средств!")
            return

    await increment_sys_stat("total_spins")
    await increment_quest(from_user.id, "slot")

    dice_msg = await message.answer_dice(emoji="🎰")
    await asyncio.sleep(2.2)

    r1, r2, r3 = decode_slot_value(dice_msg.dice.value)
    multiplier = 0.0
    comb = "Ничего не выпало"

    if r1 == 3 and r2 == 3 and r3 == 3:
        multiplier, comb = 10.0, "🎰 ТРИ СЕМЁРКИ — ДЖЕКПОТ!"
    elif (r1 == r2 == r3) and r1 in (1, 2):
        multiplier, comb = 7.0, "🎉 3 в ряд!"
    elif r1 == 0 and r2 == 0 and r3 == 0:
        multiplier, comb = 1.5, "🔥 3 BAR"
    elif r1 == 3 and r2 == 3:
        multiplier, comb = 3.0, "✨ 2 Семёрки"
    elif r1 == r2 and r1 in (1, 2):
        multiplier, comb = 2.0, "✨ 2 совпадения"
    elif r1 == 0 and r2 == 0:
        multiplier, comb = 1.5, "✨ BAR × 2"
    elif r1 == 0 and r2 == 3:
        multiplier, comb = 1.0, "🪙 Возврат"

    cn = safe_name(from_user.first_name)
    if multiplier > 0:
        win = int(bet * multiplier)
        await update_balance(from_user.id, win)
        await check_and_grant_achievements(from_user.id)
        text = f"🎰 {cn}\n{comb} ×{multiplier}\n💰 +<b>{win}</b> DOOM!"
    else:
        text = f"🎰 {cn}\n💸 Не повезло."
    await message.answer(text, reply_markup=get_slot_end_keyboard(bet), parse_mode="HTML")

# ============================================================
# --- MULTIPLAYER ROULETTE ---
# ============================================================
def build_roulette_status_text(chat_id: int) -> str:
    rd = active_roulette_rounds.get(chat_id)
    if not rd:
        return (
            "🎲 <b>Мультиплеерная рулетка</b>\n\n"
            "Нет активного раунда.\n"
            "Нажмите «🎲 Запустить раунд» чтобы начать!\n\n"
            "<b>Коэффициенты:</b>\n"
            "🔴⚫ Красное/Чёрное — ×2\n"
            "1–12 / 13–24 / 25–36 — ×3\n"
            "Чётное/Нечётное — ×2\n"
            "🎯 Число (0–36) — ×36\n"
            "🟢 Зеро — ×36"
        )
    secs_left = max(0, int((rd['end_time'] - datetime.now()).total_seconds()))
    bets = rd['bets']
    total_pot = sum(b['amount'] for b in bets.values())
    bet_lines = ""
    for uid2, b in list(bets.items())[:10]:
        type_label = ROULETTE_BET_TYPES.get(b['type'], (b['type'], 0))[0]
        num_str = f" ({b['value']})" if b['type'] == "number" else ""
        bet_lines += f"• {b['name']} → {type_label}{num_str}: <b>{b['amount']:,}</b>\n"
    if len(bets) > 10:
        bet_lines += f"...и ещё {len(bets)-10} ставок\n"
    return (
        f"🎲 <b>Мультиплеерная рулетка</b>\n\n"
        f"⏳ До броска: <b>{secs_left} сек.</b>\n"
        f"👥 Участников: <b>{len(bets)}</b> | 💰 Банк: <b>{total_pot:,}</b> DOOM\n\n"
        f"{bet_lines or 'Ставок ещё нет — делайте!'}\n"
        f"Выберите тип ставки ↓"
    )

@dp.callback_query(F.data == "multi_roulette_menu")
async def multi_roulette_menu_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    await state.clear()
    chat_id = callback.message.chat.id
    text = build_roulette_status_text(chat_id)
    try:
        await callback.message.edit_text(text, reply_markup=get_multi_roulette_menu_keyboard(chat_id), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=get_multi_roulette_menu_keyboard(chat_id), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "mroul_noop")
async def mroul_noop(callback: CallbackQuery):
    rd = active_roulette_rounds.get(callback.message.chat.id)
    if rd:
        secs = max(0, int((rd['end_time'] - datetime.now()).total_seconds()))
        await callback.answer(f"⏳ До броска: {secs} сек.", show_alert=False)
    else:
        await callback.answer()

@dp.callback_query(F.data == "mroul_start")
async def mroul_start_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    chat_id = callback.message.chat.id
    if chat_id in active_roulette_rounds:
        await callback.answer("❌ Раунд уже идёт!", show_alert=True)
        return
    fname = safe_name(callback.from_user.first_name)
    end_time = datetime.now() + timedelta(seconds=ROULETTE_BET_SECONDS)
    active_roulette_rounds[chat_id] = {
        'end_time': end_time,
        'bets': {},
        'message_id': callback.message.message_id,
        'starter': fname,
    }
    text = build_roulette_status_text(chat_id)
    try:
        await callback.message.edit_text(text, reply_markup=get_multi_roulette_menu_keyboard(chat_id), parse_mode="HTML")
    except Exception:
        pass
    await callback.answer(f"🎲 Раунд запущен! {ROULETTE_BET_SECONDS} сек. на ставки.")
    asyncio.create_task(run_multi_roulette_round(chat_id, callback.message))

async def run_multi_roulette_round(chat_id: int, message: Message):
    await asyncio.sleep(ROULETTE_BET_SECONDS)
    rd = active_roulette_rounds.pop(chat_id, None)
    if not rd:
        return
    bets = rd['bets']
    if not bets:
        try:
            await message.answer("🎲 <b>Рулетка</b>\n\nНикто не поставил — раунд отменён.", parse_mode="HTML")
        except Exception:
            pass
        return
    landed = random.randint(0, 36)
    color = get_bet_color(landed)
    color_emoji = {"zero": "🟢", "red": "🔴", "black": "⚫"}[color]
    result_lines = []
    total_won = 0
    total_lost = 0
    for uid2, b in bets.items():
        bet_type = b['type']
        bet_value = b.get('value')
        amount = b['amount']
        name = b['name']
        multiplier = ROULETTE_BET_TYPES.get(bet_type, ("?", 0))[1]
        won = check_bet_win(bet_type, bet_value, landed)
        type_label = ROULETTE_BET_TYPES.get(bet_type, (bet_type, 0))[0]
        num_str = f" ({bet_value})" if bet_type == "number" else ""
        if won:
            prize = amount * multiplier
            await update_balance(uid2, prize)
            await check_and_grant_achievements(uid2)
            result_lines.append(f"✅ {name} [{type_label}{num_str}] +<b>{prize:,}</b> DOOM (×{multiplier})")
            total_won += prize
        else:
            result_lines.append(f"❌ {name} [{type_label}{num_str}] −<b>{amount:,}</b> DOOM")
            total_lost += amount
    result_text = (
        f"🎲 <b>Рулетка — Результат!</b>\n\n"
        f"🎯 Выпало: <b>{landed}</b> {color_emoji}\n\n"
        + "\n".join(result_lines)
        + f"\n\n💰 Выплачено: <b>{total_won:,}</b> DOOM\n"
        f"💸 Проиграно: <b>{total_lost:,}</b> DOOM"
    )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🎲 Новый раунд", callback_data="mroul_start"))
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    try:
        await message.answer(result_text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data.startswith("mroul_bet_"))
async def mroul_bet_type_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    chat_id = callback.message.chat.id
    if chat_id not in active_roulette_rounds:
        await callback.answer("❌ Нет активного раунда!", show_alert=True)
        return
    uid = callback.from_user.id
    bet_type = callback.data.removeprefix("mroul_bet_")
    if bet_type == "number":
        await state.update_data(mroul_chat_id=chat_id, mroul_type="number", mroul_uid=uid)
        await state.set_state(GameStates.waiting_for_multi_roulette_number)
        await callback.message.answer("🎯 <b>Ставка на число</b>\n\nВведите число от 0 до 36:", parse_mode="HTML")
        await callback.answer()
        return
    label = ROULETTE_BET_TYPES.get(bet_type, (bet_type, 0))[0]
    rd = active_roulette_rounds.get(chat_id)
    secs_left = max(0, int((rd['end_time'] - datetime.now()).total_seconds())) if rd else 0
    await state.update_data(mroul_chat_id=chat_id, mroul_type=bet_type, mroul_value=None, mroul_uid=uid)
    await state.set_state(GameStates.waiting_for_multi_roulette_amount)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="mroul_cancel_bet"))
    await callback.message.answer(
        f"💰 <b>Ставка на {label}</b>\n\n⏳ До броска: {secs_left} сек.\n\nВведите сумму (мин. 10 DOOM):",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "mroul_cancel_bet")
async def mroul_cancel_bet_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("❌ Ставка отменена.", show_alert=False)
    try:
        await callback.message.delete()
    except Exception:
        pass

@dp.message(GameStates.waiting_for_multi_roulette_number)
async def mroul_receive_number(message: Message, state: FSMContext):
    if not message.text.isdigit() or not (0 <= int(message.text) <= 36):
        await message.reply("❌ Введите число от 0 до 36!")
        return
    number = int(message.text)
    data = await state.get_data()
    chat_id = data.get("mroul_chat_id", message.chat.id)
    rd = active_roulette_rounds.get(chat_id)
    secs_left = max(0, int((rd['end_time'] - datetime.now()).total_seconds())) if rd else 0
    await state.update_data(mroul_value=number)
    await state.set_state(GameStates.waiting_for_multi_roulette_amount)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="mroul_cancel_bet"))
    await message.reply(
        f"🎯 <b>Число: {number}</b>\n\n⏳ До броска: {secs_left} сек.\n\nВведите сумму (мин. 10 DOOM):",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )

@dp.message(GameStates.waiting_for_multi_roulette_amount)
async def mroul_receive_amount(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) < 10:
        await message.reply("❌ Минимум 10 DOOM!")
        return
    amount = int(message.text)
    data = await state.get_data()
    await state.clear()
    uid = data.get("mroul_uid", message.from_user.id)
    chat_id = data.get("mroul_chat_id", message.chat.id)
    bet_type = data.get("mroul_type")
    bet_value = data.get("mroul_value")
    user = await get_user(uid)
    if not user:
        await message.reply("❌ Зарегистрируйтесь через /start!")
        return
    await _place_bet_logic(
        message=message, uid=uid, chat_id=chat_id,
        bet_type=bet_type, bet_value=bet_value, amount=amount,
        player_name=user['first_name'] or user['username'] or "Игрок"
    )

async def _place_bet_logic(message: Message, uid: int, chat_id: int, bet_type: str, bet_value, amount: int, player_name: str = None):
    rd = active_roulette_rounds.get(chat_id)
    if not rd:
        await message.reply("❌ Раунд уже завершён! Ждите следующего.")
        return
    user = await get_user(uid)
    if not user:
        await message.reply("❌ Зарегистрируйтесь через /start!")
        return
    # Если у игрока уже стоит ставка в этом раунде — сначала возвращаем её,
    # а затем атомарно списываем новую (защита от ухода в минус).
    if uid in rd['bets']:
        old_amount = rd['bets'][uid]['amount']
        await update_balance(uid, old_amount)
        rd['bets'].pop(uid, None)
    if not await try_deduct_balance(uid, amount):
        await message.reply(
            f"❌ Недостаточно DOOM! Ставка: <b>{amount:,}</b>",
            parse_mode="HTML"
        )
        return
    if player_name is None:
        player_name = user['first_name'] or user['username'] or "Игрок"
    fname = safe_name(player_name)
    rd['bets'][uid] = {'type': bet_type, 'value': bet_value, 'amount': amount, 'name': fname}
    label = ROULETTE_BET_TYPES.get(bet_type, (bet_type, 0))[0]
    num_str = f" ({bet_value})" if bet_type == "number" else ""
    secs_left = max(0, int((rd['end_time'] - datetime.now()).total_seconds()))
    await message.reply(
        f"✅ <b>Ставка принята!</b>\n\n🎯 Тип: {label}{num_str}\n💰 Сумма: <b>{amount:,}</b> DOOM\n⏳ До броска: {secs_left} сек.",
        parse_mode="HTML"
    )

# ============================================================
# --- EXCHANGE (БИРЖА) ---
# ============================================================
EXCHANGE_ICONS = {"potatoes": "🥔", "apples": "🍎", "pumpkins": "🎃", "watermelons": "🍉", "dumik": "🌀"}
EXCHANGE_COLS = {"potatoes": "crop_potatoes", "apples": "crop_apples", "pumpkins": "crop_pumpkins",
                 "watermelons": "crop_watermelons", "dumik": "crop_dumik"}

async def build_exchange_menu():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT e.id, u.username, u.first_name, e.item_type, e.amount, e.price_per_unit FROM exchange e JOIN users u ON e.user_id=u.user_id ORDER BY e.created_at DESC LIMIT 15"
        ) as c:
            orders = await c.fetchall()
    text = "📊 <b>Биржа ресурсов</b>\n\n<i>Торгуй ресурсами по своей цене!</i>\n\n"
    if orders:
        for oid, uname, fn, itype, amt, price_per in orders:
            seller = uname or fn
            icon = EXCHANGE_ICONS.get(itype, "📦")
            text += f"<code>#{oid}</code> {icon}×{amt} по <b>{price_per}</b>/шт | {seller}\n"
    else:
        text += "Ордеров нет. Выставь первый!"
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📤 Продать", callback_data="exchange_sell_open"),
        InlineKeyboardButton(text="🛍 Купить (#ID)", callback_data="exchange_buy_open"),
    )
    kb.row(InlineKeyboardButton(text="📋 Мои ордера", callback_data="exchange_my_orders"))
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return text, kb.as_markup()

@dp.callback_query(F.data == "exchange_menu")
async def exchange_menu_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    await state.clear()
    text, kb = await build_exchange_menu()
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "exchange_sell_open")
async def exchange_sell_open_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🥔", callback_data="exch_item_potatoes"),
        InlineKeyboardButton(text="🍎", callback_data="exch_item_apples"),
        InlineKeyboardButton(text="🎃", callback_data="exch_item_pumpkins"),
        InlineKeyboardButton(text="🍉", callback_data="exch_item_watermelons"),
        InlineKeyboardButton(text="🌀", callback_data="exch_item_dumik"),
    )
    kb.row(InlineKeyboardButton(text="« Назад", callback_data="exchange_menu"))
    await callback.message.edit_text("📊 <b>Биржа — Продажа</b>\n\nВыберите ресурс:", reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("exch_item_"))
async def exchange_item_select(callback: CallbackQuery, state: FSMContext):
    item = callback.data.removeprefix("exch_item_")
    await state.update_data(exchange_item=item)
    await state.set_state(GameStates.waiting_for_exchange_amount)
    icon = EXCHANGE_ICONS.get(item, "📦")
    await callback.message.edit_text(
        f"📊 {icon} <b>Сколько выставить?</b>\n\nВведите количество:",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(GameStates.waiting_for_exchange_amount)
async def exchange_amount_handler(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.reply("❌ Введите положительное число!")
        return
    amount = int(message.text)
    data = await state.get_data()
    item = data.get("exchange_item")
    uid = message.from_user.id
    user = await get_user(uid)
    col = EXCHANGE_COLS.get(item)
    if not col or not user or user[col] < amount:
        await message.reply("❌ Недостаточно ресурсов на складе!")
        await state.clear()
        return
    await state.update_data(exchange_amount=amount)
    await state.set_state(GameStates.waiting_for_exchange_price)
    icon = EXCHANGE_ICONS.get(item, "📦")
    await message.reply(f"📊 {icon} ×{amount}\n\nВведите цену за <b>1 штуку</b> (DOOM):", parse_mode="HTML")

@dp.message(GameStates.waiting_for_exchange_price)
async def exchange_price_handler(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.reply("❌ Введите положительное число!")
        return
    price_per = int(message.text)
    data = await state.get_data()
    await state.clear()
    uid = message.from_user.id
    item = data.get("exchange_item")
    amount = data.get("exchange_amount")
    col = EXCHANGE_COLS.get(item)
    # Атомарно списываем ресурс со склада — если кто-то параллельно успел
    # его потратить (продажа/саботаж), ордер не будет выставлен "из воздуха".
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            f"UPDATE users SET {col}={col}-? WHERE user_id=? AND {col}>=?",
            (amount, uid, amount)
        )
        if cursor.rowcount == 0:
            await db.commit()
            await message.reply("❌ Недостаточно ресурсов!")
            return
        await db.execute(
            "INSERT INTO exchange (user_id, order_type, item_type, amount, price_per_unit, created_at) VALUES (?,?,?,?,?,?)",
            (uid, "sell", item, amount, price_per, datetime.now().isoformat())
        )
        await db.commit()
    total = price_per * amount
    icon = EXCHANGE_ICONS.get(item, "📦")
    await message.reply(
        f"✅ <b>Ордер выставлен!</b>\n\n{icon} ×{amount} по {price_per}/шт\nИтого: <b>{total:,}</b> DOOM",
        reply_markup=get_back_keyboard(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "exchange_buy_open")
async def exchange_buy_open_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GameStates.waiting_for_exchange_buy_id)
    await callback.message.answer("🛍 Введите ID ордера: <code>#5</code>", parse_mode="HTML")
    await callback.answer()

@dp.message(GameStates.waiting_for_exchange_buy_id)
async def exchange_buy_id_handler(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.startswith("#") or not text[1:].isdigit():
        await message.reply("❌ Формат: <code>#5</code>", parse_mode="HTML")
        return
    await state.clear()
    order_id = int(text[1:])
    uid = message.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM exchange WHERE id=?", (order_id,)) as c:
            order = await c.fetchone()
    if not order:
        await message.reply("❌ Ордер не найден!")
        return
    if order['user_id'] == uid:
        await message.reply("❌ Это ваш ордер!")
        return
    total_price = order['amount'] * order['price_per_unit']
    if not await try_deduct_balance(uid, total_price):
        await message.reply(f"❌ Нужно <b>{total_price:,}</b> DOOM!", parse_mode="HTML")
        return
    col = EXCHANGE_COLS.get(order['item_type'])
    if not col:
        await update_balance(uid, total_price)
        return
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("DELETE FROM exchange WHERE id=?", (order_id,))
        if cursor.rowcount == 0:
            # Кто-то успел выкупить этот ордер раньше — возвращаем деньги.
            await db.commit()
            await update_balance(uid, total_price)
            await message.reply("❌ Ордер уже выкуплен кем-то другим! Деньги возвращены.")
            return
        await db.execute("UPDATE users SET doom_balance=doom_balance+? WHERE user_id=?", (total_price, order['user_id']))
        await db.execute(f"UPDATE users SET {col}={col}+? WHERE user_id=?", (order['amount'], uid))
        await db.execute("UPDATE users SET exchange_trades_count=exchange_trades_count+1 WHERE user_id=?", (uid,))
        await db.execute("UPDATE users SET exchange_trades_count=exchange_trades_count+1 WHERE user_id=?", (order['user_id'],))
        await db.commit()
    icon = EXCHANGE_ICONS.get(order['item_type'], "📦")
    await message.reply(
        f"✅ {icon} ×{order['amount']} куплено за <b>{total_price:,}</b> DOOM!",
        reply_markup=get_back_keyboard(), parse_mode="HTML"
    )
    await check_and_grant_achievements(uid)
    await check_and_grant_achievements(order['user_id'])
    try:
        buyer = message.from_user.username or message.from_user.first_name
        await bot.send_message(order['user_id'],
            f"📊 Ваш ордер #{order_id} выкуплен @{buyer} за <b>{total_price:,}</b> DOOM!", parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data == "exchange_my_orders")
async def exchange_my_orders_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    uid = callback.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, item_type, amount, price_per_unit FROM exchange WHERE user_id=? ORDER BY created_at DESC",
            (uid,)
        ) as c:
            orders = await c.fetchall()
    if not orders:
        await callback.answer("У вас нет активных ордеров.", show_alert=True)
        return
    text = "📋 <b>Мои ордера на бирже</b>\n\n"
    for oid, itype, amt, price_per in orders:
        icon = EXCHANGE_ICONS.get(itype, "📦")
        text += f"<code>#{oid}</code> {icon}×{amt} по {price_per}/шт | итого {amt*price_per:,}\n"
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🗑 Снять все ордера", callback_data="exchange_cancel_all"))
    kb.row(InlineKeyboardButton(text="« Назад", callback_data="exchange_menu"))
    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass
    await callback.answer()

@dp.callback_query(F.data == "exchange_cancel_all")
async def exchange_cancel_all_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT item_type, amount FROM exchange WHERE user_id=?", (uid,)) as c:
            orders = await c.fetchall()
        for itype, amt in orders:
            col = EXCHANGE_COLS.get(itype)
            if col:
                await db.execute(f"UPDATE users SET {col}={col}+? WHERE user_id=?", (amt, uid))
        await db.execute("DELETE FROM exchange WHERE user_id=?", (uid,))
        await db.commit()
    await callback.answer(f"✅ Снято {len(orders)} ордеров, ресурсы возвращены.", show_alert=True)
    text, kb = await build_exchange_menu()
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

# ============================================================
# --- CREDIT (КРЕДИТ) ---
# ============================================================
MAX_CREDIT = 10_000
CREDIT_PERCENT = 0.20
CREDIT_HOURS = 24

async def build_credit_menu(uid: int, fname: str):
    user = await get_user(uid)
    has_credit = user['credit_amount'] and user['credit_amount'] > 0
    if has_credit:
        due = datetime.fromisoformat(user['credit_due']) if user['credit_due'] else None
        now = datetime.now()
        overdue = due and now > due
        due_str = due.strftime("%d.%m %H:%M") if due else "?"
        status = "⚠️ <b>ПРОСРОЧЕН!</b>" if overdue else f"До {due_str}"
        repay_total = int(user['credit_amount'] * (1 + CREDIT_PERCENT))
        text = (
            f"🏦 <b>Кредит</b> — {fname}\n\n"
            f"💳 Долг: <b>{user['credit_amount']:,}</b> DOOM\n"
            f"💰 К погашению (с %): <b>{repay_total:,}</b> DOOM\n"
            f"📅 Срок: {status}\n\n"
            f"Баланс: <b>{user['doom_balance']:,}</b> DOOM"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text=f"✅ Погасить ({repay_total:,} DOOM)", callback_data="credit_repay"))
        kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    else:
        text = (
            f"🏦 <b>Кредит</b> — {fname}\n\n"
            f"Возьмите до <b>{MAX_CREDIT:,}</b> DOOM под {int(CREDIT_PERCENT*100)}% на {CREDIT_HOURS}ч.\n\n"
            f"⚠️ При просрочке — штраф 50% от суммы долга!\n\n"
            f"Баланс: <b>{user['doom_balance']:,}</b> DOOM"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="💳 Взять кредит", callback_data="credit_take"))
        kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    return text, kb.as_markup()

@dp.callback_query(F.data == "credit_menu")
async def credit_menu_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    await state.clear()
    fname = safe_name(callback.from_user.first_name)
    text, kb = await build_credit_menu(callback.from_user.id, fname)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "credit_take")
async def credit_take_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    uid = callback.from_user.id
    user = await get_user(uid)
    if user['credit_amount'] and user['credit_amount'] > 0:
        await callback.answer("❌ У вас уже есть кредит!", show_alert=True)
        return
    await state.set_state(GameStates.waiting_for_credit_amount)
    await callback.message.edit_text(
        f"🏦 <b>Кредит</b>\n\nВведите сумму (100 – {MAX_CREDIT:,} DOOM):\n\n"
        f"Ставка: {int(CREDIT_PERCENT*100)}% на {CREDIT_HOURS}ч",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(GameStates.waiting_for_credit_amount)
async def credit_amount_handler(message: Message, state: FSMContext):
    if not message.text.isdigit() or not (100 <= int(message.text) <= MAX_CREDIT):
        await message.reply(f"❌ Введите от 100 до {MAX_CREDIT:,}!")
        return
    amount = int(message.text)
    await state.clear()
    uid = message.from_user.id
    user = await get_user(uid)
    if not user:
        return
    if user['credit_amount'] and user['credit_amount'] > 0:
        await message.reply("❌ У вас уже есть кредит!")
        return
    due = (datetime.now() + timedelta(hours=CREDIT_HOURS)).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET doom_balance=doom_balance+?, credit_amount=?, credit_due=?, credit_taken_at=? WHERE user_id=?",
            (amount, amount, due, datetime.now().isoformat(), uid)
        )
        await db.commit()
    repay = int(amount * (1 + CREDIT_PERCENT))
    await message.reply(
        f"✅ <b>Кредит выдан!</b>\n\n"
        f"💳 Получено: <b>+{amount:,}</b> DOOM\n"
        f"💰 К возврату: <b>{repay:,}</b> DOOM\n"
        f"⏰ Срок: {CREDIT_HOURS} часов",
        reply_markup=get_back_keyboard(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "credit_repay")
async def credit_repay_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user or not user['credit_amount'] or user['credit_amount'] <= 0:
        await callback.answer("❌ У вас нет кредита!", show_alert=True)
        return
    repay_total = int(user['credit_amount'] * (1 + CREDIT_PERCENT))
    if not await try_deduct_balance(uid, repay_total):
        await callback.answer(f"❌ Нужно {repay_total:,} DOOM!", show_alert=True)
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET credit_amount=0, credit_due=NULL, credit_taken_at=NULL WHERE user_id=?",
            (uid,)
        )
        await db.commit()
    # Достижение
    user2 = await get_user(uid)
    try:
        achs = json.loads(user2['achievements'] or '[]')
    except Exception:
        achs = []
    if "credit_repaid" not in achs:
        achs.append("credit_repaid")
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET achievements=? WHERE user_id=?",
                             (json.dumps(achs, ensure_ascii=False), uid))
            await db.commit()
        try:
            name, desc = ACHIEVEMENTS_LIST["credit_repaid"]
            await bot.send_message(uid, f"🏆 <b>Новое достижение!</b>\n{name}\n<i>{desc}</i>", parse_mode="HTML")
        except Exception:
            pass
    await callback.answer(f"✅ Кредит погашен! −{repay_total:,} DOOM", show_alert=True)
    fname = safe_name(callback.from_user.first_name)
    text, kb = await build_credit_menu(uid, fname)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

async def credit_penalty_task():
    """Штраф за просрочку кредита раз в час."""
    while True:
        await asyncio.sleep(3600)
        now = datetime.now()
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT user_id, credit_amount, credit_due FROM users WHERE credit_amount > 0 AND credit_due IS NOT NULL AND user_id != 0"
            ) as c:
                rows = await c.fetchall()
        for uid2, credit_amount, credit_due in rows:
            if not credit_due:
                continue
            due_dt = datetime.fromisoformat(credit_due)
            if now > due_dt:
                penalty = int(credit_amount * 0.5)
                new_amount = credit_amount + penalty
                new_due = (now + timedelta(hours=CREDIT_HOURS)).isoformat()
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute(
                        "UPDATE users SET credit_amount=?, credit_due=?, doom_balance=MAX(0, doom_balance-?) WHERE user_id=?",
                        (new_amount, new_due, penalty, uid2)
                    )
                    await db.commit()
                try:
                    await bot.send_message(
                        uid2,
                        f"⚠️ <b>Кредит просрочен!</b>\n\nШтраф: <b>{penalty:,}</b> DOOM\nДолг вырос до <b>{new_amount:,}</b> DOOM\n\nПогасите как можно скорее!",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

# ============================================================
# --- BANK ---
# ============================================================
@dp.callback_query(F.data == "bank_deposit")
async def bank_deposit_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    await state.update_data(bank_action="deposit")
    await state.set_state(GameStates.waiting_for_bank_amount)
    await callback.message.edit_text("🏛 <b>Внесение</b>\n\nСколько DOOM внести в банк?", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "bank_withdraw")
async def bank_withdraw_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    await state.update_data(bank_action="withdraw")
    await state.set_state(GameStates.waiting_for_bank_amount)
    await callback.message.edit_text("🏛 <b>Снятие</b>\n\nСколько DOOM снять?", parse_mode="HTML")
    await callback.answer()

@dp.message(GameStates.waiting_for_bank_amount)
async def bank_amount_handler(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.reply("❌ Введите положительное число!")
        return
    amount = int(message.text)
    data = await state.get_data()
    action = data.get("bank_action")
    await state.clear()
    uid = message.from_user.id

    if action == "deposit":
        if not await try_deduct_balance(uid, amount):
            await message.reply("❌ Недостаточно DOOM!")
            return
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "UPDATE users SET bank_balance=bank_balance+?, last_bank_time=? WHERE user_id=?",
                (amount, datetime.now().isoformat(), uid)
            )
            await db.commit()
        await message.reply(f"✅ Внесено <b>{amount:,}</b> DOOM в банк!", reply_markup=get_back_keyboard(), parse_mode="HTML")
    elif action == "withdraw":
        if not await try_deduct_bank_balance(uid, amount):
            await message.reply("❌ Недостаточно DOOM в банке!")
            return
        await update_balance(uid, amount)
        await message.reply(f"✅ Снято <b>{amount:,}</b> DOOM!", reply_markup=get_back_keyboard(), parse_mode="HTML")
    elif action == "clan_donate":
        user2 = await get_user(uid)
        if not user2 or not user2['clan_id']:
            await message.reply("❌ Вы не в клане!")
            return
        if not await try_deduct_balance(uid, amount):
            await message.reply("❌ Недостаточно DOOM!")
            return
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE clans SET bank=bank+? WHERE id=?", (amount, user2['clan_id']))
            await db.commit()
        await message.reply(f"✅ Внесено <b>{amount:,}</b> DOOM в казну клана!", reply_markup=get_back_keyboard(), parse_mode="HTML")

async def bank_interest_task():
    while True:
        await asyncio.sleep(86400)
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT user_id, bank_balance FROM users WHERE bank_balance > 0 AND user_id != 0") as c:
                rows = await c.fetchall()
        for uid2, bank_bal in rows:
            interest = int(bank_bal * 0.05)
            if interest > 0:
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("UPDATE users SET bank_balance=bank_balance+? WHERE user_id=?", (interest, uid2))
                    await db.commit()
                try:
                    await bot.send_message(uid2, f"🏛 Банк начислил 5%: <b>+{interest} DOOM</b>", parse_mode="HTML")
                except Exception:
                    pass

# ============================================================
# --- TRANSFER ---
# ============================================================
@dp.callback_query(F.data == "transfer_open")
async def transfer_open_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    await state.set_state(GameStates.waiting_for_transfer_user)
    await callback.message.edit_text("💸 <b>Перевод</b>\n\nВведите @username или ID:", parse_mode="HTML")
    await callback.answer()

@dp.message(GameStates.waiting_for_transfer_user)
async def transfer_user_handler(message: Message, state: FSMContext):
    arg = message.text.strip()
    target = await get_user_by_username(arg) if arg.startswith("@") else (await get_user(int(arg)) if arg.isdigit() else None)
    if not target or target['user_id'] == 0 or target['user_id'] == message.from_user.id:
        await message.reply("❌ Игрок не найден или это вы сами!")
        return
    name = target['username'] or target['first_name']
    await state.update_data(transfer_target=target['user_id'], transfer_name=name)
    await state.set_state(GameStates.waiting_for_transfer_amount)
    await message.reply(f"✅ Получатель: <b>{name}</b>\n\nСколько DOOM перевести?", parse_mode="HTML")

@dp.message(GameStates.waiting_for_transfer_amount)
async def transfer_amount_handler(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.reply("❌ Введите положительное число!")
        return
    amount = int(message.text)
    data = await state.get_data()
    await state.clear()
    uid = message.from_user.id
    if not await try_deduct_balance(uid, amount):
        await message.reply("❌ Недостаточно DOOM!")
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET doom_balance=doom_balance+? WHERE user_id=?", (amount, data['transfer_target']))
        await db.commit()
    await message.reply(
        f"✅ Переведено <b>{amount:,} DOOM</b> → {data['transfer_name']}",
        reply_markup=get_back_keyboard(), parse_mode="HTML"
    )
    try:
        sender_name = message.from_user.username or message.from_user.first_name
        await bot.send_message(data['transfer_target'], f"💸 <b>@{sender_name} перевёл {amount:,} DOOM!</b>", parse_mode="HTML")
    except Exception:
        pass

# ============================================================
# --- SHOP ---
# ============================================================
SHOP_ITEMS = {
    "fertilizer": ("🧪 Удобрение", 300, "fertilizer_count"),
    "pesticide":  ("🐛 Пестицид",  400, "pesticide_count"),
    "dog":        ("🐕 Пёс-охранник", 800, "dog_item_count"),
    "magnifier":  ("🔍 Лупа", 200, "magnifier_count"),
}

@dp.callback_query(F.data.startswith("shop_buy_"))
async def shop_buy_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    item = callback.data.removeprefix("shop_buy_")
    if item not in SHOP_ITEMS:
        await callback.answer("❌ Неизвестный предмет!", show_alert=True)
        return
    name, price, col = SHOP_ITEMS[item]
    uid = callback.from_user.id
    if not await try_deduct_balance(uid, price):
        await callback.answer(f"❌ Нужно {price} DOOM!", show_alert=True)
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f"UPDATE users SET {col}={col}+1 WHERE user_id=?", (uid,))
        await db.commit()
    await callback.answer(f"✅ {name} куплено!", show_alert=True)
    text, reply_markup = await get_menu_page_data("shop", uid, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data == "shop_use_dog")
async def shop_use_dog_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user or user['dog_item_count'] < 1:
        await callback.answer("❌ Нет пса-охранника!", show_alert=True)
        return
    dog_until = (datetime.now() + timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET dog_until=?, dog_item_count=dog_item_count-1 WHERE user_id=?", (dog_until, uid))
        await db.commit()
    await callback.answer("🐕 Пёс активирован на 24ч!", show_alert=True)

# ============================================================
# --- CASE ---
# ============================================================
@dp.callback_query(F.data.startswith("case_open_"))
async def case_open_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    price = int(callback.data.removeprefix("case_open_"))
    uid = callback.from_user.id
    if not await try_deduct_balance(uid, price):
        await callback.answer(f"❌ Нужно {price:,} DOOM!", show_alert=True)
        return
    prize = roll_case_prize()
    _, ptype, pval, pdesc = prize
    if ptype == "doom":
        await update_balance(uid, pval)
        result = f"💰 +{pval:,} DOOM"
    else:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(f"UPDATE users SET {pval}={pval}+1 WHERE user_id=?", (uid,))
            await db.commit()
        result = pdesc
    fname = safe_name(callback.from_user.first_name)
    text = (
        f"🎁 <b>Кейс открыт!</b> — {fname}\n\n"
        f"Потрачено: <b>{price:,}</b> DOOM\n"
        f"Выпало: <b>{result}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🎁 Ещё кейс", callback_data="main_case"))
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="main_root"))
    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()

# ============================================================
# --- DONATE ---
# ============================================================
@dp.callback_query(F.data == "donate_menu")
async def donate_menu_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    await state.clear()
    user = await get_user(callback.from_user.id)
    fname = safe_name(callback.from_user.first_name)
    text = (
        f"💳 <b>Донат</b> — {fname}\n\n"
        f"💰 Баланс: <b>{user['doom_balance']:,}</b> DOOM\n\n"
        f"⭐ 1 звезда = 500 DOOM\n"
        f"⭐ 5 звёзд = 2 750 DOOM <i>(+10%)</i>\n"
        f"⭐ 10 звёзд = 6 000 DOOM <i>(+20%)</i>\n"
        f"✏️ Своя сумма звёзд (мин. 1)"
    )
    await callback.message.edit_text(text, reply_markup=get_donate_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("donate_buy_"))
async def donate_buy_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    stars_map = {"1": (1, 500), "5": (5, 2750), "10": (10, 6000)}
    key = callback.data.removeprefix("donate_buy_")
    if key not in stars_map:
        await callback.answer("❌ Ошибка", show_alert=True)
        return
    stars, doom = stars_map[key]
    uid = callback.from_user.id
    await callback.message.answer_invoice(
        title=f"Донат: {stars} ⭐ → {doom:,} DOOM",
        description=f"Получите {doom:,} DOOM на игровой счёт",
        prices=[LabeledPrice(label=f"{doom} DOOM", amount=stars)],
        provider_token="", currency="XTR",
        payload=f"donate_{uid}_{doom}"
    )
    await callback.answer()

@dp.callback_query(F.data == "donate_custom")
async def donate_custom_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    await state.set_state(GameStates.waiting_for_donate_amount)
    await callback.message.edit_text(
        "✏️ <b>Своя сумма</b>\n\nВведите количество звёзд (мин. 1):\n1 ⭐ = 500 DOOM",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(GameStates.waiting_for_donate_amount)
async def donate_custom_amount_handler(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) < 1:
        await message.reply("❌ Введите целое число от 1!")
        return
    stars = int(message.text)
    doom = stars * 500
    uid = message.from_user.id
    await state.clear()
    await message.answer_invoice(
        title=f"Донат: {stars} ⭐ → {doom:,} DOOM",
        description=f"Получите {doom:,} DOOM на игровой счёт",
        prices=[LabeledPrice(label=f"{doom} DOOM", amount=stars)],
        provider_token="", currency="XTR",
        payload=f"donate_{uid}_{doom}"
    )

@dp.message(F.successful_payment, F.successful_payment.invoice_payload.startswith("donate_"))
async def donate_payment_success(message: Message):
    try:
        parts = message.successful_payment.invoice_payload.split("_")
        uid = int(parts[1])
        doom = int(parts[2])
    except Exception:
        return
    await update_balance(uid, doom)
    stars = message.successful_payment.total_amount
    await increment_sys_stat("total_stars_deposited", stars)
    await check_and_grant_achievements(uid)
    await message.answer(
        f"⭐ <b>Донат получен!</b>\n\n+<b>{doom:,} DOOM</b>\nСпасибо за поддержку! 🙏",
        parse_mode="HTML", reply_markup=get_back_keyboard()
    )

# ============================================================
# --- CLAN ---
# ============================================================
@dp.callback_query(F.data == "clan_create")
async def clan_create_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user:
        return
    if user['clan_id']:
        await callback.answer("❌ Вы уже в клане!", show_alert=True)
        return
    if user['doom_balance'] < 5000:
        await callback.answer("❌ Нужно 5000 DOOM!", show_alert=True)
        return
    await state.set_state(GameStates.waiting_for_clan_name)
    await callback.message.edit_text("🏰 <b>Создание клана</b>\n\nВведите название (3–20 символов):", parse_mode="HTML")
    await callback.answer()

@dp.message(GameStates.waiting_for_clan_name)
async def clan_name_handler(message: Message, state: FSMContext):
    name = message.text.strip()
    if not (3 <= len(name) <= 20):
        await message.reply("❌ Название: 3–20 символов!")
        return
    uid = message.from_user.id
    await state.clear()
    if not await try_deduct_balance(uid, 5000):
        await message.reply("❌ Недостаточно DOOM!")
        return
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT INTO clans (name, owner_id, created_at) VALUES (?,?,?)", (name, uid, datetime.now().isoformat()))
            async with db.execute("SELECT last_insert_rowid()") as c:
                clan_id = (await c.fetchone())[0]
            await db.execute("UPDATE users SET clan_id=? WHERE user_id=?", (clan_id, uid))
            await db.commit()
        await message.reply(f"🏰 Клан <b>{name}</b> создан! −5000 DOOM", reply_markup=get_back_keyboard(), parse_mode="HTML")
        await check_and_grant_achievements(uid)
    except aiosqlite.IntegrityError:
        await update_balance(uid, 5000)
        await message.reply("❌ Такое название уже занято! Деньги возвращены.")

@dp.callback_query(F.data == "clan_leave")
async def clan_leave_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET clan_id=NULL WHERE user_id=?", (uid,))
        await db.commit()
    await callback.answer("✅ Вы покинули клан.", show_alert=True)
    text, kb = await get_menu_page_data("clan", uid, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data == "clan_disband")
async def clan_disband_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user or not user['clan_id']:
        await callback.answer("❌ Нет клана!", show_alert=True)
        return
    clan = await get_clan_by_id(user['clan_id'])
    if not clan or clan['owner_id'] != uid:
        await callback.answer("❌ Вы не глава клана!", show_alert=True)
        return
    if await get_active_war_for_clan(clan['id']):
        await callback.answer("❌ Нельзя распустить клан во время войны!", show_alert=True)
        return
    leftover = clan['bank']
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET clan_id=NULL WHERE clan_id=?", (clan['id'],))
        await db.execute("DELETE FROM clans WHERE id=?", (clan['id'],))
        if leftover > 0:
            await db.execute("UPDATE users SET doom_balance=doom_balance+? WHERE user_id=?", (leftover, uid))
        await db.commit()
    await callback.answer(f"✅ Клан распущен. Остаток казны ({leftover:,} DOOM) переведён вам.", show_alert=True)
    text, kb = await get_menu_page_data("clan", uid, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data == "clan_donate")
async def clan_donate_callback(callback: CallbackQuery, state: FSMContext):
    await state.update_data(bank_action="clan_donate")
    await state.set_state(GameStates.waiting_for_bank_amount)
    await callback.message.edit_text("🏰 Сколько DOOM внести в казну клана?")
    await callback.answer()

@dp.callback_query(F.data == "clan_top")
async def clan_top_callback(callback: CallbackQuery):
    text = await get_top_text("clans")
    await callback.message.edit_text(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "clan_invite")
async def clan_invite_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GameStates.waiting_for_clan_invite)
    await callback.message.edit_text("📨 Введите @username игрока для приглашения:")
    await callback.answer()

@dp.message(GameStates.waiting_for_clan_invite)
async def clan_invite_handler(message: Message, state: FSMContext):
    arg = message.text.strip()
    target = await get_user_by_username(arg) if arg.startswith("@") else None
    if not target or target['user_id'] == 0:
        await message.reply("❌ Игрок не найден!")
        await state.clear()
        return
    if target['clan_id']:
        await message.reply("❌ Игрок уже в клане!")
        await state.clear()
        return
    uid = message.from_user.id
    user = await get_user(uid)
    if not user or not user['clan_id']:
        await state.clear()
        return
    clan = await get_clan_by_id(user['clan_id'])
    if not clan:
        await state.clear()
        return
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Принять", callback_data=f"clan_accept_{user['clan_id']}_{uid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data="clan_decline"),
    )
    try:
        await bot.send_message(
            target['user_id'],
            f"📨 Вас приглашают в клан <b>{clan['name']}</b>!\nОт: {message.from_user.username or message.from_user.first_name}",
            reply_markup=kb.as_markup(), parse_mode="HTML"
        )
        await message.reply("✅ Приглашение отправлено!")
    except Exception:
        await message.reply("❌ Не удалось отправить. Игрок не запустил бота.")

@dp.callback_query(F.data.startswith("clan_accept_"))
async def clan_accept_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    clan_id, inviter_id = int(parts[2]), int(parts[3])
    uid = callback.from_user.id
    user = await get_user(uid)
    if user and user['clan_id']:
        await callback.answer("❌ Вы уже в клане!", show_alert=True)
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET clan_id=? WHERE user_id=?", (clan_id, uid))
        await db.commit()
    await callback.answer("✅ Вы вступили в клан!", show_alert=True)
    try:
        await callback.message.edit_text("✅ Вы вступили в клан!")
    except Exception:
        pass

@dp.callback_query(F.data == "clan_decline")
async def clan_decline_callback(callback: CallbackQuery):
    await callback.answer("❌ Приглашение отклонено.", show_alert=True)
    try:
        await callback.message.edit_text("❌ Приглашение отклонено.")
    except Exception:
        pass

# ============================================================
# --- CLAN WARS (КЛАНОВЫЕ ВОЙНЫ) ---
# ============================================================
@dp.callback_query(F.data == "clan_war_menu")
async def clan_war_menu_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    await state.clear()
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user or not user['clan_id']:
        await callback.answer("❌ Вы не в клане!", show_alert=True)
        return
    clan = await get_clan_by_id(user['clan_id'])
    if not clan:
        await callback.answer("❌ Клан не найден!", show_alert=True)
        return
    is_owner = clan['owner_id'] == uid
    war = await get_active_war_for_clan(clan['id'])
    if war:
        enemy_id = war['clan_b_id'] if war['clan_a_id'] == clan['id'] else war['clan_a_id']
        enemy = await get_clan_by_id(enemy_id)
        my_score = war['score_a'] if war['clan_a_id'] == clan['id'] else war['score_b']
        enemy_score = war['score_b'] if war['clan_a_id'] == clan['id'] else war['score_a']
        ends = datetime.fromisoformat(war['ends_at'])
        rem = ends - datetime.now()
        h = max(0, int(rem.total_seconds() // 3600))
        m = max(0, int((rem.total_seconds() % 3600) // 60))
        enemy_name = enemy['name'] if enemy else "???"
        text = (
            f"⚔️ <b>Война с кланом «{enemy_name}»!</b>\n\n"
            f"📊 Счёт: <b>{my_score:,}</b> vs <b>{enemy_score:,}</b>\n"
            f"⏳ Осталось: {h}ч {m}мин.\n\n"
            f"Атакуйте участников вражеского клана, чтобы заработать очки для своего клана!"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⚔️ Атаковать врага", callback_data="clan_war_attack_open"))
        kb.row(InlineKeyboardButton(text="« Назад", callback_data="main_clan"))
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        text = (
            f"⚔️ <b>Клановые войны</b>\n\n"
            f"🏅 Очки войны клана: <b>{(clan['war_points'] or 0):,}</b>\n"
            f"🏆 Побед: {clan['wars_won'] or 0} | 💀 Поражений: {clan['wars_lost'] or 0}\n\n"
            f"Стоимость объявления войны: <b>{WAR_DECLARE_COST:,}</b> DOOM из казны.\n"
            f"Длительность: {WAR_DURATION_HOURS}ч. Минимум {WAR_MIN_MEMBERS} участников в каждом клане.\n\n"
            f"Победитель получает {int(WAR_WINNER_TREASURY_PCT*100)}% казны проигравшего, "
            f"+{WAR_WINNER_CLAN_POINTS} очков войны и бонус каждому участнику."
        )
        kb = InlineKeyboardBuilder()
        if is_owner:
            kb.row(InlineKeyboardButton(text="⚔️ Объявить войну", callback_data="clan_war_declare_open"))
        kb.row(InlineKeyboardButton(text="« Назад", callback_data="main_clan"))
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "clan_war_declare_open")
async def clan_war_declare_open_callback(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user or not user['clan_id']:
        await callback.answer("❌ Вы не в клане!", show_alert=True)
        return
    clan = await get_clan_by_id(user['clan_id'])
    if not clan or clan['owner_id'] != uid:
        await callback.answer("❌ Только глава клана может объявить войну!", show_alert=True)
        return
    if await get_active_war_for_clan(clan['id']):
        await callback.answer("❌ Ваш клан уже воюет!", show_alert=True)
        return
    await state.set_state(GameStates.waiting_for_war_target_clan)
    await callback.message.edit_text(
        "⚔️ <b>Объявление войны</b>\n\nВведите точное название вражеского клана:\n/cancel — отмена",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(GameStates.waiting_for_war_target_clan)
async def clan_war_target_handler(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    user = await get_user(uid)
    if not user or not user['clan_id']:
        await message.reply("❌ Вы не в клане!")
        return
    my_clan = await get_clan_by_id(user['clan_id'])
    if not my_clan or my_clan['owner_id'] != uid:
        await message.reply("❌ Только глава клана может объявить войну!")
        return
    if await get_active_war_for_clan(my_clan['id']):
        await message.reply("❌ Ваш клан уже воюет!")
        return
    enemy = await get_clan_by_name(message.text.strip())
    if not enemy:
        await message.reply("❌ Клан с таким названием не найден!")
        return
    if enemy['id'] == my_clan['id']:
        await message.reply("❌ Нельзя объявить войну самому себе!")
        return
    if await get_active_war_for_clan(enemy['id']):
        await message.reply("❌ Этот клан уже воюет с кем-то другим!")
        return
    my_members = await get_clan_members(my_clan['id'])
    enemy_members = await get_clan_members(enemy['id'])
    if len(my_members) < WAR_MIN_MEMBERS or len(enemy_members) < WAR_MIN_MEMBERS:
        await message.reply(f"❌ В каждом клане должно быть минимум {WAR_MIN_MEMBERS} участников!")
        return
    if not await try_deduct_clan_bank(my_clan['id'], WAR_DECLARE_COST):
        await message.reply(f"❌ В казне клана меньше {WAR_DECLARE_COST:,} DOOM!")
        return
    now = datetime.now()
    ends_at = (now + timedelta(hours=WAR_DURATION_HOURS)).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO clan_wars (clan_a_id, clan_b_id, started_at, ends_at, status) VALUES (?,?,?,?, 'active')",
            (my_clan['id'], enemy['id'], now.isoformat(), ends_at)
        )
        await db.commit()
    await message.reply(
        f"⚔️ <b>Война началась!</b>\n\n«{my_clan['name']}» vs «{enemy['name']}»\nДлительность: {WAR_DURATION_HOURS}ч.",
        reply_markup=get_back_keyboard(), parse_mode="HTML"
    )
    notify_text = (
        f"⚔️ <b>Война кланов!</b>\n\n«{my_clan['name']}» объявил войну «{enemy['name']}»!\n"
        f"Атакуйте участников вражеского клана через 🏰 Клан → ⚔️ Война (бесплатно, кд {WAR_ATTACK_COOLDOWN_MIN//60}ч)."
    )
    all_member_ids = [m['user_id'] for m in my_members] + [m['user_id'] for m in enemy_members]
    await notify_users(all_member_ids, notify_text)

@dp.callback_query(F.data == "clan_war_attack_open")
async def clan_war_attack_open_callback(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user or not user['clan_id']:
        await callback.answer("❌ Вы не в клане!", show_alert=True)
        return
    war = await get_active_war_for_clan(user['clan_id'])
    if not war:
        await callback.answer("❌ Война не идёт!", show_alert=True)
        return
    on_cd, m, s = check_cooldown(user['last_war_attack_time'], WAR_ATTACK_COOLDOWN_MIN)
    if on_cd:
        h = m // 60
        mins = m % 60
        await callback.answer(f"⏳ Перезарядка атаки: {h}ч {mins}мин.", show_alert=True)
        return
    await state.set_state(GameStates.waiting_for_war_attack_target)
    await callback.message.answer("⚔️ Введите @username участника вражеского клана для атаки:")
    await callback.answer()

@dp.message(GameStates.waiting_for_war_attack_target)
async def clan_war_attack_target_handler(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    arg = message.text.strip()
    if not arg.startswith("@"):
        await message.reply("❌ Введите @username!")
        return
    attacker = await get_user(uid)
    if not attacker or not attacker['clan_id']:
        await message.reply("❌ Вы не в клане!")
        return
    war = await get_active_war_for_clan(attacker['clan_id'])
    if not war:
        await message.reply("❌ Война уже завершилась!")
        return
    on_cd, m, s = check_cooldown(attacker['last_war_attack_time'], WAR_ATTACK_COOLDOWN_MIN)
    if on_cd:
        h, mins = m // 60, m % 60
        await message.reply(f"⏳ Перезарядка атаки: {h}ч {mins}мин.")
        return
    victim = await get_user_by_username(arg)
    if not victim or victim['user_id'] == 0:
        await message.reply("❌ Игрок не найден!")
        return
    if victim['user_id'] == uid:
        await message.reply("❌ Нельзя атаковать самого себя!")
        return
    enemy_clan_id = war['clan_b_id'] if war['clan_a_id'] == attacker['clan_id'] else war['clan_a_id']
    if victim['clan_id'] != enemy_clan_id:
        await message.reply("❌ Этот игрок не из вражеского клана!")
        return
    now = datetime.now()
    if victim['shield_until'] and datetime.fromisoformat(victim['shield_until']) > now:
        await message.reply("🛡 У цели щит! Атака невозможна.")
        return
    is_clan_a = (war['clan_a_id'] == attacker['clan_id'])
    score_col = "score_a" if is_clan_a else "score_b"
    vic_name = victim['username'] or victim['first_name']

    if random.random() < 0.5 and victim['doom_balance'] > 0:
        percent = random.randint(WAR_STEAL_MIN_PCT, WAR_STEAL_MAX_PCT)
        stolen = max(1, int(victim['doom_balance'] * percent / 100))
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET doom_balance=MAX(0,doom_balance-?) WHERE user_id=?", (stolen, victim['user_id']))
            await db.execute(
                "UPDATE users SET doom_balance=doom_balance+?, last_war_attack_time=? WHERE user_id=?",
                (stolen, now.isoformat(), uid)
            )
            await db.execute(f"UPDATE clan_wars SET {score_col}={score_col}+? WHERE id=?", (stolen, war['id']))
            await db.commit()
        await message.reply(
            f"⚔️ <b>Успешная атака!</b>\n{vic_name} потерял <b>{stolen:,}</b> DOOM!\n"
            f"💰 Очки клана за войну: +{stolen:,}\nКД {WAR_ATTACK_COOLDOWN_MIN//60}ч.",
            parse_mode="HTML"
        )
    else:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET last_war_attack_time=? WHERE user_id=?", (now.isoformat(), uid))
            await db.commit()
        await message.reply(f"🛡 Атака отбита! Цель устояла. КД {WAR_ATTACK_COOLDOWN_MIN//60}ч.")

# --- Снятие денег из казны клана (только глава) ---
@dp.callback_query(F.data == "clan_withdraw_open")
async def clan_withdraw_open_callback(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user or not user['clan_id']:
        await callback.answer("❌ Вы не в клане!", show_alert=True)
        return
    clan = await get_clan_by_id(user['clan_id'])
    if not clan or clan['owner_id'] != uid:
        await callback.answer("❌ Только глава клана может выводить деньги!", show_alert=True)
        return
    on_cd, m, s = check_cooldown(clan['last_withdraw_time'], CLAN_WITHDRAW_COOLDOWN_MIN)
    if on_cd:
        h, mins = m // 60, m % 60
        await callback.answer(f"⏳ Вывод доступен через {h}ч {mins}мин.", show_alert=True)
        return
    await state.set_state(GameStates.waiting_for_clan_withdraw_amount)
    await callback.message.answer(
        f"📤 <b>Вывод из казны</b>\n\nВ казне: <b>{clan['bank']:,}</b> DOOM\nВведите сумму:",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(GameStates.waiting_for_clan_withdraw_amount)
async def clan_withdraw_amount_handler(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.reply("❌ Введите положительное число!")
        return
    amount = int(message.text)
    await state.clear()
    uid = message.from_user.id
    user = await get_user(uid)
    if not user or not user['clan_id']:
        await message.reply("❌ Вы не в клане!")
        return
    clan = await get_clan_by_id(user['clan_id'])
    if not clan or clan['owner_id'] != uid:
        await message.reply("❌ Только глава клана может выводить деньги!")
        return
    on_cd, m, s = check_cooldown(clan['last_withdraw_time'], CLAN_WITHDRAW_COOLDOWN_MIN)
    if on_cd:
        await message.reply("❌ Вывод пока недоступен (кулдаун)!")
        return
    if not await try_deduct_clan_bank(clan['id'], amount):
        await message.reply("❌ Недостаточно DOOM в казне!")
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE clans SET last_withdraw_time=? WHERE id=?", (datetime.now().isoformat(), clan['id']))
        await db.execute("UPDATE users SET doom_balance=doom_balance+? WHERE user_id=?", (amount, uid))
        await db.commit()
    await message.reply(
        f"✅ Выведено <b>{amount:,}</b> DOOM из казны клана на ваш баланс!",
        reply_markup=get_back_keyboard(), parse_mode="HTML"
    )

async def resolve_clan_war(war):
    clan_a = await get_clan_by_id(war['clan_a_id'])
    clan_b = await get_clan_by_id(war['clan_b_id'])
    if not clan_a or not clan_b:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE clan_wars SET status='finished' WHERE id=?", (war['id'],))
            await db.commit()
        return

    score_a, score_b = war['score_a'], war['score_b']
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE clan_wars SET status='finished' WHERE id=?", (war['id'],))
        await db.commit()

    if score_a == score_b:
        notify_text = (
            f"⚔️ <b>Война окончена — ничья!</b>\n\n«{clan_a['name']}» {score_a:,} : {score_b:,} «{clan_b['name']}»\n"
            f"Никто не получил очков войны."
        )
        for clan in (clan_a, clan_b):
            members = await get_clan_members(clan['id'])
            await notify_users([m['user_id'] for m in members], notify_text)
        return

    if score_a > score_b:
        winner, loser, winner_score, loser_score = clan_a, clan_b, score_a, score_b
    else:
        winner, loser, winner_score, loser_score = clan_b, clan_a, score_b, score_a

    transferred = int((loser['bank'] or 0) * WAR_WINNER_TREASURY_PCT)
    async with aiosqlite.connect(DB_NAME) as db:
        if transferred > 0:
            await db.execute("UPDATE clans SET bank=MAX(0, bank-?) WHERE id=?", (transferred, loser['id']))
            await db.execute("UPDATE clans SET bank=bank+? WHERE id=?", (transferred, winner['id']))
        await db.execute(
            "UPDATE clans SET war_points=war_points+?, wars_won=wars_won+1 WHERE id=?",
            (WAR_WINNER_CLAN_POINTS, winner['id'])
        )
        await db.execute("UPDATE clans SET wars_lost=wars_lost+1 WHERE id=?", (loser['id'],))
        await db.commit()

    winner_members = await get_clan_members(winner['id'])
    loser_members = await get_clan_members(loser['id'])

    for m in winner_members:
        await update_balance(m['user_id'], WAR_WINNER_MEMBER_REWARD)
        await grant_achievement_directly(m['user_id'], "war_winner")

    winner_text = (
        f"🏆 <b>Победа в войне!</b>\n\n«{winner['name']}» {winner_score:,} : {loser_score:,} «{loser['name']}»\n\n"
        f"💰 В казну: +{transferred:,} DOOM\n🎁 Каждому участнику: +{WAR_WINNER_MEMBER_REWARD} DOOM\n"
        f"🏅 Очки войны клана: +{WAR_WINNER_CLAN_POINTS}"
    )
    loser_text = (
        f"💀 <b>Война проиграна.</b>\n\n«{loser['name']}» {loser_score:,} : {winner_score:,} «{winner['name']}»\n\n"
        f"💸 Из казны утрачено: {transferred:,} DOOM"
    )
    await notify_users([m['user_id'] for m in winner_members], winner_text)
    await notify_users([m['user_id'] for m in loser_members], loser_text)

async def clan_war_check_task():
    """Раз в WAR_CHECK_INTERVAL_SEC проверяем, не закончились ли активные войны."""
    while True:
        await asyncio.sleep(WAR_CHECK_INTERVAL_SEC)
        now = datetime.now()
        async with aiosqlite.connect(DB_NAME) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM clan_wars WHERE status='active'") as c:
                wars = await c.fetchall()
        for war in wars:
            if datetime.fromisoformat(war['ends_at']) <= now:
                try:
                    await resolve_clan_war(war)
                except Exception:
                    logging.exception("Ошибка при завершении клановой войны #%s", war['id'])

# ============================================================
# --- MARKET ---
# ============================================================
@dp.callback_query(F.data == "market_sell_open")
async def market_sell_open_callback(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription(callback):
        return
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🥔", callback_data="mkt_item_potatoes"),
        InlineKeyboardButton(text="🍎", callback_data="mkt_item_apples"),
        InlineKeyboardButton(text="🎃", callback_data="mkt_item_pumpkins"),
        InlineKeyboardButton(text="🍉", callback_data="mkt_item_watermelons"),
        InlineKeyboardButton(text="🌀", callback_data="mkt_item_dumik"),
    )
    kb.row(InlineKeyboardButton(text="« Назад", callback_data="main_market"))
    await callback.message.edit_text("🏪 Что выставить на продажу?", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("mkt_item_"))
async def market_item_select_callback(callback: CallbackQuery, state: FSMContext):
    item = callback.data.removeprefix("mkt_item_")
    await state.update_data(market_item=item)
    await state.set_state(GameStates.waiting_for_trade_amount)
    icons = {"potatoes": "🥔", "apples": "🍎", "pumpkins": "🎃", "watermelons": "🍉", "dumik": "🌀"}
    await callback.message.edit_text(f"📦 Сколько {icons.get(item,'?')} выставить?")
    await callback.answer()

@dp.message(GameStates.waiting_for_trade_amount)
async def market_amount_handler(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.reply("❌ Введите положительное число!")
        return
    amount = int(message.text)
    await state.update_data(market_amount=amount)
    await state.set_state(GameStates.waiting_for_trade_price)
    await message.reply(f"💰 Цена за всю партию ({amount} шт.) в DOOM:")

@dp.message(GameStates.waiting_for_trade_price)
async def market_price_handler(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.reply("❌ Введите положительное число!")
        return
    price = int(message.text)
    data = await state.get_data()
    await state.clear()
    uid = message.from_user.id
    item = data.get("market_item")
    amount = data.get("market_amount")
    col_map = {"potatoes": "crop_potatoes", "apples": "crop_apples", "pumpkins": "crop_pumpkins",
               "watermelons": "crop_watermelons", "dumik": "crop_dumik"}
    col = col_map.get(item)
    if not col:
        await message.reply("❌ Ошибка!")
        return
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            f"UPDATE users SET {col}={col}-? WHERE user_id=? AND {col}>=?",
            (amount, uid, amount)
        )
        if cursor.rowcount == 0:
            await db.commit()
            await message.reply("❌ Недостаточно товара на складе!")
            return
        await db.execute(
            "INSERT INTO market (seller_id, item_type, amount, price, created_at) VALUES (?,?,?,?,?)",
            (uid, item, amount, price, datetime.now().isoformat())
        )
        await db.commit()
    icons = {"potatoes": "🥔", "apples": "🍎", "pumpkins": "🎃", "watermelons": "🍉", "dumik": "🌀"}
    await message.reply(
        f"✅ Лот выставлен!\n{icons.get(item,'📦')} ×{amount} за <b>{price:,}</b> DOOM",
        reply_markup=get_back_keyboard(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "market_buy_open")
async def market_buy_open_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🛍 Отправьте ID лота: <code>#5</code>", parse_mode="HTML")
    await callback.answer()

@dp.message(F.text.regexp(r"^#\d+$"))
async def market_buy_by_id(message: Message, state: FSMContext):
    # Проверяем, не в состоянии ли пользователь ожидания биржи
    current_state = await state.get_state()
    if current_state == GameStates.waiting_for_exchange_buy_id:
        await exchange_buy_id_handler(message, state)
        return
    lot_id = int(message.text[1:])
    uid = message.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM market WHERE id=?", (lot_id,)) as c:
            lot = await c.fetchone()
    if not lot:
        await message.reply("❌ Лот не найден!")
        return
    if lot['seller_id'] == uid:
        await message.reply("❌ Это ваш лот!")
        return
    if not await try_deduct_balance(uid, lot['price']):
        await message.reply(f"❌ Нужно {lot['price']:,} DOOM!")
        return
    col_map = {"potatoes": "crop_potatoes", "apples": "crop_apples", "pumpkins": "crop_pumpkins",
               "watermelons": "crop_watermelons", "dumik": "crop_dumik"}
    col = col_map.get(lot['item_type'])
    if not col:
        await update_balance(uid, lot['price'])
        return
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("DELETE FROM market WHERE id=?", (lot_id,))
        if cursor.rowcount == 0:
            # Лот уже купили — возвращаем деньги.
            await db.commit()
            await update_balance(uid, lot['price'])
            await message.reply("❌ Лот уже куплен кем-то другим! Деньги возвращены.")
            return
        await db.execute("UPDATE users SET doom_balance=doom_balance+? WHERE user_id=?", (lot['price'], lot['seller_id']))
        await db.execute(f"UPDATE users SET {col}={col}+? WHERE user_id=?", (lot['amount'], uid))
        await db.commit()
    icons = {"potatoes": "🥔", "apples": "🍎", "pumpkins": "🎃", "watermelons": "🍉", "dumik": "🌀"}
    await message.reply(
        f"✅ {icons.get(lot['item_type'],'📦')} ×{lot['amount']} куплено за <b>{lot['price']:,}</b> DOOM!",
        reply_markup=get_back_keyboard(), parse_mode="HTML"
    )
    try:
        buyer_name = message.from_user.username or message.from_user.first_name
        await bot.send_message(lot['seller_id'], f"💰 Лот #{lot_id} куплен @{buyer_name} за <b>{lot['price']:,}</b> DOOM!", parse_mode="HTML")
    except Exception:
        pass

# ============================================================
# --- ROB TOP ---
# ============================================================
@dp.callback_query(F.data == "rob_top")
async def rob_top_callback(callback: CallbackQuery):
    text = await get_top_text("robs")
    await callback.message.edit_text(text, reply_markup=get_top_keyboard("robs"), parse_mode="HTML")
    await callback.answer()

# ============================================================
# --- UPGRADE & SELL ---
# ============================================================
@dp.callback_query(F.data == "action_buy_upgrade")
async def action_buy_upgrade_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user or user['farm_level'] >= 15:
        return
    cost = 600 * (2 ** (user['farm_level'] - 1))
    if not await try_deduct_balance(uid, cost):
        await callback.answer("❌ Не хватает DOOM!", show_alert=True)
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET farm_level=farm_level+1 WHERE user_id=?", (uid,))
        await db.commit()
    await callback.answer("📈 Уровень повышен!")
    await check_and_grant_achievements(uid)
    text, kb = await get_menu_page_data("upgrade", uid, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data == "crop_sell_all")
async def action_sell_crops_callback(callback: CallbackQuery):
    if not await require_subscription(callback):
        return
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user:
        return
    mod = await get_market_modifier()
    total = calc_crop_value(user, mod)
    if total <= 0:
        await callback.answer("📭 Склад пустой!", show_alert=True)
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            UPDATE users SET doom_balance=doom_balance+?,
            crop_potatoes=0,crop_apples=0,crop_pumpkins=0,crop_watermelons=0,crop_dumik=0
            WHERE user_id=?
        """, (total, uid))
        await db.commit()
    await callback.answer(f"💰 +{total:,} DOOM!", show_alert=True)
    await check_and_grant_achievements(uid)
    text, kb = await get_menu_page_data("inventory", uid, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

# ============================================================
# --- DUEL ---
# ============================================================
@dp.callback_query(F.data.startswith("duel_accept_"))
async def duel_accept_callback(callback: CallbackQuery):
    duel_id = int(callback.data.removeprefix("duel_accept_"))
    uid = callback.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM duels WHERE id=? AND status='pending'", (duel_id,)) as c:
            duel = await c.fetchone()
    if not duel:
        await callback.answer("❌ Дуэль устарела!", show_alert=True)
        return
    if duel['opponent_id'] != uid:
        await callback.answer("❌ Не ваша дуэль!", show_alert=True)
        return
    bet = duel['amount']
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("UPDATE duels SET status='done' WHERE id=? AND status='pending'", (duel_id,))
        await db.commit()
        if cursor.rowcount == 0:
            await callback.answer("❌ Дуэль уже завершена!", show_alert=True)
            return

    if not await try_deduct_balance(duel['challenger_id'], bet):
        # У вызывающего не хватает средств — дуэль отменяется без последствий
        await callback.answer("❌ У вызывающего нет средств!", show_alert=True)
        return
    if not await try_deduct_balance(uid, bet):
        await update_balance(duel['challenger_id'], bet)  # возврат
        await callback.answer(f"❌ У вас нет {bet} DOOM!", show_alert=True)
        return

    winner_id = duel['challenger_id'] if random.random() < 0.5 else uid
    # Возвращаем ставки обоим, а затем перечисляем удвоенную ставку победителю
    await update_balance(duel['challenger_id'], bet)
    await update_balance(uid, bet)
    await update_balance(winner_id, bet)

    challenger = await get_user(duel['challenger_id'])
    opponent = await get_user(uid)
    winner = await get_user(winner_id)
    winner_name = (winner['username'] or winner['first_name']) if winner else "?"
    ch_name = challenger['username'] or challenger['first_name']
    op_name = opponent['username'] or opponent['first_name']
    result = (
        f"🤝 <b>Дуэль!</b>\n⚔️ {ch_name} vs {op_name}\n"
        f"💰 Ставка: {bet:,} DOOM\n\n"
        f"🏆 Победитель: <b>{winner_name}</b> +{bet:,} DOOM!"
    )
    try:
        await callback.message.edit_text(result, parse_mode="HTML")
    except Exception:
        pass
    try:
        await bot.send_message(duel['challenger_id'], result, parse_mode="HTML")
    except Exception:
        pass
    await callback.answer("Дуэль завершена!")

@dp.callback_query(F.data.startswith("duel_decline_"))
async def duel_decline_callback(callback: CallbackQuery):
    duel_id = int(callback.data.removeprefix("duel_decline_"))
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE duels SET status='declined' WHERE id=?", (duel_id,))
        await db.commit()
    await callback.answer("❌ Дуэль отклонена.", show_alert=True)
    try:
        await callback.message.edit_text("❌ Дуэль отклонена.")
    except Exception:
        pass

# ============================================================
# --- ROB / SABOTAGE LOGIC (БЕСПЛАТНО) ---
# ============================================================
async def do_rob(message: Message, attacker_id: int, victim_id: int):
    attacker = await get_user(attacker_id)
    victim = await get_user(victim_id)
    if not attacker or not victim:
        await message.reply("❌ Один из игроков не зарегистрирован!")
        return
    on_cd, h, m = check_cooldown(attacker['last_rob_time'], 480)
    if on_cd:
        hours_left = h // 60
        mins_left = h % 60
        await message.reply(f"⏳ Перезарядка грабежа: {hours_left}ч {mins_left}мин.")
        return
    now = datetime.now()
    if victim['shield_until'] and datetime.fromisoformat(victim['shield_until']) > now:
        await message.reply("🛡 У цели щит!")
        return
    att_bal, vic_bal = attacker['doom_balance'], victim['doom_balance']
    if vic_bal <= 0:
        await message.reply("❌ У цели нет DOOM!")
        return
    if abs(att_bal - vic_bal) > max(att_bal, vic_bal) * 0.5:
        await message.reply("❌ Разница в балансах слишком большая (>50%)!")
        return

    vic_name = victim['username'] or victim['first_name']
    if random.random() < 0.40:
        percent = random.randint(10, 30)
        stolen = max(1, int(vic_bal * percent / 100))
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET doom_balance=MAX(0,doom_balance-?) WHERE user_id=?", (stolen, victim_id))
            await db.execute(
                "UPDATE users SET doom_balance=doom_balance+?, last_rob_time=?, total_robs_success=total_robs_success+1 WHERE user_id=?",
                (stolen, now.isoformat(), attacker_id)
            )
            await db.commit()
        await increment_sys_stat("total_robs")
        await increment_quest(attacker_id, "rob")
        await check_and_grant_achievements(attacker_id)
        await message.reply(
            f"🥷 <b>Успешный налёт!</b>\n{vic_name} потерял <b>{stolen:,}</b> DOOM!\nКД 8ч.",
            parse_mode="HTML"
        )
    else:
        penalty = max(10, int(att_bal * 0.10))
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "UPDATE users SET doom_balance=MAX(0,doom_balance-?), last_rob_time=?, total_robs_caught=total_robs_caught+1 WHERE user_id=?",
                (penalty, now.isoformat(), attacker_id)
            )
            await db.execute("UPDATE users SET doom_balance=doom_balance+? WHERE user_id=0", (penalty,))
            await db.commit()
        await check_and_grant_achievements(attacker_id)
        await message.reply(
            f"🚓 <b>Провалился!</b>\nШтраф <b>{penalty:,}</b> DOOM → Казна. КД 8ч.",
            parse_mode="HTML"
        )

async def do_sabotage(message: Message, attacker_id: int, victim_id: int):
    attacker = await get_user(attacker_id)
    victim = await get_user(victim_id)
    if not attacker or not victim:
        await message.reply("❌ Один из игроков не зарегистрирован!")
        return
    on_cd, h, m = check_cooldown(attacker['sabotage_cooldown'], 720)
    if on_cd:
        hours_left = h // 60
        mins_left = h % 60
        await message.reply(f"⏳ Саботаж: {hours_left}ч {mins_left}мин.")
        return
    if victim['dog_until'] and datetime.fromisoformat(victim['dog_until']) > datetime.now():
        await message.reply("🐕 У цели пёс-охранник! Заблокировано.")
        return

    now = datetime.now()
    percent = random.uniform(0.20, 0.40)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            UPDATE users SET
                crop_potatoes=MAX(0, CAST(crop_potatoes*(1-?) AS INT)),
                crop_apples=MAX(0, CAST(crop_apples*(1-?) AS INT)),
                crop_pumpkins=MAX(0, CAST(crop_pumpkins*(1-?) AS INT)),
                crop_watermelons=MAX(0, CAST(crop_watermelons*(1-?) AS INT)),
                crop_dumik=MAX(0, CAST(crop_dumik*(1-?) AS INT))
            WHERE user_id=?
        """, (percent, percent, percent, percent, percent, victim_id))
        await db.execute("UPDATE users SET sabotage_cooldown=? WHERE user_id=?", (now.isoformat(), attacker_id))
        await db.commit()
    pct_int = int(percent * 100)
    vic_name = victim['username'] or victim['first_name']
    await message.reply(
        f"🗡 <b>Саботаж!</b> Уничтожено {pct_int}% склада {vic_name}. КД 12ч.",
        parse_mode="HTML"
    )
    try:
        await bot.send_message(victim_id, f"🗡 Ваш склад уничтожен саботажником! Потери: {pct_int}%.", parse_mode="HTML")
    except Exception:
        pass

# ============================================================
# --- SHORT COMMAND HANDLERS ---
# ============================================================
async def cmd_farm_common(message: Message, is_big: bool):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!", reply_markup=get_subscribe_keyboard())
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    user = await get_user(uid)
    if not user:
        return
    cd_field = 'last_big_farm_time' if is_big else 'last_farm_time'
    cd_minutes = 360 if is_big else 60
    on_cd, m, s = check_cooldown(user[cd_field], cd_minutes)
    if on_cd:
        h = m // 60
        mins = m % 60
        txt = f"⏳ Большой сбор: {h}ч {mins}мин. {s}сек." if is_big else f"⏳ Обычный сбор: {m}мин. {s}сек."
        await message.reply(txt)
        return
    await run_harvest_logic(message, uid, message.from_user.first_name, user['farm_level'],
                            is_big=is_big,
                            fertilized=(user['fertilizer_count'] > 0),
                            has_pesticide=(user['pesticide_count'] > 0))

@dp.message(Command("f", "farm"))
async def cmd_f(message: Message):
    await cmd_farm_common(message, is_big=False)

@dp.message(Command("fb"))
async def cmd_fb(message: Message):
    await cmd_farm_common(message, is_big=True)

@dp.message(Command("sell"))
async def cmd_sell(message: Message):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!")
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    user = await get_user(uid)
    if not user:
        return
    mod = await get_market_modifier()
    total = calc_crop_value(user, mod)
    if total <= 0:
        await message.reply("📭 Склад пустой!")
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            UPDATE users SET doom_balance=doom_balance+?,
            crop_potatoes=0,crop_apples=0,crop_pumpkins=0,crop_watermelons=0,crop_dumik=0
            WHERE user_id=?
        """, (total, uid))
        await db.commit()
    await check_and_grant_achievements(uid)
    await message.reply(f"💰 Продано на <b>+{total:,}</b> DOOM! (×{mod})", parse_mode="HTML")

@dp.message(Command("b", "bal", "balance"))
async def cmd_bal(message: Message):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!")
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    args = message.text.split()
    if len(args) > 1:
        arg = args[1]
        target = await get_user_by_username(arg) if arg.startswith("@") else (await get_user(int(arg)) if arg.isdigit() else None)
    elif message.reply_to_message:
        target = await get_user(message.reply_to_message.from_user.id)
    else:
        target = await get_user(uid)
    if not target or target['user_id'] == 0:
        await message.reply("❌ Игрок не найден.")
        return
    name = target['username'] or target['first_name']
    await message.reply(format_user_stat_text(target, name), parse_mode="HTML")

@dp.message(Command("top"))
async def cmd_top(message: Message):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!")
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    text = await get_top_text("balance")
    await message.reply(text, reply_markup=get_top_keyboard("balance"), parse_mode="HTML")

@dp.message(Command("slot"))
async def cmd_slot(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!")
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    user = await get_user(uid)
    if not user:
        return
    args = message.text.split()
    try:
        bet = int(args[1])
        if bet < 10:
            raise ValueError
    except (IndexError, ValueError):
        await message.reply("❌ Укажите ставку!\nПример: <code>/slot 100</code>", parse_mode="HTML")
        return
    on_cd, m, s = check_cooldown(user['last_slot_time'], 10)
    if on_cd:
        await message.reply(f"⏳ Слот: ещё {m} мин. {s} сек.")
        return
    if user['doom_balance'] < bet:
        await message.reply("❌ Недостаточно DOOM!")
        return
    await state.update_data(current_bet=bet)
    await run_slot_machine(message, message.from_user, bet)

@dp.message(Command("up"))
async def cmd_upgrade(message: Message):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!")
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    user = await get_user(uid)
    if not user:
        return
    if user['farm_level'] >= 15:
        await message.reply("⚡️ Максимальный уровень фермы!")
        return
    cost = 600 * (2 ** (user['farm_level'] - 1))
    if not await try_deduct_balance(uid, cost):
        await message.reply(f"❌ Нужно <b>{cost:,}</b> DOOM (у вас: {user['doom_balance']:,})", parse_mode="HTML")
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET farm_level=farm_level+1 WHERE user_id=?", (uid,))
        await db.commit()
    new_user = await get_user(uid)
    await check_and_grant_achievements(uid)
    await message.reply(
        f"📈 Ферма прокачана до <b>{new_user['farm_level']}</b> уровня!\nМножитель: ×{1+(new_user['farm_level']-1)*0.5:.1f}",
        parse_mode="HTML"
    )

@dp.message(Command("q"))
async def cmd_quests(message: Message):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!")
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    user = await get_user(uid)
    if not user:
        return
    status = await get_quest_status(user)
    fname = safe_name(message.from_user.first_name)
    text = f"📋 <b>Квесты</b> — {fname}\n\n"
    for qtype, (qdesc, qtarget, qreward) in DAILY_QUESTS.items():
        done = status.get(qtype, 0)
        bar = "✅" if done >= qtarget else f"{done}/{qtarget}"
        text += f"{qdesc}\n+{qreward} DOOM | {bar}\n\n"
    await message.reply(text, reply_markup=get_back_keyboard(), parse_mode="HTML")

@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!")
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    user = await get_user(uid)
    if not user:
        return
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={uid}"
    await message.reply(
        f"🔗 <b>Рефералы</b>\n\nПриглашено: <b>{user['referral_count']}</b>\n\n<code>{ref_link}</code>",
        parse_mode="HTML"
    )

@dp.message(Command("case"))
async def cmd_case(message: Message):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!")
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    text, kb = await get_menu_page_data("case", uid, message.from_user.first_name)
    await message.reply(text, reply_markup=kb, parse_mode="HTML")

@dp.message(Command("shop"))
async def cmd_shop(message: Message):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!")
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    text, kb = await get_menu_page_data("shop", uid, message.from_user.first_name)
    await message.reply(text, reply_markup=kb, parse_mode="HTML")

@dp.message(Command("bank"))
async def cmd_bank(message: Message):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!")
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    text, kb = await get_menu_page_data("bank", uid, message.from_user.first_name)
    await message.reply(text, reply_markup=kb, parse_mode="HTML")

@dp.message(Command("daily"))
async def cmd_daily(message: Message):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!")
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    await handle_daily_bonus(message, uid)

async def handle_daily_bonus(message: Message, uid: int):
    user = await get_user(uid)
    if not user:
        return
    now = datetime.now()
    last = user['last_daily_time']
    if last and datetime.fromisoformat(last).date() >= now.date():
        next_time = (datetime.fromisoformat(last) + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        rem = next_time - now
        h = int(rem.total_seconds() // 3600)
        m = int((rem.total_seconds() % 3600) // 60)
        await message.reply(f"⏳ Следующий бонус через <b>{h}ч {m}мин.</b>", parse_mode="HTML")
        return
    bonus = random.randint(200, 1000)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET doom_balance=doom_balance+?, last_daily_time=? WHERE user_id=?",
            (bonus, now.isoformat(), uid)
        )
        await db.commit()
    await message.reply(
        f"🎁 <b>Ежедневный бонус!</b>\n\n+<b>{bonus}</b> DOOM\nПриходите завтра!",
        reply_markup=get_back_keyboard(), parse_mode="HTML"
    )

@dp.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!", reply_markup=get_subscribe_keyboard())
        return
    await state.clear()
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    user = await get_user(uid)
    bal = user['doom_balance'] if user else 0
    lvl = user['farm_level'] if user else 1
    rank = get_rank(bal)
    fname = safe_name(message.from_user.first_name)
    await message.reply(
        f"🛸 <b>DOOM Ферма</b> — {fname}\n💰 <b>{bal:,}</b> DOOM | 🚜 Лвл <b>{lvl}</b> | {rank}",
        reply_markup=get_main_keyboard(), parse_mode="HTML"
    )

@dp.message(Command("roulette"))
async def cmd_roulette(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not await is_subscribed(uid):
        await message.reply("❌ Подпишитесь на @fermadoom!", reply_markup=get_subscribe_keyboard())
        return
    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)
    await state.clear()
    chat_id = message.chat.id
    text = build_roulette_status_text(chat_id)
    await message.reply(text, reply_markup=get_multi_roulette_menu_keyboard(chat_id), parse_mode="HTML")

# ============================================================
# --- GLOBAL TEXT HANDLER ---
# ============================================================
@dp.message(F.text)
async def global_text_handler(message: Message, state: FSMContext):
    if message.from_user.is_bot:
        return

    uid = message.from_user.id
    text_raw = message.text.strip()
    cmd = text_raw.lower()
    parts = cmd.split()
    cmd0 = parts[0]

    current_state = await state.get_state()
    if current_state:
        return

    if not await is_subscribed(uid):
        known = {"ферма","фарм","ф","большой","продать","баланс","стата","меню","топ","слот",
                 "апгрейд","прокачать","квесты","задания","реф","рефералы",
                 "магазин","банк","кейс","бонус","грабить","ограбить","г","саботаж","дуэль","лупа",
                 "рулетка","биржа","кредит","сезон","событие"}
        if cmd0 in known:
            await message.reply("📢 Подпишитесь на @fermadoom!", reply_markup=get_subscribe_keyboard())
        return

    await register_user_chat(uid, message.from_user.username, message.from_user.first_name)

    if cmd0 in ["ферма", "фарм", "ф", "/f"]:
        await cmd_farm_common(message, is_big=False)
        return

    if cmd0 in ["большой", "/fb"]:
        await cmd_farm_common(message, is_big=True)
        return

    if cmd0 in ["продать", "sell", "/sell"]:
        user = await get_user(uid)
        if not user:
            return
        mod = await get_market_modifier()
        total = calc_crop_value(user, mod)
        if total <= 0:
            await message.reply("📭 Склад пустой!")
            return
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
                UPDATE users SET doom_balance=doom_balance+?,
                crop_potatoes=0,crop_apples=0,crop_pumpkins=0,crop_watermelons=0,crop_dumik=0
                WHERE user_id=?
            """, (total, uid))
            await db.commit()
        await check_and_grant_achievements(uid)
        await message.reply(f"💰 Продано на <b>+{total:,}</b> DOOM! (×{mod})", parse_mode="HTML")
        return

    if cmd0 in ["баланс", "стата", "статистика", "bal", "s", "б"]:
        target_user = None
        if message.reply_to_message:
            target_user = await get_user(message.reply_to_message.from_user.id)
        elif len(parts) > 1:
            arg = parts[1]
            target_user = await get_user_by_username(arg) if arg.startswith("@") else (await get_user(int(arg)) if arg.isdigit() else None)
        else:
            target_user = await get_user(uid)
        if not target_user or target_user['user_id'] == 0:
            await message.reply("❌ Игрок не зарегистрирован.")
            return
        name = target_user['username'] or target_user['first_name']
        await message.reply(format_user_stat_text(target_user, name), parse_mode="HTML")
        return

    if cmd0 in ["меню", "menu"]:
        user = await get_user(uid)
        bal = user['doom_balance'] if user else 0
        lvl = user['farm_level'] if user else 1
        rank = get_rank(bal)
        fname = safe_name(message.from_user.first_name)
        await message.reply(
            f"🛸 <b>DOOM Ферма</b> — {fname}\n💰 <b>{bal:,}</b> DOOM | 🚜 Лвл <b>{lvl}</b> | {rank}",
            reply_markup=get_main_keyboard(), parse_mode="HTML"
        )
        return

    if cmd0 in ["топ", "top"]:
        text = await get_top_text("balance")
        await message.reply(text, reply_markup=get_top_keyboard("balance"), parse_mode="HTML")
        return

    if cmd0 in ["помощь", "help", "правила", "справка"]:
        await message.reply(get_help_text(), reply_markup=get_back_keyboard(), parse_mode="HTML")
        return

    if cmd0 in ["апгрейд", "прокачать", "up"]:
        user = await get_user(uid)
        if not user:
            return
        if user['farm_level'] >= 15:
            await message.reply("⚡️ Максимальный уровень фермы!")
            return
        cost = 600 * (2 ** (user['farm_level'] - 1))
        if not await try_deduct_balance(uid, cost):
            await message.reply(f"❌ Нужно <b>{cost:,}</b> DOOM (у вас: {user['doom_balance']:,})", parse_mode="HTML")
            return
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET farm_level=farm_level+1 WHERE user_id=?", (uid,))
            await db.commit()
        new_user = await get_user(uid)
        await check_and_grant_achievements(uid)
        await message.reply(
            f"📈 Ферма прокачана до <b>{new_user['farm_level']}</b> уровня! ×{1+(new_user['farm_level']-1)*0.5:.1f}",
            parse_mode="HTML"
        )
        return

    if cmd0 in ["квесты", "задания", "квест"]:
        user = await get_user(uid)
        if not user:
            return
        status = await get_quest_status(user)
        fname = safe_name(message.from_user.first_name)
        text = f"📋 <b>Квесты</b> — {fname}\n\n"
        for qtype, (qdesc, qtarget, qreward) in DAILY_QUESTS.items():
            done = status.get(qtype, 0)
            bar = "✅" if done >= qtarget else f"{done}/{qtarget}"
            text += f"{qdesc}\n+{qreward} DOOM | {bar}\n\n"
        await message.reply(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
        return

    if cmd0 in ["реф", "рефералы", "ref"]:
        user = await get_user(uid)
        if not user:
            return
        me = await bot.get_me()
        ref_link = f"https://t.me/{me.username}?start={uid}"
        await message.reply(
            f"🔗 <b>Рефералы</b>\n\nПриглашено: <b>{user['referral_count']}</b>\n\n<code>{ref_link}</code>",
            parse_mode="HTML"
        )
        return

    if cmd0 in ["магазин", "shop"]:
        text, kb = await get_menu_page_data("shop", uid, message.from_user.first_name)
        await message.reply(text, reply_markup=kb, parse_mode="HTML")
        return

    if cmd0 in ["банк", "bank"]:
        text, kb = await get_menu_page_data("bank", uid, message.from_user.first_name)
        await message.reply(text, reply_markup=kb, parse_mode="HTML")
        return

    if cmd0 in ["кейс", "case"]:
        text, kb = await get_menu_page_data("case", uid, message.from_user.first_name)
        await message.reply(text, reply_markup=kb, parse_mode="HTML")
        return

    if cmd0 in ["бонус", "daily", "ежедневный"]:
        await handle_daily_bonus(message, uid)
        return

    if cmd0 in ["рулетка", "roulette"]:
        chat_id = message.chat.id
        text = build_roulette_status_text(chat_id)
        await message.reply(text, reply_markup=get_multi_roulette_menu_keyboard(chat_id), parse_mode="HTML")
        return

    if cmd0 in ["биржа", "exchange"]:
        text, kb = await build_exchange_menu()
        await message.reply(text, reply_markup=kb, parse_mode="HTML")
        return

    if cmd0 in ["кредит", "credit"]:
        fname = safe_name(message.from_user.first_name)
        text, kb = await build_credit_menu(uid, fname)
        await message.reply(text, reply_markup=kb, parse_mode="HTML")
        return

    if cmd0 in ["клан", "clan"]:
        text, kb = await get_menu_page_data("clan", uid, message.from_user.first_name)
        await message.reply(text, reply_markup=kb, parse_mode="HTML")
        return

    if cmd0 in ["война", "войны", "war"]:
        user = await get_user(uid)
        if not user or not user['clan_id']:
            await message.reply("❌ Вы не в клане! Создайте или вступите через 🏰 Клан.")
            return
        text, kb = await get_menu_page_data("clan", uid, message.from_user.first_name)
        await message.reply(text, reply_markup=kb, parse_mode="HTML")
        return

    if cmd0 in ["сезон", "season"]:
        season = get_season_info()
        day_number = (datetime.now() - datetime(2024, 1, 1)).days
        days_left = 7 - (day_number % 7)
        text = (
            f"🗓 <b>Текущий сезон: {season['name']}</b>\n\n"
            f"Эффект: {season['desc']}\n"
            f"До смены: <b>{days_left}</b> дн."
        )
        await message.reply(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
        return

    if cmd0 in ["событие", "event"]:
        event = await get_active_event()
        if event:
            ends = datetime.fromisoformat(event["ends_at"])
            rem = ends - datetime.now()
            h = int(rem.total_seconds() // 3600)
            m = int((rem.total_seconds() % 3600) // 60)
            text = f"🌍 <b>{event['name']}</b>\n{event['desc']}\n⏳ Осталось: {h}ч {m}мин."
        else:
            text = "🌍 Сейчас нет активных событий."
        await message.reply(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
        return

    if cmd0 in ["слот", "спин", "slot", "spin"]:
        user = await get_user(uid)
        if not user:
            return
        on_cd, m, s = check_cooldown(user['last_slot_time'], 10)
        if on_cd:
            await message.reply(f"⏳ Слот: {m} мин. {s} сек.")
            return
        try:
            bet = int(parts[1])
            if bet < 10:
                raise ValueError
        except (IndexError, ValueError):
            await message.reply("❌ Пример: <code>слот 100</code>", parse_mode="HTML")
            return
        if user['doom_balance'] < bet:
            await message.reply("❌ Недостаточно DOOM!")
            return
        await run_slot_machine(message, message.from_user, bet)
        return

    if cmd0 in ["дуэль", "дуель", "duel"]:
        if len(parts) < 3:
            await message.reply("❌ Пример: <code>дуэль @player 500</code>", parse_mode="HTML")
            return
        target_arg = parts[1]
        if not parts[2].isdigit():
            await message.reply("❌ Ставка — число!")
            return
        bet = int(parts[2])
        if bet < 10:
            await message.reply("❌ Минимум 10 DOOM!")
            return
        challenger = await get_user(uid)
        opponent = await get_user_by_username(target_arg) if target_arg.startswith("@") else None
        if not opponent or opponent['user_id'] == 0 or opponent['user_id'] == uid:
            await message.reply("❌ Игрок не найден!")
            return
        if not challenger or challenger['doom_balance'] < bet:
            await message.reply("❌ Недостаточно DOOM!")
            return
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO duels (challenger_id, opponent_id, amount, created_at) VALUES (?,?,?,?)",
                (uid, opponent['user_id'], bet, datetime.now().isoformat())
            )
            async with db.execute("SELECT last_insert_rowid()") as c:
                duel_id = (await c.fetchone())[0]
            await db.commit()
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Принять", callback_data=f"duel_accept_{duel_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"duel_decline_{duel_id}"),
        )
        ch_name = challenger['username'] or challenger['first_name']
        op_name = opponent['username'] or opponent['first_name']
        try:
            await bot.send_message(
                opponent['user_id'],
                f"⚔️ <b>{ch_name}</b> вызывает вас на дуэль!\nСтавка: <b>{bet:,}</b> DOOM",
                reply_markup=kb.as_markup(), parse_mode="HTML"
            )
            await message.reply(f"⚔️ Вызов отправлен <b>{op_name}</b>!", parse_mode="HTML")
        except Exception:
            await message.reply("❌ Не удалось отправить. Игрок не запустил бота.")
        return

    if cmd0 in ["лупа", "magnifier"]:
        user = await get_user(uid)
        if not user or user['magnifier_count'] < 1:
            await message.reply("❌ Нет лупы! Купите в 🛒 Магазине.")
            return
        if len(parts) < 2:
            await message.reply("❌ Пример: <code>лупа @username</code>", parse_mode="HTML")
            return
        target = await get_user_by_username(parts[1]) if parts[1].startswith("@") else None
        if not target or target['user_id'] == 0:
            await message.reply("❌ Игрок не найден!")
            return
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET magnifier_count=magnifier_count-1 WHERE user_id=?", (uid,))
            await db.commit()
        name = target['username'] or target['first_name']
        rank = get_rank(target['doom_balance'])
        await message.reply(
            f"🔍 <b>{name}</b> {rank}\n💰 {target['doom_balance']:,} DOOM | 🚜 Лвл {target['farm_level']}\n📦 Склад: {calc_crop_value(target):,} DOOM",
            parse_mode="HTML"
        )
        return

    if cmd0 in ["грабить", "ограбить", "г", "rob"]:
        is_group = message.chat.type in ["group", "supergroup"]
        if is_group:
            if not message.reply_to_message or message.reply_to_message.from_user.is_bot:
                await message.reply("❌ Ответьте на сообщение цели командой «грабить»")
                return
            victim_id = message.reply_to_message.from_user.id
        else:
            if len(parts) < 2 or not parts[1].startswith("@"):
                await message.reply("❌ В ЛС укажите цель: <code>грабить @username</code>", parse_mode="HTML")
                return
            target = await get_user_by_username(parts[1])
            if not target or target['user_id'] == 0:
                await message.reply("❌ Игрок не найден!")
                return
            victim_id = target['user_id']
        if uid == victim_id:
            return
        await do_rob(message, uid, victim_id)
        return

    if cmd0 in ["саботаж", "sabotage"]:
        is_group = message.chat.type in ["group", "supergroup"]
        if is_group:
            if not message.reply_to_message or message.reply_to_message.from_user.is_bot:
                await message.reply("❌ Ответьте на сообщение цели командой «саботаж»")
                return
            victim_id = message.reply_to_message.from_user.id
        else:
            if len(parts) < 2 or not parts[1].startswith("@"):
                await message.reply("❌ В ЛС укажите цель: <code>саботаж @username</code>", parse_mode="HTML")
                return
            target = await get_user_by_username(parts[1])
            if not target or target['user_id'] == 0:
                await message.reply("❌ Игрок не найден!")
                return
            victim_id = target['user_id']
        if uid == victim_id:
            return
        await do_sabotage(message, uid, victim_id)
        return

# ============================================================
# --- PRE-CHECKOUT (звёзды: только донат и пропуск кулдауна) ---
# ============================================================
@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

# ============================================================
# --- MARKET PRICE UPDATE ---
# ============================================================
async def market_price_update_task():
    while True:
        await asyncio.sleep(3600 * 6)
        await update_market_prices()
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT market_price_modifier FROM system_stats LIMIT 1") as c:
                row = await c.fetchone()
        if row:
            logging.info(f"Market price updated: ×{row[0]}")

# ============================================================
# --- STARTUP ---
# ============================================================
async def main():
    await init_db()
    asyncio.create_task(bank_interest_task())
    asyncio.create_task(market_price_update_task())
    asyncio.create_task(credit_penalty_task())
    asyncio.create_task(farm_event_task())
    asyncio.create_task(clan_war_check_task())
    print("✅ DOOM FERMA запущена!")
    print(f"🗓 Текущий сезон: {get_season_info()['name']}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
