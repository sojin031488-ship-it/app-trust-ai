"""자료 수집(IO): 앱 이름 -> App Store 정보 -> 개발사 사이트 -> GitHub -> (선택) 웹 리서치
표준 라이브러리만 사용하므로 Playwright 설치 없이 동작합니다."""
import difflib, ipaddress, json, os, re, socket, urllib.error, urllib.parse, urllib.request
from urllib.parse import quote, urljoin, urlparse
import detector as D

UA = "Mozilla/5.0 (AppTrustAI prototype)"
norm = lambda s: re.sub(r"[\W_]+", "", (s or "").lower())

def is_public_url(url):
    """SSRF 방지: http/https 이고, 주소가 공용 인터넷(내부망/로컬/메타데이터 주소 아님)일 때만 True"""
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname: return False
        for info in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global: return False        # 사설망, 127.0.0.1, 169.254.x.x(클라우드 메타데이터) 등
        return True
    except Exception:
        return False

class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트로 내부 주소에 우회 접근하는 것도 막는다"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_public_url(newurl): raise urllib.error.URLError("blocked redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

_opener = urllib.request.build_opener(_SafeRedirect)

def http_get(url, headers=None, timeout=6, max_bytes=800_000):
    if not is_public_url(url): return 0, {}, ""         # 공용 주소가 아니면 아예 요청하지 않는다
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with _opener.open(req, timeout=timeout) as r:
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
        if d and d.get("results"): return d["results"][0], cc
    return None, "kr"

def itunes_siblings(artist_id, cc):
    if not artist_id: return []
    d = http_json(f"https://itunes.apple.com/lookup?id={artist_id}&entity=software&country={cc}&limit=200")
    return [a for a in (d or {}).get("results", []) if a.get("wrapperType") == "software" or a.get("kind") == "software"]

# ---------- 개발사 웹사이트 ----------
def fetch_site_basic(url):
    st, headers, html = http_get(url)
    if st == 0 or not html: return None
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()[:3000]
    bundles = []
    for src in re.findall(r"<script[^>]+src=[\"']([^\"']+)", html, re.I)[:2]:
        s2, _, js = http_get(urljoin(url, src), max_bytes=600_000)
        if s2 == 200: bundles.append(js)
    u = urlparse(url); origin = f"{u.scheme}://{u.netloc}"
    rs, rh, _ = http_get(origin + "/robots.txt")
    s404, _, _ = http_get(origin + "/__atai_probe_404__")
    return {"headers": headers, "html": html, "text": text, "bundles": bundles,
            "probe": {"robots": rs == 200 and "text/html" not in rh.get("content-type", ""), "404": s404 == 404}}

# ---------- GitHub ----------
def gh_json(path):
    h = {"Accept": "application/vnd.github+json"}
    if os.getenv("GITHUB_TOKEN"): h["Authorization"] = f"Bearer {os.getenv('GITHUB_TOKEN')}"
    return http_json("https://api.github.com" + path, h)

def fetch_repo(repo_url):
    m = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", repo_url)
    if not m: return None
    o, n = m.group(1), m.group(2).removesuffix(".git")
    root = gh_json(f"/repos/{o}/{n}/contents")
    if not isinstance(root, list): return None
    by = {f["name"]: f for f in root}
    def text(name):
        f = by.get(name)
        return http_get(f["download_url"])[2] if f and f.get("download_url") else ""
    try: pkg = json.loads(text("package.json") or "{}")
    except Exception: pkg = {}
    readme = text(next((k for k in by if k.upper().startswith("README")), ""))
    ghdir = gh_json(f"/repos/{o}/{n}/contents/.github") if ".github" in by else None
    commits = gh_json(f"/repos/{o}/{n}/commits?per_page=100") or []
    return {"names": set(by), "gh_names": {f["name"] for f in ghdir} if isinstance(ghdir, list) else set(),
            "pkg": pkg if isinstance(pkg, dict) else {}, "readme": readme,
            "commits": [{"msg": c["commit"]["message"], "date": c["commit"]["author"]["date"],
                         "author": c["commit"]["author"]["name"] or "", "login": (c.get("author") or {}).get("login", "")}
                        for c in commits if isinstance(c, dict)]}

def github_candidates(app_name, seller=""):
    """이름 검색으로 나온 저장소 중 앱 이름/개발자와 관련 있어 보이는 것만 남긴다 (확정이 아니라 '후보')"""
    d = http_json(f"https://api.github.com/search/repositories?q={quote(app_name + ' in:name,description')}&per_page=10",
                  {"Accept": "application/vnd.github+json"})
    nm, sl = norm(app_name), norm(seller)
    out = []
    for r in (d or {}).get("items", []):
        repo_name, owner = norm(r.get("name")), norm((r.get("owner") or {}).get("login"))
        related = (len(nm) >= 3 and nm in repo_name) or (len(sl) >= 3 and (sl in owner or owner in sl))
        if related:
            out.append({"url": r["html_url"], "desc": (r.get("description") or "")[:80], "stars": r.get("stargazers_count", 0)})
    return out[:3]

# ---------- 웹 리서치 (선택: ANTHROPIC_API_KEY 필요) ----------
def web_research(name, seller, store_url):
    if not os.getenv("ANTHROPIC_API_KEY"): return None, "ANTHROPIC_API_KEY가 없어 웹 리서치를 건너뜀"
    try:
        import anthropic
        prompt = (f"앱 '{name}'(개발자/판매자: {seller}, 스토어: {store_url})를 웹에서 조사하라. 확인할 것: "
                  "1) 이 앱이 AI 코딩 도구(Cursor, Claude Code, Lovable, Bolt 등, 바이브코딩)로 만들어졌다는 공개 언급(개발자 본인 발언, 블로그, 인터뷰, 커뮤니티 글) "
                  "2) 개발자 배경과 다른 앱 3) 공식 GitHub 저장소 주소. 찾지 못한 것은 추측하지 말고 비워 둬라. "
                  '마지막에 JSON만 출력: {"self_declared_ai": true|false|null, "mentions":[{"claim":"","source_url":""}], '
                  '"github_repo":"", "developer_background":"", "other_notes":""}')
        r = anthropic.Anthropic().messages.create(
            model="claude-sonnet-4-6", max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            messages=[{"role": "user", "content": prompt}])
        txt = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        return json.loads(re.search(r"\{.*\}", txt, re.S).group(0)), None
    except Exception as e:
        return None, f"웹 리서치 실패: {str(e)[:100]}"

# ---------- 전체 파이프라인 ----------
def run(query, track_id=None, repo_url=None, declared_ai=None, weights=None):
    query, steps, notes, sigs, checks, sources = query.strip(), [], [], [], [], set()
    store, cands, site, repo, site_url, cc = None, [], None, None, None, "kr"
    mismatch = False                                       # 검색어와 다른 앱이 잡혔는가
    if re.match(r"https?://", query):                       # URL을 직접 넣은 경우
        if "github.com" in query: repo_url = repo_url or query
        else: site_url = query
    elif track_id:
        store, cc = itunes_lookup(track_id)
    else:
        rs, cc = itunes_search(query)
        store, cands = (rs[0] if rs else None), rs[1:5]
        # 수정: 검색어와 이름이 많이 다른 앱이 잡히면 경고를 남긴다
        if store and norm(query) not in norm(store.get("trackName")) and \
                difflib.SequenceMatcher(None, norm(query), norm(store.get("trackName"))).ratio() < 0.5:
            mismatch = True
            notes.append(f"'{query}'와 이름이 비슷한 앱을 찾지 못했어요. 가장 가까운 '{store.get('trackName')}'로 분석했으니 맞는 앱인지 확인하세요. 영문 이름(예: Pieceful)으로도 검색해 보세요.")
    name = (store or {}).get("trackName") or query
    seller = (store or {}).get("sellerName", "")

    if store:
        sources.add("store")
        s, q = D.store_signals(store, itunes_siblings(store.get("artistId"), cc)); sigs += s; checks += q
        steps.append({"label": "App Store 정보", "detail": f"{name} · 개발자 {seller} · v{store.get('version','?')}", "url": store.get("trackViewUrl")})
        site_url = site_url or store.get("sellerUrl")
    elif not re.match(r"https?://", query):
        notes.append("App Store에서 앱을 찾지 못함 (Google Play 앱이면 URL이나 GitHub 주소를 직접 입력하세요)")

    if site_url:
        site = fetch_site_basic(site_url)
        if site:
            sources.add("site"); s, q = D.site_signals(site_url, site); sigs += s; checks += q
            steps.append({"label": "개발사/앱 웹사이트", "detail": f"HTML·스크립트 {len(site['bundles'])}개 분석", "url": site_url})
        else: notes.append(f"웹사이트를 열지 못함: {site_url}")

    pool = (site["html"] if site else "") + " " + ((store or {}).get("description") or "")
    linked = [f"https://github.com/{o}/{n}" for o, n in re.findall(r"github\.com/([\w.-]+)/([\w.-]+)", pool)
              if o.lower() not in ("sponsors", "features", "orgs", "topics", "apps")]
    linked = list(dict.fromkeys(linked))[:3]
    # 수정: 페이지에 링크된 GitHub 주소를 자동으로 '이 앱의 저장소'로 단정하지 않는다.
    # 라이브러리 링크일 수도 있으므로 후보로만 보여 주고, 확정은 사용자가 직접 주소를 넣어야 한다.
    if repo_url:
        repo = fetch_repo(repo_url)
        if repo:
            sources.add("repo"); s, q = D.repo_signals(repo); sigs += s; checks += q
            steps.append({"label": "GitHub 저장소 분석", "detail": f"파일 {len(repo['names'])}개, 커밋 {len(repo['commits'])}건", "url": repo_url})
        else: notes.append("저장소를 읽지 못함 (비공개/주소 오류/API 한도)")

    unconfirmed = []
    if not repo:
        unconfirmed = [{"url": u, "desc": "개발자 페이지에 링크됨 (이 앱의 저장소인지 확인 필요)", "stars": None} for u in linked]
        if store:
            unconfirmed += [x for x in github_candidates(name, seller) if x["url"] not in {u["url"] for u in unconfirmed}]
    res, note = web_research(name, seller, (store or {}).get("trackViewUrl", "")) if (store or site or repo) else (None, None)
    if note: notes.append(note)
    if res:
        sources.add("research"); sigs += D.research_signals(res)
        steps.append({"label": "웹 리서치", "detail": (res.get("developer_background") or "개발자 배경 정보 없음")[:120], "url": None})
        gr = res.get("github_repo")
        if not repo and isinstance(gr, str) and re.match(r"https://github\.com/[\w.-]+/[\w.-]+", gr) and gr not in {u["url"] for u in unconfirmed}:
            unconfirmed.insert(0, {"url": gr, "desc": "웹 리서치에서 발견 (확인 필요)", "stars": None})
    if not sources:
        return {"name": name, "error": "분석할 자료를 찾지 못했습니다. " + " / ".join(notes)}

    st_ = store or {}
    sample = {"store_meta": {"release_date": st_.get("releaseDate"), "last_update": st_.get("currentVersionReleaseDate"),
                             "rating_count": st_.get("userRatingCount"), "languages": len(st_.get("languageCodesISO2A") or [])},
              "web_research": (res or {}).get("mentions"),
              "store_description": ((store or {}).get("description") or "")[:1500],
              "release_notes": ((store or {}).get("releaseNotes") or "")[:500],
              "readme": (repo or {}).get("readme", "")[:2000],
              "commit_messages": [c["msg"][:100] for c in (repo or {}).get("commits", [])[:15]],
              "page_text": (site or {}).get("text", "")[:1200]}
    out = D.evaluate(sigs, checks, sources, D.llm_judge(sample), declared_ai, weights)
    if mismatch:                                           # 엉뚱한 앱일 수 있으면 판정하지 않는다
        out["verdict"] = "판단 보류 (검색한 앱과 다를 수 있음)"
        out["triage"] = "검색한 앱이 맞는지 확인 필요 → 영문 이름으로 다시 검색"
    return {"name": name, "seller": seller, "steps": steps, "notes": notes, "unconfirmed_repos": unconfirmed,
            "candidates": [{"track_id": a.get("trackId"), "name": a.get("trackName"), "seller": a.get("sellerName")} for a in cands],
            "research": res, **out}
