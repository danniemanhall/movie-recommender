#!/usr/bin/env python3
"""Build catalogue.json: titles streaming in South Africa, scored against the household's ratings.

Usage: python3 tools/build_catalogue.py <ratings_dir> <out.json> [cache_dir]
<ratings_dir> holds one JSON file per rated title, as exported from the page's database.

Authentication: set TMDB_API_KEY to a v3 key, or leave it unset to send a placeholder bearer
token, which works where the network proxy swaps in a saved TMDB Read Access Token.
"""
import json, os, sys, glob, math, time, hashlib, re, datetime, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict

KEY = os.environ.get("TMDB_API_KEY", "")
RATINGS_DIR, OUT = sys.argv[1], sys.argv[2]
CACHE = sys.argv[3] if len(sys.argv) > 3 else None
API = "https://api.themoviedb.org/3"

# Platform sources. TMDB has no South African listing for Disney+, so the UK catalogue stands in for it.
SOURCES = [("nfx", 8, "ZA"), ("prv", 119, "ZA"), ("dnp", 337, "GB")]
PAGES = 60
WEIGHT = {"love": 3.0, "like": 1.5, "ok": 0.2, "no": -2.5}
GENRE_VAL = {"love": 2.0, "like": 1.0, "ok": -0.2, "no": -2.0}
BLOCK_GENRES = {27}                      # horror
BLOCK_TV_GENRES = {10762, 10763, 10767, 10766}  # kids, news, talk, soap
BLOCK_KEYWORDS = re.compile(r"nudity|erotic|sexual|\bsex\b|raunchy|gross.?out|adult animation|stoner|softcore|strip ?club|pornograph|prostitut|slasher|gore|torture|serial killer|cannibal|vampire|werewolf|drug cartel|sex comedy|adult humor|dark comedy|black comedy|satanic|demon|zombie", re.I)
LANGS = {"en", "af"}
LEVEL = {  # 0 all ages, 1 parental guidance, 2 teens, 3 mature
    "G": 0, "TV-G": 0, "TV-Y": 0, "TV-Y7": 0, "U": 0, "0": 0, "6": 0, "A": 0, "ALL": 0,
    "PG": 1, "TV-PG": 1, "7-9PG": 1, "10-12PG": 1,
    "PG-13": 2, "TV-14": 2, "12": 2, "12A": 2, "M": 2, "13": 2, "14": 2, "12+": 2,
    "R": 3, "NC-17": 3, "TV-MA": 3, "15": 3, "16": 3, "18": 3, "MA15+": 3, "MA 15+": 3, "R18+": 3, "R 18+": 3,
}

def get(path, **params):
    if KEY:
        params["api_key"] = KEY
    url = f"{API}{path}?{urllib.parse.urlencode(sorted(params.items()))}"
    cf = None
    if CACHE:
        cf = os.path.join(CACHE, hashlib.sha1((url.replace(KEY, "") if KEY else url).encode()).hexdigest() + ".json")
        if os.path.exists(cf):
            return json.load(open(cf))
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={} if KEY else {"Authorization": "Bearer placeholder"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.load(r)
            if cf:
                json.dump(data, open(cf, "w"))
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            time.sleep(1.5 * (attempt + 1))
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return {}

# ---------- DStv: the public TV guide (the streaming catalogue is not readable from outside Africa) ----------
DSTV = "https://www.dstv.com/umbraco/api/TvGuide"
DSTV_DAYS = 7
DSTV_CHANNELS = set("101 106 107 108 109 110 112 113 114 117 120 121 122 124 134 135 136 137 138 140 144 145 146 147 151 154 161 "
                    "174 175 176 177 179 181 182 184 185 186 189 191 192 193 194 195".split())
DSTV_SKIP_SUB = {"News", "Current Affairs", "Talk", "Game Show", "Sport", "Religion", "Music Mix", "All Music", "Adult Animation"}
DSTV_LEVEL = {"Family": 0, "PG": 1, "PG13": 2, "13": 2, "16": 3, "18": 3}

def dget(path, **params):
    url = f"{DSTV}/{path}?{urllib.parse.urlencode(sorted(params.items()))}"
    cf = os.path.join(CACHE, "dstv-" + hashlib.sha1(url.encode()).hexdigest() + ".json") if CACHE else None
    if cf and os.path.exists(cf):
        return json.load(open(cf))
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.load(r)
            if cf:
                json.dump(data, open(cf, "w"))
            return data
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return {}

def norm(title):
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"^(the|a|an) ", "", (title or "").lower().replace("&", "and")))

def dstv_schedule():
    """Titles airing on DStv entertainment channels in the coming week, matched to TMDB."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=2)          # South African time
    groups = {}
    for i in range(DSTV_DAYS):
        day = (now + datetime.timedelta(days=i)).date().isoformat()
        for ch in dget("GetProgrammes", d=day, country="zaf", unit="dstv").get("Channels") or []:
            if ch["Number"] not in DSTV_CHANNELS:
                continue
            for p in ch["Programmes"]:
                if p["StartTime"] < now.isoformat():
                    continue
                g = groups.setdefault((ch["Number"], p["Title"]), {"id": p["Id"], "air": []})
                g["air"].append((p["StartTime"][:16], ch["Name"], ch["Number"]))
    def look(item):
        (num, _), g = item
        d = dget("GetProgramme", id=g["id"])
        if not d.get("Title"):
            return None
        main, sub = set(d.get("MainGenres") or []), set(d.get("SubGenres") or [])
        if not main & {"Movies", "Series", "Shows", "Actuality"} or sub & DSTV_SKIP_SUB:
            return None
        kw = {k["Name"]: k["Value"] for k in d.get("Keywords") or [] if k.get("Value")}
        year = next((x["Value"] for x in d.get("Items") or [] if x["Key"] == "Year"), None) or kw.get("Yearofrelease")
        media = "movie" if "Movies" in main else "tv"
        params = {"query": d["Title"]}
        if media == "movie" and year and str(year).isdigit():
            params["year"] = year
        hit = None
        for x in (get(f"/search/{media}", **params).get("results") or [])[:6]:
            names = {norm(x.get("title") or x.get("name")), norm(x.get("original_title") or x.get("original_name"))}
            if norm(d["Title"]) in names:
                hit = x; break
        if not hit or (media == "movie" and not params.get("year") and (hit.get("vote_count") or 0) < 50):
            return None
        return (media, hit["id"]), hit, d.get("Rating"), kw.get("fpb"), g["air"]
    found = {}
    for r in pmap(look, list(groups.items()), workers=5):
        if not r:
            continue
        key, hit, rating, fpb, air = r
        f = found.setdefault(key, {"hit": hit, "rating": rating, "fpb": fpb, "air": []})
        f["air"] += air
        if DSTV_LEVEL.get(rating, -1) > DSTV_LEVEL.get(f["rating"], -1):
            f["rating"] = rating
        f["fpb"] = f["fpb"] or fpb
    print("dstv: programmes", len(groups), "matched titles", len(found), file=sys.stderr)
    return found

def pmap(fn, items, workers=12):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(fn, items))

# ---------- 1. resolve rated titles ----------
def load_ratings():
    out = []
    for f in sorted(glob.glob(os.path.join(RATINGS_DIR, "*.json"))):
        d = json.load(open(f)); doc_id = d.get("id") or os.path.basename(f)[:-5]; d = d.get("data", d)
        if d.get("r"):
            out.append(dict(d, doc=doc_id))
    return out

def resolve(r):
    m = re.match(r"^t-([mt])(\d+)$", r["doc"])          # rated from the picks tab: id is already known
    if m:
        return dict(r, media="movie" if m.group(1) == "m" else "tv", tmdb=int(m.group(2)))
    media = "movie" if r.get("kind") == "movie" else "tv"
    q = re.sub(r"\s*\((US|UK)\)$", "", r["title"])
    params = {"query": q}
    if r.get("year"):
        params["year" if media == "movie" else "first_air_date_year"] = r["year"]
    res = get(f"/search/{media}", **params).get("results") or []
    if not res and r.get("year"):
        res = get(f"/search/{media}", query=q).get("results") or []
    if not res:
        return dict(r, media=media, tmdb=None)
    exact = [x for x in res[:8] if (x.get("title") or x.get("name") or "").lower() == q.lower()]
    best = res[0] if r.get("year") else max(exact or res[:5], key=lambda x: x.get("vote_count", 0))
    return dict(r, media=media, tmdb=best["id"])

if not get("/configuration").get("images"):
    sys.exit("TMDB did not answer. Check the saved token and that api.themoviedb.org is allowed.")
ratings = pmap(resolve, load_ratings())
missing = [r["title"] for r in ratings if not r["tmdb"]]
print("rated:", len(ratings), "unresolved:", missing, file=sys.stderr)
ratings = [r for r in ratings if r["tmdb"]]
BLOCK_TITLES = {l.strip().lower() for l in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocklist.txt")) if l.strip() and not l.startswith("#")}
rated_keys = {(r["media"], r["tmdb"]): r for r in ratings}

# ---------- 2. taste signals from rated titles ----------
def seed_data(r):
    d = get(f"/{r['media']}/{r['tmdb']}", append_to_response="keywords")
    recs = []
    if r["r"] in WEIGHT:
        for page in (1, 2):
            recs += get(f"/{r['media']}/{r['tmdb']}/recommendations", page=page).get("results") or []
    return r, d, recs

rec_score = defaultdict(float); because = defaultdict(list)
g_sum = defaultdict(float); g_n = defaultdict(int)
k_sum = defaultdict(float); k_n = defaultdict(int)
for r, d, recs in pmap(seed_data, ratings):
    if r["r"] not in WEIGHT:
        continue
    w = WEIGHT[r["r"]]
    if r["r"] == "no" and r.get("why") == ["taste"]:
        w = -1.5
    for i, x in enumerate(recs):
        key = (r["media"], x["id"])
        rec_score[key] += w * (1 - i / 50)
        if r["r"] == "love" or (r["r"] == "like" and i < 12):
            because[key].append((w * (1 - i / 50), r["title"]))
    for g in d.get("genres") or []:
        g_sum[(r["media"], g["id"])] += GENRE_VAL[r["r"]]; g_n[(r["media"], g["id"])] += 1
    kws = d.get("keywords") or {}
    for k in (kws.get("keywords") or kws.get("results") or []):
        k_sum[k["id"]] += GENRE_VAL[r["r"]]; k_n[k["id"]] += 1
g_aff = {k: g_sum[k] / (g_n[k] + 3) for k in g_sum}
k_aff = {k: k_sum[k] / (k_n[k] + 1) for k in k_sum if k_n[k] >= 2}

# ---------- 3. what is streaming ----------
def discover(job):
    code, pid, region, media, page = job
    extra = {"vote_count.gte": 80 if media == "movie" else 40}
    d = get(f"/discover/{media}", watch_region=region, with_watch_providers=pid, with_watch_monetization_types="flatrate",
            sort_by="popularity.desc", page=page, include_adult="false", **extra)
    return code, media, d.get("results") or []

jobs = [(c, p, reg, m, pg) for c, p, reg in SOURCES for m in ("movie", "tv") for pg in range(1, PAGES + 1)]
titles = {}
for code, media, results in pmap(discover, jobs):
    for x in results:
        t = titles.setdefault((media, x["id"]), dict(x, media=media, prov=set()))
        t["prov"].add(code)
print("streaming titles:", len(titles), file=sys.stderr)
dstv = {}
try:
    dstv = dstv_schedule()
except Exception as e:                                   # DStv being down must not stop the refresh
    print("dstv: skipped,", e, file=sys.stderr)
for key, f in dstv.items():
    t = titles.setdefault(key, dict(f["hit"], media=key[0], prov=set()))
    t["prov"].add("dstv"); t["dstv"] = f

def genre_score(t):
    gs = [g_aff.get((t["media"], g), 0.0) for g in t.get("genre_ids") or []]
    return sum(gs) / len(gs) if gs else 0.0

def quality(t):
    v, n = t.get("vote_average") or 0, t.get("vote_count") or 0
    return ((v * n + 6.5 * 300) / (n + 300)) - 6.5

def base_score(key, t):
    rs = rec_score.get(key, 0.0)
    rs = math.copysign(math.log1p(abs(rs)), rs)
    return 1.6 * rs + 1.3 * genre_score(t) + 0.9 * quality(t) + 0.08 * math.log1p(t.get("popularity") or 0)

cands = []
for key, t in titles.items():
    if key in rated_keys and rated_keys[key]["r"] != "unseen":
        continue
    if t.get("original_language") not in LANGS:
        continue
    gids = set(t.get("genre_ids") or [])
    if gids & BLOCK_GENRES or (t["media"] == "tv" and gids & BLOCK_TV_GENRES):
        continue
    cands.append((base_score(key, t), key))
cands.sort(reverse=True)
short = [k for _, k in cands if k[0] == "movie"][:1100] + [k for _, k in cands if k[0] == "tv"][:800]
short += [k for _, k in cands if "dstv" in titles[k]["prov"] and k not in set(short)]

# ---------- 4. details and content check ----------
def cert_of(media, d):
    by = {}
    if media == "movie":
        for c in (d.get("release_dates") or {}).get("results") or []:
            cs = [x["certification"] for x in c["release_dates"] if x.get("certification")]
            if cs: by[c["iso_3166_1"]] = cs[0]
    else:
        for c in (d.get("content_ratings") or {}).get("results") or []:
            if c.get("rating"): by[c["iso_3166_1"]] = c["rating"]
    for country in ("US", "ZA", "GB", "AU", "CA", "NZ", "IE", "DE"):
        c = by.get(country)
        if c and c.upper() in LEVEL:
            return c, LEVEL[c.upper()]
        if c and c not in ("NR", "Unrated"):
            m = re.match(r"^(\d+)", c)
            if m:
                n = int(m.group(1)); return c, 0 if n < 7 else 1 if n < 12 else 2 if n < 15 else 3
    return None, None

def clip(text, n=260):
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(",.;:") + "…"

def detail(key):
    media, tid = key
    extra = "release_dates,keywords" if media == "movie" else "content_ratings,keywords"
    return key, get(f"/{media}/{tid}", append_to_response=extra)

items = []
for key, d in pmap(detail, short):
    if not d:
        continue
    t = titles[key]; media = key[0]
    kws = d.get("keywords") or {}
    kws = kws.get("keywords") or kws.get("results") or []
    kwnames = [k["name"] for k in kws]
    if any(BLOCK_KEYWORDS.search(n) for n in kwnames) or (d.get("title") or d.get("name") or "").lower() in BLOCK_TITLES:
        continue
    cert, level = cert_of(media, d)
    za = None
    if t.get("dstv"):                                  # South African rating from the DStv guide
        f = t["dstv"]; za = f["fpb"] or f["rating"]
        marks = re.sub(r"\(.*?\)", "", re.sub(r"^[\d\-]*(PG)?", "", f["fpb"] or ""))
        if "N" in marks or "H" in marks:
            continue                                   # nudity or horror flagged by the Film and Publication Board
        dl = DSTV_LEVEL.get(f["rating"])
        if re.match(r"^1[68]", f["fpb"] or ""):
            dl = 3
        if dl is not None:
            level = dl if level is None else max(level, dl)
        if not cert and f["rating"]:
            cert = "Family" if f["rating"] == "Family" else f["rating"].replace("PG13", "PG-13")
    gnames = {g["name"] for g in d.get("genres") or []}
    kiddie = gnames & {"Family", "Animation", "Kids"}
    if media == "tv" and (cert in ("TV-Y", "TV-Y7") or (level == 0 and kiddie)):
        continue                                   # children's television
    if media == "movie" and kiddie and (d.get("vote_count") or 0) < 2500:
        continue                                   # keep only the well-loved family films
    ka = sorted((k_aff[k["id"]] for k in kws if k["id"] in k_aff), key=abs, reverse=True)[:4]
    score = base_score(key, t) + 0.5 * sum(ka)
    date = d.get("release_date") or d.get("first_air_date") or ""
    why = list(dict.fromkeys(n for _, n in sorted(because.get(key, []), reverse=True)))[:3]
    item = {
        "id": ("m" if media == "movie" else "t") + str(key[1]), "t": d.get("title") or d.get("name"),
        "y": int(date[:4]) if date[:4].isdigit() else None, "k": "movie" if media == "movie" else "series",
        "p": sorted(t["prov"]), "g": [g["name"] for g in d.get("genres") or []],
        "c": cert, "lv": level, "va": round(d.get("vote_average") or 0, 1),
        "o": clip((d.get("overview") or "").strip()), "s": round(score, 3), "b": why,
        "n": d.get("runtime") if media == "movie" else d.get("number_of_seasons"),
        "d": date, "pp": d.get("poster_path"),
    }
    if t.get("dstv"):
        item["za"] = za
        item["on"] = [list(a) for a in sorted(set(t["dstv"]["air"]))[:4]]
    if key in rated_keys:
        item["doc"] = rated_keys[key]["doc"]       # marked "not seen" on the ratings tab
    items.append(item)

if len(titles) < 2000 or len(items) < 600:
    sys.exit(f"Too few titles came back ({len(titles)} streaming, {len(items)} scored), so the old catalogue is kept.")
items.sort(key=lambda x: -x["s"])
keep = [x for x in items if x["k"] == "movie"][:650] + [x for x in items if x["k"] == "series"][:450]
kept_ids = {x["id"] for x in keep}
keep += [x for x in items if "dstv" in x["p"] and x["id"] not in kept_ids][:350]
keep.sort(key=lambda x: -x["s"])

# ---------- 5. posters, packed into sheets so the page loads a few images rather than hundreds ----------
SHEET_COLS, SHEET_ROWS, TILE_W, TILE_H = 6, 4, 154, 231
def poster_sheets(items, out_dir):
    try:
        from PIL import Image
    except ImportError:
        print("posters: skipped, Pillow is not installed (pip install pillow)", file=sys.stderr); return {}
    import io
    def fetch(x):
        if not x.get("pp"):
            return None
        cf = os.path.join(CACHE, "poster-" + x["pp"].strip("/")) if CACHE else None
        if cf and os.path.exists(cf):
            return open(cf, "rb").read()
        for attempt in range(4):
            try:
                with urllib.request.urlopen("https://image.tmdb.org/t/p/w154" + x["pp"], timeout=30) as r:
                    data = r.read()
                if cf:
                    open(cf, "wb").write(data)
                return data
            except Exception:
                time.sleep(1 + attempt)
        return None
    blobs = pmap(fetch, items, workers=10)
    os.makedirs(out_dir, exist_ok=True)
    per, files, got = SHEET_COLS * SHEET_ROWS, {}, 0
    for n in range(0, len(items), per):
        sheet = Image.new("RGB", (SHEET_COLS * TILE_W, SHEET_ROWS * TILE_H), (40, 36, 56))
        for i, (x, blob) in enumerate(zip(items[n:n + per], blobs[n:n + per])):
            if not blob:
                continue
            try:
                im = Image.open(io.BytesIO(blob)).convert("RGB").resize((TILE_W, TILE_H))
            except Exception:
                continue
            sheet.paste(im, ((i % SHEET_COLS) * TILE_W, (i // SHEET_COLS) * TILE_H))
            x["ps"] = [n // per, i]; got += 1
        name = f"posters/{n // per:02d}.jpg"
        sheet.save(os.path.join(os.path.dirname(out_dir), name), "JPEG", quality=72, optimize=True)
        files[name] = name
    print("posters:", got, "of", len(items), "in", len(files), "sheets", file=sys.stderr)
    return files

root = os.path.dirname(os.path.abspath(OUT))
files = {"catalogue.json": os.path.basename(OUT)}
files.update(poster_sheets(keep, os.path.join(root, "posters")))
for x in keep:
    x.pop("pp", None)
json.dump(files, open(os.path.join(root, "publish_files.json"), "w"), indent=0)   # the `files` map for publishing
json.dump({"generated": datetime.date.today().isoformat(), "items": keep}, open(OUT, "w"), ensure_ascii=False, separators=(",", ":"))
print("kept:", len(keep), "bytes:", os.path.getsize(OUT), file=sys.stderr)
