import base64, glob, hashlib, os, re, shutil, tempfile, threading, time, uuid, zipfile
from urllib.parse import parse_qs, quote, urlparse
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
PUBLIC = {"/manifest.webmanifest", "/sw.js", "/icon-192.png", "/icon-512.png"}  # app-install files, no password

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
    return jsonify(ok=True, ytdlp=version, ffmpeg=bool(FFMPEG), password=bool(PASSWORD), proxy=bool(PROXY), max_channel=MAX_CHANNEL)

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
                if had_wm and "format is not available" in err.lower():
                    err = "Only a watermarked copy of this video is available, so it was not downloaded."
                elif re.search(r"login|log in|cookie|sign in|private|rate.?limit|403|429", err, re.I):
                    err += " (Instagram/Facebook often need login cookies, or the site is blocking this server. See README.)"
                return jsonify(error=err), 422
            path = found()
    if not path:
        return jsonify(error="Nothing could be downloaded from this link"), 422
    return jsonify(title=title, src="/media/" + os.path.basename(path))

# ---------------------------------------------------------------------------
# Channel / page / playlist download (new)
# Lists the videos of a channel, downloads them one by one in the background,
# then packs everything into one ZIP file. Single-video download above is unchanged.
# ---------------------------------------------------------------------------
MAX_CHANNEL = int(os.environ.get("EROR_MAX_CHANNEL", "100"))   # most videos per channel job
CH_MAX_FILESIZE = int(os.environ.get("EROR_CH_MAX_FILESIZE", "1000000000"))  # bytes per video
JOBS = {}
jobs_lock = threading.Lock()
active_job = {"id": None}

def http_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    if PROXY:
        s.proxies.update({"http": PROXY, "https": PROXY})
    if COOKIES and os.path.isfile(COOKIES):
        try:
            import http.cookiejar
            jar = http.cookiejar.MozillaCookieJar(COOKIES)
            jar.load(ignore_discard=True, ignore_expires=True)
            s.cookies = jar
        except Exception:
            pass
    return s

def host_of(url):
    h = (urlparse(url).hostname or "").lower()
    return re.sub(r"^(www|m|mobile|old|new|mbasic|web)\.", "", h)

def ytdlp_list(url, limit):
    """Any channel / playlist / profile that yt-dlp understands (YouTube, TikTok, Vimeo, Dailymotion, X ...)."""
    import yt_dlp
    u = url
    if re.search(r"youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)/?$", url):
        u = url.rstrip("/") + "/videos"   # only the real videos tab, not Shorts/Live duplicates
    opts = ydl_opts(extract_flat="in_playlist", noplaylist=False, playlistend=limit, skip_download=True)
    urls, seen = [], set()

    def add(link):
        if link and link not in seen and len(urls) < limit:
            seen.add(link)
            urls.append(link)

    def walk(e, depth=0):
        if not e or len(urls) >= limit:
            return
        if e.get("entries") is not None:
            for sub in e["entries"]:
                walk(sub, depth)
            return
        link = e.get("url") or e.get("webpage_url")
        key = str(e.get("ie_key") or "")
        if key == "YoutubeTab" and link and depth < 2:      # a tab/playlist inside a channel
            try:
                with yt_dlp.YoutubeDL(opts) as y2:
                    walk(y2.extract_info(link, download=False), depth + 1)
            except Exception:
                pass
            return
        if link and not str(link).startswith("http") and key.startswith("Youtube") and e.get("id"):
            link = "https://www.youtube.com/watch?v=" + e["id"]
        if link and str(link).startswith("http"):
            add(link)
        elif e.get("webpage_url"):
            add(e["webpage_url"])

    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(u, download=False)
    title = info.get("title") or info.get("uploader") or "channel"
    if info.get("entries") is None:       # it was just one video
        return title, [url]
    walk(info)
    return title, urls

REDDIT_SORTS = ("hot", "new", "top", "rising", "controversial", "best")
VIDEO_HOSTS = ("v.redd.it", "youtube.com", "youtu.be", "streamable.com", "redgifs.com", "gfycat.com", "vimeo.com", "imgur.com")

def reddit_list(url, limit):
    """Videos of a subreddit (r/name) or a user profile (u/name) through Reddit's public JSON."""
    u = urlparse(url)
    parts = [p for p in u.path.split("/") if p]
    if "comments" in parts or (u.hostname or "").endswith("redd.it"):
        return "Reddit post", [url]                      # a single post, nothing to list
    qs = parse_qs(u.query)
    if len(parts) >= 2 and parts[0] == "r":
        name = parts[1]
        sort = parts[2] if len(parts) > 2 and parts[2] in REDDIT_SORTS else (qs.get("sort") or ["hot"])[0]
        path = "/r/%s/%s.json" % (name, sort if sort in REDDIT_SORTS else "hot")
        title = "r_" + name
    elif len(parts) >= 2 and parts[0] in ("user", "u"):
        name = parts[1]
        path = "/user/%s/submitted.json" % name
        sort = (qs.get("sort") or ["new"])[0]
        title = "u_" + name
    else:
        raise ValueError("Paste a subreddit link (reddit.com/r/NAME) or a user link (reddit.com/user/NAME).")
    params = {"limit": 100, "raw_json": 1, "sort": sort, "t": (qs.get("t") or ["all"])[0]}
    s = http_session()
    urls, seen, after = [], set(), None
    for _ in range(12):                                   # at most 12 pages of 100 posts
        if len(urls) >= limit:
            break
        if after:
            params["after"] = after
        data, last = None, None
        for host in ("https://www.reddit.com", "https://old.reddit.com"):
            try:
                r = s.get(host + path, params=params, timeout=25)
                if r.status_code == 200:
                    data = r.json()
                    break
                last = "Reddit answered %s" % r.status_code
            except Exception as e:
                last = str(e)
        if data is None:
            if urls:
                break
            raise RuntimeError("Reddit did not give the list (%s). Reddit often blocks servers; run Eror on your own computer or set YTDLP_PROXY." % last)
        kids = (data.get("data") or {}).get("children") or []
        for k in kids:
            d = k.get("data") or {}
            xp = (d.get("crosspost_parent_list") or [{}])[0]
            is_vid = d.get("is_video") or xp.get("is_video") or d.get("post_hint") in ("hosted:video", "rich:video") \
                or any(h in str(d.get("domain", "")) for h in VIDEO_HOSTS)
            if not is_vid:
                continue
            ext = d.get("url_overridden_by_dest") or d.get("url") or ""
            if ext.startswith("http") and "reddit.com" not in ext and "redd.it" not in ext:
                link = ext                                 # YouTube / Streamable ... posted on Reddit
            else:
                link = "https://www.reddit.com" + (d.get("permalink") or "")
            if d.get("permalink") and link not in seen:
                seen.add(link)
                urls.append(link)
                if len(urls) >= limit:
                    break
        after = (data.get("data") or {}).get("after")
        if not after:
            break
        time.sleep(1)
    return title, urls

FB_RESERVED = {"watch", "reel", "reels", "videos", "video", "groups", "events", "marketplace", "story.php", "share", "photo", "photos", "login", "plugins", "permalink.php"}
FB_ID_RES = (re.compile(r"/videos/(?:[^/\"'?\s]+/)?(\d{6,})"), re.compile(r"/reel/(\d{6,})"),
             re.compile(r"[?&]v=(\d{6,})"), re.compile(r"video_id=(\d{6,})"))

def facebook_list(url, limit):
    """Facebook page / profile videos. Facebook hides most of this from servers, so cookies.txt usually helps."""
    errors = []
    try:                                                    # 1) let yt-dlp try (playlists, some pages)
        t, urls = ytdlp_list(url, limit)
        if urls and urls != [url]:
            return t, urls
        if urls == [url] and re.search(r"/(?:videos|reel|watch)/?(?:\d|\?v=)", url):
            return t, urls                                  # it really is a single video
    except Exception as e:
        errors.append(str(e)[:200])
    u = urlparse(url)                                       # 2) read the page itself
    segs = [p for p in u.path.split("/") if p]
    name = segs[0] if segs else ""
    if name == "profile.php":
        cands = ["https://www.facebook.com/profile.php?id=%s&sk=videos" % (parse_qs(u.query).get("id") or [""])[0]]
    elif name and name not in FB_RESERVED:
        cands = ["https://www.facebook.com/%s/videos" % name, "https://mbasic.facebook.com/%s/videos" % name,
                 "https://m.facebook.com/%s/videos" % name]
    else:
        cands = [url]
    s = http_session()
    ids = []
    for c in cands:
        try:
            r = s.get(c, timeout=25)
            html = r.text.replace("\\/", "/").replace("&amp;", "&")
            for rx in FB_ID_RES:
                for m in rx.finditer(html):
                    if m.group(1) not in ids:
                        ids.append(m.group(1))
            if ids:
                break
        except Exception as e:
            errors.append(str(e)[:200])
    if not ids:
        raise RuntimeError("Facebook did not show any videos for this page to the server (it usually needs login). "
                           "Put a cookies.txt next to server.py (see README), or paste links of single videos. %s" % " ".join(errors[:1]))
    return name or "facebook", ["https://www.facebook.com/watch/?v=" + i for i in ids[:limit]]

def list_channel(url, limit):
    h = host_of(url)
    if h in ("reddit.com", "redd.it"):
        return reddit_list(url, limit)
    if h in ("facebook.com", "fb.watch", "fb.com"):
        return facebook_list(url, limit)
    return ytdlp_list(url, limit)

def download_one(url, q, outdir, idx):
    """Download one video of a channel into outdir as NNN.ext, return (path, title)."""
    import yt_dlp
    def found():
        fs = [f for f in glob.glob(os.path.join(outdir, "%03d.*" % idx)) if not f.endswith((".part", ".ytdl"))]
        return max(fs, key=os.path.getmtime) if fs else None
    with dl_lock:
        with yt_dlp.YoutubeDL(ydl_opts(skip_download=True)) as y:
            info = y.extract_info(url, download=False)
        title = info.get("title") or "video"
        excl, _ = watermark_filter(info)
        opts = ydl_opts(outtmpl=os.path.join(outdir, "%03d.%%(ext)s" % idx), playlist_items="1",
                        max_filesize=CH_MAX_FILESIZE)
        if q == "audio":
            opts.update(format="ba/b", postprocessors=[{"key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3", "preferredquality": "192"}])
        else:
            opts.update(format=video_format(q, excl), merge_output_format="mp4")
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])
    p = found()
    if not p:
        raise RuntimeError("Nothing could be downloaded")
    return p, title

def run_channel(jid, url, q, limit):
    job = JOBS[jid]
    outdir = os.path.join(CACHE, "job-" + jid)
    try:
        os.makedirs(outdir, exist_ok=True)
        job["status"] = "listing"
        title, urls = list_channel(url, limit)
        urls = urls[:limit]
        if not urls:
            raise RuntimeError("No videos found at this link.")
        job.update(title=title, total=len(urls), status="downloading")
        names = []
        for i, link in enumerate(urls, 1):
            if job.get("cancel"):
                break
            job["current"] = link
            try:
                path, vtitle = download_one(link, q, outdir, i)
                job["current"] = vtitle
                safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", vtitle).strip()[:80] or "video"
                names.append((path, "%03d - %s%s" % (i, safe, os.path.splitext(path)[1])))
                job["done"] += 1
            except Exception as e:
                err = str(e).replace("ERROR: ", "")[:200]
                job["failed"].append({"url": link, "error": err})
        if not names:
            first = job["failed"][0]["error"] if job["failed"] else "cancelled"
            raise RuntimeError("No video could be downloaded. First error: " + first)
        job["status"] = "zipping"
        zname = "channel-%s.zip" % jid
        with zipfile.ZipFile(os.path.join(CACHE, zname), "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for path, arc in names:
                z.write(path, arc)
            if job["failed"]:
                z.writestr("failed.txt", "\n".join("%s  ->  %s" % (f["url"], f["error"]) for f in job["failed"]))
        job["zip"] = "/media/" + zname
        job["status"] = "cancelled" if job.get("cancel") else "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e).replace("ERROR: ", "")[:400]
    finally:
        shutil.rmtree(outdir, ignore_errors=True)
        with jobs_lock:
            if active_job["id"] == jid:
                active_job["id"] = None

@app.get("/api/channel/start")
def channel_start():
    url = request.args.get("url", "").strip()
    q = request.args.get("q", "best")
    q = q if q in SIZES else "best"
    try:
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, MAX_CHANNEL))
    if not url.startswith(("http://", "https://")):
        return jsonify(error="Invalid URL"), 400
    with jobs_lock:
        if active_job["id"] and JOBS.get(active_job["id"], {}).get("status") in ("queued", "listing", "downloading", "zipping"):
            return jsonify(error="Another channel download is still running. Wait for it or cancel it first.", job=active_job["id"]), 409
        clean_old()
        jid = uuid.uuid4().hex[:12]
        JOBS[jid] = {"status": "queued", "total": 0, "done": 0, "failed": [], "title": None, "current": None,
                     "limit": limit, "created": time.time()}
        while len(JOBS) > 20:
            JOBS.pop(next(iter(JOBS)))
        active_job["id"] = jid
    threading.Thread(target=run_channel, args=(jid, url, q, limit), daemon=True).start()
    return jsonify(job=jid, limit=limit)

@app.get("/api/channel/status")
def channel_status():
    job = JOBS.get(request.args.get("job", ""))
    if not job:
        return jsonify(error="Unknown job"), 404
    return jsonify({k: job.get(k) for k in ("status", "total", "done", "failed", "title", "current", "zip", "error")})

@app.get("/api/channel/cancel")
def channel_cancel():
    job = JOBS.get(request.args.get("job", ""))
    if job:
        job["cancel"] = True
    return jsonify(ok=True)

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

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)), threaded=True)
