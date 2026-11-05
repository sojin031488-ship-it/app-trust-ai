"""APP TRUST AI - 바이브코딩 판별 로직 (외부 의존성 없음, 단독 테스트 가능)

핵심 아이디어
1) 증거를 '강/중/약' 세기로 나눠 근거와 함께 제시 (판결이 아니라 심사 보조 지표)
2) 두 축으로 평가: AI 생성 가능성 x 엔지니어링 완성도  ->  심사 분류 권고(triage)
3) 증거 충분도(신뢰도)를 따로 계산, 근거가 부족하면 '판단 보류'
4) 개발자가 AI 사용을 공개했는지와 비교해 불일치를 표시 (앱스토어 정책 연계)
"""
import json, os, re
from datetime import datetime, timezone
from urllib.parse import urlparse

PTS = {"strong": 40, "medium": 15, "weak": 5}

# ※ 아래 패턴 목록은 출발점입니다. 실제 도구 산출물을 확인하며 계속 추가/검증하세요.
AI_HOSTS = ("lovable.app", "lovable.dev", "lovableproject.com", "bolt.host", "bolt.new",
            "replit.app", "repl.co", "v0.dev", "v0.app", "base44.app")
HTML_STRONG = [(r"gptengineer|gpteng\.co|lovable\.dev", "Lovable(구 GPT Engineer) 흔적이 HTML에 남아 있음")]
DEFAULT_TITLE = r"<title>\s*(Vite \+ React|Vite App|Create Next App|React App)[^<]*</title>"
STACK = [r"bg-gradient-to-", r"lucide", r"data-radix|@radix-ui", r"backdrop-blur",
         r"from-(purple|violet|indigo|blue|pink)-\d+"]
REPO_AI_FILES = {".cursorrules": "Cursor", ".cursor": "Cursor", "CLAUDE.md": "Claude Code",
                 ".claude": "Claude Code", "AGENTS.md": "AI 코딩 에이전트", ".windsurfrules": "Windsurf",
                 ".clinerules": "Cline", ".lovable": "Lovable", ".bolt": "Bolt",
                 "GEMINI.md": "Gemini CLI", ".aider.conf.yml": "Aider"}
COAUTH = re.compile(r"co-authored-by:[^\n]*(claude|copilot|cursor|codex|gemini|devin|aider)"
                    r"|generated with[^\n]*(claude|cursor|copilot)|🤖", re.I)
BOT = re.compile(r"lovable|gpt-engineer|v0\[bot\]|copilot", re.I)
GENERIC = re.compile(r"^(update|fix|changes?|edit|add|create|initial commit|wip|commit)\b.{0,40}$", re.I)

S = lambda side, strength, text, src: {"side": side, "strength": strength, "text": text, "src": src}

def site_signals(url, site):
    sig, host = [], urlparse(url).netloc.lower()
    html, text, h = site["html"], site["text"], site["headers"]
    pool = html + "\n" + "\n".join(site["bundles"])
    for d in AI_HOSTS:
        if host.endswith(d):
            sig.append(S("ai", "strong", f"AI 앱 빌더 도메인에서 호스팅됨 ({d})", "site"))
    for pat, label in HTML_STRONG:
        if re.search(pat, html, re.I): sig.append(S("ai", "strong", label, "site"))
    default_title = bool(re.search(DEFAULT_TITLE, html, re.I))
    if default_title: sig.append(S("ai", "medium", "기본 템플릿 제목이 그대로 남아 있음", "site"))
    if "vite.svg" in html: sig.append(S("ai", "medium", "기본 Vite 파비콘을 교체하지 않음", "site"))
    if re.search(r"lorem ipsum|john doe", text, re.I): sig.append(S("ai", "weak", "더미 문구(Lorem ipsum 등)가 남아 있음", "site"))
    if sum(bool(re.search(p, pool, re.I)) for p in STACK) >= 4:
        sig.append(S("ai", "medium", "AI 빌더에서 흔한 기본 스택 조합(Tailwind 그라데이션+shadcn/Radix+lucide). 사람도 많이 쓰므로 단독 근거는 약함", "site"))
    if len(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text)) >= 6:
        sig.append(S("ai", "weak", "화면 문구에 이모지가 많음", "site"))
    p = site["probe"]
    sec = sum(k in h for k in ("content-security-policy", "strict-transport-security", "x-content-type-options"))
    q = [("HTTPS 사용", url.startswith("https"), 1),
         ("보안 헤더 2개 이상 (CSP/HSTS/nosniff)", sec >= 2, 2),
         ("meta description", bool(re.search(r'name=["\']description["\']', html, re.I)), 1),
         ("Open Graph 태그", "og:title" in html, 1),
         ("html lang 속성", bool(re.search(r"<html[^>]+lang=", html, re.I)), 1),
         ("기본 제목/파비콘 교체", not default_title and "vite.svg" not in html, 1),
         ("robots.txt 제공", p["robots"], 1),
         ("없는 경로를 404로 처리", p["404"], 1)]
    imgs = re.findall(r"<img\b[^>]*>", html, re.I)
    if imgs: q.append(("이미지 alt 90% 이상", sum("alt=" in i for i in imgs) / len(imgs) >= 0.9, 1))
    return sig, q

def repo_signals(repo):
    sig, names, gh, pkg, cm = [], repo["names"], repo["gh_names"], repo["pkg"], repo["commits"]
    for f, tool in REPO_AI_FILES.items():
        if f in names: sig.append(S("ai", "strong", f"{tool} 설정 파일 발견: {f}", "repo"))
    if ".replit" in names: sig.append(S("ai", "medium", "Replit 설정 파일 발견 (사람도 사용)", "repo"))
    if "copilot-instructions.md" in gh: sig.append(S("ai", "strong", "Copilot 지침 파일 발견", "repo"))
    if "lovable-tagger" in json.dumps(pkg): sig.append(S("ai", "strong", "package.json에 lovable-tagger 의존성", "repo"))
    n = len(cm)
    hits = [c for c in cm if COAUTH.search(c["msg"]) or BOT.search(c["author"] + " " + c["login"])]
    if hits: sig.append(S("ai", "strong", f"커밋 {len(hits)}건에서 AI 도구 흔적(공동 저자 표기/봇 계정)", "repo"))
    if n >= 10:
        generic = sum(bool(GENERIC.match((c["msg"].splitlines() or [""])[0].strip())) for c in cm)
        if generic / n >= 0.6: sig.append(S("ai", "medium", f"커밋 메시지 {generic}/{n}건이 'Update ...' 류의 기계적 문구", "repo"))
    span = None
    try:
        ds = sorted(datetime.fromisoformat(c["date"].replace("Z", "+00:00")) for c in cm)
        span = (ds[-1] - ds[0]).days if ds else None
    except Exception: pass
    if n >= 3 and span is not None and span <= 2:
        sig.append(S("ai", "weak", "전체 커밋이 2일 이내에 몰려 있음 (해커톤 프로젝트면 정상)", "repo"))
    if n >= 50 and span is not None and span >= 30:
        sig.append(S("human", "medium", f"{span}일에 걸친 꾸준한 커밋 이력 ({n}건+)", "repo"))
    if len({c["login"] or c["author"] for c in cm}) >= 2:
        sig.append(S("human", "medium", "둘 이상의 기여자가 존재", "repo"))
    tests = any(re.match(r"(tests?|__tests__|spec)$", x, re.I) or re.search(r"\.(test|spec)\.", x) for x in names) \
            or bool(pkg.get("scripts", {}).get("test"))
    ci = "workflows" in gh
    lint = any(re.match(r"(\.eslintrc|eslint\.config|\.prettierrc|ruff\.toml|\.flake8|biome\.json)", x) for x in names) \
           or "eslint" in json.dumps(pkg.get("devDependencies", {}))
    if tests and ci: sig.append(S("human", "medium", "테스트와 CI 구성이 함께 존재", "repo"))
    q = [("자동 테스트 존재", tests, 3), ("CI 설정(.github/workflows)", ci, 2), ("린터/포매터 설정", lint, 1),
         ("README 충실(1,500자+)", len(repo["readme"]) >= 1500, 1),
         ("LICENSE", any(x.upper().startswith("LICENSE") for x in names), 1),
         (".env.example 제공", bool({".env.example", ".env.sample"} & names), 1),
         ("Docker 구성", bool({"Dockerfile", "docker-compose.yml"} & names), 1)]
    return sig, q

def llm_judge(sample):
    """보조 증거(가중 15%). 키가 없거나 실패하면 None."""
    if not os.getenv("ANTHROPIC_API_KEY"): return None
    try:
        import anthropic
        prompt = ("앱 스토어 심사 보조자로서, 아래 자료만 근거로 이 앱이 AI 코딩 도구(바이브코딩)로 만들어졌을 가능성을 0~100으로 추정하라. "
                  "출시일, 평가 수, 언어 수 같은 스토어 정보도 판단에 쓰고, 사람이 만들었다고 볼 근거(오래된 출시일, 많은 평가 수)와 AI로 만들었다고 볼 근거를 모두 따져 보라. 정말 자료가 없을 때만 50 근처로 두라. 확정적으로 단정하지 말 것. "
                  'JSON만 출력: {"ai_likelihood":0,"observations":["관찰 근거"]}\n' + json.dumps(sample, ensure_ascii=False)[:9000])
        r = anthropic.Anthropic().messages.create(model="claude-sonnet-4-6", max_tokens=600,
                                                  messages=[{"role": "user", "content": prompt}])
        d = json.loads(re.sub(r"```json|```", "", r.content[0].text).strip())
        d["ai_likelihood"] = max(0, min(100, int(d["ai_likelihood"])))
        return d
    except Exception:
        return None

COV = {"repo": 40, "store": 25, "site": 15, "research": 15}   # 증거 출처별 충분도 가중치

def evaluate(sigs, checks, sources, llm, declared_ai):
    ai_pts = min(70, sum(PTS[s["strength"]] for s in sigs if s["side"] == "ai"))
    h_pts = min(50, sum(PTS[s["strength"]] for s in sigs if s["side"] == "human"))
    rule = 30 + ai_pts - h_pts                      # 30 = 임의의 사전값(prior), 실제 데이터로 보정 필요
    strong_ai = any(s["side"] == "ai" and s["strength"] == "strong" for s in sigs)
    strong_h = any(s["side"] == "human" and s["strength"] == "strong" for s in sigs)
    strong = strong_ai or strong_h                  # 어느 쪽이든 강한 근거가 있는가
    ai = rule if llm is None else 0.85 * rule + 0.15 * llm["ai_likelihood"]
    if strong_ai and strong_h: ai = 55               # 양쪽 강한 근거 -> 혼합
    elif strong_ai: ai = max(ai, 85)
    elif strong_h: ai = min(ai, 20)                  # 사람 쪽 강한 근거 -> AI 점수 20 이하
    ai = round(max(0, min(100, ai)))
    qual = round(100 * sum(w for _, ok, w in checks if ok) / max(1, sum(w for *_, w in checks))) if checks else None
    cov = sum(COV[k] for k in sources) + (5 if llm else 0)
    conf = "높음" if cov >= 60 else "보통" if cov >= 35 else "낮음"
    # 수정: 증거(signals)가 하나도 없으면 '직접 제작'이라고 단정하지 않고 판단 보류
    hold = (conf == "낮음" or not sigs) and not strong
    if hold: verdict = "판단 보류 (근거 부족)"
    elif ai >= 70: verdict = "AI 도구 사용 가능성 높음 (바이브코딩 의심)"
    elif ai >= 40: verdict = "AI 보조 개발 가능성 (혼합)"
    else: verdict = "개발자 직접 제작 가능성 높음"
    q = qual if qual is not None else 50
    if hold: triage = "근거 부족 → 심사자가 직접 확인"
    elif ai >= 65 and q >= 60: triage = "AI 활용 · 완성도 양호 → 일반 심사"
    elif ai >= 65: triage = "AI 생성 의심 · 완성도 낮음 → 정밀 심사 우선"
    elif q >= 60: triage = "직접 개발 가능성 · 완성도 양호 → 빠른 통과"
    else: triage = "직접 개발 가능성 · 완성도 미흡 → 보완 요청"
    flag = None
    if declared_ai is False and (strong or ai >= 65): flag = "AI 사용 미공개로 신고했으나 AI 흔적이 발견됨 → 소명 요청 권고"
    elif declared_ai is True and ai < 40: flag = "AI 사용을 공개했으나 코드/페이지의 흔적은 적음 (정상일 수 있음)"
    return {"ai_score": ai, "quality_score": qual, "confidence": conf, "verdict": verdict, "triage": triage,
            "priority": round(ai * (100 - q) / 100), "declaration_flag": flag,
            "signals": sorted(sigs, key=lambda s: -PTS[s["strength"]]),
            "quality_checks": [{"label": l, "ok": ok} for l, ok, _ in checks],
            "llm": llm, "disclaimer": "확정 판결이 아닌 심사 보조 지표입니다. 근거를 확인하고 필요하면 개발자 소명을 받으세요."}


# ---------- 스토어 메타데이터 / 웹 리서치 증거 ----------
def _dt(x):
    try: return datetime.fromisoformat((x or "").replace("Z", "+00:00"))
    except Exception: return None

CHATGPT_DATE = datetime(2022, 11, 30, tzinfo=timezone.utc)   # 생성형 AI 코딩이 대중화되기 시작한 시점

def store_signals(meta, siblings):
    """meta: iTunes Search API 결과 1건, siblings: 같은 개발자의 앱 목록"""
    sig, desc = [], meta.get("description", "") or ""
    rel, cur = _dt(meta.get("releaseDate")), _dt(meta.get("currentVersionReleaseDate"))
    ratings = meta.get("userRatingCount") or 0
    langs = len(meta.get("languageCodesISO2A") or [])
    now = datetime.now(timezone.utc)
    old_app = bool(rel and rel < CHATGPT_DATE)                      # 2022-11-30 이전 출시
    big_app = ratings >= 100000
    if len(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", desc)) >= 6:
        sig.append(S("ai", "weak", "스토어 설명에 이모지가 많음", "store"))
    # --- AI 쪽: 단기간 대량 제작 (오래됐거나 대형 앱은 제외해 오탐을 막음) ---
    if rel and not old_app and not big_app:
        near = [a for a in siblings if (d := _dt(a.get("releaseDate"))) and abs((d - rel).days) <= 90]
        if len(near) >= 3:
            sig.append(S("ai", "medium", f"같은 개발자가 이 앱 출시 전후 90일 안에 앱 {len(near)}개를 출시 (대량 제작 패턴)", "store"))
    # --- 사람 쪽: 스토어 정보로 보는 직접 개발 근거 ---
    if old_app:
        sig.append(S("human", "strong", f"ChatGPT 공개(2022-11-30) 이전인 {rel.year}년에 출시된 앱", "store"))
    if ratings >= 100000:
        sig.append(S("human", "medium", f"평가 수가 {ratings:,}건으로 많음 (오랜 기간 검증된 앱)", "store"))
    elif ratings >= 10000:
        sig.append(S("human", "weak", f"평가 수가 {ratings:,}건 (어느 정도 이용자가 있는 앱)", "store"))
    if rel and cur and (cur - rel).days >= 1095 and (now - cur).days <= 365:
        sig.append(S("human", "medium", f"출시 후 {round((cur - rel).days / 365)}년 넘게 꾸준히 업데이트됨", "store"))
    if langs >= 10:
        sig.append(S("human", "medium", f"{langs}개 언어로 현지화된 앱", "store"))
    q = [("개발자 웹사이트 등록", bool(meta.get("sellerUrl")), 1),
         ("릴리스 노트 작성", len(meta.get("releaseNotes") or "") >= 20, 1)]
    if rel and cur and (now - rel).days >= 30:    # 신규 앱은 업데이트 이력을 따지지 않음
        q.append(("출시 후 업데이트 이력", (cur - rel).days >= 7, 2))
    return sig, q

def research_signals(r):
    """웹 리서치(LLM+검색) 결과. 출처 URL이 있는 '본인 발언'만 강한 증거로 인정."""
    if not r: return []
    ms = [m for m in r.get("mentions", []) if m.get("claim")]
    if r.get("self_declared_ai") is True and ms and ms[0].get("source_url"):
        return [S("ai", "strong", f"공개 자료에서 AI 도구 사용 언급: {ms[0]['claim'][:90]} (출처: {ms[0]['source_url']})", "research")]
    if ms: return [S("ai", "medium", f"AI 도구 관련 언급 {len(ms)}건 발견 (제작 방식에 대한 언급인지 출처 확인 필요)", "research")]
    return []
