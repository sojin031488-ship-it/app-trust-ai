"""자료 수집(IO): 앱 이름 -> App Store 정보 -> 개발사 사이트 -> GitHub -> (선택) 웹 리서치
표준 라이브러리만 사용하므로 Playwright 설치 없이 동작합니다."""
import difflib, json, os, re, urllib.error, urllib.parse, urllib.request
from urllib.parse import quote, urljoin, urlparse
import detector as D

UA = "Mozilla/5.0 (AppTrustAI prototype)"
norm = lambda s: re.sub(r"[\W_]+", "", (s or "").lower())

def http_get(url, headers=None, timeout=15, max_bytes=800_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(max_bytes).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, ""
    except Exception:
        return 0, {}, ""

def http_json(url, headers=None):
    st, _, body = http_get(url, headers)
    try: return json.loads(body) if st == 200 else None
    except Exception: return None

# ---------- App Store (iTunes Search API, 무료/키 불필요) ----------
def itunes_search(q):
    for cc in ("kr", "us"):
        d = http_json(f"https://itunes.apple.com/search?term={quote(q)}&entity=software&country={cc}&limit=8")
        if d and d.get("results"):
            rs = d["results"]
            rs.sort(key=lambda a: (difflib.SequenceMatcher(None, norm(q), norm(a.get("trackName"))).ratio(),
                                   a.get("userRatingCount", 0)), reverse=True)
            return rs, cc
    return [], "kr"

def itunes_lookup(track_id):
    for cc in ("kr", "us"):
        d = http_json(f"https://itunes.apple.com/lookup?id={track_id}&country={cc}")
        if d and d.get("results"): return d["results"][0

