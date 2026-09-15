import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import parse_qsl

import aiohttp
import uvicorn

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CallbackQuery,
)


# ============================================================
# CONFIG
# ============================================================

# CONFIG
BOT_TOKEN = os.getenv("BOT_TOKEN")
KP_API_TOKEN = os.getenv("KP_API_TOKEN")

# URL твоего Netlify Mini App
NETLIFY_URL = os.getenv("NETLIFY_URL", "https://kinopoiskov1k.netlify.app")

# Порт сервера
PORT = int(os.getenv("PORT", "8000"))

# Максимум пользователей в одной комнате
MAX_ROOM_USERS = 20

# Через сколько секунд считать пользователя отключенным
USER_TIMEOUT = 60

# Максимальная длина сообщения
MAX_MESSAGE_LENGTH = 500

# Максимальное количество сообщений, хранимых в памяти комнаты
MAX_CHAT_MESSAGES = 100


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("kino-bot")


# ============================================================
# VALIDATION
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не установлен. "
        "Создай переменную окружения BOT_TOKEN."
    )

if not KP_API_TOKEN:
    raise RuntimeError(
        "KP_API_TOKEN не установлен. "
        "Создай переменную окружения KP_API_TOKEN."
    )


# ============================================================
# TELEGRAM BOT
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Kinopoisk Telegram Mini App API",
    version="1.0.0",
)


# Разрешаем запросы от Netlify Mini App.
# Для production можно будет ограничить конкретным доменом.
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

    # Текущее состояние плеера
    playing: bool = False
    position: float = 0.0

    # Кто последний изменил состояние
    controller_id: Optional[str] = None

    # Пользователи
    users: Dict[str, RoomUser] = field(default_factory=dict)

    # История чата
    messages: List[dict] = field(default_factory=list)

    # Время последнего изменения
    updated_at: float = field(default_factory=time.time)


# ============================================================
# GLOBAL ROOMS STORAGE
# ============================================================

rooms: Dict[str, RoomState] = {}

rooms_lock = asyncio.Lock()


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def create_room_id() -> str:
    """
    Создает короткий ID комнаты.
    Например: A7F92C
    """
    return uuid.uuid4().hex[:6].upper()


def clean_room_id(room_id: str) -> str:
    """
    Очищает и проверяет ID комнаты.
    """
    room_id = str(room_id).strip().upper()

    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"

    if not room_id:
        raise ValueError("Пустой ID комнаты.")

    if len(room_id) > 32:
        raise ValueError("Слишком длинный ID комнаты.")

    if any(char not in allowed for char in room_id):
        raise ValueError("Недопустимый ID комнаты.")

    return room_id


def clean_kp_id(kp_id: str) -> str:
    """
    Проверяет Kinopoisk ID.
    """
    kp_id = str(kp_id).strip()

    if not kp_id.isdigit():
        raise ValueError("Kinopoisk ID должен быть числом.")

    if len(kp_id) > 20:
        raise ValueError("Некорректный Kinopoisk ID.")

    return kp_id


def sanitize_name(name: str) -> str:
    """
    Безопасное имя пользователя.
    """
    name = str(name or "Зритель").strip()

    if not name:
        return "Зритель"

    return name[:50]


def sanitize_message(text: str) -> str:
    """
    Безопасное сообщение.
    """
    text = str(text or "").strip()

    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH]

    return text


# ============================================================
# TELEGRAM MINI APP INIT DATA VALIDATION
# ============================================================

def validate_telegram_init_data(init_data: str) -> Optional[dict]:
    """
    Проверяет Telegram WebApp initData.

    JS:
        Telegram.WebApp.initData

    Python:
        validate_telegram_init_data(initData)

    Возвращает данные пользователя либо None.
    """

    if not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None

    received_hash = parsed.pop("hash", None)

    if not received_hash:
        return None

    # Формируем data-check-string
    data_check_string = "\n".join(
        f"{key}={parsed[key]}"
        for key in sorted(parsed.keys())
    )

    # Telegram WebApp secret key
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

    if not hmac.compare_digest(
        calculated_hash,
        received_hash,
    ):
        return None

    # Проверяем возраст initData.
    # 24 часа достаточно для нашего приложения.
    auth_date = parsed.get("auth_date")

    if auth_date:
        try:
            auth_timestamp = int(auth_date)

            if time.time() - auth_timestamp > 86400:
                return None

        except ValueError:
            return None

    user_data = {}

    if parsed.get("user"):
        try:
            user_data = json.loads(parsed["user"])
        except Exception:
            user_data = {}

    return {
        "query_id": parsed.get("query_id"),
        "auth_date": parsed.get("auth_date"),
        "user": user_data,
    }


# ============================================================
# API USER
# ============================================================

def get_user_from_init_data(init_data: Optional[str]) -> dict:
    """
    Получает Telegram пользователя.

    Если Mini App запущен внутри Telegram —
    возвращаем реального пользователя.

    Для разработки разрешаем fallback.
    """

    if not init_data:
        return {
            "id": "anonymous",
            "first_name": "Зритель",
            "username": None,
        }

    result = validate_telegram_init_data(init_data)

    if not result:
        raise HTTPException(
            status_code=401,
            detail="Недействительные Telegram initData.",
        )

    user = result.get("user") or {}

    return {
        "id": str(user.get("id", "anonymous")),
        "first_name": sanitize_name(
            user.get("first_name", "Зритель")
        ),
        "username": user.get("username"),
    }


# ============================================================
# ROOM FUNCTIONS
# ============================================================

async def get_or_create_room(
    room_id: str,
    kp_id: str,
) -> RoomState:

    room_id = clean_room_id(room_id)
    kp_id = clean_kp_id(kp_id)

    async with rooms_lock:

        if room_id not in rooms:

            rooms[room_id] = RoomState(
                room_id=room_id,
                kp_id=kp_id,
            )

            logger.info(
                "Создана комната %s для фильма %s",
                room_id,
                kp_id,
            )

        else:

            room = rooms[room_id]

            # Защита:
            # нельзя открыть ту же комнату с другим фильмом.
            if room.kp_id != kp_id:
                raise ValueError(
                    "Эта комната уже привязана к другому фильму."
                )

        return rooms[room_id]


async def broadcast_room(
    room: RoomState,
    data: dict,
    exclude_user: Optional[str] = None,
):
    """
    Отправляет сообщение всем пользователям комнаты.
    """

    dead_users = []

    for user_id, user in list(room.users.items()):

        if exclude_user and user_id == exclude_user:
            continue

        try:
            await user.websocket.send_json(data)

        except Exception as e:

            logger.warning(
                "Ошибка отправки пользователю %s: %s",
                user_id,
                e,
            )

            dead_users.append(user_id)

    for user_id in dead_users:
        room.users.pop(user_id, None)


async def broadcast_room_state(room: RoomState):
    """
    Отправляет актуальное состояние плеера всем.
    """

    await broadcast_room(
        room,
        {
            "type": "player_state",
            "room": room.room_id,
            "kp_id": room.kp_id,
            "playing": room.playing,
            "position": room.position,
            "controller_id": room.controller_id,
            "server_time": time.time(),
        },
    )


# ============================================================
# ROOM API
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Kinopoisk Telegram Mini App API",
        "version": "1.0.0",
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "rooms": len(rooms),
        "time": time.time(),
    }


@app.get("/api/room/{room_id}")
async def room_info(
    room_id: str,
    kp_id: str,
):
    """
    Получить состояние комнаты.
    """

    try:
        room = await get_or_create_room(
            room_id,
            kp_id,
        )

    except ValueError as e:

        raise HTTPException(
            status_code=400,
            detail=str(e),
        )

    return {
        "room": room.room_id,
        "kp_id": room.kp_id,
        "playing": room.playing,
        "position": room.position,
        "controller_id": room.controller_id,
        "users": [
            {
                "id": user.user_id,
                "name": user.name,
            }
            for user in room.users.values()
        ],
        "messages": room.messages,
        "updated_at": room.updated_at,
    }


# ============================================================
# SEARCH API
# ============================================================

async def kinopoisk_search(
    query: str,
    page: int = 1,
):
    """
    Поиск фильмов через Kinopoisk API.
    """

    query = query.strip()

    if not query:
        return []

    if len(query) > 100:
        query = query[:100]

    url = (
        "https://kinopoiskapiunofficial.tech/"
        "api/v2.1/films/search-by-keyword"
    )

    params = {
        "keyword": query,
        "page": page,
    }

    headers = {
        "X-API-KEY": KP_API_TOKEN,
        "Accept": "application/json",
    }

    timeout = aiohttp.ClientTimeout(
        total=15
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        try:

            async with session.get(
                url,
                params=params,
                headers=headers,
            ) as response:

                if response.status != 200:

                    logger.error(
                        "Kinopoisk API status: %s",
                        response.status,
                    )

                    return []

                data = await response.json()

        except asyncio.TimeoutError:

            logger.error(
                "Kinopoisk API timeout"
            )

            return []

        except aiohttp.ClientError as e:

            logger.error(
                "Kinopoisk API connection error: %s",
                e,
            )

            return []

        except Exception as e:

            logger.exception(
                "Kinopoisk API error: %s",
                e,
            )

            return []

    films = (
        data.get("films")
        or data.get("items")
        or []
    )

    result = []

    for film in films:

        film_id = (
            film.get("kinopoiskId")
            or film.get("filmId")
            or film.get("id")
        )

        if not film_id:
            continue

        name = (
            film.get("nameRu")
            or film.get("nameEn")
            or film.get("nameOriginal")
            or "Без названия"
        )

        year = film.get("year") or "—"

        poster = (
            film.get("posterUrlPreview")
            or film.get("posterUrl")
            or ""
        )

        description = (
            film.get("description")
            or ""
        )

        result.append(
            {
                "id": str(film_id),
                "name": str(name),
                "year": str(year),
                "poster": poster,
                "description": description,
            }
        )

    return result[:20]


@app.get("/api/search")
async def search_api(
    q: str,
    page: int = 1,
):
    """
    Поиск из Mini App.

    GET /api/search?q=Interstellar
    """

    if not q.strip():
        return {
            "success": True,
            "results": [],
        }

    if page < 1:
        page = 1

    if page > 20:
        page = 20

    results = await kinopoisk_search(
        q,
        page,
    )

    return {
        "success": True,
        "query": q,
        "page": page,
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
    """
    WebSocket:

        ws://SERVER/ws/room/ROOM_ID

    Основной канал Watch Party.
    """

    await websocket.accept()

    room: Optional[RoomState] = None
    user_id: Optional[str] = None

    try:

        # ----------------------------------------------------
        # Первое сообщение от клиента
        # ----------------------------------------------------

        try:
            first_message = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=15,
            )

        except asyncio.TimeoutError:

            await websocket.close(
                code=4000,
                reason="Не получены данные пользователя.",
            )

            return

        if first_message.get("type") != "join":

            await websocket.close(
                code=4001,
                reason="Первое сообщение должно быть join.",
            )

            return

        kp_id = str(
            first_message.get("kp_id", "")
        )

        init_data = first_message.get(
            "initData"
        )

        client_name = sanitize_name(
            first_message.get(
                "name",
                "Зритель",
            )
        )

        # ----------------------------------------------------
        # Проверяем Telegram пользователя
        # ----------------------------------------------------

        if init_data:

            try:

                telegram_user = get_user_from_init_data(
                    init_data
                )

                user_id = telegram_user["id"]

                if telegram_user["first_name"]:
                    client_name = telegram_user["first_name"]

            except HTTPException:

                await websocket.send_json(
                    {
                        "type": "error",
                        "message": (
                            "Не удалось проверить "
                            "Telegram пользователя."
                        ),
                    }
                )

                await websocket.close(
                    code=4003,
                    reason="Invalid Telegram initData",
                )

                return

        else:

            # Для локальной разработки
            user_id = (
                first_message.get("user_id")
                or uuid.uuid4().hex
            )

            user_id = str(user_id)

        # ----------------------------------------------------
        # Комната
        # ----------------------------------------------------

        try:

            room = await get_or_create_room(
                room_id,
                kp_id,
            )

        except ValueError as e:

            await websocket.send_json(
                {
                    "type": "error",
                    "message": str(e),
                }
            )

            await websocket.close(
                code=4004,
                reason=str(e),
            )

            return

        # ----------------------------------------------------
        # Проверяем лимит
        # ----------------------------------------------------

        if (
            user_id not in room.users
            and len(room.users) >= MAX_ROOM_USERS
        ):

            await websocket.send_json(
                {
                    "type": "error",
                    "message": (
                        "Комната заполнена."
                    ),
                }
            )

            await websocket.close(
                code=4005,
                reason="Room is full",
            )

            return

        # ----------------------------------------------------
        # Если пользователь уже подключен,
        # заменяем старое соединение
        # ----------------------------------------------------

        old_user = room.users.get(user_id)

        if old_user:

            try:
                await old_user.websocket.close()

            except Exception:
                pass

        room.users[user_id] = RoomUser(
            user_id=user_id,
            name=client_name,
            websocket=websocket,
        )

        # ----------------------------------------------------
        # Отправляем состояние новому пользователю
        # ----------------------------------------------------

        await websocket.send_json(
            {
                "type": "room_state",
                "room": room.room_id,
                "kp_id": room.kp_id,
                "playing": room.playing,
                "position": room.position,
                "controller_id": room.controller_id,
                "users": [
                    {
                        "id": user.user_id,
                        "name": user.name,
                    }
                    for user in room.users.values()
                ],
                "messages": room.messages,
                "server_time": time.time(),
            }
        )

        # ----------------------------------------------------
        # Уведомляем остальных
        # ----------------------------------------------------

        await broadcast_room(
            room,
            {
                "type": "user_joined",
                "user": {
                    "id": user_id,
                    "name": client_name,
                },
                "users_count": len(room.users),
            },
            exclude_user=user_id,
        )

        logger.info(
            "User %s joined room %s",
            client_name,
            room.room_id,
        )

        # ====================================================
        # MESSAGE LOOP
        # ====================================================

        while True:

            data = await websocket.receive_json()

            if not isinstance(data, dict):
                continue

            # Обновляем last_seen
            current_user = room.users.get(user_id)

            if current_user:
                current_user.last_seen = time.time()

            message_type = data.get("type")

            # ------------------------------------------------
            # PING
            # ------------------------------------------------

            if message_type == "ping":

                await websocket.send_json(
                    {
                        "type": "pong",
                        "server_time": time.time(),
                    }
                )

                continue

            # ------------------------------------------------
            # PLAY
            # ------------------------------------------------

            if message_type == "play":

                try:
                    position = float(
                        data.get(
                            "position",
                            room.position,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    position = room.position

                position = max(
                    0.0,
                    position,
                )

                room.position = position
                room.playing = True
                room.controller_id = user_id
                room.updated_at = time.time()

                await broadcast_room(
                    room,
                    {
                        "type": "play",
                        "position": room.position,
                        "controller_id": user_id,
                        "server_time": time.time(),
                    },
                )

                continue

            # ------------------------------------------------
            # PAUSE
            # ------------------------------------------------

            if message_type == "pause":

                try:
                    position = float(
                        data.get(
                            "position",
                            room.position,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    position = room.position

                room.position = max(
                    0.0,
                    position,
                )

                room.playing = False
                room.controller_id = user_id
                room.updated_at = time.time()

                await broadcast_room(
                    room,
                    {
                        "type": "pause",
                        "position": room.position,
                        "controller_id": user_id,
                        "server_time": time.time(),
                    },
                )

                continue

            # ------------------------------------------------
            # SEEK
            # ------------------------------------------------

            if message_type == "seek":

                try:
                    position = float(
                        data.get(
                            "position",
                            0,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    continue

                position = max(
                    0.0,
                    position,
                )

                room.position = position
                room.controller_id = user_id
                room.updated_at = time.time()

                await broadcast_room(
                    room,
                    {
                        "type": "seek",
                        "position": room.position,
                        "controller_id": user_id,
                        "server_time": time.time(),
                    },
                )

                continue

            # ------------------------------------------------
            # TIME UPDATE
            # ------------------------------------------------

            if message_type == "time_update":

                try:

                    position = float(
                        data.get(
                            "position",
                            room.position,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    continue

                room.position = max(
                    0.0,
                    position,
                )

                room.updated_at = time.time()

                # Не надо отправлять каждый timeupdate
                # абсолютно всем каждую миллисекунду.
                # Отправляем только по запросу sync.
                continue

            # ------------------------------------------------
            # REQUEST SYNC
            # ------------------------------------------------

            if message_type == "request_sync":

                await websocket.send_json(
                    {
                        "type": "player_state",
                        "room": room.room_id,
                        "kp_id": room.kp_id,
                        "playing": room.playing,
                        "position": room.position,
                        "controller_id": room.controller_id,
                        "server_time": time.time(),
                    }
                )

                continue

            # ------------------------------------------------
            # CHAT
            # ------------------------------------------------

            if message_type == "chat":

                text = sanitize_message(
                    data.get("text", "")
                )

                if not text:
                    continue

                chat_message = {
                    "id": uuid.uuid4().hex,
                    "user_id": user_id,
                    "name": client_name,
                    "text": text,
                    "timestamp": time.time(),
                }

                room.messages.append(
                    chat_message
                )

                # Ограничиваем историю
                if len(room.messages) > MAX_CHAT_MESSAGES:

                    room.messages = room.messages[
                        -MAX_CHAT_MESSAGES:
                    ]

                await broadcast_room(
                    room,
                    {
                        "type": "chat",
                        "message": chat_message,
                    },
                )

                continue

            # ------------------------------------------------
            # USER NAME
            # ------------------------------------------------

            if message_type == "set_name":

                new_name = sanitize_name(
                    data.get(
                        "name",
                        client_name,
                    )
                )

                client_name = new_name

                if user_id in room.users:

                    room.users[user_id].name = (
                        new_name
                    )

                await broadcast_room(
                    room,
                    {
                        "type": "user_updated",
                        "user": {
                            "id": user_id,
                            "name": new_name,
                        },
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
            "User disconnected from room %s",
            room_id,
        )

    except Exception as e:

        logger.exception(
            "WebSocket error: %s",
            e,
        )

    finally:

        # ----------------------------------------------------
        # Удаляем пользователя
        # ----------------------------------------------------

        if room and user_id:

            current = room.users.get(
                user_id
            )

            if (
                current
                and current.websocket is websocket
            ):

                room.users.pop(
                    user_id,
                    None,
                )

                await broadcast_room(
                    room,
                    {
                        "type": "user_left",
                        "user_id": user_id,
                        "users_count": len(
                            room.users
                        ),
                    },
                )

                logger.info(
                    "User %s left room %s",
                    user_id,
                    room.room_id,
                )


# ============================================================
# ROOM CLEANUP
# ============================================================

async def room_cleanup_loop():
    """
    Периодически удаляет пустые комнаты.
    """

    while True:

        try:

            await asyncio.sleep(60)

            current_time = time.time()

            async with rooms_lock:

                to_delete = []

                for room_id, room in rooms.items():

                    if room.users:
                        continue

                    if (
                        current_time
                        - room.updated_at
                        > 3600
                    ):

                        to_delete.append(
                            room_id
                        )

                for room_id in to_delete:

                    rooms.pop(
                        room_id,
                        None,
                    )

                    logger.info(
                        "Удалена пустая комната %s",
                        room_id,
                    )

        except asyncio.CancelledError:

            break

        except Exception as e:

            logger.exception(
                "Room cleanup error: %s",
                e,
            )


# ============================================================
# TELEGRAM KEYBOARD
# ============================================================

def get_main_keyboard():

    return ReplyKeyboardMarkup(
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
async def cmd_start(
    message: types.Message,
):

    args = message.text.split()

    # --------------------------------------------------------
    # Приглашение в комнату
    # --------------------------------------------------------

    if (
        len(args) > 1
        and args[1].startswith("room_")
    ):

        parts = args[1].split("_")

        if len(parts) >= 3:

            try:

                room_id = clean_room_id(
                    parts[1]
                )

                kp_id = clean_kp_id(
                    parts[2]
                )

            except ValueError:

                await message.answer(
                    "❌ Некорректная ссылка "
                    "на комнату."
                )

                return

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
                            text=(
                                "🍿 "
                                "Присоединиться "
                                "к просмотру"
                            ),
                            web_app=WebAppInfo(
                                url=webapp_url
                            ),
                        )
                    ]
                ]
            )

            await message.answer(
                "👋 Тебя пригласили "
                "в совместную комнату!",
                reply_markup=keyboard,
            )

            return

    # --------------------------------------------------------
    # Обычный /start
    # --------------------------------------------------------

    await message.answer(
        (
            "🎬 <b>Привет!</b>\n\n"
            "Отправь название фильма или сериала, "
            "и я попробую найти его в базе."
        ),
        reply_markup=get_main_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# SEARCH BUTTON
# ============================================================

@dp.message(
    F.text == "🔍 Поискать другой фильм"
)
async def restart_search(
    message: types.Message,
):

    await message.answer(
        "🎬 Введи название фильма:",
        reply_markup=get_main_keyboard(),
    )


# ============================================================
# MOVIE SEARCH
# ============================================================

@dp.message(F.text)
async def search_movie(
    message: types.Message,
):

    query = message.text.strip()

    if not query:
        return

    if query.startswith("/"):
        return

    # --------------------------------------------------------
    # Показываем пользователю, что идет поиск
    # --------------------------------------------------------

    searching_message = await message.answer(
        "🔎 Ищу фильм..."
    )

    films = await kinopoisk_search(
        query
    )

    # --------------------------------------------------------
    # Удаляем сообщение "Ищу..."
    # --------------------------------------------------------

    try:

        await searching_message.delete()

    except Exception:
        pass

    # --------------------------------------------------------
    # Ничего не найдено
    # --------------------------------------------------------

    if not films:

        await message.answer(
            "❌ Ничего не найдено.\n\n"
            "Попробуй написать название "
            "по-другому."
        )

        return

    # --------------------------------------------------------
    # Формируем кнопки
    # --------------------------------------------------------

    keyboard_buttons = []

    for film in films[:10]:

        film_id = film["id"]

        name = film["name"]

        year = film["year"]

        button_text = (
            f"{name} ({year})"
        )

        # Telegram ограничивает длину текста
        button_text = button_text[:60]

        keyboard_buttons.append(
            [
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=(
                        f"sel_film:{film_id}"
                    ),
                )
            ]
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=keyboard_buttons
    )

    await message.answer(
        (
            "🎬 <b>Результаты поиска</b>\n\n"
            f"Запрос: "
            f"<b>{query[:100]}</b>"
        ),
        reply_markup=keyboard,
        parse_mode="HTML",
    )


# ============================================================
# SELECT FILM
# ============================================================

@dp.callback_query(
    F.data.startswith("sel_film:")
)
async def select_view_mode(
    callback: CallbackQuery,
):

    film_id = (
        callback.data.split(":", 1)[1]
    )

    try:

        film_id = clean_kp_id(
            film_id
        )

    except ValueError:

        await callback.answer(
            "Некорректный ID фильма.",
            show_alert=True,
        )

        return

    # --------------------------------------------------------
    # Создаем комнату
    # --------------------------------------------------------

    room_id = create_room_id()

    # --------------------------------------------------------
    # Solo
    # --------------------------------------------------------

    solo_url = (
        f"{NETLIFY_URL}/"
        f"?kp_id={film_id}"
        f"&mode=solo"
    )

    # --------------------------------------------------------
    # Friends
    # --------------------------------------------------------

    friends_url = (
        f"{NETLIFY_URL}/"
        f"?kp_id={film_id}"
        f"&mode=friends"
        f"&room={room_id}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👤 Смотреть одному",
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

    await callback.message.edit_text(
        (
            "🎬 <b>Фильм выбран</b>\n\n"
            "Как ты хочешь смотреть?"
        ),
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    await callback.answer()


# ============================================================
# SERVER + BOT STARTUP
# ============================================================

async def start_web_server():

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )

    server = uvicorn.Server(
        config
    )

    await server.serve()


async def main():

    logger.info(
        "======================================"
    )

    logger.info(
        "Starting Kinopoisk Telegram Bot"
    )

    logger.info(
        "Mini App URL: %s",
        NETLIFY_URL,
    )

    logger.info(
        "API port: %s",
        PORT,
    )

    logger.info(
        "======================================"
    )

    cleanup_task = asyncio.create_task(
        room_cleanup_loop()
    )

    web_task = asyncio.create_task(
        start_web_server()
    )

    bot_task = asyncio.create_task(
        dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    )

    try:

        await asyncio.gather(
            web_task,
            bot_task,
        )

    except asyncio.CancelledError:

        logger.info(
            "Shutdown requested."
        )

    finally:

        cleanup_task.cancel()

        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

        await bot.session.close()

        logger.info(
            "Bot stopped."
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Stopped by user."
        )

