"""실행: pip install -r requirements.txt
        export ANTHROPIC_API_KEY=...   (선택: 웹 리서치 + LLM 보조 판정)
        export GITHUB_TOKEN=...        (선택: GitHub API 한도 상향)
        export SUPABASE_URL=...        (선택: 결과 저장)
        export SUPABASE_KEY=...        (선택: 서버 전용 secret 키)
        export REVIEW_TOKEN=...        (필수: 심사자 판정 저장용 암호. 없으면 판정 저장이 막힘)
                                        (앱 제출 기능의 심사자 화면도 같은 암호를 사용)
        uvicorn main:app --reload      ->  http://127.0.0.1:8000
공개된 정보만 읽습니다 (비밀키 탐색·침투 테스트 없음).
앱 제출 기능(submissions / revision_requests / submission_events 테이블)은 SUPABASE_URL/KEY 가 있어야 동작합니다."""
import hmac
import os
import time
import traceback
from typing import Literal
from concurrent.futures import ThreadPoolExecutor, wait
from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
import collect
import detector as D
import store as ST
import submissions as SUB

# /docs, /redoc, /openapi.json 을 꺼서 내부 API 구조가 공개되지 않게 한다
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

SEC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    # index.html 이 인라인 스크립트/스타일을 쓰므로 'unsafe-inline' 이 필요하다
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                              "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
}

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    resp = await call_next(request)
    for k, v in SEC_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp

# Supabase 연결 (환경변수가 없으면 저장 기능만 꺼지고 나머지는 정상 동작)
sb = None
if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY"):
    try:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    except Exception:
        traceback.print_exc()

# 앱 제출 기능 저장소 (같은 Supabase 연결을 사용. 연결이 없으면 제출 기능만 503)
# main.py의 56번째 줄
db = ST.SupabaseStore(sb) if sb else None


def reviewer_denied(x_reviewer_token):
    """REVIEW_TOKEN 확인. 통과하면 None, 막히면 오류 응답을 돌려준다."""
    token = os.getenv("REVIEW_TOKEN")
    if not token:                                  # 암호가 설정되지 않았으면 아무도 저장할 수 없다
        return JSONResponse({"error": "서버에 심사자 코드(REVIEW_TOKEN)가 아직 설정되지 않았어요"}, status_code=503)
    if not hmac.compare_digest((x_reviewer_token or "").encode(), token.encode()):
        return JSONResponse({"error": "심사자 코드가 맞지 않아요"}, status_code=401)
    return None

class Q(BaseModel):
    query: str = ""                  # 앱 이름 (또는 URL / GitHub 주소)
    url: str | None = None           # 공식 웹사이트 주소 (선택. 이름과 함께 넣으면 같은 앱인지 확인)
    track_id: int | None = None      # 후보 중에서 다른 앱을 고른 경우
    repo: str | None = None          # GitHub 주소를 직접 지정 (선택)
    declared_ai: bool | None = None  # 개발자가 신고한 AI 사용 여부 (선택)

class Review(BaseModel):
    analysis_id: int
    reviewer_label: Literal["ai", "human", "mixed"]   # 이 세 값이 아니면 422로 거절
    note: str = ""

# ---------- 심사자 판정 불러오기 / 점수 보정 ----------
CACHE = {"at": 0.0, "rows": []}

def load_rows():
    """심사자 판정 + 그 분석의 점수/근거. 같은 앱은 가장 최근 판정 1건만 쓴다. 10분 동안은 저장해 둔 값을 쓴다."""
    if not sb: return []
    if time.time() - CACHE["at"] < 600: return CACHE["rows"]
    latest = {}
    try:
        data = sb.table("reviews").select(
            "id, reviewer_label, analyses(app_name, seller, ai_score, verdict, signals)"
        ).order("id").limit(5000).execute().data or []
        for d in data:                    # id 순서(오래된 것 먼저)로 덮어써서 '앱마다 가장 최근 판정 1건'만 남긴다
            a = d.get("analyses") or {}
            key = (a.get("app_name") or "", a.get("seller") or "") if a.get("app_name") else ("#", d["id"])
            latest[key] = {"label": d["reviewer_label"], "signals": a.get("signals") or [],
                           "ai_score": a.get("ai_score"), "verdict": a.get("verdict")}
    except Exception:
        traceback.print_exc()
        return CACHE["rows"]             # 실패하면 이전 값을 그대로 쓴다
    rows = list(latest.values())
    CACHE["at"], CACHE["rows"] = time.time(), rows
    return rows

def current_weights():
    return {k: v["weight"] for k, v in D.compute_weights(load_rows()).items()}

# ---------- 호출 제한 (같은 IP가 10분에 분석 30건까지) ----------
# 서버리스 특성상 서버 인스턴스마다 따로 세는 간이 방식이라 완벽한 차단은 아니다.
HITS = {}
LIMIT, WINDOW = 30, 600
BATCH_MAX, BATCH_WORKERS, BATCH_BUDGET = 10, 5, 50   # 한 번에 10개, 동시 5개, 50초 안에 못 끝내면 중단

def too_many(request: Request, n: int = 1) -> bool:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    now = time.time()
    recent = [t for t in HITS.get(ip, []) if now - t < WINDOW]
    if len(recent) + n > LIMIT:
        HITS[ip] = recent
        return True
    HITS[ip] = recent + [now] * n
    if len(HITS) > 2000:                       # 메모리가 불어나지 않게 오래된 IP 정리
        for k in [k for k, v in HITS.items() if not v or now - v[-1] > WINDOW]: HITS.pop(k, None)
    return False

class Batch(BaseModel):
    queries: list[str]

def analyze_one(q: Q, research: bool = True):
    try:
        r = collect.run(q.query, q.track_id, q.repo, q.declared_ai, current_weights(), research, q.url)
        if sb and "error" not in r and not r.get("needs_selection"):
            try:
                row = sb.table("analyses").insert({
                    "app_name": r.get("name"), "seller": r.get("seller"),
                    "ai_score": r.get("ai_score"), "quality_score": r.get("quality_score"),
                    "confidence": r.get("confidence"), "verdict": r.get("verdict"),
                    "triage": r.get("triage"), "priority": r.get("priority"),
                    "declaration_flag": r.get("declaration_flag"),
                    "signals": r.get("signals"), "raw": r}).execute()
                r["analysis_id"] = row.data[0]["id"]
            except Exception as e:
                # Vercel 로그에서 'SAVE_FAIL' 로 검색하면 원인(컬럼 불일치, 권한 등)이 보인다
                print("SAVE_FAIL", type(e).__name__, str(e)[:500])
                traceback.print_exc()   # 저장 실패해도 분석 결과는 돌려줌
                r["saved"] = False
        return r
    except Exception as e:
        traceback.print_exc()                       # 자세한 원인은 서버 로그에만 남긴다
        return {"error": "분석 중 문제가 생겼어요. 잠시 뒤 다시 시도해 주세요."}

@app.post("/api/search")
def search(q: Q, request: Request):
    if too_many(request):
        return JSONResponse({"error": "요청이 너무 많아요. 잠시 뒤 다시 시도해 주세요."}, status_code=429)
    return analyze_one(q)

@app.post("/api/batch")
def batch(b: Batch, request: Request):
    """앱 이름 여러 개(최대 10개)를 동시에 분석해 심사 우선순위 순으로 돌려준다."""
    names = []
    for x in b.queries:
        x = (x or "").strip()[:100]
        if x and x not in names: names.append(x)
    if not names:
        return JSONResponse({"error": "앱 이름을 입력해 주세요."}, status_code=400)
    if len(names) > BATCH_MAX:
        return JSONResponse({"error": f"한 번에 {BATCH_MAX}개까지만 가능해요. (지금 {len(names)}개)"}, status_code=400)
    if too_many(request, len(names)):
        return JSONResponse({"error": "요청이 너무 많아요. 잠시 뒤 다시 시도해 주세요."}, status_code=429)
    ex = ThreadPoolExecutor(max_workers=BATCH_WORKERS)
    futs = {n: ex.submit(analyze_one, Q(query=n), False) for n in names}
    wait(list(futs.values()), timeout=BATCH_BUDGET)
    ex.shutdown(wait=False, cancel_futures=True)
    items = []
    for n, f in futs.items():
        if f.done():
            try: items.append({"query": n, "data": f.result()}); continue
            except Exception: err = "분석 중 문제가 생겼어요"
        else:
            err = "시간 초과 (이 앱만 다시 분석해 보세요)"
        items.append({"query": n, "data": {"error": err}})
    items.sort(key=lambda i: -(i["data"].get("priority") if isinstance(i["data"].get("priority"), (int, float)) else -1))
    return {"items": items}

@app.post("/api/review")
def review(r: Review, x_reviewer_token: str | None = Header(default=None)):
    if not sb:
        return JSONResponse({"error": "Supabase 미연결"}, status_code=503)
    if (denied := reviewer_denied(x_reviewer_token)): return denied
    try:
        sb.table("reviews").insert(r.model_dump()).execute()
    except Exception:
        traceback.print_exc()
        return JSONResponse({"error": "저장하지 못했어요 (없는 분석 번호일 수 있어요)"}, status_code=400)
    CACHE["at"] = 0.0                              # 새 판정이 바로 보정에 반영되도록
    return {"ok": True}

@app.get("/api/stats")
def stats():
    """도구 정확도와 근거별 보정값 (심사자 판정이 쌓일수록 의미가 생김)"""
    if not sb:
        return JSONResponse({"error": "Supabase 미연결"}, status_code=503)
    rows = load_rows()
    rep = D.accuracy_report(rows)
    w = D.compute_weights(rows)
    rep["min_n"] = D.MIN_N
    rep["weights"] = sorted(
        [{"key": k, "n": v["n"], "precision": v["precision"], "weight": v["weight"]} for k, v in w.items()],
        key=lambda x: -x["n"])[:12]
    return rep

# ---------- 앱 제출 (개발자) / 제출 심사 (심사자) ----------
class SubmissionIn(BaseModel):
    app_name: str = ""
    developer: str = ""
    store: str = ""
    store_track_id: str | int | None = None
    store_url: str | None = None
    website: str | None = None
    description: str | None = None
    ai_disclosure: str = ""
    ai_note: str | None = None
    privacy_url: str | None = None
    operator_info: str | None = None
    repo: str | None = None

class StatusIn(BaseModel):
    status: str
    note: str = ""

class RevisionIn(BaseModel):
    field: str
    reason: str

class AnswerIn(BaseModel):
    value: str | None = ""

class ResolveIn(BaseModel):
    outcome: Literal["resolved", "dismissed"] = "resolved"

NO_DB = {"error": "Supabase 미연결 (앱 제출 기능을 쓰려면 SUPABASE_URL / SUPABASE_KEY 가 필요해요)"}

def sub_call(fn, *args):
    """제출 기능 공통 처리: 입력 오류는 그 메시지로, DB 오류는 로그만 남기고 일반 메시지로 돌려준다."""
    if not db:
        return JSONResponse(NO_DB, status_code=503)
    try:
        return fn(db, *args)
    except SUB.SubmissionError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)
    except Exception as e:
        # Vercel 로그에서 'SUBMISSION_FAIL' 로 검색하면 원인(컬럼 불일치, 권한 등)이 보인다
        print("SUBMISSION_FAIL", type(e).__name__, str(e)[:500])
        traceback.print_exc()
        return JSONResponse({"error": "저장소 처리 중 문제가 생겼어요. 잠시 뒤 다시 시도해 주세요."}, status_code=500)

@app.post("/api/submissions")
def submission_create(s: SubmissionIn, request: Request):
    if too_many(request):
        return JSONResponse({"error": "요청이 너무 많아요. 잠시 뒤 다시 시도해 주세요."}, status_code=429)
    def go(st, data):
        sub, code = SUB.create(st, data)
        return {"submission": sub, "author_code": code, "criteria": SUB.C.evaluate(sub)}
    return sub_call(go, s.model_dump())

@app.get("/api/submissions/{sid}/mine")
def submission_mine(sid: int, request: Request, x_author_code: str | None = Header(default=None)):
    if too_many(request):                          # 작성자 코드 무차별 대입을 늦춘다
        return JSONResponse({"error": "요청이 너무 많아요. 잠시 뒤 다시 시도해 주세요."}, status_code=429)
    return sub_call(SUB.author_view, sid, x_author_code)

@app.post("/api/submissions/{sid}/revisions/{rid}/answer")
def submission_answer(sid: int, rid: int, a: AnswerIn, request: Request, x_author_code: str | None = Header(default=None)):
    if too_many(request):
        return JSONResponse({"error": "요청이 너무 많아요. 잠시 뒤 다시 시도해 주세요."}, status_code=429)
    return sub_call(SUB.answer_revision, sid, x_author_code, rid, a.value)

@app.get("/api/submissions")
def submission_list(status: str | None = None, x_reviewer_token: str | None = Header(default=None)):
    if (denied := reviewer_denied(x_reviewer_token)): return denied
    return sub_call(lambda st, s: {"items": SUB.reviewer_list(st, s)}, status or None)

@app.get("/api/submissions/{sid}")
def submission_detail(sid: int, x_reviewer_token: str | None = Header(default=None)):
    if (denied := reviewer_denied(x_reviewer_token)): return denied
    return sub_call(SUB.reviewer_view, sid)

@app.post("/api/submissions/{sid}/status")
def submission_status(sid: int, s: StatusIn, x_reviewer_token: str | None = Header(default=None)):
    if (denied := reviewer_denied(x_reviewer_token)): return denied
    return sub_call(SUB.set_status, sid, s.status, s.note)

@app.post("/api/submissions/{sid}/revisions")
def submission_revision(sid: int, r: RevisionIn, x_reviewer_token: str | None = Header(default=None)):
    if (denied := reviewer_denied(x_reviewer_token)): return denied
    return sub_call(SUB.request_revision, sid, r.field, r.reason)

@app.post("/api/revisions/{rid}/resolve")
def revision_resolve(rid: int, r: ResolveIn, x_reviewer_token: str | None = Header(default=None)):
    if (denied := reviewer_denied(x_reviewer_token)): return denied
    return sub_call(SUB.resolve_revision, rid, r.outcome)

@app.post("/api/submissions/{sid}/analyze")
def submission_analyze(sid: int, request: Request, x_reviewer_token: str | None = Header(default=None)):
    """제출 정보로 기존 앱 분석을 실행한다. 결과는 /api/search 와 같은 모양이고 analyses 테이블에도 그대로 저장된다."""
    if (denied := reviewer_denied(x_reviewer_token)): return denied
    if not db:
        return JSONResponse(NO_DB, status_code=503)
    if too_many(request):
        return JSONResponse({"error": "요청이 너무 많아요. 잠시 뒤 다시 시도해 주세요."}, status_code=429)
    sub = sub_call(lambda st: st.get_submission(sid))
    if isinstance(sub, JSONResponse): return sub
    if not sub:
        return JSONResponse({"error": "제출 건을 찾을 수 없어요."}, status_code=404)
    args = SUB.analysis_args(sub)
    if not args["query"]:
        return JSONResponse({"error": "분석할 앱 이름이나 주소가 없어요."}, status_code=400)
    r = analyze_one(Q(**args))
    logged = sub_call(SUB.record_analysis, sid, r)
    if isinstance(logged, JSONResponse): r["linked"] = False   # 분석은 성공했지만 제출 이력 연결만 실패
    r["submission_id"] = sid
    return r

@app.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nAllow: /\nDisallow: /api/\n")

@app.get("/api/health")
def health(): return {"ok": True, "db": sb is not None}

@app.get("/")
def index():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"message": "APP TRUST AI API is running."}
