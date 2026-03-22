"""
WAVARCHIVE Bot — merged
Combines:
  • Track upload → 5-step conversation → admin approval (inline buttons)
  • GitHub upload (MP3 + cover) + tracks.json update
  • Artist profiles, bio, photo, links
  • Subscriptions + subscriber notifications
  • Artist search, discography navigation
  • Optional Telegram channel posting on approval

Deploy: Railway  (Procfile → `worker: python bot.py`)
"""

import os, json, asyncio, logging, base64, re, time, string, random, sqlite3
from datetime import date
import urllib.request
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config (from Railway env vars) ────────────────────────────────────────────
BOT_TOKEN  = os.environ["BOT_TOKEN"]
# Один ID или несколько через запятую: "111,222,333"
ADMIN_IDS  = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", os.environ.get("ADMIN_ID", "0")).split(",")}
ADMIN_ID   = next(iter(ADMIN_IDS))  # первый — для обратной совместимости
GITHUB_TOKEN  = os.environ["GITHUB_TOKEN"]
GITHUB_OWNER  = os.environ.get("GITHUB_OWNER", "artevhr")
GITHUB_REPO   = os.environ.get("GITHUB_REPO",  "wavarchive-music")
SITE_URL      = os.environ.get("SITE_URL", f"https://{GITHUB_OWNER}.github.io/wavarchive-site/")
CHANNEL_ID         = os.environ.get("CHANNEL_ID", "")           # optional: @channel_username
RULES_LINK         = os.environ.get("RULES_LINK", "")           # optional
DB_PATH            = os.environ.get("DB_PATH", "database.db")
# Группа для модерации треков. Если не задана — бот пишет в личку ADMIN_ID.
MODERATION_CHAT_ID = int(os.environ.get("MODERATION_CHAT_ID", ADMIN_ID))

# ── ConversationHandler state IDs ─────────────────────────────────────────────
# Track upload
TITLE, ARTIST, ALBUM, COVER, FILE = range(5)
# Profile edit
P_NAME, P_BIO, P_PHOTO, P_LINKS = range(5, 9)

# ── In-memory pending submissions (cleared after approve/reject) ───────────────
pending: dict[int, dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS artists (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id           INTEGER UNIQUE NOT NULL,
                slug              TEXT UNIQUE NOT NULL,
                name              TEXT,
                bio               TEXT,
                photo_id          TEXT,
                links             TEXT,
                is_allowed        INTEGER DEFAULT 0,
                first_song        INTEGER DEFAULT 0,
                subscribers_count INTEGER DEFAULT 0,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tracks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                track_name   TEXT NOT NULL,
                artist_name  TEXT,
                album        TEXT,
                file_id      TEXT,
                github_path  TEXT,
                cover_path   TEXT,
                duration     INTEGER DEFAULT 0,
                channel_url  TEXT,
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                subscriber_id INTEGER NOT NULL,
                artist_id     INTEGER NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(subscriber_id, artist_id)
            );

            -- Auto-maintain subscriber count
            CREATE TRIGGER IF NOT EXISTS trg_sub_up
            AFTER INSERT ON subscriptions BEGIN
                UPDATE artists SET subscribers_count = subscribers_count + 1
                WHERE user_id = NEW.artist_id;
            END;

            CREATE TRIGGER IF NOT EXISTS trg_sub_down
            AFTER DELETE ON subscriptions BEGIN
                UPDATE artists SET subscribers_count = MAX(0, subscribers_count - 1)
                WHERE user_id = OLD.artist_id;
            END;
        """)
    logger.info("Database initialised at %s", DB_PATH)


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _gen_slug(length: int = 8) -> str:
    chars = string.ascii_letters + string.digits
    while True:
        slug = "".join(random.choice(chars) for _ in range(length))
        with _db() as conn:
            if not conn.execute("SELECT 1 FROM artists WHERE slug=?", (slug,)).fetchone():
                return slug


def ensure_artist(user_id: int) -> None:
    with _db() as conn:
        if not conn.execute("SELECT 1 FROM artists WHERE user_id=?", (user_id,)).fetchone():
            conn.execute("INSERT INTO artists (user_id, slug) VALUES (?,?)", (user_id, _gen_slug()))


def get_artist(user_id: int):
    with _db() as conn:
        return conn.execute("SELECT * FROM artists WHERE user_id=?", (user_id,)).fetchone()


def get_artist_by_slug(slug: str):
    with _db() as conn:
        return conn.execute("SELECT * FROM artists WHERE slug=?", (slug,)).fetchone()


def search_artists(query: str) -> list:
    q = query.replace(" ", "")
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM artists WHERE is_allowed=1 AND name IS NOT NULL AND name!='' "
            "AND REPLACE(LOWER(name),' ','') LIKE REPLACE(LOWER(?),' ','%') LIMIT 15",
            (q,),
        ).fetchall()


def get_subscriptions(user_id: int) -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT a.* FROM subscriptions s JOIN artists a ON s.artist_id=a.user_id "
            "WHERE s.subscriber_id=? ORDER BY s.created_at DESC",
            (user_id,),
        ).fetchall()


def is_subscribed(subscriber_id: int, artist_id: int) -> bool:
    with _db() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM subscriptions WHERE subscriber_id=? AND artist_id=?",
            (subscriber_id, artist_id),
        ).fetchone())


def subscribe(subscriber_id: int, artist_id: int) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions (subscriber_id, artist_id) VALUES (?,?)",
            (subscriber_id, artist_id),
        )


def unsubscribe(subscriber_id: int, artist_id: int) -> None:
    with _db() as conn:
        conn.execute(
            "DELETE FROM subscriptions WHERE subscriber_id=? AND artist_id=?",
            (subscriber_id, artist_id),
        )


def get_subscribers(artist_id: int) -> list[int]:
    with _db() as conn:
        return [
            r["subscriber_id"]
            for r in conn.execute(
                "SELECT subscriber_id FROM subscriptions WHERE artist_id=?", (artist_id,)
            ).fetchall()
        ]


def save_track(
    user_id: int,
    track_name: str,
    artist_name: str,
    album: str,
    file_id: str,
    github_path: str,
    cover_path: str | None,
    duration: int,
    channel_url: str = "",
) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO tracks "
            "(user_id, track_name, artist_name, album, file_id, github_path, cover_path, duration, channel_url) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, track_name, artist_name, album, file_id, github_path, cover_path, duration, channel_url),
        )
        conn.execute("UPDATE artists SET first_song=1 WHERE user_id=?", (user_id,))


def get_artist_tracks(user_id: int) -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM tracks WHERE user_id=? ORDER BY published_at DESC",
            (user_id,),
        ).fetchall()


# ══════════════════════════════════════════════════════════════════════════════
#  GITHUB API
# ══════════════════════════════════════════════════════════════════════════════

def _translit(s: str) -> str:
    table = {
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"yo","ж":"zh","з":"z",
        "и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
        "с":"s","т":"t","у":"u","ф":"f","х":"kh","ц":"ts","ч":"ch","ш":"sh",
        "щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
    }
    table.update({k.upper(): v for k, v in table.items()})
    result = "".join(table.get(c, c) for c in s)
    return re.sub(r"[^a-z0-9]+", "-", result.lower()).strip("-") or "track"


def _gh_request(path: str, method: str = "GET", body: dict | None = None) -> dict:
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "WavArchiveBot/2.0",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}") from e


async def _download_tg_file(bot, file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    with urllib.request.urlopen(tg_file.file_path, timeout=60) as r:
        return r.read()


async def upload_to_github(sub: dict, ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    """Upload MP3 (+ optional cover) → GitHub, patch tracks.json. Returns new track entry."""
    mp3_bytes = await _download_tg_file(ctx.bot, sub["file_id"])

    artist_slug = _translit(sub["artist"])
    title_slug  = _translit(sub["title"])
    ts          = int(time.time())
    mp3_path    = f"tracks/{artist_slug}/{title_slug}-{ts}.mp3"
    cover_path  = None

    # Upload MP3
    _gh_request(mp3_path, "PUT", {
        "message": f"Add: {sub['title']} by {sub['artist']}",
        "content": base64.b64encode(mp3_bytes).decode(),
    })
    logger.info("MP3 uploaded → %s", mp3_path)

    # Upload cover (if provided)
    if sub.get("cover_file_id"):
        cover_bytes = await _download_tg_file(ctx.bot, sub["cover_file_id"])
        ext = (sub.get("cover_name") or "cover.jpg").rsplit(".", 1)[-1].lower()
        cover_path = f"covers/{artist_slug}/{title_slug}-{ts}.{ext}"
        _gh_request(cover_path, "PUT", {
            "message": f"Cover: {sub['title']}",
            "content": base64.b64encode(cover_bytes).decode(),
        })
        logger.info("Cover uploaded → %s", cover_path)

    # Update tracks.json
    tracks_data = _gh_request("tracks.json")
    catalog     = json.loads(base64.b64decode(tracks_data["content"]).decode())
    sha_json    = tracks_data["sha"]

    new_track = {
        "id":          f"{artist_slug}_{title_slug}_{ts}",
        "title":       sub["title"],
        "artist":      sub["artist"],
        "album":       sub.get("album") or "",
        "genre":       "другое",
        "duration":    sub.get("duration", 0),
        "file":        mp3_path,
        "cover":       cover_path,
        "albumCover":  None,
        "description": "",
        "tags":        [],
        "addedAt":     date.today().isoformat(),
    }
    catalog.append(new_track)

    _gh_request("tracks.json", "PUT", {
        "message": f"Catalog: {sub['title']} by {sub['artist']}",
        "content": base64.b64encode(
            json.dumps(catalog, ensure_ascii=False, indent=2).encode()
        ).decode(),
        "sha": sha_json,
    })
    logger.info("tracks.json updated → %s", new_track["id"])
    return new_track


# ══════════════════════════════════════════════════════════════════════════════
#  UI HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _format_links(raw: str) -> str:
    if not raw:
        return ""
    KNOWN = {
        "t.me": "TG", "tiktok": "TT", "instagram": "IG",
        "youtube": "YT", "vk.com": "VK", "soundcloud": "SC",
        "spotify": "SP", "genius": "GS",
    }
    parts = []
    for link in raw.split("|"):
        link = link.strip()
        if not link:
            continue
        label = next((v for k, v in KNOWN.items() if k in link),
                     link.replace("https://", "").replace("http://", "").split("/")[0])
        parts.append(f"<a href='{link}'>{label}</a>")
    return "  |  ".join(parts)


def _artist_card_text(artist) -> str:
    text = f"👤 <b>{artist['name']}</b>"
    if artist["subscribers_count"] > 0:
        text += f"  •  👥 {artist['subscribers_count']}"
    text += "\n\n"
    if artist["bio"]:
        text += f"{artist['bio']}\n\n"
    if artist["links"]:
        text += f"🔗 {_format_links(artist['links'])}\n"
    return text


def _main_kb(is_artist: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("отправить трек 📥"), KeyboardButton("найти артиста 🔍")],
        [KeyboardButton("мои подписки 📋")],
    ]
    if is_artist:
        rows[1].append(KeyboardButton("моя карточка 👤"))
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def _send_main_menu(update: Update, text: str, is_artist: bool) -> None:
    await update.effective_message.reply_text(
        text, reply_markup=_main_kb(is_artist), parse_mode="HTML"
    )


async def _show_artist_card(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, slug: str, viewer_id: int
) -> None:
    artist = get_artist_by_slug(slug)
    if not artist or artist["is_allowed"] != 1:
        await update.effective_message.reply_text("❌ Артист не найден.")
        return

    text   = _artist_card_text(artist)
    subbed = is_subscribed(viewer_id, artist["user_id"])
    tracks = get_artist_tracks(artist["user_id"])

    sub_btn = InlineKeyboardButton(
        "❤️ отписаться" if subbed else "🤍 подписаться",
        callback_data=f"{'unsub' if subbed else 'sub'}_{artist['slug']}",
    )
    kb = [[sub_btn]]
    if tracks:
        kb.append([InlineKeyboardButton(
            f"💿 дискография ({len(tracks)})",
            callback_data=f"disc_{artist['user_id']}_0",
        )])

    markup = InlineKeyboardMarkup(kb)
    if artist["photo_id"]:
        await update.effective_message.reply_photo(
            artist["photo_id"], caption=text, reply_markup=markup, parse_mode="HTML"
        )
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_artist(uid)

    if ctx.args:
        await _show_artist_card(update, ctx, ctx.args[0], uid)
        return

    artist   = get_artist(uid)
    is_artist = artist and artist["first_song"] == 1
    if is_artist:
        me   = await ctx.bot.get_me()
        link = f"https://t.me/{me.username}?start={artist['slug']}"
        await _send_main_menu(
            update,
            f"🥀 <b>с возвращением!</b>\n\n🔗 твоя ссылка:\n<code>{link}</code>",
            True,
        )
    else:
        await _send_main_menu(update, "🥀 <b>добро пожаловать на WAVARCHIVE!</b>", False)


# ══════════════════════════════════════════════════════════════════════════════
#  TRACK UPLOAD CONVERSATION
# ══════════════════════════════════════════════════════════════════════════════

async def upload_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    rules = f"📋 <a href='{RULES_LINK}'>Правила загрузки</a>\n\n" if RULES_LINK else ""
    await update.message.reply_text(
        f"🎵 <b>Загрузка трека на WAVARCHIVE</b>\n\n{rules}"
        "1️⃣ Напиши <b>название трека</b>:",
        parse_mode="HTML",
    )
    return TITLE


async def upload_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["title"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ <b>{ctx.user_data['title']}</b>\n\n2️⃣ Напиши <b>имя артиста</b>:",
        parse_mode="HTML",
    )
    return ARTIST


async def upload_artist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["artist"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ <b>{ctx.user_data['artist']}</b>\n\n3️⃣ Напиши <b>альбом</b> (или «нет»):",
        parse_mode="HTML",
    )
    return ALBUM


async def upload_album(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip().lower()
    ctx.user_data["album"] = "" if val in ("нет", "no", "-", "none", ".") else update.message.text.strip()
    await update.message.reply_text(
        "4️⃣ Пришли <b>обложку</b> (фото или файл) или напиши «нет»:",
        parse_mode="HTML",
    )
    return COVER


async def upload_cover(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text and update.message.text.strip().lower() in ("нет", "no", "-", "none", "."):
        ctx.user_data["cover_file_id"] = None
        ctx.user_data["cover_name"]    = None
    elif update.message.photo:
        ctx.user_data["cover_file_id"] = update.message.photo[-1].file_id
        ctx.user_data["cover_name"]    = "cover.jpg"
    elif update.message.document:
        ctx.user_data["cover_file_id"] = update.message.document.file_id
        ctx.user_data["cover_name"]    = update.message.document.file_name or "cover.jpg"
    else:
        await update.message.reply_text("Пришли фото, файл-изображение или напиши «нет»:")
        return COVER

    await update.message.reply_text(
        "5️⃣ Последний шаг — пришли <b>файл трека</b> (MP3):",
        parse_mode="HTML",
    )
    return FILE


async def upload_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.audio:
        fid      = update.message.audio.file_id
        fname    = update.message.audio.file_name or f"{ctx.user_data.get('title', 'track')}.mp3"
        duration = update.message.audio.duration or 0
    elif update.message.document:
        fid      = update.message.document.file_id
        fname    = update.message.document.file_name or "track.mp3"
        duration = 0
    else:
        await update.message.reply_text("Пришли аудио-файл (MP3):")
        return FILE

    ctx.user_data.update({
        "file_id":   fid,
        "file_name": fname,
        "duration":  duration,
        "from_id":   update.effective_user.id,
        "from_name": update.effective_user.full_name,
    })
    d = ctx.user_data

    caption = (
        f"🎵 <b>Новый трек на проверку</b>\n\n"
        f"👤 От: {d['from_name']} (<code>{d['from_id']}</code>)\n"
        f"🎶 <b>Название:</b> {d['title']}\n"
        f"🎤 <b>Артист:</b> {d['artist']}\n"
        f"💿 <b>Альбом:</b> {d['album'] or '—'}\n"
        f"🕐 <b>Длина:</b> {duration}с\n"
        f"📁 <b>Файл:</b> {fname}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять",   callback_data=f"approve_{d['from_id']}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{d['from_id']}"),
    ]])

    if d.get("cover_file_id"):
        await ctx.bot.send_photo(MODERATION_CHAT_ID, d["cover_file_id"], caption="⬆️ Обложка трека")

    admin_msg = await ctx.bot.send_document(
        MODERATION_CHAT_ID, fid, caption=caption, reply_markup=kb, parse_mode="HTML"
    )
    pending[d["from_id"]] = {**d, "admin_msg_id": admin_msg.message_id}

    await update.message.reply_text(
        "✅ Трек отправлен на проверку!\n"
        "Как только админ рассмотрит заявку — ты получишь уведомление.\n\n"
        "Чтобы отправить ещё один — /start"
    )
    return ConversationHandler.END


async def upload_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено. /start чтобы начать заново.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  PROFILE EDIT CONVERSATION
# ══════════════════════════════════════════════════════════════════════════════

async def profile_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point via callback or text button."""
    uid = update.effective_user.id
    if not get_artist(uid) or get_artist(uid)["first_song"] == 0:
        msg = "❌ Профиль доступен после публикации первого трека."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            "✏️ <b>Редактирование профиля</b>\n\n1/4: напиши <b>имя артиста</b>:",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "✏️ <b>Редактирование профиля</b>\n\n1/4: напиши <b>имя артиста</b>:",
            parse_mode="HTML",
        )
    return P_NAME


async def profile_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    with _db() as conn:
        conn.execute("UPDATE artists SET name=? WHERE user_id=?", (update.message.text.strip(), uid))
    await update.message.reply_text("2/4: напиши <b>биографию</b> (пару строк о себе):", parse_mode="HTML")
    return P_BIO


async def profile_bio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    with _db() as conn:
        conn.execute("UPDATE artists SET bio=? WHERE user_id=?", (update.message.text.strip(), uid))
    await update.message.reply_text("3/4: отправь <b>фото профиля</b> (или «нет»):", parse_mode="HTML")
    return P_PHOTO


async def profile_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if update.message.photo:
        pid = update.message.photo[-1].file_id
        with _db() as conn:
            conn.execute("UPDATE artists SET photo_id=? WHERE user_id=?", (pid, uid))
    elif update.message.text and update.message.text.strip().lower() not in ("нет", "no", "-"):
        await update.message.reply_text("Пришли фото или напиши «нет»:")
        return P_PHOTO

    await update.message.reply_text(
        "4/4: отправь ссылки через <code>|</code>\n"
        "Пример: <code>https://t.me/you|https://soundcloud.com/you</code>\n"
        "Или напиши «нет»",
        parse_mode="HTML",
    )
    return P_LINKS


async def profile_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid  = update.effective_user.id
    val  = update.message.text.strip()
    links = "" if val.lower() in ("нет", "no", "-", "none", ".") else val
    with _db() as conn:
        conn.execute(
            "UPDATE artists SET links=?, is_allowed=1 WHERE user_id=?",
            (links, uid),
        )
    await update.message.reply_text("✅ Профиль обновлён!")
    artist = get_artist(uid)
    await _send_main_menu(update, "Что дальше?", artist and artist["first_song"] == 1)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    data      = query.data
    viewer_id = update.effective_user.id

    await query.answer()

    # ── Admin: approve ─────────────────────────────────────────────────────────
    if data.startswith("approve_"):
        if not await _is_moderator(ctx, viewer_id):
            await query.answer("Нет доступа", show_alert=True)
            return

        user_id = int(data.split("_", 1)[1])
        sub = pending.get(user_id)
        if not sub:
            await _edit_caption(query, "\n\n⚠️ Данные устарели — попроси отправить трек заново.")
            return

        await _edit_caption(query, "\n\n⏳ Загружаю в GitHub...")
        try:
            track = await upload_to_github(sub, ctx)

            channel_url = ""
            if CHANNEL_ID:
                try:
                    pub = await ctx.bot.send_audio(
                        CHANNEL_ID, sub["file_id"],
                        caption=(
                            f"🎵 <b>{sub['title']}</b> — {sub['artist']}\n\n"
                            f"🌐 <a href='{SITE_URL}'>WAVARCHIVE</a>"
                        ),
                        parse_mode="HTML",
                    )
                    channel_url = f"https://t.me/{CHANNEL_ID.lstrip('@')}/{pub.message_id}"
                except Exception as e:
                    logger.warning("Channel post failed: %s", e)

            save_track(
                user_id, sub["title"], sub["artist"], sub.get("album", ""),
                sub["file_id"], track["file"], track.get("cover"),
                sub.get("duration", 0), channel_url,
            )

            # Notify subscribers
            subs = get_subscribers(user_id)
            notify_text = (
                f"🎵 <b>{sub['artist']}</b> выпустил новый трек!\n\n"
                f"<b>{sub['title']}</b>\n\n"
                + (f"📻 {channel_url}\n" if channel_url else "")
                + f"🌐 {SITE_URL}"
            )
            for sid in subs:
                try:
                    await ctx.bot.send_message(sid, notify_text, parse_mode="HTML")
                except Exception:
                    pass

            moderator_name = update.effective_user.first_name
            await _edit_caption(query, f"\n\n✅ ПОДТВЕРЖДЕНО — @{update.effective_user.username or moderator_name}")
            await ctx.bot.send_message(
                user_id,
                f"🎉 Трек <b>{sub['title']}</b> одобрен и добавлен на WAVARCHIVE!\n\n"
                + (f"📻 {channel_url}\n" if channel_url else "")
                + f"🎧 <a href='{SITE_URL}'>Открыть сайт</a>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Upload error: %s", e)
            await _edit_caption(query, f"\n\n❌ Ошибка загрузки: {e}")
        finally:
            pending.pop(user_id, None)

    # ── Admin: reject ──────────────────────────────────────────────────────────
    elif data.startswith("reject_"):
        if not await _is_moderator(ctx, viewer_id):
            await query.answer("Нет доступа", show_alert=True)
            return

        user_id = int(data.split("_", 1)[1])
        sub = pending.get(user_id)
        if not sub:
            await _edit_caption(query, "\n\n⚠️ Данные устарели.")
            return

        ctx.bot_data[f"reject_{user_id}"] = sub
        # Просим написать причину прямо в группе модерации
        await ctx.bot.send_message(
            MODERATION_CHAT_ID,
            f"📝 @{update.effective_user.username or update.effective_user.first_name}, "
            f"напиши причину отклонения трека «{sub['title']}» (или «—» без причины):",
        )
        await _edit_caption(query, "\n\n⏳ Жду причину отклонения...")

    # ── Subscribe / Unsubscribe ────────────────────────────────────────────────
    elif data.startswith(("sub_", "unsub_")):
        action, slug = data.split("_", 1)
        artist = get_artist_by_slug(slug)
        if not artist:
            return

        if action == "sub":
            if viewer_id == artist["user_id"]:
                await query.answer("Нельзя подписаться на себя 😅", show_alert=True)
                return
            subscribe(viewer_id, artist["user_id"])
            new_action, new_label = "unsub", "❤️ отписаться"
        else:
            unsubscribe(viewer_id, artist["user_id"])
            new_action, new_label = "sub", "🤍 подписаться"

        artist  = get_artist_by_slug(slug)   # refresh counter
        tracks  = get_artist_tracks(artist["user_id"])
        new_kb  = [[InlineKeyboardButton(new_label, callback_data=f"{new_action}_{slug}")]]
        if tracks:
            new_kb.append([InlineKeyboardButton(
                f"💿 дискография ({len(tracks)})",
                callback_data=f"disc_{artist['user_id']}_0",
            )])
        text = _artist_card_text(artist)
        try:
            if query.message.photo:
                await query.edit_message_caption(text, reply_markup=InlineKeyboardMarkup(new_kb), parse_mode="HTML")
            else:
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(new_kb), parse_mode="HTML")
        except Exception:
            pass

    # ── Discography navigation ─────────────────────────────────────────────────
    elif data.startswith("disc_"):
        parts    = data.split("_")          # disc_{user_id}_{idx}
        uid      = int(parts[1])
        idx      = int(parts[2])
        tracks   = get_artist_tracks(uid)
        if not tracks:
            await query.answer("Нет треков", show_alert=True)
            return

        idx   = idx % len(tracks)
        track = tracks[idx]

        nav = []
        if len(tracks) > 1:
            prev_i = (idx - 1) % len(tracks)
            next_i = (idx + 1) % len(tracks)
            nav.append(InlineKeyboardButton("◀️", callback_data=f"disc_{uid}_{prev_i}"))
            nav.append(InlineKeyboardButton("▶️", callback_data=f"disc_{uid}_{next_i}"))

        artist = get_artist(uid)
        kb = []
        if nav:
            kb.append(nav)
        kb.append([InlineKeyboardButton("👤 к карточке", callback_data=f"card_{artist['slug']}" if artist else "noop")])

        caption = (
            f"💿 <b>{track['track_name']}</b>"
            + (f"\n<i>{track['album']}</i>" if track["album"] else "")
            + (f"\n\n🔗 <a href='{track['channel_url']}'>слушать в канале</a>" if track["channel_url"] else "")
            + f"\n<i>{idx+1} из {len(tracks)}</i>"
        )
        try:
            await query.message.delete()
        except Exception:
            pass
        if track["file_id"]:
            await query.message.chat.send_audio(
                track["file_id"], caption=caption,
                reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
            )
        else:
            await query.message.chat.send_message(
                caption + "\n\n❌ файл недоступен",
                reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
            )

    # ── Back to artist card ────────────────────────────────────────────────────
    elif data.startswith("card_"):
        slug = data[5:]
        try:
            await query.message.delete()
        except Exception:
            pass

        class _FakeUpdate:
            effective_message = query.message

        await _show_artist_card(_FakeUpdate(), ctx, slug, viewer_id)

    # ── Show artist (from search results) ─────────────────────────────────────
    elif data.startswith("show_"):
        slug = data[5:]
        await _show_artist_card(update, ctx, slug, viewer_id)


async def _is_moderator(ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """True если user_id — один из ADMIN_IDS или админ/создатель группы модерации."""
    if user_id in ADMIN_IDS:
        return True
    if MODERATION_CHAT_ID == ADMIN_ID:
        return False
    try:
        member = await ctx.bot.get_chat_member(MODERATION_CHAT_ID, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def _edit_caption(query, suffix: str):
    """Helper: append suffix to the current caption (fire-and-forget coroutine)."""
    return query.edit_message_caption(
        (query.message.caption or "") + suffix,
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN REJECTION REASON HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def handle_rejection_in_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Хендлер группы модерации — ловит причину отклонения от модераторов."""
    sender_id = update.effective_user.id
    waiting   = {k: v for k, v in ctx.bot_data.items() if k.startswith("reject_")}
    if not waiting:
        return
    if not await _is_moderator(ctx, sender_id):
        return

    key, sub = next(iter(waiting.items()))
    user_id  = sub["from_id"]
    reason   = update.message.text.strip()

    msg = f"😔 Трек <b>{sub['title']}</b> был отклонён."
    if reason not in ("—", "-", "нет", "no"):
        msg += f"\n\n📝 Причина: {reason}"
    msg += "\n\nЕсли хочешь попробовать снова — /start"

    await ctx.bot.send_message(user_id, msg, parse_mode="HTML")
    await update.message.reply_text("✅ Артист уведомлён об отклонении.")
    pending.pop(user_id, None)
    del ctx.bot_data[key]


# ══════════════════════════════════════════════════════════════════════════════
#  GENERAL TEXT HANDLER (menu buttons + search)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    text = (update.message.text or "").strip()

    # ── Menu: my card ──────────────────────────────────────────────────────────
    if text == "моя карточка 👤":
        artist = get_artist(uid)
        if not artist or artist["first_song"] == 0:
            await update.message.reply_text("❌ Сначала опубликуй первый трек.")
            return

        me   = await ctx.bot.get_me()
        link = f"https://t.me/{me.username}?start={artist['slug']}"
        card = f"👤 <b>Твоя карточка</b>\n\n🔗 <code>{link}</code>\n\n"
        if artist["is_allowed"]:
            card += f"✅ <b>Активна</b>\n"
            if artist["name"]:
                card += f"Имя: {artist['name']}\n"
            if artist["bio"]:
                card += f"О себе: {artist['bio'][:120]}\n"
            if artist["links"]:
                card += f"Соцсети: {_format_links(artist['links'])}\n"
        else:
            card += "📝 Профиль не заполнен\n"

        tracks = get_artist_tracks(uid)
        if tracks:
            card += f"\n🎵 Треков: {len(tracks)}"

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Редактировать профиль", callback_data="edit_profile")]])
        if artist["photo_id"]:
            await update.message.reply_photo(artist["photo_id"], caption=card, reply_markup=kb, parse_mode="HTML")
        else:
            await update.message.reply_text(card, reply_markup=kb, parse_mode="HTML")

    # ── Menu: subscriptions ────────────────────────────────────────────────────
    elif text == "мои подписки 📋":
        subs = get_subscriptions(uid)
        if not subs:
            await update.message.reply_text(
                "📋 Ты пока ни на кого не подписан.\nНайди артистов через поиск 🔍"
            )
            return
        t  = f"📋 <b>Твои подписки ({len(subs)})</b>\n\n"
        kb = []
        for a in subs:
            t += f"• <b>{a['name']}</b>"
            if a["subscribers_count"]:
                t += f"  👥 {a['subscribers_count']}"
            t += "\n"
            kb.append([InlineKeyboardButton(f"👤 {a['name']}", callback_data=f"show_{a['slug']}")])
        await update.message.reply_text(t, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    # ── Menu: search ───────────────────────────────────────────────────────────
    elif text == "найти артиста 🔍":
        ctx.user_data["searching"] = True
        await update.message.reply_text(
            "🔍 <b>Поиск артиста</b>\n\n"
            "Введи имя, слаг или ссылку-приглашение:",
            parse_mode="HTML",
        )

    # ── Active search ──────────────────────────────────────────────────────────
    elif ctx.user_data.pop("searching", False):
        if "?start=" in text:
            slug = text.split("start=")[-1].strip()
            await _show_artist_card(update, ctx, slug, uid)
        elif re.match(r"^[a-zA-Z0-9]{4,12}$", text):
            artist = get_artist_by_slug(text)
            if artist and artist["is_allowed"]:
                await _show_artist_card(update, ctx, text, uid)
                return
            results = search_artists(text)
            if results:
                await _show_search_results(update, results, text)
            else:
                await update.message.reply_text("Ничего не найдено 🤷")
        else:
            results = search_artists(text)
            if results:
                await _show_search_results(update, results, text)
            else:
                await update.message.reply_text("Ничего не найдено 🤷")

    # ── Fallback ───────────────────────────────────────────────────────────────
    else:
        artist = get_artist(uid)
        is_a   = artist and artist["first_song"] == 1
        await _send_main_menu(update, "Используй кнопки меню 👇", is_a)


async def _show_search_results(update: Update, results: list, query: str) -> None:
    text = f"🔎 <b>Результаты по «{query}»:</b>\n\n"
    kb   = []
    for a in results:
        text += f"• <b>{a['name']}</b>"
        if a["bio"]:
            bio = a["bio"][:50] + ("…" if len(a["bio"]) > 50 else "")
            text += f"\n  <i>{bio}</i>"
        text += "\n"
        kb.append([InlineKeyboardButton(f"👤 {a['name']}", callback_data=f"show_{a['slug']}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    with _db() as conn:
        artists = conn.execute("SELECT COUNT(*) FROM artists WHERE first_song=1").fetchone()[0]
        tracks  = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        subs    = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        pending_cnt = len(pending)
    await update.message.reply_text(
        f"📊 <b>WAVARCHIVE — статистика</b>\n\n"
        f"👤 Артистов: <b>{artists}</b>\n"
        f"🎵 Треков: <b>{tracks}</b>\n"
        f"❤️ Подписок: <b>{subs}</b>\n"
        f"⏳ Ожидают проверки: <b>{pending_cnt}</b>",
        parse_mode="HTML",
    )


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not pending:
        await update.message.reply_text("✅ Очередь пуста.")
        return
    text = f"⏳ <b>В очереди ({len(pending)}):</b>\n\n"
    for uid, sub in pending.items():
        text += f"• {sub['title']} — {sub['artist']} (от {sub['from_name']})\n"
    await update.message.reply_text(text, parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
#  ONE-TIME DB IMPORT  (/import_db — только для ADMIN_IDS, удали после переноса)
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_import_db(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not update.message.document:
        await update.message.reply_text(
            "Пришли файл export.json — он лежит в том же архиве что и bot.py"
        )
        return

    await update.message.reply_text("⏳ Импортирую...")

    tg_file = await ctx.bot.get_file(update.message.document.file_id)
    with urllib.request.urlopen(tg_file.file_path, timeout=30) as r:
        data = json.loads(r.read())

    artists_ok = tracks_ok = subs_ok = 0

    with _db() as conn:
        for row in data.get("artists", []):
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO artists
                    (user_id, slug, name, bio, photo_id, links,
                     is_allowed, first_song, subscribers_count, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    row["user_id"], row["slug"], row.get("name"), row.get("bio"),
                    row.get("photo_id"), row.get("links"),
                    row.get("is_allowed", 0), row.get("first_song", 0),
                    row.get("subscribers_count", 0), row.get("created_at"),
                ))
                artists_ok += 1
            except Exception as e:
                logger.warning("artist skip %s: %s", row.get("user_id"), e)

        for row in data.get("tracks", []):
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO tracks
                    (user_id, track_name, file_id, channel_url, published_at)
                    VALUES (?,?,?,?,?)
                """, (
                    row["user_id"], row["track_name"], row.get("file_id"),
                    row.get("track_url", ""),   # старое поле → channel_url
                    row.get("published_at"),
                ))
                tracks_ok += 1
            except Exception as e:
                logger.warning("track skip %s: %s", row.get("track_name"), e)

        for row in data.get("subscriptions", []):
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO subscriptions (subscriber_id, artist_id, created_at)
                    VALUES (?,?,?)
                """, (row["subscriber_id"], row["artist_id"], row.get("created_at")))
                subs_ok += 1
            except Exception as e:
                logger.warning("sub skip: %s", e)

    await update.message.reply_text(
        f"✅ <b>Импорт завершён!</b>\n\n"
        f"👤 Артистов: <b>{artists_ok}</b>\n"
        f"🎵 Треков: <b>{tracks_ok}</b>\n"
        f"❤️ Подписок: <b>{subs_ok}</b>\n\n"
        f"Команду /import_db можно удалить из кода.",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    PRIVATE = filters.ChatType.PRIVATE

    # Track upload conversation
    upload_conv = ConversationHandler(
        entry_points=[
            MessageHandler(PRIVATE & filters.Regex(r"^отправить трек 📥$"), upload_start),
            MessageHandler(PRIVATE & filters.Regex(r"^отправить файл 📥$"), upload_start),
        ],
        states={
            TITLE:  [MessageHandler(PRIVATE & filters.TEXT & ~filters.COMMAND, upload_title)],
            ARTIST: [MessageHandler(PRIVATE & filters.TEXT & ~filters.COMMAND, upload_artist)],
            ALBUM:  [MessageHandler(PRIVATE & filters.TEXT & ~filters.COMMAND, upload_album)],
            COVER:  [MessageHandler(
                PRIVATE & (filters.PHOTO | filters.Document.IMAGE | filters.TEXT) & ~filters.COMMAND,
                upload_cover,
            )],
            FILE: [MessageHandler(
                PRIVATE & (filters.AUDIO | filters.Document.ALL) & ~filters.COMMAND,
                upload_file,
            )],
        },
        fallbacks=[CommandHandler("cancel", upload_cancel)],
        allow_reentry=True,
    )

    # Profile edit conversation
    profile_conv = ConversationHandler(
        entry_points=[
            CommandHandler("profile", profile_start, filters=PRIVATE),
            CallbackQueryHandler(profile_start, pattern="^edit_profile$"),
        ],
        states={
            P_NAME:  [MessageHandler(PRIVATE & filters.TEXT & ~filters.COMMAND, profile_name)],
            P_BIO:   [MessageHandler(PRIVATE & filters.TEXT & ~filters.COMMAND, profile_bio)],
            P_PHOTO: [MessageHandler(
                PRIVATE & (filters.PHOTO | filters.TEXT) & ~filters.COMMAND, profile_photo
            )],
            P_LINKS: [MessageHandler(PRIVATE & filters.TEXT & ~filters.COMMAND, profile_links)],
        },
        fallbacks=[CommandHandler("cancel", upload_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start",   cmd_start,   filters=PRIVATE))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("import_db", cmd_import_db))  # одноразовый импорт
    app.add_handler(upload_conv)
    app.add_handler(profile_conv)
    app.add_handler(CallbackQueryHandler(
        handle_callback,
        pattern=r"^(approve|reject|sub|unsub|disc|card|show)_",
    ))
    app.add_handler(MessageHandler(PRIVATE & filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(
        (filters.Chat(MODERATION_CHAT_ID) | filters.Chat(list(ADMIN_IDS))) & filters.TEXT & ~filters.COMMAND,
        handle_rejection_in_group,
    ))

    logger.info("WAVARCHIVE Bot (merged) starting — polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
