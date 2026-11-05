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
from fastapi import FastAPI, Header
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

@app.post("/api/search")
def search(q: Q):
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
    return {"message": "APP TRUST AI API is running. /docs 에서 테스트할 수 있어요."}
