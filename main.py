import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import parse_qsl

import aiohttp

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CallbackQuery,
    Update,
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("filmbot")


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
KP_API_TOKEN = os.getenv("KP_API_TOKEN")

NETLIFY_URL = os.getenv(
    "NETLIFY_URL",
    "https://kinopoiskov1k.netlify.app",
)

# Автоматически подтягиваем WEBHOOK_URL из Render или используем дефолт
WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL",
    "https://filmbot-backend.onrender.com/webhook",
)

MAX_ROOM_USERS = 20
USER_TIMEOUT = 60
MAX_MESSAGE_LENGTH = 500
MAX_CHAT_MESSAGES = 100

ROOM_CLEANUP_INTERVAL = 300
EMPTY_ROOM_LIFETIME = 3600


if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. Добавь BOT_TOKEN в Environment Variables Render."
    )

if not KP_API_TOKEN:
    raise RuntimeError(
        "KP_API_TOKEN не найден. Добавь KP_API_TOKEN в Environment Variables Render."
    )


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# DATA MODELS
# ============================================================

@dataclass
class RoomUser:
    user_id: str
    name: str
    websocket: WebSocket
    joined_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


@dataclass
class RoomState:
    room_id: str
    kp_id: str

    playing: bool = False
    position: float = 0.0

    controller_id: Optional[str] = None

    users: Dict[str, RoomUser] = field(default_factory=dict)

    messages: List[dict] = field(default_factory=list)

    updated_at: float = field(default_factory=time.time)


# ============================================================
# GLOBAL STATE
# ============================================================

rooms: Dict[str, RoomState] = {}

rooms_lock = asyncio.Lock()


# ============================================================
# HELPERS
# ============================================================

def validate_room_id(room_id: str) -> bool:
    if not room_id:
        return False

    if len(room_id) > 64:
        return False

    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"

    return all(char in allowed for char in room_id)


def validate_kp_id(kp_id: str) -> bool:
    if not kp_id:
        return False

    if len(kp_id) > 32:
        return False

    return kp_id.isdigit()


def sanitize_name(name: str) -> str:
    if not name:
        return "Гость"

    name = str(name).strip()

    if not name:
        return "Гость"

    return name[:50]


def sanitize_message(message: str) -> str:
    if not message:
        return ""

    message = str(message).strip()

    return message[:MAX_MESSAGE_LENGTH]


def create_room_id() -> str:
    return uuid.uuid4().hex[:12]


# ============================================================
# TELEGRAM MINI APP INIT DATA VALIDATION
# ============================================================

def validate_init_data(init_data: str) -> bool:
    if not init_data:
        return False

    try:
        parsed = dict(
            parse_qsl(
                init_data,
                keep_blank_values=True,
            )
        )

        received_hash = parsed.pop("hash", None)

        if not received_hash:
            return False

        auth_date = parsed.get("auth_date")

        if auth_date:
            try:
                auth_timestamp = int(auth_date)

                if time.time() - auth_timestamp > 86400:
                    return False

            except ValueError:
                return False

        data_check_string = "\n".join(
            f"{key}={parsed[key]}"
            for key in sorted(parsed.keys())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(
            calculated_hash,
            received_hash,
        )

    except Exception:
        logger.exception("Ошибка проверки Telegram initData")
        return False


def get_user_from_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(
            status_code=401,
            detail="Telegram initData отсутствует",
        )

    if not validate_init_data(init_data):
        raise HTTPException(
            status_code=401,
            detail="Недействительный Telegram initData",
        )

    parsed = dict(
        parse_qsl(
            init_data,
            keep_blank_values=True,
        )
    )

    user_raw = parsed.get("user")

    if not user_raw:
        raise HTTPException(
            status_code=401,
            detail="Информация о пользователе отсутствует",
        )

    try:
        return json.loads(user_raw)

    except json.JSONDecodeError:
        raise HTTPException(
            status_code=401,
            detail="Некорректные данные пользователя",
        )


# ============================================================
# ROOM HELPERS
# ============================================================

async def broadcast_room(
    room: RoomState,
    message: dict,
    exclude_user_id: Optional[str] = None,
):
    dead_users = []

    for user_id, room_user in list(room.users.items()):

        if exclude_user_id and user_id == exclude_user_id:
            continue

        try:
            await room_user.websocket.send_json(message)

        except Exception:
            dead_users.append(user_id)

    for user_id in dead_users:
        room.users.pop(user_id, None)


def room_state_payload(room: RoomState) -> dict:
    users = []

    for user in room.users.values():
        users.append(
            {
                "user_id": user.user_id,
                "name": user.name,
                "joined_at": user.joined_at,
            }
        )

    return {
        "type": "room_state",
        "room": {
            "room_id": room.room_id,
            "kp_id": room.kp_id,
            "playing": room.playing,
            "position": room.position,
            "controller_id": room.controller_id,
            "users": users,
            "messages": room.messages,
        },
    }


# ============================================================
# KINOPOISK SEARCH (С фильтрацией фильмов из будущего)
# ============================================================

async def search_kinopoisk(query: str) -> List[dict]:
    query = query.strip()

    if not query:
        return []

    if len(query) > 100:
        query = query[:100]

    url = (
        "https://kinopoiskapiunofficial.tech"
        "/api/v2.1/films/search-by-keyword"
    )

    headers = {
        "X-API-KEY": KP_API_TOKEN,
        "Accept": "application/json",
    }

    params = {
        "keyword": query,
        "page": 1,
    }

    timeout = aiohttp.ClientTimeout(total=20)

    try:
        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.get(
                url,
                headers=headers,
                params=params,
            ) as response:

                if response.status != 200:
                    body = await response.text()

                    logger.error(
                        "Kinopoisk API error: %s | %s",
                        response.status,
                        body[:500],
                    )

                    return []

                data = await response.json()

    except Exception:
        logger.exception(
            "Ошибка запроса к Kinopoisk API"
        )

        return []

    films = data.get("films", [])
    result = []
    
    # Автоматически берем текущий год (сейчас 2026), чтобы отсеивать анонсы из будущего
    import datetime
    current_year = datetime.datetime.now().year

    for film in films[:30]:
        film_id = film.get("filmId") or film.get("id")

        if not film_id:
            continue

        year_val = film.get("year")
        is_future = False
        if year_val:
            try:
                clean_year = "".join(filter(str.isdigit, str(year_val)[:4]))
                if clean_year:
                    movie_year = int(clean_year)
                    # Отсеиваем фильмы, которые выходят позже, чем через 1 год
                    if movie_year > current_year + 1:
                        is_future = True
            except ValueError:
                pass

        if is_future:
            continue

        name_ru = film.get("nameRu") or film.get("name") or ""
        name_en = film.get("nameEn") or ""

        name = name_ru or name_en or "Без названия"

        year = str(year_val) if year_val else ""

        rating = (
            film.get("rating")
            or film.get("ratingVoteCount")
            or ""
        )

        countries = film.get("countries") or []
        genres = film.get("genres") or []

        country_names = [
            item.get("country")
            for item in countries
            if item.get("country")
        ]

        genre_names = [
            item.get("genre")
            for item in genres
            if item.get("genre")
        ]

        result.append(
            {
                "id": str(film_id),
                "name": name,
                "name_ru": name_ru,
                "name_en": name_en,
                "year": year,
                "rating": str(rating),
                "poster": film.get("posterUrlPreview")
                or film.get("posterUrl")
                or "",
                "genres": genre_names,
                "countries": country_names,
            }
        )

        if len(result) >= 15:
            break

    return result


# ============================================================
# FASTAPI LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    logger.info("========================================")
    logger.info("Запуск FilmBot backend")
    logger.info("NETLIFY_URL: %s", NETLIFY_URL)
    logger.info("WEBHOOK_URL: %s", WEBHOOK_URL)
    logger.info("========================================")

    cleanup_task = None

    # --------------------------------------------------------
    # SET TELEGRAM WEBHOOK
    # --------------------------------------------------------

    try:

        await bot.set_webhook(
            url=WEBHOOK_URL,
            drop_pending_updates=True,
        )

        logger.info(
            "Telegram webhook успешно установлен: %s",
            WEBHOOK_URL,
        )

    except Exception:
        logger.exception(
            "Не удалось установить Telegram webhook"
        )

    # --------------------------------------------------------
    # START ROOM CLEANUP
    # --------------------------------------------------------

    cleanup_task = asyncio.create_task(
        room_cleanup_loop()
    )

    try:

        yield

    finally:

        # ----------------------------------------------------
        # STOP CLEANUP
        # ----------------------------------------------------

        if cleanup_task:

            cleanup_task.cancel()

            try:
                await cleanup_task

            except asyncio.CancelledError:
                pass

            except Exception:
                logger.exception(
                    "Ошибка остановки cleanup task"
                )

        # ----------------------------------------------------
        # DELETE WEBHOOK
        # ----------------------------------------------------

        try:

            await bot.delete_webhook(
                drop_pending_updates=False
            )

            logger.info(
                "Telegram webhook удалён при завершении."
            )

        except Exception:
            logger.exception(
                "Ошибка удаления Telegram webhook"
            )

        # ----------------------------------------------------
        # CLOSE TELEGRAM SESSION
        # ----------------------------------------------------

        try:

            await bot.session.close()

        except Exception:
            logger.exception(
                "Ошибка закрытия Telegram session"
            )


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(
    title="Kinopoisk Telegram Mini App API",
    version="1.0.0",
    lifespan=lifespan,
)


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        NETLIFY_URL,
        "https://kinopoiskov1k.netlify.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# BASIC ROUTES
# ============================================================

@app.get("/")
async def root():

    return {
        "status": "ok",
        "service": "FilmBot backend",
        "webhook": WEBHOOK_URL,
        "netlify": NETLIFY_URL,
        "rooms": len(rooms),
    }


@app.get("/health")
async def health():

    return {
        "status": "healthy",
        "rooms": len(rooms),
        "timestamp": int(time.time()),
    }


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post("/webhook")
async def telegram_webhook(update: dict):

    try:

        logger.info(
            "Получен Telegram update: %s",
            update.get("update_id"),
        )

        telegram_update = Update.model_validate(
            update,
            context={
                "bot": bot,
            },
        )

        await dp.feed_update(
            bot,
            telegram_update,
        )

        return {
            "ok": True,
        }

    except Exception:

        logger.exception(
            "Ошибка обработки Telegram webhook"
        )

        raise HTTPException(
            status_code=500,
            detail="Webhook processing failed",
        )


# ============================================================
# ROOM INFO API
# ============================================================

@app.get("/api/room/{room_id}")
async def get_room(
    room_id: str,
    kp_id: str,
):

    if not validate_room_id(room_id):

        raise HTTPException(
            status_code=400,
            detail="Некорректный room_id",
        )

    if not validate_kp_id(kp_id):

        raise HTTPException(
            status_code=400,
            detail="Некорректный kp_id",
        )

    async with rooms_lock:

        room = rooms.get(room_id)

        if not room:

            raise HTTPException(
                status_code=404,
                detail="Комната не найдена",
            )

        if room.kp_id != kp_id:

            raise HTTPException(
                status_code=400,
                detail="Фильм комнаты не совпадает",
            )

        return room_state_payload(room)


# ============================================================
# SEARCH API
# ============================================================

@app.get("/api/search")
async def search_api(q: str):

    query = q.strip()

    if not query:

        return {
            "results": [],
        }

    results = await search_kinopoisk(query)

    return {
        "results": results,
    }


# ============================================================
# WEBSOCKET WATCH PARTY
# ============================================================

@app.websocket("/ws/room/{room_id}")
async def websocket_room(
    websocket: WebSocket,
    room_id: str,
):

    await websocket.accept()

    logger.info(
        "WebSocket подключение: room=%s",
        room_id,
    )

    if not validate_room_id(room_id):

        await websocket.send_json(
            {
                "type": "error",
                "message": "Некорректный room_id",
            }
        )

        await websocket.close()

        return

    room_user_id = None

    try:

        # ----------------------------------------------------
        # WAIT FOR JOIN
        # ----------------------------------------------------

        try:

            raw_message = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=15,
            )

        except asyncio.TimeoutError:

            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Время подключения истекло",
                }
            )

            await websocket.close()

            return

        if raw_message.get("type") != "join":

            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Первое сообщение должно быть join",
                }
            )

            await websocket.close()

            return

        kp_id = str(
            raw_message.get("kp_id", "")
        )

        if not validate_kp_id(kp_id):

            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Некорректный kp_id",
                }
            )

            await websocket.close()

            return

        # ----------------------------------------------------
        # TELEGRAM USER
        # ----------------------------------------------------

        init_data = raw_message.get(
            "initData",
            "",
        )

        telegram_user = None

        if init_data:

            try:

                telegram_user = get_user_from_init_data(
                    init_data
                )

            except HTTPException:

                telegram_user = None

        # ----------------------------------------------------
        # USER ID
        # ----------------------------------------------------

        if telegram_user:

            room_user_id = str(
                telegram_user.get("id")
            )

            first_name = telegram_user.get(
                "first_name",
                "",
            )

            last_name = telegram_user.get(
                "last_name",
                "",
            )

            username = telegram_user.get(
                "username",
                "",
            )

            if username:

                user_name = f"@{username}"

            else:

                user_name = (
                    f"{first_name} {last_name}"
                ).strip()

        else:

            room_user_id = str(
                raw_message.get(
                    "user_id",
                    uuid.uuid4().hex,
                )
            )

            user_name = raw_message.get(
                "name",
                "Гость",
            )

        user_name = sanitize_name(
            user_name
        )

        # ----------------------------------------------------
        # CREATE / GET ROOM
        # ----------------------------------------------------

        async with rooms_lock:

            room = rooms.get(room_id)

            if room is None:

                room = RoomState(
                    room_id=room_id,
                    kp_id=kp_id,
                )

                rooms[room_id] = room

                logger.info(
                    "Создана новая комната: %s | kp=%s",
                    room_id,
                    kp_id,
                )

            else:

                if room.kp_id != kp_id:

                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": (
                                "Эта комната создана "
                                "для другого фильма"
                            ),
                        }
                    )

                    await websocket.close()

                    return

            # ------------------------------------------------
            # MAX USERS
            # ------------------------------------------------

            if (
                room_user_id not in room.users
                and len(room.users) >= MAX_ROOM_USERS
            ):

                await websocket.send_json(
                    {
                        "type": "error",
                        "message": (
                            "Комната заполнена. "
                            f"Максимум {MAX_ROOM_USERS} человек."
                        ),
                    }
                )

                await websocket.close()

                return

            # ------------------------------------------------
            # ADD USER
            # ------------------------------------------------

            room_user = RoomUser(
                user_id=room_user_id,
                name=user_name,
                websocket=websocket,
            )

            room.users[room_user_id] = room_user

            room.updated_at = time.time()

            if room.controller_id is None:

                room.controller_id = room_user_id

            logger.info(
                "Пользователь вошёл: room=%s user=%s name=%s",
                room_id,
                room_user_id,
                user_name,
            )

        # ----------------------------------------------------
        # SEND INITIAL STATE
        # ----------------------------------------------------

        await websocket.send_json(
            room_state_payload(room)
        )

        # ----------------------------------------------------
        # BROADCAST JOIN
        # ----------------------------------------------------

        await broadcast_room(
            room,
            {
                "type": "user_joined",
                "user": {
                    "user_id": room_user_id,
                    "name": user_name,
                },
            },
            exclude_user_id=room_user_id,
        )

        # ----------------------------------------------------
        # MESSAGE LOOP
        # ----------------------------------------------------

        while True:

            try:

                data = await websocket.receive_json()

            except WebSocketDisconnect:

                break

            except Exception:

                logger.exception(
                    "Ошибка получения WebSocket сообщения"
                )

                break

            message_type = data.get("type")

            # ------------------------------------------------
            # PING
            # ------------------------------------------------

            if message_type == "ping":

                room_user.last_seen = time.time()

                await websocket.send_json(
                    {
                        "type": "pong",
                        "timestamp": int(time.time()),
                    }
                )

                continue

            # ------------------------------------------------
            # GET ROOM STATE
            # ------------------------------------------------

            if message_type == "request_sync":

                room_user.last_seen = time.time()

                await websocket.send_json(
                    room_state_payload(room)
                )

                continue

            # ------------------------------------------------
            # SET NAME
            # ------------------------------------------------

            if message_type == "set_name":

                new_name = sanitize_name(
                    data.get("name", "")
                )

                room_user.name = new_name

                room_user.last_seen = time.time()

                room.updated_at = time.time()

                await broadcast_room(
                    room,
                    {
                        "type": "user_name_changed",
                        "user_id": room_user_id,
                        "name": new_name,
                    },
                )

                continue

            # ------------------------------------------------
            # PLAY
            # ------------------------------------------------

            if message_type == "play":

                room_user.last_seen = time.time()

                position = data.get(
                    "position",
                    room.position,
                )

                try:

                    position = float(position)

                except (
                    ValueError,
                    TypeError,
                ):

                    position = room.position

                room.position = max(
                    0.0,
                    position,
                )

                room.playing = True

                room.updated_at = time.time()

                await broadcast_room(
                    room,
                    {
                        "type": "play",
                        "position": room.position,
                        "user_id": room_user_id,
                    },
                    exclude_user_id=room_user_id,
                )

                continue

            # ------------------------------------------------
            # PAUSE
            # ------------------------------------------------

            if message_type == "pause":

                room_user.last_seen = time.time()

                position = data.get(
                    "position",
                    room.position,
                )

                try:

                    position = float(position)

                except (
                    ValueError,
                    TypeError,
                ):

                    position = room.position

                room.position = max(
                    0.0,
                    position,
                )

                room.playing = False

                room.updated_at = time.time()

                await broadcast_room(
                    room,
                    {
                        "type": "pause",
                        "position": room.position,
                        "user_id": room_user_id,
                    },
                    exclude_user_id=room_user_id,
                )

                continue

            # ------------------------------------------------
            # SEEK
            # ------------------------------------------------

            if message_type == "seek":

                room_user.last_seen = time.time()

                position = data.get(
                    "position",
                    0,
                )

                try:

                    position = float(position)

                except (
                    ValueError,
                    TypeError,
                ):

                    position = 0.0

                room.position = max(
                    0.0,
                    position,
                )

                room.updated_at = time.time()

                await broadcast_room(
                    room,
                    {
                        "type": "seek",
                        "position": room.position,
                        "user_id": room_user_id,
                    },
                    exclude_user_id=room_user_id,
                )

                continue

            # ------------------------------------------------
            # TIME UPDATE
            # ------------------------------------------------

            if message_type == "time_update":

                room_user.last_seen = time.time()

                position = data.get(
                    "position",
                    room.position,
                )

                try:

                    position = float(position)

                except (
                    ValueError,
                    TypeError,
                ):

                    position = room.position

                room.position = max(
                    0.0,
                    position,
                )

                room.updated_at = time.time()

                continue

            # ------------------------------------------------
            # CHAT
            # ------------------------------------------------

            if message_type == "chat":

                room_user.last_seen = time.time()

                text = sanitize_message(
                    data.get("message", "")
                )

                if not text:

                    continue

                chat_message = {
                    "id": uuid.uuid4().hex,
                    "user_id": room_user_id,
                    "name": room_user.name,
                    "message": text,
                    "timestamp": int(time.time()),
                }

                room.messages.append(
                    chat_message
                )

                if len(room.messages) > MAX_CHAT_MESSAGES:

                    room.messages = room.messages[
                        -MAX_CHAT_MESSAGES:
                    ]

                room.updated_at = time.time()

                await broadcast_room(
                    room,
                    {
                        "type": "chat",
                        "message": chat_message,
                    },
                )

                continue

            # ------------------------------------------------
            # UNKNOWN MESSAGE
            # ------------------------------------------------

            await websocket.send_json(
                {
                    "type": "error",
                    "message": (
                        f"Неизвестный тип сообщения: "
                        f"{message_type}"
                    ),
                }
            )

    except WebSocketDisconnect:

        logger.info(
            "WebSocket отключён: room=%s user=%s",
            room_id,
            room_user_id,
        )

    except Exception:

        logger.exception(
            "Ошибка WebSocket: room=%s user=%s",
            room_id,
            room_user_id,
        )

    finally:

        # ----------------------------------------------------
        # REMOVE USER
        # ----------------------------------------------------

        if room_user_id:

            async with rooms_lock:

                room = rooms.get(room_id)

                if room:

                    room.users.pop(
                        room_user_id,
                        None,
                    )

                    room.updated_at = time.time()

                    if (
                        room.controller_id
                        == room_user_id
                    ):

                        if room.users:

                            room.controller_id = next(
                                iter(room.users.keys())
                            )

                        else:

                            room.controller_id = None

                    logger.info(
                        "Пользователь вышел: room=%s user=%s",
                        room_id,
                        room_user_id,
                    )

                    # ------------------------------------------------
                    # BROADCAST LEAVE
                    # ------------------------------------------------

                    try:

                        await broadcast_room(
                            room,
                            {
                                "type": "user_left",
                                "user_id": room_user_id,
                            },
                        )

                    except Exception:

                        logger.exception(
                            "Ошибка broadcast user_left"
                        )


# ============================================================
# ROOM CLEANUP
# ============================================================

async def room_cleanup_loop():

    while True:

        try:

            await asyncio.sleep(
                ROOM_CLEANUP_INTERVAL
            )

            now = time.time()

            async with rooms_lock:

                rooms_to_delete = []

                for room_id, room in rooms.items():

                    # ------------------------------------------------
                    # DELETE EMPTY OLD ROOMS
                    # ------------------------------------------------

                    if (
                        not room.users
                        and now - room.updated_at
                        > EMPTY_ROOM_LIFETIME
                    ):

                        rooms_to_delete.append(
                            room_id
                        )

                        continue

                    # ------------------------------------------------
                    # REMOVE STALE USERS
                    # ------------------------------------------------

                    stale_users = []

                    for user_id, user in room.users.items():

                        if (
                            now - user.last_seen
                            > USER_TIMEOUT
                        ):

                            stale_users.append(
                                user_id
                            )

                    for user_id in stale_users:

                        room.users.pop(
                            user_id,
                            None,
                        )

                        try:

                            await broadcast_room(
                                room,
                                {
                                    "type": "user_left",
                                    "user_id": user_id,
                                },
                            )

                        except Exception:

                            logger.exception(
                                "Ошибка stale user broadcast"
                            )

                # ----------------------------------------------------
                # DELETE ROOMS
                # ----------------------------------------------------

                for room_id in rooms_to_delete:

                    rooms.pop(
                        room_id,
                        None,
                    )

                    logger.info(
                        "Удалена пустая комната: %s",
                        room_id,
                    )

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "Ошибка room cleanup loop"
            )


# ============================================================
# TELEGRAM BOT KEYBOARD
# ============================================================

search_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="🔍 Поискать другой фильм"
            )
        ]
    ],
    resize_keyboard=True,
)


# ============================================================
# /START
# ============================================================

@dp.message(Command("start"))
async def start_handler(message):

    args = ""

    if message.text:

        parts = message.text.split(
            maxsplit=1
        )

        if len(parts) > 1:

            args = parts[1].strip()

    # --------------------------------------------------------
    # FRIEND ROOM DEEP LINK
    # --------------------------------------------------------

    if args.startswith("room_"):

        room_data = args[5:]

        parts = room_data.split("_")

        if len(parts) >= 2:

            room_id = parts[0]
            kp_id = parts[1]

            if (
                validate_room_id(room_id)
                and validate_kp_id(kp_id)
            ):

                webapp_url = (
                    f"{NETLIFY_URL}/"
                    f"?kp_id={kp_id}"
                    f"&mode=friends"
                    f"&room={room_id}"
                )

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🎬 Войти в комнату",
                                web_app=WebAppInfo(
                                    url=webapp_url
                                ),
                            )
                        ]
                    ]
                )

                await message.answer(
                    "🎬 Тебя пригласили смотреть "
                    "фильм вместе с друзьями.",
                    reply_markup=keyboard,
                )

                return

    # --------------------------------------------------------
    # NORMAL START
    # --------------------------------------------------------

    await message.answer(
        "🎬 <b>FilmBot</b>\n\n"
        "Найди фильм и смотри его один "
        "или вместе с друзьями.\n\n"
        "Просто отправь мне название фильма.",
        parse_mode="HTML",
        reply_markup=search_keyboard,
    )


# ============================================================
# SEARCH BUTTON
# ============================================================

@dp.message(
    F.text == "🔍 Поискать другой фильм"
)
async def search_again_handler(message):

    await message.answer(
        "🔍 Напиши название фильма:",
        reply_markup=search_keyboard,
    )


# ============================================================
# FILM SEARCH HANDLER
# ============================================================

@dp.message(F.text)
async def film_search_handler(message):

    text = (
        message.text
        if message.text
        else ""
    ).strip()

    if not text:
        return

    # --------------------------------------------------------
    # IGNORE COMMANDS
    # --------------------------------------------------------

    if text.startswith("/"):
        return

    # --------------------------------------------------------
    # IGNORE BUTTON
    # --------------------------------------------------------

    if text == "🔍 Поискать другой фильм":
        return

    logger.info(
        "Поиск фильма: user=%s query=%s",
        message.from_user.id,
        text,
    )

    waiting_message = await message.answer(
        "🔎 Ищу фильм..."
    )

    films = await search_kinopoisk(
        text
    )

    # --------------------------------------------------------
    # NO RESULTS
    # --------------------------------------------------------

    if not films:

        await waiting_message.edit_text(
            "😔 Ничего не нашёл.\n\n"
            "Попробуй другое название."
        )

        return

    # --------------------------------------------------------
    # BUILD BUTTONS
    # --------------------------------------------------------

    buttons = []

    for film in films:

        film_id = film["id"]

        title = film["name"]

        year = film["year"]

        button_text = title

        if year:

            button_text += f" ({year})"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=button_text[:64],
                    callback_data=(
                        f"sel_film:{film_id}"
                    ),
                )
            ]
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=buttons
    )

    await waiting_message.edit_text(
        "🎬 <b>Нашёл фильмы:</b>\n\n"
        "Выбери нужный фильм:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ============================================================
# FILM SELECTION
# ============================================================

@dp.callback_query(
    F.data.startswith("sel_film:")
)
async def select_film_handler(
    callback: CallbackQuery
):

    try:

        await callback.answer()

    except Exception:

        logger.exception(
            "Ошибка callback.answer()"
        )

    # --------------------------------------------------------
    # FILM ID
    # --------------------------------------------------------

    try:

        film_id = (
            callback.data
            .split(":", 1)[1]
            .strip()
        )

    except Exception:

        await callback.message.answer(
            "❌ Не удалось определить фильм."
        )

        return

    if not validate_kp_id(film_id):

        logger.error(
            "Некорректный film_id: %s",
            film_id,
        )

        await callback.message.answer(
            "❌ Некорректный ID фильма."
        )

        return

    # --------------------------------------------------------
    # CREATE ROOM
    # --------------------------------------------------------

    room_id = create_room_id()

    logger.info(
        "Фильм выбран: user=%s film=%s room=%s",
        callback.from_user.id,
        film_id,
        room_id,
    )

    # --------------------------------------------------------
    # SOLO URL
    # --------------------------------------------------------

    solo_url = (
        f"{NETLIFY_URL}/"
        f"?kp_id={film_id}"
        f"&mode=solo"
    )

    # --------------------------------------------------------
    # FRIENDS URL
    # --------------------------------------------------------

    friends_url = (
        f"{NETLIFY_URL}/"
        f"?kp_id={film_id}"
        f"&mode=friends"
        f"&room={room_id}"
    )

    # --------------------------------------------------------
    # BUTTONS
    # --------------------------------------------------------

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Смотреть одному",
                    web_app=WebAppInfo(
                        url=solo_url
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Смотреть с друзьями",
                    web_app=WebAppInfo(
                        url=friends_url
                    ),
                )
            ],
        ]
    )

    # --------------------------------------------------------
    # EDIT MESSAGE
    # --------------------------------------------------------

    try:

        await callback.message.edit_text(
            "🎬 <b>Фильм выбран!</b>\n\n"
            "Как хочешь смотреть?",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    except Exception:

        logger.exception(
            "Ошибка редактирования сообщения "
            "после выбора фильма"
        )

        # ----------------------------------------------------
        # FALLBACK
        # ----------------------------------------------------

        try:

            await callback.message.answer(
                "🎬 <b>Фильм выбран!</b>\n\n"
                "Как хочешь смотреть?",
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        except Exception:

            logger.exception(
                "Fallback отправки сообщения тоже не удался"
            )


# ============================================================
# STARTUP LOGGING
# ============================================================

logger.info(
    "FilmBot main.py загружен."
)
@app.post("/api/auth")
async def api_auth(data: dict):
    init_data = data.get("initData", "")
    if not validate_init_data(init_data):
        raise HTTPException(status_code=401, detail="Invalid initData")
    user = get_user_from_init_data(init_data)
    return {"ok": True, "user": user}