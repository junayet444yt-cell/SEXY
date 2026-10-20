import base64, glob, hashlib, os, re, tempfile, threading, time, uuid
from urllib.parse import quote
import requests
from flask import Flask, Response, abort, jsonify, request, send_from_directory, stream_with_context

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
os.makedirs(CACHE, exist_ok=True)
app = Flask(__name__)
lock = threading.Lock()
STREAMS = {}
PASSWORD = os.environ.get("EROR_PASSWORD")
PROXY = os.environ.get("YTDLP_PROXY")  # optional, e.g. http://user:pass@host:port
dl_lock = threading.Lock()
PUBLIC = {"/api/ping", "/manifest.webmanifest", "/sw.js", "/icon-192.png", "/icon-512.png"}  # app-install files, no password

# Optional login cookies (helps with Instagram/Facebook). Either put cookies.txt next to server.py,
# set YTDLP_COOKIES=/path/to/cookies.txt, or set YTDLP_COOKIES_B64 to the base64 of the file.
COOKIES = os.environ.get("YTDLP_COOKIES") or os.path.join(BASE, "cookies.txt")
if os.environ.get("YTDLP_COOKIES_B64"):
    try:
        COOKIES = os.path.join(tempfile.gettempdir(), "eror-cookies.txt")
        with open(COOKIES, "wb") as fh:
            fh.write(base64.b64decode(os.environ["YTDLP_COOKIES_B64"]))
    except Exception:
        pass

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = None

@app.before_request
def guard():
    if request.path in PUBLIC:
        return None
    if os.environ.get("REQUIRE_PASSWORD") and not PASSWORD:
        return Response("Set the EROR_PASSWORD environment variable first.", 503)
    if PASSWORD:
        a = request.authorization
        if not a or a.password != PASSWORD:
            return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="Eror"'})

def ydl_opts(**extra):
    o = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if FFMPEG:
        o["ffmpeg_location"] = FFMPEG
    if PROXY:
        o["proxy"] = PROXY
    if COOKIES and os.path.isfile(COOKIES):
        o["cookiefile"] = COOKIES
    o.update(extra)
    return o

def clean_old(hours=3):
    for f in glob.glob(os.path.join(CACHE, "*")):
        if time.time() - os.path.getmtime(f) > hours * 3600:
            try: os.remove(f)
            except OSError: pass

def disposition(title, ext, attach):
    safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title or "video").strip()[:80] or "video"
    plain = safe.encode("ascii", "ignore").decode().strip() or "video"
    return "%s; filename=\"%s.%s\"; filename*=UTF-8''%s" % (
        "attachment" if attach else "inline", plain, ext, quote(safe + "." + ext))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MEDIA_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.(?:m3u8|mp4|webm)(?:\?[^\s\"'<>\\]*)?", re.I)
ATTR_RE = re.compile(r"""(?:src|data-src|file|contentUrl|content|source|url)["']?\s*[:=]\s*["']([^"']+\.(?:m3u8|mp4|webm)[^"']*)["']""", re.I)

def proxies():
    return {"http": PROXY, "https": PROXY} if PROXY else None

def scrape(url, depth=0, referer=None):
    """Fallback for sites yt-dlp cannot read: open the page, look for the real video file inside it."""
    from urllib.parse import urljoin
    r = requests.get(url, headers={"User-Agent": UA, "Referer": referer or url, "Accept-Language": "en-US,en;q=0.9"},
                     timeout=25, proxies=proxies())
    r.raise_for_status()
    html = r.text.replace("\\/", "/").replace("&amp;", "&")
    t = re.search(r'property=["\']og:title["\'][^>]*content=["\']([^"\']+)', html, re.I) or re.search(r"<title[^>]*>([^<]+)", html, re.I)
    title = (t.group(1).strip() if t else None)
    found = []
    for m in ATTR_RE.finditer(html):
        found.append(urljoin(url, m.group(1)))
    found += MEDIA_RE.findall(html)
    seen, cands = set(), []
    for u in found:
        if u not in seen:
            seen.add(u); cands.append(u)
    def score(u):
        x = u.lower()
        sc = 0
        if re.search(r"preview|trailer|thumb|sprite|teaser|poster|sample", x): sc -= 50
        if ".mp4" in x: sc += 20
        elif ".m3u8" in x: sc += 10
        for q, w in (("2160", 8), ("1080", 7), ("720", 5), ("480", 3), ("360", 1)):
            if q in x: sc += w; break
        return -sc
    cands.sort(key=score)
    out = [(u, url) for u in cands]
    if not out and depth < 1:
        for f in re.findall(r"<iframe[^>]+src=[\"']([^\"']+)", html, re.I)[:3]:
            try:
                tt, sub = scrape(urljoin(url, f), depth + 1, url)
                out += sub
                title = title or tt
            except Exception:
                continue
    return title, out

def fallback(url, key, found):
    import yt_dlp
    try:
        title, cands = scrape(url)
    except Exception:
        return None
    for media, ref in cands[:5]:
        hdr = {"User-Agent": UA, "Referer": ref}
        try:
            if ".m3u8" in media.lower():
                opts = ydl_opts(outtmpl=os.path.join(CACHE, key + ".%(ext)s"), format="b/bv*+ba",
                                merge_output_format="mp4", http_headers=hdr, max_filesize=1_500_000_000)
                with yt_dlp.YoutubeDL(opts) as y:
                    y.extract_info(media, download=True)
                p = found()
                if p:
                    return {"title": title, "src": "/media/" + os.path.basename(p)}
            else:
                rr = requests.get(media, headers=hdr, stream=True, timeout=20, proxies=proxies())
                ok = rr.status_code < 400
                rr.close()
                if ok:
                    tok = uuid.uuid4().hex[:16]
                    STREAMS[tok] = (media, hdr)
                    if len(STREAMS) > 50:
                        STREAMS.pop(next(iter(STREAMS)))
                    ext = "webm" if ".webm" in media.lower() else "mp4"
                    return {"title": title, "src": "/stream/%s.%s" % (tok, ext)}
        except Exception:
            continue
    return None

@app.get("/")
def home():
    return send_from_directory(BASE, "index.html")

@app.get("/api/ping")
def ping():
    return jsonify(ok=True)

@app.get("/api/health")
def health():
    try:
        import yt_dlp
        version = yt_dlp.version.__version__
    except Exception as e:
        version = "missing: %s" % e
    return jsonify(ok=True, ytdlp=version, ffmpeg=bool(FFMPEG), password=bool(PASSWORD), proxy=bool(PROXY))

@app.get("/api/prepare")
def prepare():
    import yt_dlp
    url = request.args.get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify(error="Invalid URL"), 400

    # 1) Fast path: a plain file stream that can start playing immediately.
    try:
        with yt_dlp.YoutubeDL(ydl_opts(format="b[ext=mp4][protocol^=http]/b[protocol^=http]")) as y:
            i = y.extract_info(url, download=False)
        if i.get("url") and str(i.get("protocol", "")).startswith("http") and not i.get("requested_formats"):
            tok = uuid.uuid4().hex[:16]
            STREAMS[tok] = (i["url"], i.get("http_headers") or {})
            if len(STREAMS) > 50:
                STREAMS.pop(next(iter(STREAMS)))
            return jsonify(title=i.get("title"), src="/stream/%s.%s" % (tok, i.get("ext") or "mp4"))
    except Exception:
        pass

    # 2) Slow path: download and merge (needed for HLS and split video/audio).
    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    def found():
        fs = [f for f in glob.glob(os.path.join(CACHE, key + ".*")) if not f.endswith((".part", ".ytdl"))]
        return max(fs, key=os.path.getmtime) if fs else None
    with lock:
        clean_old()
        path, title = found(), None
        if not path:
            opts = ydl_opts(
                outtmpl=os.path.join(CACHE, key + ".%(ext)s"),
                format="bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
                merge_output_format="mp4",
                max_filesize=1_500_000_000,
            )
            try:
                with yt_dlp.YoutubeDL(opts) as y:
                    title = y.extract_info(url, download=True).get("title")
            except Exception as e:
                err = str(e).replace("ERROR: ", "")[:300]
                fb = fallback(url, key, found)
                if fb:
                    return jsonify(**fb)
                return jsonify(error=err), 422
            path = found()
    if not path:
        return jsonify(error="Nothing could be downloaded from this link"), 422
    return jsonify(title=title, src="/media/" + os.path.basename(path))

SIZES = ("best", "1080", "720", "480", "360", "audio")

def watermark_filter(info):
    """TikTok (and a few others) list a watermarked copy beside the clean one. Exclude those formats."""
    fmts = list(info.get("formats") or [])
    for e in info.get("entries") or []:
        fmts += (e or {}).get("formats") or []
    bad = [f for f in fmts if "watermark" in ("%s %s" % (f.get("format_note"), f.get("format"))).lower()]
    ids = [str(f["format_id"]) for f in bad if re.fullmatch(r"[A-Za-z0-9._-]+", str(f.get("format_id", "")))]
    return "".join("[format_id!=%s]" % i for i in ids), bool(bad)

def video_format(q, x):
    h = "[height<=%s]" % q if q.isdigit() else ""
    x = h + x
    return "bv*%s[vcodec^=avc1]+ba[ext=m4a]/b%s[ext=mp4]/bv*%s+ba/b%s" % (x, x, x, x)

@app.get("/api/download")
def download():
    import yt_dlp
    url = request.args.get("url", "").strip()
    q = request.args.get("q", "best")
    q = q if q in SIZES else "best"
    if not url.startswith(("http://", "https://")):
        return jsonify(error="Invalid URL"), 400
    key = hashlib.sha1((url + "|" + q).encode()).hexdigest()[:16]
    def found():
        fs = [f for f in glob.glob(os.path.join(CACHE, key + ".*")) if not f.endswith((".part", ".ytdl"))]
        return max(fs, key=os.path.getmtime) if fs else None
    with dl_lock:
        clean_old()
        path, title = found(), request.args.get("t")
        if not path:
            excl, had_wm = "", False
            try:
                with yt_dlp.YoutubeDL(ydl_opts(skip_download=True)) as y:
                    info = y.extract_info(url, download=False)
                title = info.get("title")
                excl, had_wm = watermark_filter(info)
                opts = ydl_opts(outtmpl=os.path.join(CACHE, key + ".%(ext)s"), playlist_items="1",
                                max_filesize=1_500_000_000)
                if q == "audio":
                    opts.update(format="ba/b", postprocessors=[{"key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3", "preferredquality": "192"}])
                else:
                    opts.update(format=video_format(q, excl), merge_output_format="mp4")
                with yt_dlp.YoutubeDL(opts) as y:
                    y.download([url])
            except Exception as e:
                err = str(e).replace("ERROR: ", "")[:300]
                if not had_wm:
                    fb = fallback(url, key, found)
                    if fb:
                        return jsonify(**fb)
                if had_wm and "format is not available" in err.lower():
                    err = "Only a watermarked copy of this video is available, so it was not downloaded."
                elif re.search(r"login|log in|cookie|sign in|private|rate.?limit|403|429", err, re.I):
                    err += " (Instagram/Facebook often need login cookies, or the site is blocking this server. See README.)"
                return jsonify(error=err), 422
            path = found()
    if not path:
        return jsonify(error="Nothing could be downloaded from this link"), 422
    return jsonify(title=title, src="/media/" + os.path.basename(path))

@app.get("/stream/<name>")
def stream(name):
    s = STREAMS.get(name.rsplit(".", 1)[0])
    if not s:
        abort(404)
    headers = dict(s[1])
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    r = requests.get(s[0], headers=headers, stream=True, timeout=30,
                     proxies={"http": PROXY, "https": PROXY} if PROXY else None)
    out = {k: v for k, v in r.headers.items()
           if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges")}
    if request.args.get("dl"):
        out["Content-Disposition"] = disposition(request.args.get("name"), name.rsplit(".", 1)[-1], True)
    def gen():
        try:
            for chunk in r.iter_content(65536):
                yield chunk
        finally:
            r.close()
    return Response(stream_with_context(gen()), status=r.status_code, headers=out)

@app.get("/media/<name>")
def media(name):
    resp = send_from_directory(CACHE, name, conditional=True)
    if request.args.get("dl"):
        resp.headers["Content-Disposition"] = disposition(request.args.get("name"), name.rsplit(".", 1)[-1], True)
    return resp

@app.get("/manifest.webmanifest")
@app.get("/sw.js")
@app.get("/icon-192.png")
@app.get("/icon-512.png")
def pwa_files():
    name = request.path.lstrip("/")
    resp = send_from_directory(BASE, name)
    if name.endswith(".webmanifest"):
        resp.mimetype = "application/manifest+json"
    if name == "sw.js":
        resp.headers["Cache-Control"] = "no-cache"
    return resp

def _keepalive():
    url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEPALIVE_URL")
    if not url:
        return
    url = url.rstrip("/") + "/api/ping"
    while True:
        time.sleep(600)
        try:
            requests.get(url, timeout=20)
        except Exception:
            pass

threading.Thread(target=_keepalive, daemon=True).start()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)), threaded=True)
