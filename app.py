#!/usr/bin/env python3
"""LINE webhook for the 睡不飽 bot (shui-bu-bao-3).

GET / -> health check.
POST /callback -> LINE webhook. Verifies the signature when
LINE_CHANNEL_SECRET is set, then answers 200 immediately and
replies in a background thread, so a slow AI call never makes
LINE time out the webhook (LINE allows about 2 seconds).

Reply logic for a text message:
- Commands (/股價 /天氣 /新聞 /記住 /待辦 /發票 /美食 /生圖 /選單
  and their plain-text forms) are handled directly.
- Long-term memory and to-dos live in SQLite (data/memory.db):
  「記住…」saves a fact, 「幫我記住明天…」saves a to-do,
  「你記得什麼」lists memories, 「待辦」lists open to-dos.
  Chat history (last 20 turns per user) is also in SQLite and
  is reloaded after a restart, so the bot no longer forgets.
- Otherwise it detects what the user is asking about and fetches
  live data: Taiwan stocks (TWSE public API), weather (Open-Meteo for any place, the user's Cloudflare Worker as fallback for 西屯), news headlines (Google News RSS).
- Asks Groq first (OpenAI-compatible API, model from GROQ_MODEL,
  default openai/gpt-oss-120b) with the live data and the
  user's stored memories as context, and replies with its answer.
  If Groq fails or has no key, falls back to Apinex (model from
  APINEX_MODEL, default free/deepseek-v4.1-flash). If Apinex also
  fails, falls back to Mistral (model from MISTRAL_MODEL,
  default ministral-8b-latest). If Mistral also fails, falls
  back to AgentScope (model from AGENTSCOPE_MODEL, default
  qwen3.8-flash). If AgentScope also fails, falls
  back to Requesty (model from REQUESTY_MODEL, default
  google/gemma-4-31b-it).
- Without any key, or if all AI calls fail, falls back to
  echoing "收到：" + the user's text, so the bot always
  answers something.

Media messages:
- Image: downloads it from LINE and asks a vision model
  (VISION_MODEL, default free/gpt-6-luna) what it shows.
- Video: downloads it, grabs a few frames with ffmpeg and
  asks the vision model to sum them up.
- File: downloads it and extracts text from PDF / Word /
  Excel / plain text, then asks the chat model to summarise.
- Audio: transcribed with Groq Whisper, then handled like text.

Image generation (/生圖 <描述>, 生圖 <描述>, 幫我畫 <描述>):
asks Agnes (agnes-image-2.5-flash, free) for a PNG, saves it under
/data/img/<uuid>.png and replies with a LINE image message whose
URL is PUBLIC_URL + /img/<uuid>.png. Needs AGNES_API_KEY set.

Voice replies: every short text reply is also spoken, saved as MP3
under /data/audio/<uuid>.mp3 and served at PUBLIC_URL +
/audio/<uuid>.mp3. Edge TTS first (Taiwanese Mandarin voice,
default zh-TW-YunJheNeural, free, no key); if it fails, falls
back to Gemini TTS (gemini-2.5-flash-preview-tts, voice Leda).
Needs GEMINI_API_KEY set for the fallback; without either,
replies are text-only. Voice replies are paused unless
VOICE_ENABLED=1.

Other features:
- 附近美食： asks the AI for 3-5 places near 台中市西屯區
  永福路 (or the area the user named). No invented addresses
  or phone numbers.
- 發票對獎： fetches the latest winning numbers from the
  財政部稅務入口網 (etax.nat.gov.tw), falls back to the
  bundled winning_numbers.json. Matches 特別獎 / 特獎 /
  頭獎 / 二獎～六獎 / 增開六獎.
"""
import base64
import datetime
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
APINEX_API_KEY = os.environ.get("APINEX_API_KEY", "")
APINEX_MODEL = os.environ.get("APINEX_MODEL", "free/deepseek-v4.1-flash")
APINEX_BASE_URL = os.environ.get(
    "APINEX_BASE_URL", "https://api.apinex.bond/v1"
).rstrip("/")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_BASE_URL = os.environ.get(
    "GROQ_BASE_URL", "https://api.groq.com/openai/v1"
).rstrip("/")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "ministral-8b-latest")
MISTRAL_BASE_URL = os.environ.get(
    "MISTRAL_BASE_URL", "https://api.mistral.ai/v1"
).rstrip("/")
AGENTSCOPE_API_KEY = os.environ.get("AGENTSCOPE_API_KEY", "")
AGENTSCOPE_MODEL = os.environ.get("AGENTSCOPE_MODEL", "qwen3.8-flash")
AGENTSCOPE_BASE_URL = os.environ.get(
    "AGENTSCOPE_BASE_URL", "https://platform.agentscope.io/v1"
).rstrip("/")

REQUESTY_API_KEY = os.environ.get("REQUESTY_API_KEY", "")
REQUESTY_MODEL = os.environ.get("REQUESTY_MODEL", "google/gemma-4-31b-it")
REQUESTY_BASE_URL = os.environ.get(
    "REQUESTY_BASE_URL", "https://router.requesty.ai/v1"
).rstrip("/")
AGNES_API_KEY = os.environ.get("AGNES_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_TTS_MODEL = os.environ.get(
    "GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts"
)
GEMINI_TTS_VOICE = os.environ.get("GEMINI_TTS_VOICE", "Leda")
EDGE_TTS_VOICE = os.environ.get("EDGE_TTS_VOICE", "zh-TW-YunJheNeural")
VOICE_ENABLED = os.environ.get("VOICE_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
AGNES_IMAGE_MODEL = os.environ.get("AGNES_IMAGE_MODEL", "agnes-image-2.5-flash")
AGNES_BASE_URL = os.environ.get(
    "AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1"
).rstrip("/")
PUBLIC_URL = os.environ.get(
    "PUBLIC_URL", "https://shui-bu-bao-rfx57zqs-workspace-lfakel.cloady.io"
).rstrip("/")
VISION_MODEL = os.environ.get("VISION_MODEL", "free/gpt-6-luna")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 40 * 1024 * 1024
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_EXTRACT_CHARS = 12000
WEATHER_URL = os.environ.get(
    "WEATHER_URL", "https://cwa-weather-api.r-l-s.workers.dev/weather"
)
PORT = int(os.environ.get("PORT", "8000"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_db_path():
    """Prefer the /data volume (survives redeploys); fall back to local."""
    try:
        os.makedirs("/data", exist_ok=True)
        probe = os.path.join("/data", ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return os.path.join("/data", "memory.db")
    except Exception as exc:
        print("volume /data not writable, using local db:", exc, flush=True)
        return os.path.join(BASE_DIR, "data", "memory.db")


DB_PATH = os.environ.get("MEMORY_DB_PATH") or _resolve_db_path()
INVOICE_JSON_PATH = os.path.join(BASE_DIR, "winning_numbers.json")


def _resolve_img_dir():
    """Prefer the /data volume (survives redeploys); fall back to local."""
    for base in ("/data", BASE_DIR):
        try:
            path = os.path.join(base, "img")
            os.makedirs(path, exist_ok=True)
            return path
        except Exception as exc:
            print("img dir not writable:", base, exc, flush=True)
    return os.path.join(BASE_DIR, "img")


IMG_DIR = _resolve_img_dir()


def _resolve_audio_dir():
    """Prefer the /data volume (survives redeploys); fall back to local."""
    for base in ("/data", BASE_DIR):
        try:
            path = os.path.join(base, "audio")
            os.makedirs(path, exist_ok=True)
            return path
        except Exception as exc:
            print("audio dir not writable:", base, exc, flush=True)
    return os.path.join(BASE_DIR, "audio")


AUDIO_DIR = _resolve_audio_dir()

SYSTEM_PROMPT = (
    "你是「睡不飽」，LINE 上一個很會聊天的朋友。"
    "一律用繁體中文，講話像台灣人平常在 LINE 上聊天："
    "簡短、直接、自然，不用客套，不用句句都很有禮貌，"
    "可以講幹話、開玩笑，語氣輕鬆一點。"
    "不要像客服，也不要像老師在說教。"
    "回話一兩句就好，不要長篇大論。"
    "不要用簡體中文。"
    "如果下面附了【即時資料】，就用那些數字回答，不要自己編數字，"
    "不知道的就說不知道，不要亂猜。"
)

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# stock code -> TWSE ex_ch
KNOWN_STOCKS = {
    "0050": "tse_0050.tw",
    "0056": "tse_0056.tw",
    "00878": "tse_00878.tw",
    "00406A": "tse_00406A.tw",
    "009826": "tse_009826.tw",
    "006208": "tse_006208.tw",
    "00662": "tse_00662.tw",
    "2330": "tse_2330.tw",
    "2317": "tse_2317.tw",
    "2454": "tse_2454.tw",
}
INDEX_CH = "tse_t00.tw"  # 加權指數

_history = {}
_history_lock = threading.Lock()
MAX_HISTORY = 20  # entries (user + assistant) kept per user

_db_lock = threading.Lock()


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def init_db():
    """Create data dir and tables. Logs the DB path at startup."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memories ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id TEXT NOT NULL, content TEXT NOT NULL, "
                "created_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS notes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id TEXT NOT NULL, text TEXT NOT NULL, "
                "due_text TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 0, "
                "created_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id TEXT NOT NULL, role TEXT NOT NULL, "
                "content TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()
    print("memory db:", DB_PATH, "exists:", os.path.exists(DB_PATH), flush=True)


def _db():
    return sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)


# ---- long-term memories: facts and preferences about the user ----

def save_memory(user_id, content):
    content = content.strip()
    if not content:
        return False
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO memories (user_id, content, created_at) VALUES (?, ?, ?)",
                (user_id, content, _now()),
            )
            conn.commit()
        finally:
            conn.close()
    return True


def list_memories(user_id, limit=20):
    with _db_lock:
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT content FROM memories WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        finally:
            conn.close()
    return [r[0] for r in rows]


def delete_memory(user_id, keyword):
    """Delete memories whose content contains the keyword. Returns count."""
    with _db_lock:
        conn = _db()
        try:
            cur = conn.execute(
                "DELETE FROM memories WHERE user_id=? AND content LIKE ?",
                (user_id, "%" + keyword + "%"),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


# ---- to-dos: things the user asked to be reminded of ----

TIME_WORDS = (
    "明天", "後天", "大後天", "下週", "下星期", "下個月",
    "幾號", "早上", "下午", "晚上", "中午", "幾點",
    "週一", "週二", "週三", "週四", "週五", "週六", "週日",
    "星期一", "星期二", "星期三", "星期四", "星期五",
    "星期六", "星期日", "月底", "月初", "這個月",
)


def find_time_word(text):
    for w in TIME_WORDS:
        if w in text:
            return w
    return ""


def save_note(user_id, text, due_text=""):
    text = text.strip()
    if not text:
        return False
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO notes (user_id, text, due_text, done, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (user_id, text, due_text, _now()),
            )
            conn.commit()
        finally:
            conn.close()
    return True


def list_notes(user_id):
    """Undone to-dos, oldest first."""
    with _db_lock:
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT text, due_text FROM notes "
                "WHERE user_id=? AND done=0 ORDER BY id ASC",
                (user_id,),
            ).fetchall()
        finally:
            conn.close()
    return [(r[0], r[1]) for r in rows]


def complete_note(user_id, keyword):
    """Mark the newest matching undone note done. Returns count."""
    with _db_lock:
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT id FROM notes WHERE user_id=? AND done=0 AND text LIKE ? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, "%" + keyword + "%"),
            ).fetchall()
            if not rows:
                return 0
            conn.execute("UPDATE notes SET done=1 WHERE id=?", (rows[0][0],))
            conn.commit()
            return 1
        finally:
            conn.close()


def delete_note(user_id, keyword):
    with _db_lock:
        conn = _db()
        try:
            cur = conn.execute(
                "DELETE FROM notes WHERE user_id=? AND text LIKE ?",
                (user_id, "%" + keyword + "%"),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


# ---- chat history, persisted in SQLite ----

def save_history_turn(user_id, user_text, answer):
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO history (user_id, role, content, created_at) "
                "VALUES (?, 'user', ?, ?)",
                (user_id, user_text, _now()),
            )
            conn.execute(
                "INSERT INTO history (user_id, role, content, created_at) "
                "VALUES (?, 'assistant', ?, ?)",
                (user_id, answer, _now()),
            )
            conn.execute(
                "DELETE FROM history WHERE user_id=? AND id NOT IN "
                "(SELECT id FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?)",
                (user_id, user_id, MAX_HISTORY),
            )
            conn.commit()
        finally:
            conn.close()
    with _history_lock:
        hist = _history.setdefault(user_id, [])
        hist.append({"role": "user", "content": user_text})
        hist.append({"role": "assistant", "content": answer})
        del hist[: max(0, len(hist) - MAX_HISTORY)]


def load_history(user_id):
    with _db_lock:
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT role, content FROM history WHERE user_id=? "
                "ORDER BY id ASC LIMIT ?",
                (user_id, MAX_HISTORY),
            ).fetchall()
        finally:
            conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]


def get_history(user_id):
    """In-memory history, loaded from SQLite on first use after restart."""
    with _history_lock:
        if user_id not in _history:
            _history[user_id] = load_history(user_id)
        return list(_history[user_id])


def _fetch(url, timeout=8):
    req = urllib.request.Request(url, headers=UA, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_stock_info(codes):
    """codes: list of ex_ch strings. Returns a compact text summary."""
    if not codes:
        return ""
    query = "|".join(codes)
    url = (
        "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
        f"?ex_ch={query}&json=1&delay=0"
    )
    try:
        data = json.loads(_fetch(url, timeout=8).decode("utf-8", "ignore"))
    except Exception:
        return ""
    lines = []
    for item in data.get("msgArray", []):
        code = item.get("c", "")
        name = item.get("n", "")
        last = item.get("z", "-")
        prev = item.get("y", "-")
        date = item.get("d", "")
        time_ = item.get("t", "")
        try:
            z = float(last)
            y = float(prev)
            change = z - y
            pct = change / y * 100 if y else 0
            lines.append(
                f"{name}({code})：{z:.2f}，漲跌 {change:+.2f}（{pct:+.2f}%），"
                f"昨收 {y:.2f}，開 {item.get('o', '-')}，高 {item.get('h', '-')}，"
                f"低 {item.get('l', '-')}，{date} {time_}"
            )
        except (TypeError, ValueError):
            lines.append(f"{name}({code})：今日尚無成交價，昨收 {prev}，{date}")
    return "\n".join(lines)


# ---- weather: Open-Meteo for any place, Worker fallback for 西屯 ----

WMO_WEATHER = {
    0: "晴天", 1: "大致晴朗", 2: "局部多雲", 3: "陰天",
    45: "霧", 48: "霧淞",
    51: "輕微毛毛雨", 53: "毛毛雨", 55: "較強毛毛雨",
    56: "輕微凍毛毛雨", 57: "凍毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "輕微凍雨", 67: "凍雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "輕微陣雨", 81: "陣雨", 82: "強陣雨",
    85: "小陣雪", 86: "大陣雪",
    95: "雷陣雨", 96: "輕微冰雹雷雨", 99: "冰雹雷雨",
}
RAIN_CODES = frozenset(
    [51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99]
)

WEATHER_STRIP_WORDS = (
    "會不會冷", "要不要帶傘", "冷不冷", "熱不熱", "會不會",
    "天氣", "下雨", "氣溫", "幾度", "颱風", "大後天", "後天", "明天",
    "今天", "現在", "如何", "會", "嗎", "的", "請問", "一下",
    "幫我查", "查", "給我", "出門",
)
WEATHER_DIRECT_WORDS = (
    "天氣", "下雨", "氣溫", "會不會冷", "颱風",
    "冷不冷", "熱不熱", "要不要帶傘", "現在幾度",
)
DAY_LABELS = ("今天", "明天", "後天", "大後天")
DEFAULT_PLACE = "台中市西屯區"


def parse_weather_request(text):
    """Extract (place_or_None, day_offset, want_now) from a message."""
    t = (text or "").strip()
    want_now = "現在" in t
    if "大後天" in t:
        day_offset = 3
    elif "後天" in t:
        day_offset = 2
    elif "明天" in t:
        day_offset = 1
    else:
        day_offset = 0
    s = t
    for w in WEATHER_STRIP_WORDS:
        s = s.replace(w, "")
    s = re.sub(r"[？\?！!。，、\s]+", "", s)
    return (s or None), day_offset, want_now


# Chinese place name -> English query for the geocoder. GeoNames has no
# Chinese index for Taiwan city names, and some names also exist in
# China (e.g. 高雄 is a town in 四川), so Taiwan places go by their
# English name and prefer a TW result.
TAIWAN_ALIASES = {
    "台北": "Taipei", "新北": "New Taipei", "基隆": "Keelung",
    "桃園": "Taoyuan", "新竹": "Hsinchu", "苗栗": "Miaoli",
    "台中": "Taichung", "彰化": "Changhua", "南投": "Nantou",
    "雲林": "Yunlin", "嘉義": "Chiayi", "台南": "Tainan",
    "高雄": "Kaohsiung", "屏東": "Pingtung", "宜蘭": "Yilan",
    "花蓮": "Hualien", "台東": "Taitung", "澎湖": "Penghu",
    "金門": "Kinmen", "馬祖": "Matsu", "台灣": "Taiwan",
    "西屯": "Taichung", "北屯": "Taichung", "南屯": "Taichung",
}
FOREIGN_ALIASES = {
    "東京": "Tokyo", "大阪": "Osaka", "京都": "Kyoto",
    "首爾": "Seoul", "釜山": "Busan", "香港": "Hong Kong",
    "澳門": "Macau", "新加坡": "Singapore", "曼谷": "Bangkok",
    "倫敦": "London", "巴黎": "Paris", "紐約": "New York",
    "洛杉磯": "Los Angeles", "舊金山": "San Francisco",
    "雪梨": "Sydney", "墨爾本": "Melbourne",
}
CELESTIAL = {"火星", "月球", "太陽", "金星", "木星", "水星", "土星", "冥王星"}

# The geocoder answers in Simplified Chinese; map the simplified
# characters that show up in place / country names back to Traditional.
# (台 is left alone: 台北 is the standard spelling in Taiwan.)
_SIMPLIFIED_TO_TRADITIONAL = {
    "湾": "灣", "国": "國", "罗": "羅", "亚": "亞", "尔": "爾",
    "东": "東", "龙": "龍", "门": "門", "岛": "島", "兰": "蘭",
    "卢": "盧", "达": "達", "乌": "烏", "兹": "茲", "韩": "韓",
    "马": "馬", "乐": "樂", "苏": "蘇", "宾": "賓", "几": "幾",
    "内": "內", "贝": "貝", "发": "發", "广": "廣", "庆": "慶",
    "圣": "聖", "维": "維", "纳": "納", "鲁": "魯", "萨": "薩",
    "图": "圖",
}


def _to_traditional(s):
    return "".join(_SIMPLIFIED_TO_TRADITIONAL.get(ch, ch) for ch in (s or ""))


def _geocode_search(query, count=5):
    """One geocoding call. Returns (status, results)."""
    url = (
        "https://geocoding-api.open-meteo.com/v1/search?name="
        + urllib.parse.quote(query)
        + f"&count={count}&language=zh&format=json"
    )
    try:
        data = json.loads(_fetch(url, timeout=8).decode("utf-8", "ignore"))
    except Exception as exc:
        print("geocode failed:", exc, flush=True)
        return "error", []
    return "ok", data.get("results") or []


def _pick_result(results, prefer_tw):
    if not results:
        return None
    if prefer_tw:
        for r in results:
            if r.get("country_code") == "TW":
                return r
    return results[0]


def _display_name(r):
    name = _to_traditional(r.get("name", ""))
    country = _to_traditional(r.get("country", ""))
    if country and country != "台灣":
        return f"{name}（{country}）"
    return name


def geocode_place(place):
    """Geocode via Open-Meteo. Returns (status, payload).

    status "ok" -> payload (lat, lon, display_name);
    "notfound" -> no such place; "error" -> API unreachable.
    """
    # 1. Taiwan alias first: Chinese names don't resolve, and some
    #    also exist in China (e.g. 高雄 in 四川).
    for key, eng in sorted(TAIWAN_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if place.startswith(key):
            status, results = _geocode_search(eng)
            if status == "error":
                return "error", None
            r = _pick_result(results, prefer_tw=True)
            if r:
                return "ok", (r.get("latitude"), r.get("longitude"), _display_name(r))
            break
    # 2. raw name (works for 東京, 巴黎, ...)
    status, results = _geocode_search(place)
    if status == "error":
        return "error", None
    r = _pick_result(results, prefer_tw=False)
    if r:
        return "ok", (r.get("latitude"), r.get("longitude"), _display_name(r))
    # 3. foreign alias
    for key, eng in sorted(FOREIGN_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if place.startswith(key):
            status, results = _geocode_search(eng)
            if status == "error":
                return "error", None
            r = _pick_result(results, prefer_tw=False)
            if r:
                return "ok", (r.get("latitude"), r.get("longitude"), _display_name(r))
            break
    # 4. strip a trailing 區/市/縣/鄉/鎮/村 and retry
    short = re.sub(r"[區市縣鄉鎮村路街]$", "", place)
    if short and short != place:
        return geocode_place(short)
    return "notfound", None
def fetch_forecast(lat, lon):
    """Open-Meteo forecast (current + 4 days). Returns dict or None."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
        "precipitation,weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max"
        "&timezone=auto&forecast_days=4"
    )
    try:
        return json.loads(_fetch(url, timeout=8).decode("utf-8", "ignore"))
    except Exception as exc:
        print("forecast failed:", exc, flush=True)
        return None


def format_weather(display, fc, day_offset, want_now):
    """Short, casual Traditional Chinese weather reply."""
    cur = fc.get("current") or {}
    daily = fc.get("daily") or {}
    times = daily.get("time") or []
    codes = daily.get("weather_code") or []
    his = daily.get("temperature_2m_max") or []
    los = daily.get("temperature_2m_min") or []
    pops = daily.get("precipitation_probability_max") or []
    if want_now and day_offset == 0:
        code = cur.get("weather_code")
        temp = cur.get("temperature_2m")
        feels = cur.get("apparent_temperature")
        rh = cur.get("relative_humidity_2m")
        wind = cur.get("wind_speed_10m")
        parts = [f"{display}現在：{WMO_WEATHER.get(code, '天氣未知')}"]
        if temp is not None:
            parts.append(f"氣溫 {temp} 度")
        if feels is not None:
            parts.append(f"體感 {feels} 度")
        if rh is not None:
            parts.append(f"濕度 {rh}%")
        if wind is not None:
            parts.append(f"風速 {wind} 公里/時")
        if code in RAIN_CODES:
            parts.append("欸外面正在下雨，出門記得帶把傘喔！")
        return "，".join(parts)
    idx = min(day_offset, max(len(times) - 1, 0))
    label = DAY_LABELS[day_offset]
    date = times[idx] if idx < len(times) else ""
    code = codes[idx] if idx < len(codes) else None
    hi = his[idx] if idx < len(his) else None
    lo = los[idx] if idx < len(los) else None
    pop = pops[idx] if idx < len(pops) else None
    desc = WMO_WEATHER.get(code, "天氣未知")
    head = f"{display}{label}"
    if date and len(date) >= 10:
        head += f"（{date[5:]}）"
    parts = [f"{head}：{desc}"]
    if hi is not None and lo is not None:
        parts.append(f"最高 {hi} 度／最低 {lo} 度")
    elif hi is not None:
        parts.append(f"最高 {hi} 度")
    if pop is not None:
        parts.append(f"降雨機率 {pop}%")
    if code in RAIN_CODES or (pop is not None and pop >= 50):
        parts.append("等下感覺會下雨，出門記得隨手拿把傘喔～")
    return "，".join(parts)


def _weather_fallback(place, default_place):
    """Worker data for 西屯 / 台中 / default; '' otherwise."""
    if default_place or "西屯" in place or "台中" in place:
        w = get_weather_xitun()
        if w:
            return w + "（以上是西屯的資料）"
    return ""


def get_weather(text=""):
    """Weather for the place named in text (default 台中市西屯區).

    Returns a short reply, or '' if nothing could be fetched.
    """
    place, day_offset, want_now = parse_weather_request(text or "")
    default_place = place is None
    if default_place:
        place = DEFAULT_PLACE
    if place in CELESTIAL:
        return f"「{place}」在外太空，沒有天氣預報啦。"
    status, geo = geocode_place(place)
    if status == "ok":
        lat, lon, display = geo
        fc = fetch_forecast(lat, lon)
        if fc is not None:
            return format_weather(display, fc, day_offset, want_now)
        return _weather_fallback(place, default_place)
    # notfound or error: 西屯 / 台中 / default still has the Worker
    if default_place or "西屯" in place or "台中" in place:
        w = get_weather_xitun()
        if w:
            return w + "（以上是西屯的資料）"
    if status == "notfound":
        return f"找不到「{place}」這個地方，換個講法再問我一次。"
    return ""


def get_weather_xitun():
    """Weather for 台中市西屯區 from the user's Cloudflare Worker, or ''."""
    """Returns a short weather summary for 台中市西屯區, or ''."""
    try:
        data = json.loads(_fetch(WEATHER_URL, timeout=8).decode("utf-8", "ignore"))
    except Exception:
        return ""
    try:
        target = None
        for ds in data.get("datasets", []):
            rec = ds.get("data", {}).get("records", {})
            locs = rec.get("Locations")
            if not locs:
                continue
            loc_group = locs[0] if isinstance(locs, list) else locs
            for loc in loc_group.get("Location", []):
                if loc.get("LocationName") == "西屯區":
                    target = loc
                    break
            if target:
                break
        if not target:
            return ""
        elems = {}
        for el in target.get("WeatherElement", []):
            times = el.get("Time", [])
            if not times:
                continue
            vals = times[0].get("ElementValue", [])
            if not vals:
                continue
            first = vals[0]
            val = next(iter(first.values()), "") if isinstance(first, dict) else ""
            elems[el.get("ElementName", "")] = val
        wx = elems.get("天氣現象", "")
        temp = elems.get("平均溫度", "")
        hi = elems.get("最高溫度", "")
        lo = elems.get("最低溫度", "")
        pop = elems.get("12小時降雨機率", "") or elems.get("6小時降雨機率", "")
        rh = elems.get("平均相對濕度", "")
        parts = ["台中市西屯區天氣"]
        if wx:
            parts.append(f"天氣：{wx}")
        if temp:
            t = f"氣溫：{temp} 度"
            if hi and lo:
                t += f"（最高 {hi} / 最低 {lo}）"
            parts.append(t)
        if pop:
            parts.append(f"降雨機率：{pop}%")
        if rh:
            parts.append(f"濕度：{rh}%")
        if len(parts) <= 1:
            return ""
        return "，".join(parts)
    except Exception:
        return ""


def get_news(query):
    """Top 5 Google News headlines for the query, or ''."""
    url = (
        "https://news.google.com/rss/search?q="
        + urllib.request.quote(query)
        + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    )
    try:
        raw = _fetch(url, timeout=8)
        root = ET.fromstring(raw)
    except Exception:
        return ""
    titles = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if title:
            titles.append(title)
        if len(titles) >= 5:
            break
    if not titles:
        return ""
    return "\n".join(f"- {t}" for t in titles)


def build_context(text):
    """Live data relevant to this message, or ''."""
    sections = []
    # stocks: known codes, or market words
    codes = []
    for code, ch in KNOWN_STOCKS.items():
        if code in text:
            codes.append(ch)
    if any(w in text for w in ("大盤", "加權", "台股", "股市")):
        if INDEX_CH not in codes:
            codes.append(INDEX_CH)
    if codes:
        info = get_stock_info(codes)
        if info:
            sections.append("【台股即時行情】\n" + info)
    # weather
    if any(w in text for w in ("天氣", "下雨", "氣溫", "會不會冷", "颱風", "出門")):
        w = get_weather(text)
        if w:
            sections.append("【天氣】\n" + w)
    # news
    if "新聞" in text:
        q = "台灣股市" if any(w in text for w in ("股市", "台股", "股票")) else "台灣"
        news = get_news(q)
        if news:
            sections.append(f"【最新新聞：{q}】\n" + news)
    return "\n\n".join(sections)


# ---- 發票對獎 ----

INVOICE_PRIZES = [
    ("特別獎", "1,000萬元"),
    ("特獎", "200萬元"),
    ("頭獎", "20萬元"),
    ("二獎", "4萬元"),
    ("三獎", "1萬元"),
    ("四獎", "4千元"),
    ("五獎", "1千元"),
    ("六獎", "200元"),
    ("增開六獎", "200元"),
]


def _latest_drawn_period(today=None):
    """Latest invoice period whose draw (25th) has passed.

    Periods are (1,2) (3,4) (5,6) (7,8) (9,10) (11,12); drawn on the
    25th of the month after the period ends, except (11,12) which is
    drawn on Jan 25 of the next year. Returns (roc_year, start_month).
    """
    today = today or datetime.date.today()
    periods = [(1, 3), (3, 5), (5, 7), (7, 9), (9, 11), (11, 1)]
    best = None
    for start, draw_month in periods:
        year = today.year
        if start == 11:
            draw = datetime.date(year + 1, 1, 25)
            period_year = year
        else:
            draw = datetime.date(year, draw_month, 25)
            period_year = year
        if draw <= today:
            best = (period_year - 1911, start)
    if best is None:
        # early January before the (11,12) draw: use last year's (11,12)
        return (today.year - 1 - 1911, 11)
    return best


def fetch_invoice_online():
    """Fetch the latest winning numbers from 財政部稅務入口網.

    Returns a dict like the bundled JSON, or None on failure.
    """
    roc_year, start_month = _latest_drawn_period()
    url = (
        "https://www.etax.nat.gov.tw/etw-main/"
        f"ETW183W2_{roc_year}{start_month:02d}/"
    )
    try:
        html = _fetch(url, timeout=20).decode("utf-8", "ignore")
    except Exception as exc:
        print("invoice fetch failed:", exc, flush=True)
        return None
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    m_special = re.search(r"特別獎\s*(\d{8})", text)
    m_grand = re.search(r"特獎\s*(\d{8})", text)
    m_first = re.search(r"頭獎\s*((?:\d{8}\s*){1,3})", text)
    m_period = re.search(r"(\d+)\s*年\s*(\d+)\s*~\s*(\d+)\s*月", text)
    if not (m_special and m_grand and m_first):
        print("invoice parse failed for", url, flush=True)
        return None
    first = re.findall(r"\d{8}", m_first.group(1))
    period = (
        f"{m_period.group(1)}年{m_period.group(2):>02}-{m_period.group(3):>02}月"
        if m_period
        else f"{roc_year}年{start_month:02d}月"
    )
    return {
        "period": period,
        "special": m_special.group(1),
        "grand": m_grand.group(1),
        "first": first,
        "extra6": [],
        "source": "財政部稅務入口網（即時抓取）",
    }


def load_local_invoice():
    try:
        with open(INVOICE_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print("local invoice json failed:", exc, flush=True)
        return None


def get_invoice_numbers():
    """(data, source_label). Prefers the live eTax page, falls back
    to the bundled winning_numbers.json."""
    data = fetch_invoice_online()
    if data:
        return data, "財政部稅務入口網（即時）"
    data = load_local_invoice()
    if data:
        return data, "內建資料（手動更新）"
    return None, ""


def match_invoice(digits, data):
    """digits: 8 digits. Returns (prize_name, amount) or None."""
    if not data or len(digits) != 8:
        return None
    if digits == data.get("special", ""):
        return ("特別獎", "1,000萬元")
    if digits == data.get("grand", ""):
        return ("特獎", "200萬元")
    first = data.get("first", [])
    if digits in first:
        return ("頭獎", "20萬元")
    if any(digits[-7:] == f[-7:] for f in first):
        return ("二獎", "4萬元")
    if any(digits[-6:] == f[-6:] for f in first):
        return ("三獎", "1萬元")
    if any(digits[-5:] == f[-5:] for f in first):
        return ("四獎", "4千元")
    if any(digits[-4:] == f[-4:] for f in first):
        return ("五獎", "1千元")
    if any(digits[-3:] == f[-3:] for f in first):
        return ("六獎", "200元")
    if digits[-3:] in data.get("extra6", []):
        return ("增開六獎", "200元")
    return None


def invoice_numbers_text(data):
    lines = [f"{data.get('period', '')}統一發票中獎號碼"]
    lines.append(f"特別獎：{data.get('special', '')}（1,000萬元）")
    lines.append(f"特獎：{data.get('grand', '')}（200萬元）")
    lines.append(f"頭獎：{'、'.join(data.get('first', []))}（20萬元）")
    lines.append("二獎：末7碼與頭獎相同（4萬元）")
    lines.append("三獎：末6碼與頭獎相同（1萬元）")
    lines.append("四獎：末5碼與頭獎相同（4千元）")
    lines.append("五獎：末4碼與頭獎相同（1千元）")
    lines.append("六獎：末3碼與頭獎相同（200元）")
    if data.get("extra6"):
        lines.append(f"增開六獎：{'、'.join(data['extra6'])}（200元）")
    if data.get("claim_from") and data.get("claim_to"):
        lines.append(f"領獎期間：{data['claim_from']}～{data['claim_to']}")
    return "\n".join(lines)


def _fullwidth_to_half(s):
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif o == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def find_invoice_number(text):
    """Find a 2-letter + 8-digit invoice number, or None."""
    m = re.search(r"([A-Za-z]{2})(\d{8})", _fullwidth_to_half(text))
    return m.group(2) if m else None


# ---- 附近美食 ----

FOOD_WORDS = ("好吃", "美食", "吃什麼", "推薦吃的", "附近有什麼吃的")
CUISINE_WORDS = ("拉麵", "火鍋", "燒烤", "小吃")
AREA_WORDS = (
    "台北", "新北", "桃園", "新竹", "台中", "彰化", "南投", "雲林",
    "嘉義", "台南", "高雄", "屏東", "宜蘭", "花蓮", "台東", "苗栗",
    "基隆",
)


def detect_food_area(text):
    for area in AREA_WORDS:
        if area in text:
            return area
    return ""


def food_system_prompt(text):
    area = detect_food_area(text)
    place = f"{area}附近" if area else "台中市西屯區永福路附近"
    return (
        "你是「睡不飽」，LINE 上一個很會聊天的朋友。"
        "一律用繁體中文，講話像台灣人平常在 LINE 上聊天，"
        "簡短、直接、自然，不要長篇大論，不要用簡體中文。"
        f"使用者想找吃的，請推薦{place} 3 到 5 家餐廳或小吃，"
        "每家講類型、特色、大概位置（例如哪條路附近）就好。"
        "絕對不要編造詳細地址、門牌號碼或電話號碼，"
        "不知道的就不要寫。"
        "最後加一句：營業時間以店家公告為準。"
    )


# ---- 指令選單 ----

MENU_ITEMS = [
    ("股價", "/股價"),
    ("天氣", "/天氣"),
    ("新聞", "/新聞"),
    ("記事情", "/待辦"),
    ("發票", "/發票"),
    ("美食", "/美食"),
    ("生圖", "/生圖"),
]


def build_menu_message():
    return {
        "type": "text",
        "text": "你想查什麼？點一下就好：",
        "quickReply": {
            "items": [
                {
                    "type": "action",
                    "action": {
                        "type": "message",
                        "label": label,
                        "text": cmd,
                    },
                }
                for label, cmd in MENU_ITEMS
            ]
        },
    }


def download_content(message_id, max_bytes):
    """Download a message's binary content from LINE.

    Returns the bytes, b"" when it exceeds max_bytes, or None
    when the download itself fails.
    """
    req = urllib.request.Request(
        "https://api-data.line.me/v2/bot/message/" + message_id + "/content",
        method="GET",
        headers={"Authorization": "Bearer " + CHANNEL_TOKEN, **UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            chunks = []
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    return b""
            return b"".join(chunks)
    except Exception as exc:
        print("content download failed:", exc, flush=True)
        return None


def extract_file_text(data, filename):
    """Extract readable text from PDF / Word / Excel / plain text.

    Returns (text, kind). Empty text means it could not be read.
    """
    name = (filename or "").lower()
    try:
        if name.endswith(".pdf"):
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            parts = []
            for page in reader.pages[:30]:
                parts.append(page.extract_text() or "")
            return "\n".join(parts), "PDF"
        if name.endswith(".docx"):
            from docx import Document

            doc = Document(io.BytesIO(data))
            parts = [p.text for p in doc.paragraphs]
            for table in doc.tables:
                for row in table.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            return "\n".join(parts), "Word"
        if name.endswith(".xlsx"):
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets[:5]:
                parts.append(f"【工作表：{ws.title}】")
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= 100:
                        break
                    cells = ["" if v is None else str(v) for v in row]
                    while cells and cells[-1] == "":
                        cells.pop()
                    if cells:
                        parts.append(" | ".join(cells))
            return "\n".join(parts), "Excel"
        if name.endswith((".txt", ".md", ".csv", ".json", ".log")):
            return data.decode("utf-8", "ignore"), "文字檔"
    except Exception as exc:
        print("file parse failed:", exc, flush=True)
        return "", "檔案"
    try:
        text = data.decode("utf-8")
        if text.strip():
            return text, "文字檔"
    except Exception:
        pass
    return "", "檔案"


def extract_video_frames(data):
    """Extract up to 6 JPEG frames from video bytes.

    Returns a list of base64 strings, empty when ffmpeg fails.
    """
    tmpdir = tempfile.mkdtemp(prefix="sbb-video-")
    try:
        src = os.path.join(tmpdir, "input.mp4")
        with open(src, "wb") as f:
            f.write(data)
        pattern = os.path.join(tmpdir, "frame_%d.jpg")
        cmd = [
            "ffmpeg", "-y", "-v", "error", "-i", src,
            "-vf", "fps=1/10,scale=640:-1", "-frames:v", "6", pattern,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode != 0:
            print(
                "ffmpeg failed:",
                proc.stderr.decode("utf-8", "ignore")[:300],
                flush=True,
            )
            return []
        frames = []
        for i in range(1, 7):
            path = os.path.join(tmpdir, f"frame_{i}.jpg")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    frames.append(base64.b64encode(f.read()).decode())
        return frames
    except Exception as exc:
        print("video frames failed:", exc, flush=True)
        return []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def ask_vision(image_urls, question, user_id, label="圖片"):
    """Ask the vision model about one or more image data URLs."""
    content = [{"type": "text", "text": question}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    payload = json.dumps(
        {
            "model": VISION_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是「睡不飽」，LINE 上一個很會聊天的朋友。"
                        "一律用繁體中文，講話像台灣人平常在 LINE 上聊天："
                        "簡短、直接、自然，不用客套。"
                        "回話一兩句就好，不要長篇大論。不要用簡體中文。"
                    ),
                },
                {"role": "user", "content": content},
            ],
            "temperature": 0.7,
        }
    ).encode()
    req = urllib.request.Request(
        APINEX_BASE_URL + "/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + APINEX_API_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    answer = data["choices"][0]["message"]["content"].strip()
    if not answer:
        raise ValueError("empty vision reply")
    answer = answer[:4000]
    save_history_turn(user_id, f"[{label}] {question}", answer)
    return answer


def image_data_url(data):
    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return "data:" + mime + ";base64," + base64.b64encode(data).decode()


def verify_signature(body: bytes, signature: str) -> bool:
    if not CHANNEL_SECRET:
        return True
    if not signature:
        return False
    digest = hmac.new(
        CHANNEL_SECRET.encode(), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def post_chat(base_url, api_key, model, messages, timeout):
    """One OpenAI-compatible chat completions call. Returns the text."""
    payload = json.dumps(
        {"model": model, "messages": messages, "temperature": 0.7}
    ).encode()
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    answer = data["choices"][0]["message"]["content"].strip()
    if not answer:
        raise ValueError("empty AI reply")
    return answer


def ask_ai(text: str, user_id: str, context: str, system_prompt=None) -> str:
    system = system_prompt or SYSTEM_PROMPT
    if context:
        system += "\n\n" + context
    if not system_prompt:
        # long-term memories go into the system prompt so the bot
        # can actually use them
        mems = list_memories(user_id, limit=10)
        if mems:
            system += "\n\n【關於這個使用者的記憶】\n" + "\n".join(
                "- " + m for m in reversed(mems)
            )
    messages = [{"role": "system", "content": system}]
    messages.extend(get_history(user_id))
    messages.append({"role": "user", "content": text})
    # Groq first: fast and free. Apinex is the second fallback,
    # Mistral (ministral-8b) the third, AgentScope (qwen3.8-flash)
    # the fourth, Requesty (gemma-4-31b-it) the last resort.
    if GROQ_API_KEY:
        try:
            answer = post_chat(
                GROQ_BASE_URL, GROQ_API_KEY, GROQ_MODEL, messages, 10
            )
        except Exception as exc:
            print("Groq failed, falling back to Apinex:", exc, flush=True)
        else:
            answer = answer[:4000]
            save_history_turn(user_id, text, answer)
            return answer
    if APINEX_API_KEY:
        try:
            answer = post_chat(
                APINEX_BASE_URL, APINEX_API_KEY, APINEX_MODEL, messages, 25
            )
        except Exception as exc:
            print("Apinex failed, falling back to Mistral:", exc, flush=True)
        else:
            answer = answer[:4000]
            save_history_turn(user_id, text, answer)
            return answer
    if MISTRAL_API_KEY:
        try:
            answer = post_chat(
                MISTRAL_BASE_URL, MISTRAL_API_KEY, MISTRAL_MODEL, messages, 25
            )
        except Exception as exc:
            print("Mistral failed, falling back to AgentScope:", exc, flush=True)
        else:
            answer = answer[:4000]
            save_history_turn(user_id, text, answer)
            return answer
    if AGENTSCOPE_API_KEY:
        try:
            answer = post_chat(
                AGENTSCOPE_BASE_URL, AGENTSCOPE_API_KEY, AGENTSCOPE_MODEL, messages, 25
            )
        except Exception as exc:
            print("AgentScope failed, falling back to Requesty:", exc, flush=True)
        else:
            answer = answer[:4000]
            save_history_turn(user_id, text, answer)
            return answer
    if not REQUESTY_API_KEY:
        raise ValueError("no AI key set")
    answer = post_chat(
        REQUESTY_BASE_URL, REQUESTY_API_KEY, REQUESTY_MODEL, messages, 60
    )
    answer = answer[:4000]
    save_history_turn(user_id, text, answer)
    return answer


MAX_TTS_CHARS = 200


def _tts_once(read_text: str):
    """One Gemini TTS attempt. Returns (filename, duration_ms) or None."""
    pcm_path = None
    mp3_path = None
    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + GEMINI_TTS_MODEL
            + ":generateContent?key="
            + urllib.parse.quote(GEMINI_API_KEY, safe="")
        )
        payload = json.dumps({
            "contents": [{"parts": [{"text": read_text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": GEMINI_TTS_VOICE}
                    }
                },
            },
        }).encode()
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json", **UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            print("tts http error:", exc.code, flush=True)
            return None
        try:
            part = result["candidates"][0]["content"]["parts"][0]
        except Exception:
            print("tts bad shape:", list(result.keys()),
                  [list(c.keys()) for c in result.get("candidates", [])][:1],
                  flush=True)
            return None
        inline = part.get("inlineData") or {}
        audio_b64 = inline.get("data") or ""
        if not audio_b64:
            print("tts no audio data", flush=True)
            return None
        raw = base64.b64decode(audio_b64)
        name = uuid.uuid4().hex + ".mp3"
        pcm_path = os.path.join(tempfile.gettempdir(), uuid.uuid4().hex + ".pcm")
        with open(pcm_path, "wb") as f:
            f.write(raw)
        mp3_path = os.path.join(AUDIO_DIR, name)
        if "L16" in (inline.get("mimeType") or ""):
            cmd = ["ffmpeg", "-y", "-v", "error",
                   "-f", "s16le", "-ar", "24000", "-ac", "1",
                   "-i", pcm_path,
                   "-codec:a", "libmp3lame", "-q:a", "4", mp3_path]
        else:
            wav_path = pcm_path + ".wav"
            os.rename(pcm_path, wav_path)
            pcm_path = wav_path
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", pcm_path,
                   "-codec:a", "libmp3lame", "-q:a", "4", mp3_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            print("ffmpeg tts failed:", proc.stderr[:200], flush=True)
            try:
                os.remove(mp3_path)
            except Exception:
                pass
            return None
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", mp3_path],
            capture_output=True, text=True, timeout=30)
        duration_ms = int(float(probe.stdout.strip()) * 1000)
        if duration_ms <= 0 or duration_ms > 60000:
            try:
                os.remove(mp3_path)
            except Exception:
                pass
            return None
        return name, duration_ms
    except Exception as exc:
        print("tts failed:", exc, flush=True)
        return None
    finally:
        if pcm_path:
            try:
                os.remove(pcm_path)
            except Exception:
                pass


_TTS_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "\U0000200D"
    "]+"
)


def _tts_text(text: str) -> str:
    """Make text safe for TTS: drop emoji, flatten newlines.

    Emoji and line breaks can make the TTS model try to
    generate text instead of reading aloud (HTTP 400).
    """
    cleaned = _TTS_EMOJI_RE.sub("", text)
    return " ".join(cleaned.split()).strip()


def _probe_mp3_duration(mp3_path):
    """ffprobe an MP3 and return duration in ms, or None if invalid."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", mp3_path],
        capture_output=True, text=True, timeout=30)
    try:
        duration_ms = int(float(probe.stdout.strip()) * 1000)
    except Exception:
        return None
    if duration_ms <= 0 or duration_ms > 60000:
        return None
    return duration_ms


def _tts_edge_once(clean_text: str):
    """One Edge TTS attempt (Taiwanese Mandarin, free, no key).

    Returns (filename, duration_ms) or None.
    """
    try:
        import asyncio
        import edge_tts
    except Exception as exc:
        print("edge tts import failed:", exc, flush=True)
        return None
    name = uuid.uuid4().hex + ".mp3"
    mp3_path = os.path.join(AUDIO_DIR, name)
    try:
        async def _run():
            communicate = edge_tts.Communicate(clean_text, EDGE_TTS_VOICE)
            await communicate.save(mp3_path)
        asyncio.run(_run())
    except Exception as exc:
        print("edge tts failed:", exc, flush=True)
        try:
            os.remove(mp3_path)
        except Exception:
            pass
        return None
    duration_ms = _probe_mp3_duration(mp3_path)
    if duration_ms is None:
        try:
            os.remove(mp3_path)
        except Exception:
            pass
        return None
    return name, duration_ms


def synthesize_speech(text: str):
    """Text -> spoken MP3 via Edge TTS (Taiwanese Mandarin).

    Edge TTS first (free, no key, zh-TW voice); if it fails,
    falls back to Gemini TTS. Returns (filename, duration_ms),
    or None when TTS is unavailable, the text is too long, or
    synthesis fails.
    """
    if not VOICE_ENABLED:
        # Voice replies are paused; re-enable with VOICE_ENABLED=1.
        return None
    if not text or len(text) > MAX_TTS_CHARS:
        return None
    clean = _tts_text(text)
    if not clean:
        return None
    audio = _tts_edge_once(clean)
    if audio is not None:
        return audio
    if not GEMINI_API_KEY:
        return None
    # Frame the text as a read-aloud instruction. Plain text
    # makes the TTS model try to answer it instead (HTTP 400);
    # the "Say ..." framing tells it to only read aloud.
    audio = _tts_once("Say in a cheerful tone: " + clean)
    if audio is None:
        # Quote it and try once more. Wait first: the free
        # tier rate-limits back-to-back requests (HTTP 429).
        time.sleep(5)
        audio = _tts_once("\u300c" + clean + "\u300d")
    return audio


def reply_message(reply_token: str, text: str) -> None:
    messages = [{"type": "text", "text": text}]
    audio = synthesize_speech(text)
    if audio is not None:
        name, duration_ms = audio
        messages.append({
            "type": "audio",
            "originalContentUrl": PUBLIC_URL + "/audio/" + name,
            "duration": duration_ms,
        })
    reply_message_full(reply_token, messages)


def reply_message_full(reply_token: str, messages: list) -> None:
    if not CHANNEL_TOKEN:
        print("no LINE_CHANNEL_ACCESS_TOKEN, skipping reply", flush=True)
        return
    payload = json.dumps(
        {"replyToken": reply_token, "messages": messages}
    ).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/reply",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + CHANNEL_TOKEN,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        print("reply status", resp.status, flush=True)


def generate_image(prompt: str) -> bytes:
    """Call Agnes image generation. Returns PNG bytes."""
    payload = json.dumps(
        {
            "model": AGNES_IMAGE_MODEL,
            "prompt": prompt,
            "size": "1K",
            "ratio": "1:1",
            "extra_body": {"response_format": "b64_json"},
        }
    ).encode()
    req = urllib.request.Request(
        AGNES_BASE_URL + "/images/generations",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + AGNES_API_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    items = data.get("data") or []
    if not items or not items[0].get("b64_json"):
        raise ValueError("no image in Agnes reply")
    return base64.b64decode(items[0]["b64_json"])


def handle_draw(reply_token: str, prompt: str, user_id: str) -> None:
    """Generate an image in a background thread and reply with it."""
    try:
        if not AGNES_API_KEY:
            reply_message(reply_token, "生圖還沒設定好，等我一下。")
            return
        png = generate_image(prompt)
        name = uuid.uuid4().hex + ".png"
        path = os.path.join(IMG_DIR, name)
        with open(path, "wb") as f:
            f.write(png)
        url = PUBLIC_URL + "/img/" + name
        reply_message_full(
            reply_token,
            [{"type": "image", "originalContentUrl": url, "previewImageUrl": url}],
        )
    except Exception as exc:
        print("draw failed:", exc, flush=True)
        try:
            reply_message(reply_token, "這張圖生不出來，你換個描述再試一次。")
        except Exception:
            pass


# ---- command routing ----

def _record_direct(user_id, user_text, answer):
    """Record a directly-answered command turn in history."""
    try:
        save_history_turn(user_id, user_text, answer)
    except Exception as exc:
        print("history save failed:", exc, flush=True)


def _stock_reply():
    codes = [KNOWN_STOCKS[c] for c in ("0050", "0056", "00878", "00406A", "009826")]
    codes.append(INDEX_CH)
    info = get_stock_info(codes)
    return info or "現在查不到股價，你晚點再問我。"


def _weather_reply(text=""):
    return get_weather(text) or "欸氣象現在查不到耶，我腦袋卡住了，你晚點再來問我好不好？"


def _news_reply():
    news = get_news("台灣")
    return f"最新新聞：\n{news}" if news else "糟糕，現在抓不到新聞，你過一下下再來問我啦！"


def _memories_reply(user_id):
    mems = list_memories(user_id)
    if not mems:
        return "我腦袋裡面還沒記你的事捏，想讓我幫忙記的話，跟我說「記住…」就好囉！"
    lines = ["翻了一下我幫你記的，有這些喔："]
    for i, m in enumerate(reversed(mems), 1):
        lines.append(f"{i}. {m}")
    return "\n".join(lines)


def _notes_reply(user_id):
    notes = list_notes(user_id)
    if not notes:
        return "你目前完全沒有待辦事項欸，過得滿爽的嘛，很閒喔～"
    lines = ["你還沒做的事："]
    for i, (text, due) in enumerate(notes, 1):
        lines.append(f"{i}. {text}" + (f"（{due}）" if due else ""))
    return "\n".join(lines)


def _invoice_reply(user_id, digits=None):
    data, source = get_invoice_numbers()
    if not data:
        return "現在查不到中獎號碼，你晚點再問我。"
    if digits:
        hit = match_invoice(digits, data)
        head = invoice_numbers_text(data)
        if hit:
            return (
                f"中了！{hit[0]}，獎金 {hit[1]}。\n\n{head}\n"
                f"（資料來源：{source}）"
            )
        return (
            f"這張沒中，差一點點而已，下次再試。\n\n{head}\n"
            f"（資料來源：{source}）"
        )
    return (
        f"{invoice_numbers_text(data)}\n"
        "把你的發票號碼傳給我（2 個英文字母 + 8 個數字，"
        "例如 AB12345678），我幫你對。\n"
        f"（資料來源：{source}）"
    )


def handle_command(text, user_id):
    """Handle a command. Returns (handled, reply_text_or_None).

    reply_text None means the caller should send a quickReply menu
    instead of a plain text reply.
    """
    t = text.strip()
    # menu
    if t in ("/選單", "選單", "功能", "help"):
        return True, None
    # image generation: /生圖 xxx / 生圖 xxx / 幫我畫 xxx
    if t in ("/生圖", "生圖", "幫我畫"):
        return True, "你想畫什麼？跟我說一下，我幫你畫。"
    m = re.match(r"^(?:/生圖|生圖|幫我畫)\s*(.+)$", t, re.DOTALL)
    if m:
        prompt = m.group(1).strip()
        if not prompt:
            return True, "你想畫什麼？跟我說一下，我幫你畫。"
        return True, "DRAW:" + prompt[:200]
    # stocks / weather / news
    if t == "/股價":
        return True, _stock_reply()
    if t == "/天氣":
        return True, _weather_reply()
    # free-text weather questions: "台北天氣", "高雄明天會下雨嗎"
    if any(w in t for w in WEATHER_DIRECT_WORDS):
        if not any(c in t for c in KNOWN_STOCKS) and "新聞" not in t:
            return True, _weather_reply(t)
    if t == "/新聞":
        return True, _news_reply()
    # invoice
    if t in ("/發票", "對獎", "發票對獎"):
        return True, _invoice_reply(user_id)
    # to-dos
    if t in ("/待辦", "待辦", "我的待辦", "我有什麼事", "還有什麼事"):
        return True, _notes_reply(user_id)
    # memories
    if t in ("你記得什麼", "我的記憶", "你記得我什麼"):
        return True, _memories_reply(user_id)
    # remember: /記住 xxx / 記住 xxx / 幫我記住 xxx
    m = re.match(r"^(?:/記住|幫我記住|記住)\s*(.+)$", t, re.DOTALL)
    if m:
        content = m.group(1).strip()
        if content:
            due = find_time_word(content)
            if due:
                save_note(user_id, content, due)
                return True, f"沒問題，幫你記好了喔：{content}（要在 {due} 搞定）"
            save_memory(user_id, content)
            return True, f"收到！我記在腦袋裡了：{content}"
    # forget a memory: 忘記 xxx / 忘掉 xxx
    m = re.match(r"^(?:忘記|忘掉)\s*(.+)$", t, re.DOTALL)
    if m:
        keyword = m.group(1).strip()
        if keyword:
            n = delete_memory(user_id, keyword)
            if n:
                return True, f"OK，關於「{keyword}」的事我通通忘掉囉！"
            return True, f"欸我想一下…腦袋裡找不到跟「{keyword}」有關的事耶。"
    # to-do done: 做完了 xxx / 完成了 xxx
    m = re.match(r"^(?:做完了|完成了|做完|完成)\s*(.+)$", t, re.DOTALL)
    if m:
        keyword = m.group(1).strip()
        if keyword:
            if complete_note(user_id, keyword):
                return True, f"太棒了！「{keyword}」搞定，幫你打勾勾囉～"
            return True, f"欸我翻了一下沒看到「{keyword}」這件事耶，你先去待辦清單看一下？"
    # to-do delete: 刪掉 xxx
    m = re.match(r"^刪掉\s*(.+)$", t, re.DOTALL)
    if m:
        keyword = m.group(1).strip()
        if keyword:
            n = delete_note(user_id, keyword)
            if n:
                return True, f"好勒，幫你把「{keyword}」從待辦清單刪掉囉。"
            return True, f"奇怪，我找了一下沒看到「{keyword}」這件事。"
    # invoice number in the message (2 letters + 8 digits)
    digits = find_invoice_number(t)
    if digits:
        return True, _invoice_reply(user_id, digits)
    return False, None


def handle_text(reply_token: str, text: str, user_id: str) -> None:
    try:
        handled, answer = handle_command(text, user_id)
        if handled:
            if answer is None:
                reply_message_full(reply_token, [build_menu_message()])
                _record_direct(user_id, text, "（選單）")
            elif answer.startswith("DRAW:"):
                threading.Thread(
                    target=handle_draw,
                    args=(reply_token, answer[5:], user_id),
                    daemon=True,
                ).start()
                _record_direct(user_id, text, "（生圖中）")
            else:
                reply_message(reply_token, answer)
                _record_direct(user_id, text, answer)
            return
        # food: ask the AI with a food-specific system prompt
        if any(w in text for w in FOOD_WORDS) or any(
            w in text for w in CUISINE_WORDS
        ):
            if GROQ_API_KEY or APINEX_API_KEY:
                answer = ask_ai(
                    text, user_id, "", system_prompt=food_system_prompt(text)
                )
            else:
                answer = "我現在沒辦法幫你找吃的，晚點再問我。"
            reply_message(reply_token, answer)
            return
        if GROQ_API_KEY or APINEX_API_KEY:
            context = build_context(text)
            answer = ask_ai(text, user_id, context)
        else:
            answer = "收到：" + text
    except Exception as exc:
        print("AI failed, falling back to echo:", exc, flush=True)
        answer = "收到：" + text
    try:
        reply_message(reply_token, answer)
    except Exception as exc:
        print("reply failed:", exc, flush=True)


def transcribe_audio(data: bytes):
    """Transcribe voice audio with Groq Whisper.

    Returns the transcribed text, or None when it fails.
    """
    if not GROQ_API_KEY:
        return None
    boundary = "----muse-audio-" + uuid.uuid4().hex
    body = io.BytesIO()
    body.write(("--" + boundary + "\r\n").encode())
    body.write(
        b'Content-Disposition: form-data; name="model"\r\n\r\n'
        b"whisper-large-v3-turbo\r\n"
    )
    body.write(("--" + boundary + "\r\n").encode())
    body.write(
        b'Content-Disposition: form-data; name="language"\r\n\r\n'
        b"zh\r\n"
    )
    body.write(("--" + boundary + "\r\n").encode())
    body.write(
        b'Content-Disposition: form-data; name="file"; '
        b'filename="voice.m4a"\r\n'
        b"Content-Type: audio/m4a\r\n\r\n"
    )
    body.write(data)
    body.write(("\r\n--" + boundary + "--\r\n").encode())
    req = urllib.request.Request(
        GROQ_BASE_URL.rstrip("/") + "/audio/transcriptions",
        data=body.getvalue(),
        method="POST",
        headers={
            "Authorization": "Bearer " + GROQ_API_KEY,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            **UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as exc:
        print("transcribe failed:", exc, flush=True)
        return None
    text = (result.get("text") or "").strip()
    return text or None


def handle_media(reply_token: str, message: dict, user_id: str) -> None:
    """Images, videos and files: download, understand, reply."""
    mtype = message.get("type", "")
    mid = message.get("id", "")
    try:
        if mtype == "image":
            data = download_content(mid, MAX_IMAGE_BYTES)
            if data is None:
                answer = "圖片下載失敗，你再傳一次看看。"
            elif data == b"":
                answer = "這張圖片太大了，我吃不下，傳小一點的給我。"
            else:
                answer = ask_vision(
                    [image_data_url(data)],
                    "這張圖片裡有什麼？簡短講重點就好。",
                    user_id,
                )
        elif mtype == "video":
            data = download_content(mid, MAX_VIDEO_BYTES)
            if data is None:
                answer = "影片下載失敗，你再傳一次看看。"
            elif data == b"":
                answer = "這支影片太大了，先傳短一點的給我。"
            else:
                frames = extract_video_frames(data)
                if not frames:
                    answer = "這支影片我打不開，換個格式或短一點的再傳一次。"
                else:
                    urls = [
                        "data:image/jpeg;base64," + f for f in frames
                    ]
                    answer = ask_vision(
                        urls,
                        "這是影片裡每隔幾秒抓的幾個畫面，大概在拍什麼？簡短講重點就好。",
                        user_id,
                        label="影片",
                    )
        elif mtype == "file":
            filename = message.get("fileName", "檔案")
            data = download_content(mid, MAX_FILE_BYTES)
            if data is None:
                answer = "檔案下載失敗，你再傳一次看看。"
            elif data == b"":
                answer = "這個檔案太大了，我吃不下，先傳小一點的。"
            else:
                text, kind = extract_file_text(data, filename)
                text = text.strip()
                if not text:
                    answer = (
                        f"這個{kind}檔案我打不開，可能是掃描圖片型的"
                        "或格式不支援，換個檔案再試試。"
                    )
                else:
                    if len(text) > MAX_EXTRACT_CHARS:
                        text = (
                            text[:MAX_EXTRACT_CHARS]
                            + "\n（內容太長，只讀了前面一部分）"
                        )
                    answer = ask_ai(
                        f"以下是{filename}（{kind}）的內容，"
                        "請用繁體中文簡短摘要重點：\n\n" + text,
                        user_id,
                        "",
                    )
        elif mtype == "audio":
            data = download_content(mid, MAX_AUDIO_BYTES)
            if data is None:
                answer = "語音下載失敗，你再傳一次看看。"
            elif data == b"":
                answer = "這段語音太長了，先傳短一點的。"
            else:
                text = transcribe_audio(data)
                if not text:
                    answer = "這段語音我聽不清楚，你再講一次看看，或改用打字的。"
                else:
                    handle_text(reply_token, text, user_id)
                    return
        else:
            return
    except Exception as exc:
        print("media failed:", exc, flush=True)
        answer = "這個我現在讀不了，你再傳一次看看，或是改傳文字給我。"
    try:
        reply_message(reply_token, answer)
    except Exception as exc:
        print("reply failed:", exc, flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

    def _send(self, code, body=b"ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        m = re.match(r"^/img/([A-Za-z0-9_-]+\.png)$", self.path)
        if m:
            path = os.path.join(IMG_DIR, m.group(1))
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except Exception:
                self._send(404, b"not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        m = re.match(r"^/audio/([A-Za-z0-9_-]+\.mp3)$", self.path)
        if m:
            path = os.path.join(AUDIO_DIR, m.group(1))
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except Exception:
                self._send(404, b"not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send(200, b"ok")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path not in ("/callback", "/webhook"):
            self._send(404, b"not found")
            return
        if not verify_signature(body, self.headers.get("X-Line-Signature", "")):
            print("bad signature", flush=True)
            self._send(401, b"bad signature")
            return
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            data = {}
        print("events:", json.dumps(data, ensure_ascii=False)[:2000], flush=True)
        for event in data.get("events", []):
            if event.get("type") != "message":
                continue
            message = event.get("message", {})
            mtype = message.get("type", "")
            reply_token = event.get("replyToken")
            if not reply_token:
                continue
            user_id = (event.get("source") or {}).get("userId", "unknown")
            if mtype == "text":
                text = message.get("text", "")
                threading.Thread(
                    target=handle_text, args=(reply_token, text, user_id), daemon=True
                ).start()
            elif mtype in ("image", "video", "file", "audio"):
                threading.Thread(
                    target=handle_media,
                    args=(reply_token, message, user_id),
                    daemon=True,
                ).start()
        self._send(200, b"ok")


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("listening on", PORT, flush=True)
    server.serve_forever()
