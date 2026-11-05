"""실행: pip install -r requirements.txt
        export ANTHROPIC_API_KEY=...   (선택: 웹 리서치 + LLM 보조 판정)
        export GITHUB_TOKEN=...        (선택: GitHub API 한도 상향)
        export SUPABASE_URL=...        (선택: 결과 저장)
        export SUPABASE_KEY=...        (선택: 서버 전용 secret 키)
        uvicorn main:app --reload      ->  http://127.0.0.1:8000
공개된 정보만 읽습니다 (비밀키 탐색·침투 테스트 없음)."""
import os
import traceback
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import collect

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
    reviewer_label: str              # "ai" | "human" | "mixed"
    note: str = ""

@app.post("/api/search")
def search(q: Q):
    try:
        r = collect.run(q.query, q.track_id, q.repo, q.declared_ai)
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
def review(r: Review):
    if not sb:
        return JSONResponse({"error": "Supabase 미연결"}, status_code=503)
    sb.table("reviews").insert(r.model_dump()).execute()
    return {"ok": True}

@app.get("/api/health")
def health(): return {"ok": True, "db": sb is not None}

@app.get("/")
def index():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"message": "APP TRUST AI API is running. /docs 에서 테스트할 수 있어요."}
