import os
import json
import asyncio
import logging
import pathlib
from collections import defaultdict

import httpx

from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
# не печатать URL с токеном в логах
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
TEXT_MODEL = os.getenv("TEXT_MODEL", "gemini-3.8-flash")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-3.1-flash-image")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render задаёт автоматически
PORT = int(os.getenv("PORT", "10000"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Vbn13256").lstrip("@").lower()
ENV_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}
ENV_NAMES = {x.strip().lstrip("@").lower() for x in os.getenv("ALLOWED_USERNAMES", "").split(",") if x.strip()}
DATA_FILE = pathlib.Path(os.getenv("DATA_FILE", "access.json"))
MAX_HISTORY = 20  # сообщений на пользователя

SYSTEM = os.getenv(
    "SYSTEM_PROMPT",
    """Ты универсальный ассистент в Telegram. Отвечай на языке пользователя.

Стиль:
- Сначала сразу дай прямой ответ, потом, если нужно, короткое пояснение.
- Пиши чётко и понятно, простыми словами, без воды и длинных вступлений.
- Не читай морали, не добавляй лишних предупреждений и дисклеймеров, не повторяй вопрос.
- Не отказывайся без веской причины: к собеседнику относись как ко взрослому человеку.
- Говори естественно и живо, можно с лёгким юмором; если уместно, допустима неформальная речь.
- Если вопрос спорный, дай честную оценку и разные точки зрения, а не уклончивый ответ.
- Если чего-то не знаешь или не уверен, так и скажи, не выдумывай.
- Если не хватает данных, задай один короткий уточняющий вопрос.
- Для инструкций используй короткие шаги, для кода готовый блок кода.

Отказывай только в том, что реально опасно: оружие массового поражения, сексуальный контент с участием несовершеннолетних, прямой вред людям. Отказ делай одной короткой фразой, без лекций.""",
)

SAFETY_LEVEL = os.getenv("SAFETY_LEVEL", "BLOCK_ONLY_HIGH")
# варианты: BLOCK_NONE, BLOCK_ONLY_HIGH, BLOCK_MEDIUM_AND_ABOVE, BLOCK_LOW_AND_ABOVE
SAFETY = [
    types.SafetySetting(category=c, threshold=SAFETY_LEVEL)
    for c in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )
]

client = genai.Client(api_key=GEMINI_KEY)
history: dict[int, list[types.Content]] = defaultdict(list)


# ---------- доступ ----------
def _load_state() -> dict:
    st = {"admin_id": None, "ids": [], "usernames": [], "notified": []}
    try:
        st.update(json.loads(DATA_FILE.read_text()))
    except Exception:
        pass
    return st


state = _load_state()


def save_state():
    try:
        DATA_FILE.write_text(json.dumps(state, ensure_ascii=False))
    except Exception as e:
        log.warning("Не удалось сохранить access.json: %s", e)


def is_admin(user) -> bool:
    uname = (user.username or "").lower()
    if uname == ADMIN_USERNAME:
        if state["admin_id"] != user.id:  # запоминаем id, чтобы писать админу
            state["admin_id"] = user.id
            save_state()
        return True
    return user.id == state["admin_id"]


def has_access(user) -> bool:
    if is_admin(user):
        return True
    uname = (user.username or "").lower()
    if user.id in state["ids"] or user.id in ENV_IDS:
        return True
    if uname and (uname in state["usernames"] or uname in ENV_NAMES):
        state["ids"].append(user.id)  # привязываем id к выданному юзернейму
        save_state()
        return True
    return False


async def check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if has_access(user):
        return True
    await update.effective_message.reply_text("⛔ У вас нет доступа к этому боту.")
    admin_id = state["admin_id"]
    if admin_id and user.id not in state["notified"]:
        state["notified"].append(user.id)
        save_state()
        uname = f"@{user.username}" if user.username else "без username"
        try:
            await ctx.bot.send_message(
                admin_id,
                f"Запрос доступа: {user.full_name} ({uname}), id {user.id}\n"
                f"Разрешить: /allow {user.id}",
            )
        except Exception as e:
            log.warning("Не удалось уведомить админа: %s", e)
    return False


def parse_target(arg: str):
    arg = arg.strip()
    if arg.lstrip("-").isdigit():
        return int(arg), None
    return None, arg.lstrip("@").lower()


async def admin_only(update: Update) -> bool:
    if is_admin(update.effective_user):
        return True
    await update.effective_message.reply_text("Команда только для админа.")
    return False


async def allow_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not ctx.args:
        await update.message.reply_text("Формат: /allow @username или /allow 123456789")
        return
    uid, name = parse_target(ctx.args[0])
    if uid:
        if uid not in state["ids"]:
            state["ids"].append(uid)
        text = f"✅ Доступ выдан: id {uid}"
        try:
            await ctx.bot.send_message(uid, "✅ Вам выдали доступ к боту. Напишите /start")
        except Exception:
            text += "\n(человек ещё не писал боту, уведомление не отправлено)"
    else:
        if name not in state["usernames"]:
            state["usernames"].append(name)
        text = f"✅ Доступ выдан: @{name}\nОн заработает, когда человек напишет боту."
    save_state()
    await update.message.reply_text(text)


async def deny_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not ctx.args:
        await update.message.reply_text("Формат: /deny @username или /deny 123456789")
        return
    uid, name = parse_target(ctx.args[0])
    if uid and uid in state["ids"]:
        state["ids"].remove(uid)
    if name and name in state["usernames"]:
        state["usernames"].remove(name)
    if name and name == ADMIN_USERNAME:
        await update.message.reply_text("Админа удалить нельзя.")
        return
    save_state()
    await update.message.reply_text("🚫 Доступ убран (если он был выдан командой /allow).")


async def users_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    ids = ", ".join(map(str, state["ids"])) or "—"
    names = ", ".join("@" + n for n in state["usernames"]) or "—"
    env = ", ".join([*map(str, ENV_IDS), *("@" + n for n in ENV_NAMES)]) or "—"
    await update.message.reply_text(
        f"Админ: @{ADMIN_USERNAME}\nПо id: {ids}\nПо username: {names}\nИз настроек Render: {env}"
    )


async def send_long(update: Update, text: str):
    text = text.strip() or "(пустой ответ)"
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


def remember(uid: int, user_text: str, bot_text: str):
    h = history[uid]
    h.append(types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))
    h.append(types.Content(role="model", parts=[types.Part.from_text(text=bot_text)]))
    del h[:-MAX_HISTORY]


async def ask(update: Update, parts: list[types.Part], note: str):
    """Отправить запрос в Gemini с историей и ответить текстом."""
    uid = update.effective_user.id
    await update.message.chat.send_action(ChatAction.TYPING)
    contents = history[uid] + [types.Content(role="user", parts=parts)]
    resp = await client.aio.models.generate_content(
        model=TEXT_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=SYSTEM, safety_settings=SAFETY),
    )
    answer = resp.text or "Не удалось получить ответ."
    remember(uid, note, answer)
    await send_long(update, answer)


async def draw(update: Update, prompt: str, source: types.Part | None = None):
    """Генерация / редактирование изображения."""
    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
    parts = [types.Part.from_text(text=prompt)]
    if source:
        parts.insert(0, source)
    resp = await client.aio.models.generate_content(
        model=IMAGE_MODEL,
        contents=parts,
        config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"], safety_settings=SAFETY),
    )
    sent = False
    for cand in resp.candidates or []:
        for p in cand.content.parts or []:
            if p.inline_data and p.inline_data.data:
                await update.message.reply_photo(p.inline_data.data)
                sent = True
            elif p.text:
                await send_long(update, p.text)
                sent = True
    if not sent:
        await update.message.reply_text("Не получилось сгенерировать (возможно, сработал фильтр).")


# ---------- команды ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check(update, ctx):
        return
    admin_note = ""
    if is_admin(update.effective_user):
        admin_note = ("\n\nАдмин:\n/allow @username или id — выдать доступ\n"
                      "/deny @username или id — забрать\n/users — список")
    await update.message.reply_text(
        "Привет! Я умею:\n"
        "• отвечать на текст\n"
        "• разбирать фото (пришли с подписью-вопросом)\n"
        "• читать файлы: PDF, TXT, CSV и др.\n"
        "• понимать голосовые\n"
        "• рисовать: /img кот в скафандре\n"
        "• править фото: фото + подпись «/img сделай фон ночным»\n\n"
        "/reset — очистить память диалога" + admin_note
    )


async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check(update, ctx):
        return
    history.pop(update.effective_user.id, None)
    await update.message.reply_text("Память очищена.")


async def img_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check(update, ctx):
        return
    prompt = " ".join(ctx.args)
    if not prompt:
        await update.message.reply_text("Напиши: /img описание картинки")
        return
    await draw(update, prompt)


# ---------- сообщения ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check(update, ctx):
        return
    text = update.message.text
    low = text.lower()
    if low.startswith(("нарисуй", "сгенерируй картинку", "сгенерируй изображение")):
        await draw(update, text)
        return
    await ask(update, [types.Part.from_text(text=text)], text)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check(update, ctx):
        return
    f = await update.message.photo[-1].get_file()
    data = bytes(await f.download_as_bytearray())
    part = types.Part.from_bytes(data=data, mime_type="image/jpeg")
    caption = update.message.caption or "Опиши это изображение."
    if caption.lower().startswith("/img"):
        await draw(update, caption[4:].strip() or "Улучши изображение", source=part)
        return
    await ask(update, [part, types.Part.from_text(text=caption)], f"[фото] {caption}")


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check(update, ctx):
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("Файл больше 20 МБ — Telegram не даёт боту его скачать.")
        return
    f = await doc.get_file()
    data = bytes(await f.download_as_bytearray())
    mime = doc.mime_type or "application/octet-stream"
    caption = update.message.caption or "Кратко перескажи содержимое файла."
    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        content = data.decode("utf-8", errors="replace")[:200_000]
        parts = [types.Part.from_text(text=f"Файл {doc.file_name}:\n{content}\n\n{caption}")]
    else:
        parts = [types.Part.from_bytes(data=data, mime_type=mime),
                 types.Part.from_text(text=caption)]
    await ask(update, parts, f"[файл {doc.file_name}] {caption}")


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check(update, ctx):
        return
    media = update.message.voice or update.message.audio
    f = await media.get_file()
    data = bytes(await f.download_as_bytearray())
    mime = getattr(media, "mime_type", None) or "audio/ogg"
    prompt = ("Это голосовое сообщение. Пойми, что в нём сказано, и ответь на него "
              "на том же языке. Если просят нарисовать — опиши словами, что нарисуешь.")
    await ask(update, [types.Part.from_bytes(data=data, mime_type=mime),
                       types.Part.from_text(text=prompt)], "[голосовое сообщение]")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("Ошибка", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Что-то пошло не так, попробуй ещё раз.")


KEEPALIVE_SECONDS = int(os.getenv("KEEPALIVE_SECONDS", "600"))  # 10 минут


async def keepalive():
    """Пингует свой публичный URL, чтобы Render не усыплял сервис."""
    await asyncio.sleep(60)
    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            try:
                r = await http.get(BASE_URL)
                log.info("keepalive: %s", r.status_code)
            except Exception as e:
                log.warning("keepalive failed: %s", e)
            await asyncio.sleep(KEEPALIVE_SECONDS)


async def post_init(app: Application):
    if BASE_URL:
        app.bot_data["keepalive_task"] = asyncio.create_task(keepalive())


def main():
    asyncio.set_event_loop(asyncio.new_event_loop())  # нужно для Python 3.14
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("img", img_cmd))
    app.add_handler(CommandHandler("allow", allow_cmd))
    app.add_handler(CommandHandler("deny", deny_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    if BASE_URL:  # на Render — webhook
        app.run_webhook(
            listen="0.0.0.0", port=PORT, url_path=TOKEN,
            webhook_url=f"{BASE_URL}/{TOKEN}", drop_pending_updates=True,
        )
    else:  # локально — polling
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
