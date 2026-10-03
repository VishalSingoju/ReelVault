import base64, glob, json, mimetypes, os, re, shutil, sqlite3, subprocess, tempfile, threading
from urllib.parse import urlparse, urlunparse
import requests
from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

DB = os.environ.get("DB_PATH", "vault.db")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
lock = threading.Lock()
app = FastAPI()

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY, url TEXT UNIQUE, status TEXT DEFAULT 'pending',
        author TEXT, caption TEXT, summary TEXT, topic TEXT, tags TEXT, points TEXT,
        error TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)""")
    for col in ("text", "headline"):
        try: c.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError: pass

def normalize(url):
    u = urlparse(url.strip())
    return urlunparse(("https", "www.instagram.com", u.path.rstrip("/") + "/", "", "", ""))

# ---------- stage 1: capture (instant, dumb) ----------
@app.post("/api/save")
def save(payload: dict, bg: BackgroundTasks):
    m = re.search(r"https?://(?:www\.)?instagram\.com/\S+", payload.get("url", ""))
    if not m:
        return {"ok": False, "error": "Not an Instagram link"}
    url = normalize(m.group(0))
    with lock, db() as c:
        row = c.execute("SELECT id FROM items WHERE url=?", (url,)).fetchone()
        if row:
            return {"ok": True, "id": row["id"], "duplicate": True}
        item_id = c.execute("INSERT INTO items(url) VALUES(?)", (url,)).lastrowid
    bg.add_task(process, item_id)
    return {"ok": True, "id": item_id}

@app.get("/share")  # PWA share target lands here, saves, then shows the app
def share(bg: BackgroundTasks, url: str = "", text: str = "", title: str = ""):
    save({"url": f"{url} {text} {title}"}, bg)
    return RedirectResponse("/?saved=1")

# ---------- stage 2: enrich (caption + the actual video/images) ----------
MAX_BYTES = 14_000_000  # Gemini inline limit is ~20MB after base64

def fetch_meta(url):
    d = tempfile.mkdtemp()
    meta = {"author": None, "caption": "", "media": []}
    try:
        cmd = ["yt-dlp", "--no-warnings", "--write-info-json", "--max-filesize", "14M",
               "--playlist-items", "1-8", "-o", f"{d}/%(autonumber)s.%(ext)s", url]
        if os.environ.get("YTDLP_COOKIES_BROWSER"):  # e.g. chrome, firefox
            cmd[1:1] = ["--cookies-from-browser", os.environ["YTDLP_COOKIES_BROWSER"]]
        subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        for f in sorted(glob.glob(f"{d}/*.info.json")):
            info = json.load(open(f))
            meta["author"] = meta["author"] or info.get("uploader") or info.get("channel")
            meta["caption"] = meta["caption"] or info.get("description") or info.get("title") or ""
        total = 0
        for f in sorted(glob.glob(f"{d}/*")):
            mime = mimetypes.guess_type(f)[0] or ""
            if not mime.startswith(("video/", "image/")):
                continue
            data = open(f, "rb").read()
            if total + len(data) <= MAX_BYTES:
                total += len(data)
                meta["media"].append((mime, data))
    except Exception:
        pass
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if not meta["media"] or not meta["caption"]:  # fallback: public og: tags (caption + cover image)
        html = requests.get(url, headers={"User-Agent": "facebookexternalhit/1.1"}, timeout=20).text
        og = lambda k: (re.search(rf'<meta[^>]+property="og:{k}"[^>]+content="([^"]*)"', html) or [None, ""])[1]
        meta["caption"] = meta["caption"] or og("description") or og("title")
        meta["author"] = meta["author"] or og("title")
        img = og("image").replace("&amp;", "&")
        if img and not meta["media"]:
            try:
                r = requests.get(img, timeout=20)
                meta["media"].append((r.headers.get("content-type", "image/jpeg"), r.content))
            except Exception:
                pass
    if not meta["caption"] and not meta["media"]:
        raise RuntimeError("Instagram blocked the fetch (private post or login wall). Try setting YTDLP_COOKIES_BROWSER=chrome")
    return meta

# ---------- stage 3: understand + stage 4: organize ----------
def analyse(meta, topics):
    prompt = f"""You are extracting information from an Instagram post/reel so the owner never has to open it again.
Author: {meta['author']}
Caption: {meta['caption'][:4000]}
The attached video/images are the post itself.

Existing topics: {json.dumps(topics)}
Return ONLY JSON with these keys:
"headline": a title of at most 8 words saying what the post gives you (e.g. "5-minute high-protein breakfast bowls").
"summary": 2 sentences on what the post is about.
"topic": reuse an existing topic if it fits; otherwise a new short one (1-3 words).
"tags": 3-6 lowercase tags.
"text": ALL readable text, verbatim, in the original language: on-screen text and slides in order, then the spoken words (transcribed). Use "\n" between slides/sections. Empty string if there is none.
"points": the concrete, usable information as short items: exact steps, names, numbers, quantities, prices, tools, places, links, recipes, tips. Never write vague lines like "the video explains X"; state X itself."""
    parts = [{"text": prompt}] + [{"inline_data": {"mime_type": m, "data": base64.b64encode(b).decode()}}
                                  for m, b in meta["media"]]
    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        json={"contents": [{"parts": parts}],
              "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 4096}},
        timeout=180)
    if r.status_code >= 400:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:200]}")
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])

def process(item_id):
    try:
        with db() as c:
            url = c.execute("SELECT url FROM items WHERE id=?", (item_id,)).fetchone()["url"]
            topics = [r[0] for r in c.execute("SELECT DISTINCT topic FROM items WHERE topic IS NOT NULL")]
        meta = fetch_meta(url)
        a = analyse(meta, topics)
        with db() as c:
            c.execute("""UPDATE items SET status='done', author=?, caption=?, summary=?, topic=?,
                         tags=?, points=?, text=?, headline=?, error=NULL WHERE id=?""",
                      (meta["author"], meta["caption"], a["summary"], a["topic"].strip().title(),
                       json.dumps(a["tags"]), json.dumps(a["points"]), (a.get("text") or "").strip(), (a.get("headline") or "").strip(), item_id))
    except Exception as e:
        with db() as c:  # link is never lost; mark failed so it can be retried
            c.execute("UPDATE items SET status='failed', error=? WHERE id=?", (str(e)[:300], item_id))

@app.post("/api/retry/{item_id}")
def retry(item_id: int, bg: BackgroundTasks):
    with db() as c:
        c.execute("UPDATE items SET status='pending', error=NULL WHERE id=?", (item_id,))
    bg.add_task(process, item_id)
    return {"ok": True}

# ---------- read ----------
def row(r):
    d = dict(r)
    for k in ("tags", "points"):
        d[k] = json.loads(d[k]) if d[k] else []
    return d

@app.get("/api/items")
def items(q: str = Query("")):
    sql, args = "SELECT * FROM items", []
    if q:
        sql += " WHERE " + " AND ".join("(summary||' '||IFNULL(caption,'')||' '||IFNULL(text,'')||' '||IFNULL(headline,'')||' '||IFNULL(tags,'')||' '||IFNULL(topic,'')) LIKE ?" for _ in q.split())
        args = [f"%{w}%" for w in q.split()]
    with db() as c:
        return [row(r) for r in c.execute(sql + " ORDER BY id DESC", args)]

app.mount("/", StaticFiles(directory="static", html=True), name="static")