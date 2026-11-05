"""실행: pip install -r requirements.txt
        export ANTHROPIC_API_KEY=...   (선택: 웹 리서치 + LLM 보조 판정)
        export GITHUB_TOKEN=...        (선택: GitHub API 한도 상향)
        export SUPABASE_URL=...        (선택: 결과 저장)
        export SUPABASE_KEY=...        (선택: 서버 전용 secret 키)
        export REVIEW_TOKEN=...        (선택: 심사자 판정 저장용 암호. 없으면 누구나 저장 가능)
        uvicorn main:app --reload      ->  http://127.0.0.1:8000
공개된 정보만 읽습니다 (비밀키 탐색·침투 테스트 없음)."""
import os
import time
import traceback
from typing import Literal
from concurrent.futures import ThreadPoolExecutor, wait
from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import collect
import detector as D

app = FastAPI()

# Supabase 연결 (환경변수가 없으면 저장 기능만 꺼지고 나머지는 정상 동작)
sb = None
if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY"):
    try:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    except Exception:
        traceback.print_exc()

class Q(BaseModel):
    query: str                       # 앱 이름 (또는 URL / GitHub 주소)
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
    """심사자 판정 + 그 분석의 점수/근거. 10분 동안은 저장해 둔 값을 쓴다."""
    if not sb: return []
    if time.time() - CACHE["at"] < 600: return CACHE["rows"]
    rows = []
    try:
        data = sb.table("reviews").select(
            "reviewer_label, analyses(ai_score, verdict, signals)").limit(5000).execute().data or []
        for d in data:
            a = d.get("analyses") or {}
            rows.append({"label": d["reviewer_label"], "signals": a.get("signals") or [],
                         "ai_score": a.get("ai_score"), "verdict": a.get("verdict")})
    except Exception:
        traceback.print_exc()
        return CACHE["rows"]             # 실패하면 이전 값을 그대로 쓴다
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

def analyze_one(q: Q):
    try:
        r = collect.run(q.query, q.track_id, q.repo, q.declared_ai, current_weights())
        if sb and "error" not in r:
            try:
                row = sb.table("analyses").insert({
                    "app_name": r.get("name"), "seller": r.get("seller"),
                    "ai_score": r.get("ai_score"), "quality_score": r.get("quality_score"),
                    "confidence": r.get("confidence"), "verdict": r.get("verdict"),
                    "triage": r.get("triage"), "priority": r.get("priority"),
                    "declaration_flag": r.get("declaration_flag"),
                    "signals": r.get("signals"), "raw": r}).execute()
                r["analysis_id"] = row.data[0]["id"]
            except Exception:
                traceback.print_exc()   # 저장 실패해도 분석 결과는 돌려줌
        return r
    except Exception as e:
        traceback.print_exc()
        return {"error": f"서버 오류: {type(e).__name__}: {e}"}

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
    futs = {n: ex.submit(analyze_one, Q(query=n)) for n in names}
    wait(list(futs.values()), timeout=BATCH_BUDGET)
    ex.shutdown(wait=False, cancel_futures=True)
    items = []
    for n, f in futs.items():
        if f.done():
            try: items.append({"query": n, "data": f.result()}); continue
            except Exception as e: err = f"{type(e).__name__}"
        else:
            err = "시간 초과 (이 앱만 다시 분석해 보세요)"
        items.append({"query": n, "data": {"error": err}})
    items.sort(key=lambda i: -(i["data"].get("priority") if isinstance(i["data"].get("priority"), (int, float)) else -1))
    return {"items": items}

@app.post("/api/review")
def review(r: Review, x_reviewer_token: str | None = Header(default=None)):
    if not sb:
        return JSONResponse({"error": "Supabase 미연결"}, status_code=503)
    token = os.getenv("REVIEW_TOKEN")
    if token and x_reviewer_token != token:       # 암호를 정해 둔 경우, 틀리면 저장하지 않는다
        return JSONResponse({"error": "심사자 코드가 맞지 않아요"}, status_code=401)
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

@app.get("/api/health")
def health(): return {"ok": True, "db": sb is not None}

@app.get("/")
def index():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"message": "APP TRUST AI 서버가 실행 중이에요. /docs 에서 테스트할 수 있어요."}
