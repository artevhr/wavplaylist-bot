"""
WAVARCHIVE Bot
Объединяет:
  - bot (6).py  : загрузка трека → 5 шагов → модерация → GitHub + tracks.json
  - main.py     : профили артистов, подписки, поиск, дискография

Railway env vars:
  BOT_TOKEN, ADMIN_IDS, MODERATION_CHAT_ID,
  GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO, SITE_URL,
  CHANNEL_ID (необяз.), DB_PATH (необяз.), RULES_LINK (необяз.)
"""

import os, json, logging, base64, re, time, string, random, sqlite3, traceback
import urllib.request
from datetime import date

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["BOT_TOKEN"]
ADMIN_IDS  = {int(x.strip()) for x in
              os.environ.get("ADMIN_IDS", os.environ.get("ADMIN_ID", "0")).split(",")}
ADMIN_ID   = next(iter(ADMIN_IDS))

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "artevhr")
GITHUB_REPO  = os.environ.get("GITHUB_REPO",  "wavarchive-music")
SITE_URL     = os.environ.get("SITE_URL", f"https://{GITHUB_OWNER}.github.io/wavarchive-site/")
CHANNEL_ID   = os.environ.get("CHANNEL_ID", "")
RULES_LINK   = os.environ.get("RULES_LINK", "")
DB_PATH      = os.environ.get("DB_PATH", "database.db")
_MOD         = os.environ.get("MODERATION_CHAT_ID", "")
MODERATION_CHAT_ID = int(_MOD) if _MOD else ADMIN_ID

# ── ConversationHandler states ────────────────────────────────────────────────
TITLE, ARTIST_ST, ALBUM, COVER, FILE = range(5)
P_NAME, P_BIO, P_PHOTO, P_LINKS     = range(5, 9)

# ── In-memory pending ─────────────────────────────────────────────────────────
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
                slug              TEXT    UNIQUE NOT NULL,
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
                track_name   TEXT    NOT NULL,
                artist_name  TEXT,
                album        TEXT,
                file_id      TEXT,
                github_path  TEXT,
                cover_path   TEXT,
                duration     INTEGER DEFAULT 0,
                channel_url  TEXT    DEFAULT '',
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                subscriber_id INTEGER NOT NULL,
                artist_id     INTEGER NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(subscriber_id, artist_id)
            );

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
    logger.info("DB ready: %s", DB_PATH)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _slug() -> str:
    chars = string.ascii_letters + string.digits
    while True:
        s = "".join(random.choice(chars) for _ in range(8))
        with _db() as c:
            if not c.execute("SELECT 1 FROM artists WHERE slug=?", (s,)).fetchone():
                return s


def ensure_artist(user_id: int) -> None:
    with _db() as c:
        if not c.execute("SELECT 1 FROM artists WHERE user_id=?", (user_id,)).fetchone():
            c.execute("INSERT INTO artists (user_id, slug) VALUES (?,?)", (user_id, _slug()))


def get_artist(user_id: int):
    with _db() as c:
        return c.execute("SELECT * FROM artists WHERE user_id=?", (user_id,)).fetchone()


def get_artist_by_slug(slug: str):
    with _db() as c:
        return c.execute("SELECT * FROM artists WHERE slug=?", (slug,)).fetchone()


def search_artists(query: str):
    q = query.replace(" ", "")
    with _db() as c:
        return c.execute(
            "SELECT * FROM artists WHERE is_allowed=1 AND name IS NOT NULL AND name!=''"
            " AND REPLACE(LOWER(name),' ','') LIKE REPLACE(LOWER(?),' ','%') LIMIT 15",
            (q,),
        ).fetchall()


def get_tracks(user_id: int):
    with _db() as c:
        return c.execute(
            "SELECT * FROM tracks WHERE user_id=? ORDER BY published_at DESC", (user_id,)
        ).fetchall()


def save_track(user_id, track_name, artist_name, album, file_id,
               github_path, cover_path, duration, channel_url) -> None:
    with _db() as c:
        c.execute(
            "INSERT INTO tracks (user_id,track_name,artist_name,album,file_id,"
            "github_path,cover_path,duration,channel_url) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, track_name, artist_name, album, file_id,
             github_path, cover_path, duration, channel_url),
        )
        c.execute("UPDATE artists SET first_song=1 WHERE user_id=?", (user_id,))


def get_subscribers(artist_id: int) -> list[int]:
    with _db() as c:
        return [r["subscriber_id"] for r in
                c.execute("SELECT subscriber_id FROM subscriptions WHERE artist_id=?",
                          (artist_id,)).fetchall()]


def is_subscribed(subscriber_id: int, artist_id: int) -> bool:
    with _db() as c:
        return bool(c.execute(
            "SELECT 1 FROM subscriptions WHERE subscriber_id=? AND artist_id=?",
            (subscriber_id, artist_id),
        ).fetchone())


def subscribe(subscriber_id: int, artist_id: int) -> None:
    with _db() as c:
        c.execute("INSERT OR IGNORE INTO subscriptions (subscriber_id, artist_id) VALUES (?,?)",
                  (subscriber_id, artist_id))


def unsubscribe(subscriber_id: int, artist_id: int) -> None:
    with _db() as c:
        c.execute("DELETE FROM subscriptions WHERE subscriber_id=? AND artist_id=?",
                  (subscriber_id, artist_id))


# ══════════════════════════════════════════════════════════════════════════════
#  GITHUB
# ══════════════════════════════════════════════════════════════════════════════

def _translit(s: str) -> str:
    tbl = {"а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"yo","ж":"zh","з":"z",
           "и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
           "с":"s","т":"t","у":"u","ф":"f","х":"kh","ц":"ts","ч":"ch","ш":"sh",
           "щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"}
    tbl.update({k.upper(): v for k, v in tbl.items()})
    r = "".join(tbl.get(c, c) for c in s)
    return re.sub(r"[^a-z0-9]+", "-", r.lower()).strip("-") or "track"


def _gh(path: str, method: str = "GET", body: dict | None = None) -> dict:
    url  = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    hdrs = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "Content-Type":  "application/json",
        "User-Agent":    "WAVArchiveBot/3.0",
    }
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub {e.code}: {e.read().decode('utf-8','replace')[:300]}") from e


async def _tg_dl(bot, file_id: str) -> bytes:
    f = await bot.get_file(file_id)
    with urllib.request.urlopen(f.file_path, timeout=60) as r:
        return r.read()


async def upload_github(sub: dict, ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    mp3     = await _tg_dl(ctx.bot, sub["file_id"])
    a_slug  = _translit(sub["artist"])
    t_slug  = _translit(sub["title"])
    ts      = int(time.time())
    mp3_path = f"tracks/{a_slug}/{t_slug}-{ts}.mp3"
    cover_path = None

    _gh(mp3_path, "PUT", {
        "message": f"Add: {sub['title']} by {sub['artist']}",
        "content": base64.b64encode(mp3).decode(),
    })
    logger.info("MP3 uploaded → %s", mp3_path)

    if sub.get("cover_file_id"):
        cov  = await _tg_dl(ctx.bot, sub["cover_file_id"])
        ext  = (sub.get("cover_name") or "cover.jpg").rsplit(".", 1)[-1].lower()
        cover_path = f"covers/{a_slug}/{t_slug}-{ts}.{ext}"
        _gh(cover_path, "PUT", {
            "message": f"Cover: {sub['title']}",
            "content": base64.b64encode(cov).decode(),
        })
        logger.info("Cover uploaded → %s", cover_path)

    td  = _gh("tracks.json")
    cat = json.loads(base64.b64decode(td["content"]).decode())
    sha = td["sha"]
    entry = {
        "id": f"{a_slug}_{t_slug}_{ts}",
        "title": sub["title"], "artist": sub["artist"],
        "album": sub.get("album") or "", "genre": "другое",
        "duration": sub.get("duration", 0), "file": mp3_path,
        "cover": cover_path, "albumCover": None,
        "description": "", "tags": [], "addedAt": date.today().isoformat(),
    }
    cat.append(entry)
    _gh("tracks.json", "PUT", {
        "message": f"Catalog: {sub['title']} by {sub['artist']}",
        "content": base64.b64encode(
            json.dumps(cat, ensure_ascii=False, indent=2).encode()
        ).decode(),
        "sha": sha,
    })
    logger.info("tracks.json → %s", entry["id"])
    return entry


# ══════════════════════════════════════════════════════════════════════════════
#  UI HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_links(raw: str) -> str:
    LABELS = {"t.me":"TG","tiktok":"TT","instagram":"IG","youtube":"YT",
               "vk.com":"VK","soundcloud":"SC","spotify":"SP","genius":"GS"}
    out = []
    for lnk in raw.split("|"):
        lnk = lnk.strip()
        if not lnk:
            continue
        label = next((v for k, v in LABELS.items() if k in lnk),
                     lnk.replace("https://","").replace("http://","").split("/")[0])
        out.append(f"<a href='{lnk}'>{label}</a>")
    return "  |  ".join(out)


def _card_text(artist) -> str:
    t = f"👤 <b>{artist['name']}</b>"
    if artist["subscribers_count"]:
        t += f"  •  👥 {artist['subscribers_count']}"
    t += "\n\n"
    if artist["bio"]:
        t += f"{artist['bio']}\n\n"
    if artist["links"]:
        t += f"🔗 {_fmt_links(artist['links'])}\n"
    return t


def _main_kb(is_artist: bool) -> ReplyKeyboardMarkup:
    row1 = [KeyboardButton("отправить трек 📥"), KeyboardButton("найти артиста 🔍")]
    row2 = [KeyboardButton("мои подписки 📋")]
    if is_artist:
        row2.append(KeyboardButton("моя карточка 👤"))
    return ReplyKeyboardMarkup([row1, row2], resize_keyboard=True)


async def _menu(update: Update, text: str, is_artist: bool) -> None:
    await update.effective_message.reply_text(
        text, reply_markup=_main_kb(is_artist), parse_mode="HTML"
    )


async def _artist_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                       slug: str, viewer_id: int) -> None:
    artist = get_artist_by_slug(slug)
    if not artist or artist["is_allowed"] != 1 or not artist["name"]:
        await update.effective_message.reply_text("❌ Артист не найден или профиль не заполнен.")
        return

    text   = _card_text(artist)
    subbed = is_subscribed(viewer_id, artist["user_id"])
    tracks = get_tracks(artist["user_id"])

    kb = []
    if viewer_id != artist["user_id"]:
        lbl = "❤️ отписаться" if subbed else "🤍 подписаться"
        act = "unsub" if subbed else "sub"
        kb.append([InlineKeyboardButton(lbl, callback_data=f"{act}_{slug}")])
    if tracks:
        kb.append([InlineKeyboardButton(
            f"💿 дискография ({len(tracks)})",
            callback_data=f"disc_{artist['user_id']}_0",
        )])

    markup = InlineKeyboardMarkup(kb) if kb else None
    if artist["photo_id"]:
        try:
            await update.effective_message.reply_photo(
                artist["photo_id"], caption=text, reply_markup=markup, parse_mode="HTML"
            )
            return
        except Exception:
            # photo_id от старого бота — показываем без фото
            pass
    await update.effective_message.reply_text(
        text, reply_markup=markup, parse_mode="HTML"
    )


async def _is_moderator(ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    if MODERATION_CHAT_ID == ADMIN_ID:
        return False
    try:
        m = await ctx.bot.get_chat_member(MODERATION_CHAT_ID, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logger.info("START uid=%s", uid)
    ensure_artist(uid)

    if ctx.args:
        await _artist_card(update, ctx, ctx.args[0], uid)
        return

    artist = get_artist(uid)
    is_a   = bool(artist and artist["first_song"])
    if is_a:
        me   = await ctx.bot.get_me()
        link = f"https://t.me/{me.username}?start={artist['slug']}"
        await _menu(update, f"🥀 <b>с возвращением!</b>\n\n🔗 <code>{link}</code>", True)
    else:
        await _menu(update, "🥀 <b>добро пожаловать на WAVARCHIVE!</b>", False)


# ══════════════════════════════════════════════════════════════════════════════
#  TRACK UPLOAD  (5-step conversation)
# ══════════════════════════════════════════════════════════════════════════════

async def upload_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    pre = f"📋 <a href='{RULES_LINK}'>Правила</a>\n\n" if RULES_LINK else ""
    await update.message.reply_text(
        f"{pre}🎵 <b>Загрузка трека</b>\n\n1️⃣ Напиши <b>название трека</b>:",
        parse_mode="HTML",
    )
    return TITLE


async def upload_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["title"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ <b>{ctx.user_data['title']}</b>\n\n2️⃣ Напиши <b>имя артиста</b>:",
        parse_mode="HTML",
    )
    return ARTIST_ST


async def upload_artist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["artist"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ <b>{ctx.user_data['artist']}</b>\n\n3️⃣ Напиши <b>альбом</b> (или «нет»):",
        parse_mode="HTML",
    )
    return ALBUM


async def upload_album(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip()
    ctx.user_data["album"] = "" if v.lower() in ("нет","no","-","none",".") else v
    await update.message.reply_text(
        "4️⃣ Пришли <b>обложку</b> (фото / файл) или напиши «нет»:",
        parse_mode="HTML",
    )
    return COVER


async def upload_cover(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text and update.message.text.strip().lower() in ("нет","no","-","none","."):
        ctx.user_data.update(cover_file_id=None, cover_name=None)
    elif update.message.photo:
        ctx.user_data.update(
            cover_file_id=update.message.photo[-1].file_id,
            cover_name="cover.jpg",
        )
    elif update.message.document:
        ctx.user_data.update(
            cover_file_id=update.message.document.file_id,
            cover_name=update.message.document.file_name or "cover.jpg",
        )
    else:
        await update.message.reply_text("Пришли фото, файл-картинку или напиши «нет»:")
        return COVER
    await update.message.reply_text(
        "5️⃣ Пришли <b>файл трека</b> (MP3):", parse_mode="HTML"
    )
    return FILE


async def upload_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.audio:
        fid  = update.message.audio.file_id
        fname = update.message.audio.file_name or f"{ctx.user_data.get('title','track')}.mp3"
        dur   = update.message.audio.duration or 0
    elif update.message.document:
        fid   = update.message.document.file_id
        fname = update.message.document.file_name or "track.mp3"
        dur   = 0
    else:
        await update.message.reply_text("Пришли аудио-файл (MP3):")
        return FILE

    d = ctx.user_data
    d.update(file_id=fid, file_name=fname, duration=dur,
             from_id=update.effective_user.id,
             from_name=update.effective_user.full_name)

    caption = (
        f"🎵 <b>Новый трек на проверку</b>\n\n"
        f"👤 {d['from_name']} (<code>{d['from_id']}</code>)\n"
        f"🎶 <b>{d['title']}</b>\n"
        f"🎤 {d['artist']}\n"
        f"💿 {d.get('album') or '—'}\n"
        f"🕐 {dur}с  •  📁 {fname}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять",   callback_data=f"approve_{d['from_id']}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{d['from_id']}"),
    ]])

    if d.get("cover_file_id"):
        await ctx.bot.send_photo(
            MODERATION_CHAT_ID, d["cover_file_id"], caption="⬆️ Обложка"
        )

    msg = await ctx.bot.send_document(
        MODERATION_CHAT_ID, fid, caption=caption, reply_markup=kb, parse_mode="HTML"
    )
    pending[d["from_id"]] = {**d, "admin_msg_id": msg.message_id}

    await update.message.reply_text(
        "✅ Трек отправлен на проверку!\n"
        "Как только рассмотрят — получишь уведомление.\n\n"
        "Ещё один? /start"
    )
    return ConversationHandler.END


async def upload_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("Отменено. /start чтобы начать заново.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  PROFILE EDIT  (4-step conversation)
# ══════════════════════════════════════════════════════════════════════════════

async def profile_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    a   = get_artist(uid)
    if not a or not a["first_song"]:
        msg = "❌ Профиль доступен после публикации первого трека."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
        target = update.callback_query.message
    else:
        target = update.message
    await target.reply_text(
        "✏️ <b>Редактирование профиля</b>\n\n1/4 Напиши <b>имя артиста</b>:",
        parse_mode="HTML",
    )
    return P_NAME


async def p_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    with _db() as c:
        c.execute("UPDATE artists SET name=? WHERE user_id=?",
                  (update.message.text.strip(), update.effective_user.id))
    await update.message.reply_text("2/4 Напиши <b>биографию</b>:", parse_mode="HTML")
    return P_BIO


async def p_bio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    with _db() as c:
        c.execute("UPDATE artists SET bio=? WHERE user_id=?",
                  (update.message.text.strip(), update.effective_user.id))
    await update.message.reply_text(
        "3/4 Пришли <b>фото профиля</b> (или «нет»):", parse_mode="HTML"
    )
    return P_PHOTO


async def p_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if update.message.photo:
        with _db() as c:
            c.execute("UPDATE artists SET photo_id=? WHERE user_id=?",
                      (update.message.photo[-1].file_id, uid))
    elif not (update.message.text and
              update.message.text.strip().lower() in ("нет","no","-",".")):
        await update.message.reply_text("Пришли фото или напиши «нет»:")
        return P_PHOTO
    await update.message.reply_text(
        "4/4 Ссылки через <code>|</code>\n"
        "Пример: <code>https://t.me/you|https://vk.com/you</code>\n"
        "Или «нет»:", parse_mode="HTML",
    )
    return P_LINKS


async def p_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid   = update.effective_user.id
    v     = update.message.text.strip()
    links = "" if v.lower() in ("нет","no","-","none",".") else v
    with _db() as c:
        c.execute("UPDATE artists SET links=?, is_allowed=1 WHERE user_id=?", (links, uid))
    await update.message.reply_text("✅ Профиль обновлён!")
    a = get_artist(uid)
    await _menu(update, "Главное меню:", bool(a and a["first_song"]))
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  CALLBACK QUERY HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    data      = query.data
    viewer_id = update.effective_user.id
    await query.answer()

    # ── approve ────────────────────────────────────────────────────────────────
    if data.startswith("approve_"):
        if not await _is_moderator(ctx, viewer_id):
            await query.answer("Нет доступа", show_alert=True)
            return
        user_id = int(data.split("_", 1)[1])
        sub = pending.get(user_id)
        if not sub:
            await query.edit_message_caption(
                (query.message.caption or "") + "\n\n⚠️ Данные устарели.",
                parse_mode="HTML",
            )
            return

        try:
            await query.edit_message_caption(
                (query.message.caption or "") + "\n\n⏳ Загружаю в GitHub...",
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            entry = await upload_github(sub, ctx)
        except Exception as e:
            logger.error("GitHub upload: %s", e)
            try:
                await query.edit_message_caption(
                    (query.message.caption or "") + f"\n\n❌ Ошибка GitHub: {e}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        # Пост в канал
        channel_url = ""
        if CHANNEL_ID:
            cap = (f"🎵 <b>{sub['title']}</b> — {sub['artist']}\n\n"
                   f"🌐 <a href='{SITE_URL}'>WAVARCHIVE</a>")
            try:
                try:
                    pub = await ctx.bot.send_audio(
                        CHANNEL_ID, sub["file_id"], caption=cap, parse_mode="HTML"
                    )
                except Exception:
                    pub = await ctx.bot.send_document(
                        CHANNEL_ID, sub["file_id"], caption=cap, parse_mode="HTML"
                    )
                channel_url = f"https://t.me/{CHANNEL_ID.lstrip('@')}/{pub.message_id}"
                logger.info("Channel → %s", channel_url)
            except Exception as e:
                logger.error("Channel post failed: %s", e)

        save_track(user_id, sub["title"], sub["artist"], sub.get("album",""),
                   sub["file_id"], entry["file"], entry.get("cover"),
                   sub.get("duration", 0), channel_url)

        # Рассылка подписчикам
        for sid in get_subscribers(user_id):
            try:
                await ctx.bot.send_message(
                    sid,
                    f"🎵 <b>{sub['artist']}</b> выпустил новый трек!\n"
                    f"<b>{sub['title']}</b>\n\n"
                    + (f"📻 {channel_url}\n" if channel_url else "")
                    + f"🌐 {SITE_URL}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        mod_tag = (f"@{update.effective_user.username}"
                   if update.effective_user.username
                   else update.effective_user.first_name)
        try:
            await query.edit_message_caption(
                (query.message.caption or "") + f"\n\n✅ ПРИНЯТО — {mod_tag}",
                parse_mode="HTML",
            )
        except Exception:
            pass

        await ctx.bot.send_message(
            user_id,
            f"🎉 Трек <b>{sub['title']}</b> одобрен и добавлен на WAVARCHIVE!\n\n"
            + (f"📻 {channel_url}\n" if channel_url else "")
            + f"🎧 <a href='{SITE_URL}'>Открыть сайт</a>",
            parse_mode="HTML",
        )
        pending.pop(user_id, None)

    # ── reject ─────────────────────────────────────────────────────────────────
    elif data.startswith("reject_"):
        if not await _is_moderator(ctx, viewer_id):
            await query.answer("Нет доступа", show_alert=True)
            return
        user_id = int(data.split("_", 1)[1])
        sub = pending.get(user_id)
        if not sub:
            await query.edit_message_caption(
                (query.message.caption or "") + "\n\n⚠️ Данные устарели.",
                parse_mode="HTML",
            )
            return
        ctx.bot_data[f"reject_{user_id}"] = sub
        mod_tag = (f"@{update.effective_user.username}"
                   if update.effective_user.username
                   else update.effective_user.first_name)
        await ctx.bot.send_message(
            MODERATION_CHAT_ID,
            f"📝 {mod_tag}, напиши причину отклонения «{sub['title']}» (или «—»):",
        )
        try:
            await query.edit_message_caption(
                (query.message.caption or "") + "\n\n⏳ Ожидаю причину...",
                parse_mode="HTML",
            )
        except Exception:
            pass

    # ── sub / unsub ────────────────────────────────────────────────────────────
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
            new_act, new_lbl = "unsub", "❤️ отписаться"
        else:
            unsubscribe(viewer_id, artist["user_id"])
            new_act, new_lbl = "sub", "🤍 подписаться"

        artist = get_artist_by_slug(slug)
        tracks = get_tracks(artist["user_id"])
        kb = [[InlineKeyboardButton(new_lbl, callback_data=f"{new_act}_{slug}")]]
        if tracks:
            kb.append([InlineKeyboardButton(
                f"💿 дискография ({len(tracks)})",
                callback_data=f"disc_{artist['user_id']}_0",
            )])
        text = _card_text(artist)
        try:
            if query.message.photo:
                await query.edit_message_caption(
                    text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
                )
            else:
                await query.edit_message_text(
                    text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
                )
        except Exception as e:
            logger.warning("edit after sub: %s", e)
            await query.answer(
                "✅ Подписался!" if action == "sub" else "✅ Отписался!",
                show_alert=True,
            )

    # ── disc navigation ────────────────────────────────────────────────────────
    elif data.startswith("disc_"):
        parts  = data.split("_")
        uid    = int(parts[1])
        idx    = int(parts[2])
        tracks = get_tracks(uid)
        if not tracks:
            await query.answer("Нет треков", show_alert=True)
            return
        idx    = idx % len(tracks)
        track  = tracks[idx]
        artist = get_artist(uid)

        nav = []
        if len(tracks) > 1:
            nav = [
                InlineKeyboardButton("◀️", callback_data=f"disc_{uid}_{(idx-1)%len(tracks)}"),
                InlineKeyboardButton("▶️", callback_data=f"disc_{uid}_{(idx+1)%len(tracks)}"),
            ]
        kb = []
        if nav:
            kb.append(nav)
        if artist:
            kb.append([InlineKeyboardButton("👤 к карточке",
                                             callback_data=f"card_{artist['slug']}")])

        cap = (
            f"💿 <b>{track['track_name']}</b>"
            + (f"\n<i>{track['album']}</i>" if track["album"] else "")
            + (f"\n\n🔗 <a href='{track['channel_url']}'>слушать</a>"
               if track["channel_url"] else "")
            + f"\n<i>{idx+1} из {len(tracks)}</i>"
        )
        try:
            await query.message.delete()
        except Exception:
            pass
        if track["file_id"]:
            try:
                await query.message.chat.send_audio(
                    track["file_id"], caption=cap,
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
                )
            except Exception:
                await query.message.chat.send_document(
                    track["file_id"], caption=cap,
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
                )
        else:
            await query.message.chat.send_message(
                cap + "\n\n❌ файл недоступен",
                reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
            )

    # ── back to card ───────────────────────────────────────────────────────────
    elif data.startswith("card_"):
        slug = data[5:]
        try:
            await query.message.delete()
        except Exception:
            pass
        await _artist_card(update, ctx, slug, viewer_id)

    # ── show from search ───────────────────────────────────────────────────────
    elif data.startswith("show_"):
        await _artist_card(update, ctx, data[5:], viewer_id)

    # ── edit profile ───────────────────────────────────────────────────────────
    elif data == "edit_profile":
        await profile_start(update, ctx)


# ══════════════════════════════════════════════════════════════════════════════
#  REJECTION REASON  (из группы модерации)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_rejection(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
    await update.message.reply_text("✅ Артист уведомлён.")
    pending.pop(user_id, None)
    del ctx.bot_data[key]


# ══════════════════════════════════════════════════════════════════════════════
#  GENERAL TEXT HANDLER  (личка — меню + поиск)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    text = (update.message.text or "").strip()
    logger.info("TEXT uid=%s: %r", uid, text[:40])

    if text == "моя карточка 👤":
        a = get_artist(uid)
        if not a or not a["first_song"]:
            await update.message.reply_text("❌ Сначала опубликуй первый трек.")
            return
        me   = await ctx.bot.get_me()
        link = f"https://t.me/{me.username}?start={a['slug']}"
        card = f"👤 <b>Твоя карточка</b>\n\n🔗 <code>{link}</code>\n\n"
        if a["is_allowed"]:
            card += "✅ <b>Активна</b>\n"
            if a["name"]:  card += f"Имя: {a['name']}\n"
            if a["bio"]:   card += f"О себе: {a['bio'][:120]}\n"
            if a["links"]: card += f"Соцсети: {_fmt_links(a['links'])}\n"
        else:
            card += "📝 Профиль не заполнен\n"
        tracks = get_tracks(uid)
        if tracks:
            card += f"\n🎵 Треков: {len(tracks)}"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Редактировать", callback_data="edit_profile")
        ]])
        if a["photo_id"]:
            try:
                await update.message.reply_photo(
                    a["photo_id"], caption=card, reply_markup=kb, parse_mode="HTML"
                )
                return
            except Exception:
                pass
        await update.message.reply_text(card, reply_markup=kb, parse_mode="HTML")

    elif text == "мои подписки 📋":
        with _db() as c:
            subs = c.execute(
                "SELECT a.* FROM subscriptions s JOIN artists a ON s.artist_id=a.user_id"
                " WHERE s.subscriber_id=? ORDER BY s.created_at DESC", (uid,)
            ).fetchall()
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
            kb.append([InlineKeyboardButton(f"👤 {a['name']}",
                                             callback_data=f"show_{a['slug']}")])
        await update.message.reply_text(t, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif text == "найти артиста 🔍":
        ctx.user_data["searching"] = True
        await update.message.reply_text("🔍 Введи имя, слаг или ссылку:")

    elif ctx.user_data.pop("searching", False):
        if "?start=" in text:
            slug = text.split("start=")[-1].strip()
            await _artist_card(update, ctx, slug, uid)
        elif re.match(r"^[a-zA-Z0-9]{4,12}$", text):
            a = get_artist_by_slug(text)
            if a and a["is_allowed"]:
                await _artist_card(update, ctx, text, uid)
            else:
                res = search_artists(text)
                await (_show_search(update, res, text) if res else
                       update.message.reply_text("Ничего не найдено 🤷"))
        else:
            res = search_artists(text)
            await (_show_search(update, res, text) if res else
                   update.message.reply_text("Ничего не найдено 🤷"))

    else:
        a = get_artist(uid)
        await _menu(update, "Используй кнопки меню 👇", bool(a and a["first_song"]))


async def _show_search(update: Update, results, query: str) -> None:
    t  = f"🔎 <b>Результаты по «{query}»:</b>\n\n"
    kb = []
    for a in results:
        t += f"• <b>{a['name']}</b>"
        if a["bio"]:
            t += f"\n  <i>{a['bio'][:50]}{'…' if len(a['bio'])>50 else ''}</i>"
        t += "\n"
        kb.append([InlineKeyboardButton(f"👤 {a['name']}",
                                         callback_data=f"show_{a['slug']}")])
    await update.message.reply_text(t, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    with _db() as c:
        artists = c.execute("SELECT COUNT(*) FROM artists WHERE first_song=1").fetchone()[0]
        tracks  = c.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        subs    = c.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    await update.message.reply_text(
        f"📊 <b>WAVARCHIVE</b>\n\n"
        f"👤 Артистов: <b>{artists}</b>\n"
        f"🎵 Треков: <b>{tracks}</b>\n"
        f"❤️ Подписок: <b>{subs}</b>\n"
        f"⏳ На проверке: <b>{len(pending)}</b>",
        parse_mode="HTML",
    )


async def cmd_pending_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not pending:
        await update.message.reply_text("✅ Очередь пуста.")
        return
    t = f"⏳ <b>В очереди ({len(pending)}):</b>\n\n"
    for uid, sub in pending.items():
        t += f"• {sub['title']} — {sub['artist']} (от {sub['from_name']})\n"
    await update.message.reply_text(t, parse_mode="HTML")


async def cmd_cancel_global(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.clear()
    uid = update.effective_user.id
    a   = get_artist(uid)
    await _menu(update, "✅ Сброшено.", bool(a and a["first_song"]))


# ══════════════════════════════════════════════════════════════════════════════
#  IMPORT DB  (/import_db — разовая команда)
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_import_db(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if update.message.document:
        await _do_import(update, ctx)
    else:
        ctx.user_data["awaiting_import"] = True
        await update.message.reply_text("📎 Пришли файл export.json")


async def handle_import_doc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not ctx.user_data.pop("awaiting_import", False):
        return
    await _do_import(update, ctx)


async def _do_import(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Импортирую...")
    try:
        f = await ctx.bot.get_file(update.message.document.file_id)
        with urllib.request.urlopen(f.file_path, timeout=30) as r:
            data = json.loads(r.read())
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось прочитать файл: {e}")
        return

    ok_a = ok_t = ok_s = 0
    with _db() as c:
        for row in data.get("artists", []):
            try:
                c.execute("""
                    INSERT INTO artists
                    (user_id,slug,name,bio,photo_id,links,
                     is_allowed,first_song,subscribers_count,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        name=excluded.name,
                        bio=excluded.bio,
                        photo_id=excluded.photo_id,
                        links=excluded.links,
                        is_allowed=excluded.is_allowed,
                        first_song=excluded.first_song,
                        subscribers_count=excluded.subscribers_count
                """, (row["user_id"], row["slug"], row.get("name"), row.get("bio"),
                      row.get("photo_id"), row.get("links"),
                      row.get("is_allowed", 0), row.get("first_song", 0),
                      row.get("subscribers_count", 0), row.get("created_at")))
                ok_a += 1
            except Exception as e:
                logger.warning("artist skip %s: %s", row.get("user_id"), e)

        for row in data.get("tracks", []):
            try:
                c.execute("""
                    INSERT OR IGNORE INTO tracks
                    (user_id,track_name,file_id,channel_url,published_at)
                    VALUES (?,?,?,?,?)
                """, (row["user_id"], row["track_name"], row.get("file_id"),
                      row.get("track_url", ""), row.get("published_at")))
                ok_t += 1
            except Exception as e:
                logger.warning("track skip: %s", e)

        for row in data.get("subscriptions", []):
            try:
                c.execute("""
                    INSERT OR IGNORE INTO subscriptions
                    (subscriber_id,artist_id,created_at)
                    VALUES (?,?,?)
                """, (row["subscriber_id"], row["artist_id"], row.get("created_at")))
                ok_s += 1
            except Exception as e:
                logger.warning("sub skip: %s", e)

    await update.message.reply_text(
        f"✅ <b>Импорт завершён!</b>\n\n"
        f"👤 Артистов: <b>{ok_a}</b>\n"
        f"🎵 Треков: <b>{ok_t}</b>\n"
        f"❤️ Подписок: <b>{ok_s}</b>",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ERROR HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception:\n%s",
                 "".join(traceback.format_exception(
                     type(ctx.error), ctx.error, ctx.error.__traceback__)))
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Ошибка. Попробуй /start")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    async def on_startup(application):
        await application.bot.delete_webhook(drop_pending_updates=False)
        me = await application.bot.get_me()
        logger.info("Started @%s | admins=%s | mod_chat=%s",
                    me.username, ADMIN_IDS, MODERATION_CHAT_ID)

    app.post_init = on_startup
    app.add_error_handler(error_handler)

    P = filters.ChatType.PRIVATE

    # ── Conversations ──────────────────────────────────────────────────────────
    upload_conv = ConversationHandler(
        entry_points=[
            MessageHandler(P & filters.Regex(r"^отправить трек 📥$"), upload_start),
            MessageHandler(P & filters.Regex(r"^отправить файл 📥$"), upload_start),
        ],
        states={
            TITLE:     [MessageHandler(P & filters.TEXT & ~filters.COMMAND, upload_title)],
            ARTIST_ST: [MessageHandler(P & filters.TEXT & ~filters.COMMAND, upload_artist)],
            ALBUM:     [MessageHandler(P & filters.TEXT & ~filters.COMMAND, upload_album)],
            COVER: [MessageHandler(
                P & (filters.PHOTO | filters.Document.IMAGE | filters.TEXT) & ~filters.COMMAND,
                upload_cover,
            )],
            FILE: [MessageHandler(
                P & (filters.AUDIO | filters.Document.ALL) & ~filters.COMMAND,
                upload_file,
            )],
        },
        fallbacks=[CommandHandler("cancel", upload_cancel)],
        allow_reentry=True,
    )

    profile_conv = ConversationHandler(
        entry_points=[
            CommandHandler("profile", profile_start, filters=P),
            CallbackQueryHandler(profile_start, pattern="^edit_profile$"),
        ],
        states={
            P_NAME:  [MessageHandler(P & filters.TEXT & ~filters.COMMAND, p_name)],
            P_BIO:   [MessageHandler(P & filters.TEXT & ~filters.COMMAND, p_bio)],
            P_PHOTO: [MessageHandler(
                P & (filters.PHOTO | filters.TEXT) & ~filters.COMMAND, p_photo
            )],
            P_LINKS: [MessageHandler(P & filters.TEXT & ~filters.COMMAND, p_links)],
        },
        fallbacks=[CommandHandler("cancel", upload_cancel)],
        allow_reentry=True,
    )

    # ── Handlers ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",     cmd_start,         filters=P))
    app.add_handler(CommandHandler("cancel",    cmd_cancel_global, filters=P))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("pending",   cmd_pending_list))
    app.add_handler(CommandHandler("import_db", cmd_import_db,     filters=P))

    # import doc — до upload_conv, чтобы не перехватил FILE state
    app.add_handler(MessageHandler(P & filters.Document.ALL, handle_import_doc), group=0)

    app.add_handler(upload_conv,  group=1)
    app.add_handler(profile_conv, group=1)

    app.add_handler(CallbackQueryHandler(handle_callback))

    # Личка — общий текст (меню, поиск)
    app.add_handler(MessageHandler(P & filters.TEXT & ~filters.COMMAND, handle_text), group=1)

    # Группа модерации — причина отклонения
    mod_chats = set(ADMIN_IDS)
    if MODERATION_CHAT_ID not in mod_chats:
        mod_chats.add(MODERATION_CHAT_ID)
    app.add_handler(MessageHandler(
        filters.Chat(list(mod_chats)) & filters.TEXT & ~filters.COMMAND,
        handle_rejection,
    ), group=2)

    logger.info("Polling started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
