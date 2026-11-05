"""실행: pip install -r requirements.txt
        export ANTHROPIC_API_KEY=...   (선택: 웹 리서치 + LLM 보조 판정)
        export GITHUB_TOKEN=...        (선택: GitHub API 한도 상향)
        uvicorn main:app --reload      ->  http://127.0.0.1:8000
공개된 정보만 읽습니다 (비밀키 탐색·침투 테스트 없음)."""
import traceback
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
import collect

app = FastAPI()

class Q(BaseModel):
    query: str                       # 앱 이름 (또는 URL / GitHub 주소)
    track_id: int | None = None      # 후보 중에서 다른 앱을 고른 경우
    repo: str | None = None          # GitHub 주소를 직접 지정 (선택)
    declared_ai: bool | None = None  # 개발자가 신고한 AI 사용 여부 (선택)

@app.post("/api/search")
def search(q: Q):                    # sync 함수: FastAPI가 스레드풀에서 실행
    try:
        return collect.run(q.query, q.track_id, q.repo, q.declared_ai)
    except Exception as e:
        traceback.print_exc()        # 터미널에도 에러를 출력
        return {"error": f"서버 오류: {type(e).__name__}: {e}"}

@app.get("/api/health")
def health(): return {"ok": True}

@app.get("/")
def index(): return FileResponse("index.html")
