import os, json, asyncio, logging, re, httpx
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── הגדרות ──────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_KEY     = os.environ["GEMINI_KEY"]
CHAT_ID        = os.environ["CHAT_ID"]
SEND_HOUR      = int(os.environ.get("SEND_HOUR", "7"))
TZ             = ZoneInfo("Asia/Jerusalem")

GEMINI_URL  = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"
PODCAST_RSS = "https://api.rss2json.com/v1/api.json?rss_url=https%3A%2F%2Ffeeds.buzzsprout.com%2F2299778.rss"
YAHOO_URL   = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2d"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

STATE_FILE = "/tmp/state.json"

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {
            "stop_loss": 7,
            "watchlist": ["SPY", "NVDA", "AAPL", "TSLA"],
            "extra_context": ""
        }

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, ensure_ascii=False)

STATE = load_state()

SYSTEM = """אתה סוכן מניות מנוסה המבוסס על האקדמיה של מיכה סטוקס.

שיטת הניתוח:
- פונדמנטלי: הכנסות, EPS, FCF, P/E, מודל עסקי
- טכני: MA150/MA50/MA20, תמיכה/התנגדות, RSI, ווליום
- ניהול סיכון: תמיד כלול Stop Loss ו-Take Profit
- שיטת מיכו: מניה מעל MA150 במגמה עולה = שוקל. מתחת = לא נוגע.
- לא לתפוס סכין נופלת. מזומן הוא גם פוזיציה.

כתוב בעברית, ברור וקצר. בסוף: ⚠️ חינוך פיננסי בלבד."""

# ── Gemini ───────────────────────────────────────────────────
async def gemini(prompt: str) -> str:
    s = STATE
    full = SYSTEM
    if s.get("watchlist"):
        full += f"\nרשימת מעקב: {', '.join(s['watchlist'])}"
    full += f"\nStop Loss מקסימלי: {s.get('stop_loss', 7)}%"
    if s.get("extra_context"):
        full += f"\n{s['extra_context']}"

    body = {
        "system_instruction": {"parts": [{"text": full}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 700, "temperature": 0.4}
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(GEMINI_URL, json=body)
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]

# ── נתוני שוק ───────────────────────────────────────────────
async def fetch_market():
    tickers = [("SPY","S&P 500"), ("QQQ","נאסד\"ק"), ("GLD","זהב"), ("USO","נפט")]
    lines = []
    async with httpx.AsyncClient(timeout=10) as c:
        for sym, label in tickers:
            try:
                r    = await c.get(YAHOO_URL.format(sym=sym))
                meta = r.json()["chart"]["result"][0]["meta"]
                p    = meta["regularMarketPrice"]
                prev = meta.get("previousClose", p)
                chg  = (p - prev) / prev * 100
                arr  = "▲" if chg >= 0 else "▼"
                lines.append(f"{label}: ${p:.2f} {arr}{abs(chg):.1f}%")
            except Exception:
                lines.append(f"{label}: —")
    return "\n".join(lines)

# ── פודקאסט ─────────────────────────────────────────────────
async def fetch_episode():
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r    = await c.get(PODCAST_RSS)
            item = r.json()["items"][0]
            desc = re.sub(r'<[^>]+>', '', item.get("description", ""))[:500]
            return {"title": item.get("title",""), "desc": desc,
                    "link": item.get("link","https://youtube.com/@Micha.Stocks/videos")}
        except Exception:
            return {"title": "סרטון בוקר — מיכה סטוקס",
                    "desc": "", "link": "https://youtube.com/@Micha.Stocks/videos"}

# ── סיכום בוקר ──────────────────────────────────────────────
async def morning_digest(bot: Bot):
    try:
        market  = await fetch_market()
        ep      = await fetch_episode()
        summary = await gemini(
            f"סכם את הסרטון הבא ב-4 נקודות קצרות:\n"
            f"כותרת: {ep['title']}\nתיאור: {ep['desc']}"
        )
        wl = ", ".join(STATE["watchlist"])
        recs = await gemini(
            f"תן המלצה קצרה (שורה אחת לכל אחת) על: {wl}. "
            f"מצב שוק: {market}"
        )
        now = datetime.now(TZ).strftime("%d/%m/%Y %H:%M")
        msg = (
            f"📈 *סוכן מניות | מיכה סטוקס*\n🗓 {now}\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"📊 *מצב שוק*\n{market}\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"🎬 *{ep['title']}*\n\n{summary}\n\n"
            f"[▶ לסרטון]({ep['link']})\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"⭐ *רשימת מעקב*\n{recs}\n\n"
            f"_⚠️ חינוך פיננסי בלבד_"
        )
        await bot.send_message(CHAT_ID, msg, parse_mode="Markdown",
                               disable_web_page_preview=True)
    except Exception as e:
        log.error(f"שגיאה בסיכום בוקר: {e}")
        await bot.send_message(CHAT_ID, f"⚠️ שגיאה בסיכום בוקר: {e}")

# ── פקודות ──────────────────────────────────────────────────
async def cmd_start(u: Update, _):
    await u.message.reply_text(
        "📈 *סוכן מניות | מיכה סטוקס*\n\n"
        "הפקודות:\n"
        "• /morning — סיכום בוקר + שוק\n"
        "• /analyze TSLA — ניתוח מניה\n"
        "• /market — מצב שוק עכשיו\n"
        "• /watchlist — ניתוח רשימת מעקב\n"
        "• /add NVDA — הוסף לרשימה\n"
        "• /remove NVDA — הסר מרשימה\n"
        "• /setstop 7 — עדכן Stop Loss\n"
        "• /settings — הגדרות נוכחיות\n\n"
        "או פשוט כתוב כל שאלה חופשית 💬\n\n"
        "_⚠️ חינוך פיננסי בלבד_",
        parse_mode="Markdown"
    )

async def cmd_morning(u: Update, _):
    m = await u.message.reply_text("⏳ מושך נתונים...")
    try:
        market  = await fetch_market()
        ep      = await fetch_episode()
        summary = await gemini(
            f"סכם ב-4 נקודות: כותרת: {ep['title']}\nתיאור: {ep['desc']}"
        )
        now = datetime.now(TZ).strftime("%d/%m/%Y %H:%M")
        txt = (
            f"📈 *סיכום בוקר — {now}*\n\n"
            f"📊 *שוק*\n{market}\n\n"
            f"🎬 *{ep['title']}*\n\n{summary}\n\n"
            f"[▶ לסרטון]({ep['link']})\n\n"
            f"_⚠️ חינוך פיננסי בלבד_"
        )
        await m.edit_text(txt, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await m.edit_text(f"⚠️ שגיאה: {e}")

async def cmd_analyze(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await u.message.reply_text("לדוגמה: /analyze TSLA")
        return
    ticker = ctx.args[0].upper()
    extra  = " ".join(ctx.args[1:])
    m = await u.message.reply_text(f"🔍 מנתח {ticker}...")
    try:
        q = f"נתח לי את {ticker}"
        if extra:
            q += f" — {extra}"
        q += ". כלול ניתוח טכני, פונדמנטלי, Stop Loss ו-Target."
        result = await gemini(q)
        await m.edit_text(f"📊 *ניתוח {ticker}*\n\n{result}", parse_mode="Markdown")
    except Exception as e:
        await m.edit_text(f"⚠️ שגיאה: {e}")

async def cmd_market(u: Update, _):
    m = await u.message.reply_text("⏳ שולף...")
    try:
        market = await fetch_market()
        now    = datetime.now(TZ).strftime("%H:%M")
        await m.edit_text(f"📊 *מצב שוק — {now}*\n\n{market}", parse_mode="Markdown")
    except Exception as e:
        await m.edit_text(f"⚠️ שגיאה: {e}")

async def cmd_watchlist(u: Update, _):
    wl = STATE["watchlist"]
    if not wl:
        await u.message.reply_text("הרשימה ריקה. הוסף עם /add TSLA")
        return
    m = await u.message.reply_text("⏳ מנתח...")
    try:
        result = await gemini(
            f"נתח בקצרה (2 שורות לכל מניה) את: {', '.join(wl)}. "
            f"לכל אחת: מגמה, מיקום ביחס ל-MA150, המלצה."
        )
        await m.edit_text(
            f"👀 *רשימת מעקב: {', '.join(wl)}*\n\n{result}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await m.edit_text(f"⚠️ שגיאה: {e}")

async def cmd_add(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await u.message.reply_text("לדוגמה: /add NVDA")
        return
    t = ctx.args[0].upper()
    if t not in STATE["watchlist"]:
        STATE["watchlist"].append(t)
        save_state(STATE)
    await u.message.reply_text(
        f"✅ {t} נוספה.\nרשימה: {', '.join(STATE['watchlist'])}"
    )

async def cmd_remove(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await u.message.reply_text("לדוגמה: /remove NVDA")
        return
    t = ctx.args[0].upper()
    if t in STATE["watchlist"]:
        STATE["watchlist"].remove(t)
        save_state(STATE)
        await u.message.reply_text(f"🗑 {t} הוסרה.")
    else:
        await u.message.reply_text(f"{t} לא נמצאת ברשימה.")

async def cmd_setstop(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await u.message.reply_text("לדוגמה: /setstop 7")
        return
    try:
        v = float(ctx.args[0])
        STATE["stop_loss"] = v
        save_state(STATE)
        await u.message.reply_text(f"✅ Stop Loss עודכן ל-{v}%")
    except ValueError:
        await u.message.reply_text("ערך לא תקין.")

async def cmd_settings(u: Update, _):
    s = STATE
    await u.message.reply_text(
        f"⚙️ *הגדרות*\n\n"
        f"Stop Loss: {s.get('stop_loss',7)}%\n"
        f"רשימת מעקב: {', '.join(s.get('watchlist',[]))}\n\n"
        f"לשינוי: /setstop /add /remove",
        parse_mode="Markdown"
    )

async def handle_msg(u: Update, _):
    txt = u.message.text.strip()
    if not txt:
        return
    m = await u.message.reply_text("🤔 מעבד...")
    try:
        result = await gemini(txt)
        await m.edit_text(result[:4000])
    except Exception as e:
        await m.edit_text(f"⚠️ שגיאה: {e}")

# ── main ─────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("morning",   cmd_morning))
    app.add_handler(CommandHandler("analyze",   cmd_analyze))
    app.add_handler(CommandHandler("market",    cmd_market))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("add",       cmd_add))
    app.add_handler(CommandHandler("remove",    cmd_remove))
    app.add_handler(CommandHandler("setstop",   cmd_setstop))
    app.add_handler(CommandHandler("settings",  cmd_settings))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        lambda: asyncio.create_task(morning_digest(app.bot)),
        trigger="cron", hour=SEND_HOUR, minute=0
    )
    scheduler.start()

    log.info(f"🤖 בוט עולה | שליחה יומית ב-{SEND_HOUR}:00")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
