"""APP TRUST AI 서버.

실행:
    pip install -r requirements.txt
    uvicorn main:app --reload

비밀값은 코드가 아닌 서버 환경변수에 설정합니다.
"""

import hmac
import os
import time
import traceback
from pathlib import Path
from typing import Literal
from concurrent.futures import ThreadPoolExecutor, wait

from fastapi import FastAPI, Header, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
)
from pydantic import BaseModel

import collect
import detector as D
import store as ST
import submissions as SUB
import criteria as C


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

BASE_DIR = Path(__file__).resolve().parent

SEC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=()"
    ),
    "Strict-Transport-Security": (
        "max-age=63072000; includeSubDomains"
    ),
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://*.mzstatic.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
}


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)

    for key, value in SEC_HEADERS.items():
        response.headers.setdefault(key, value)

    return response


# ---------- Supabase 연결 ----------

sb = None

if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY"):
    try:
        from supabase import create_client

        sb = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_KEY"],
        )
    except Exception:
        print("SUPABASE_INIT_FAIL")
        traceback.print_exc()

db = ST.SupabaseStore(sb) if sb is not None else None


# ---------- 심사자 인증 ----------

def reviewer_denied(value):
    token = os.getenv("REVIEW_TOKEN")

    if not token:
        return JSONResponse(
            {
                "error": (
                    "서버에 심사자 코드(REVIEW_TOKEN)가 "
                    "설정되지 않았어요."
                )
            },
            status_code=503,
        )

    if not hmac.compare_digest(
        (value or "").encode(),
        token.encode(),
    ):
        return JSONResponse(
            {"error": "심사자 코드가 맞지 않아요."},
            status_code=401,
        )

    return None


# ---------- 요청 형식 ----------

class Q(BaseModel):
    query: str = ""
    url: str | None = None
    track_id: int | None = None
    repo: str | None = None
    declared_ai: bool | None = None


class Review(BaseModel):
    analysis_id: int
    reviewer_label: Literal["ai", "human", "mixed"]
    note: str = ""


class Batch(BaseModel):
    queries: list[str]


# ---------- 캐시·호출 제한 ----------

CACHE = {
    "at": 0.0,
    "rows": [],
}

HITS = {}

LIMIT = 30
WINDOW = 600

BATCH_MAX = 10
BATCH_WORKERS = 5
BATCH_BUDGET = 50


def load_rows():
    if sb is None:
        return []

    if time.time() - CACHE["at"] < 600:
        return CACHE["rows"]

    latest = {}

    try:
        rows = (
            sb.table("reviews")
            .select(
                "id, reviewer_label, "
                "analyses(app_name, seller, ai_score, verdict, signals)"
            )
            .order("id")
            .limit(5000)
            .execute()
            .data
            or []
        )

        for row in rows:
            analysis = row.get("analyses") or {}

            if analysis.get("app_name"):
                key = (
                    analysis.get("app_name"),
                    analysis.get("seller") or "",
                )
            else:
                key = ("#", row["id"])

            latest[key] = {
                "label": row["reviewer_label"],
                "signals": analysis.get("signals") or [],
                "ai_score": analysis.get("ai_score"),
                "verdict": analysis.get("verdict"),
            }

    except Exception:
        traceback.print_exc()
        return CACHE["rows"]

    CACHE["at"] = time.time()
    CACHE["rows"] = list(latest.values())

    return CACHE["rows"]


def current_weights():
    return {
        key: value["weight"]
        for key, value in D.compute_weights(
            load_rows()
        ).items()
    }


def too_many(request: Request, n: int = 1):
    fallback = (
        request.client.host
        if request.client
        else "?"
    )

    ip = (
        request.headers.get("x-forwarded-for")
        or fallback
    ).split(",")[0].strip()

    now = time.time()

    recent = [
        stamp
        for stamp in HITS.get(ip, [])
        if now - stamp < WINDOW
    ]

    if len(recent) + n > LIMIT:
        HITS[ip] = recent
        return True

    HITS[ip] = recent + [now] * n

    if len(HITS) > 2000:
        for key in list(HITS):
            if (
                not HITS[key]
                or now - HITS[key][-1] > WINDOW
            ):
                HITS.pop(key, None)

    return False


def limited():
    return JSONResponse(
        {
            "error": (
                "요청이 너무 많아요. "
                "잠시 뒤 다시 시도해 주세요."
            )
        },
        status_code=429,
    )


# ---------- 추가: 스토어 검색 연결 ----------

@app.post("/api/store/search")
def store_search(q: Q, request: Request):
    if too_many(request):
        return limited()

    query = q.query.strip()

    if not query:
        return JSONResponse(
            {
                "error": (
                    "앱 이름이나 스토어 주소를 입력해 주세요."
                )
            },
            status_code=400,
        )

    if len(query) > 100:
        return JSONResponse(
            {
                "error": (
                    "검색어는 100자 이내로 입력해 주세요."
                )
            },
            status_code=400,
        )

    try:
        # 기존 store.py의 검색 기능을 재사용합니다.
        # 검색 후보를 반환할 뿐 자동으로 분석하지 않습니다.
        result = ST.search(query)

        if not isinstance(result, dict):
            raise ValueError("Invalid store response")

        return result

    except Exception:
        traceback.print_exc()

        return JSONResponse(
            {
                "error": (
                    "스토어 정보를 불러오지 못했습니다. "
                    "잠시 후 다시 시도해 주세요."
                )
            },
            status_code=502,
        )


# ---------- 추가: 기준 안내 조회 ----------

@app.get("/api/criteria")
def criteria_info():
    # 제출 정보 점검 기준과 자동 분석 신호는 구분합니다.
    # 변경 이력이나 자동 조사 기록을 임의로 만들지 않습니다.
    return {
        "version": "제출 정보 기준 v1 (criteria.py)",
        "policy": [
            (
                "AI 활용 자체를 낮은 품질의 근거로 "
                "사용하지 않습니다."
            ),
            (
                "AI 활용 관련 신호와 기술적 완성도 "
                "정보를 구분합니다."
            ),
            (
                "공개 자료만 사용하며 "
                "보안·성능 인증 결과가 아닙니다."
            ),
            (
                "근거 부족은 AI 미사용이나 "
                "낮은 품질을 뜻하지 않습니다."
            ),
            (
                "제출 정보 점검 기준과 자동 분석 신호는 "
                "별도 기준입니다."
            ),
            (
                "정보 완성도·기술적 완성도·"
                "AI 활용 투명성을 점검합니다."
            ),
            (
                "세부 제출 기준: "
                + " / ".join(
                    item["label"]
                    for item in C.CRITERIA
                )
            ),
        ],
        "changelog": [],
        "watchlist": [],
        "automation": (
            "새 도구 자동 조사·자동 기준 업데이트는 "
            "구현되지 않았습니다."
        ),
        "items": [
            {
                key: item.get(key)
                for key in (
                    "key",
                    "category",
                    "label",
                    "weight",
                    "field",
                )
            }
            for item in C.CRITERIA
        ],
    }


# ---------- 기존 앱 분석 ----------

def analyze_one(q: Q, research: bool = True):
    try:
        result = collect.run(
            q.query,
            q.track_id,
            q.repo,
            q.declared_ai,
            current_weights(),
            research,
            q.url,
        )

        if (
            "error" not in result
            and not result.get("needs_selection")
        ):
            result["saved"] = False

            if sb is not None:
                try:
                    response = (
                        sb.table("analyses")
                        .insert({
                            "app_name": result.get("name"),
                            "seller": result.get("seller"),
                            "ai_score": result.get("ai_score"),
                            "quality_score": result.get(
                                "quality_score"
                            ),
                            "confidence": result.get(
                                "confidence"
                            ),
                            "verdict": result.get("verdict"),
                            "triage": result.get("triage"),
                            "priority": result.get("priority"),
                            "declaration_flag": result.get(
                                "declaration_flag"
                            ),
                            "signals": result.get("signals"),
                            "raw": dict(result),
                        })
                        .execute()
                    )

                    result["analysis_id"] = (
                        response.data[0]["id"]
                    )
                    result["saved"] = True

                except Exception:
                    print("SAVE_FAIL")
                    traceback.print_exc()

        return result

    except Exception:
        traceback.print_exc()

        return {
            "error": (
                "분석 중 문제가 생겼어요. "
                "잠시 뒤 다시 시도해 주세요."
            )
        }


@app.post("/api/search")
def search(q: Q, request: Request):
    if too_many(request):
        return limited()

    return analyze_one(q)


@app.post("/api/batch")
def batch(b: Batch, request: Request):
    names = []

    for value in b.queries:
        value = value.strip()[:100]

        if value and value not in names:
            names.append(value)

    if not names:
        return JSONResponse(
            {"error": "앱 이름을 입력해 주세요."},
            status_code=400,
        )

    if len(names) > BATCH_MAX:
        return JSONResponse(
            {
                "error": (
                    f"한 번에 {BATCH_MAX}개까지만 가능해요."
                )
            },
            status_code=400,
        )

    if too_many(request, len(names)):
        return limited()

    executor = ThreadPoolExecutor(
        max_workers=BATCH_WORKERS
    )

    futures = {
        name: executor.submit(
            analyze_one,
            Q(query=name),
            False,
        )
        for name in names
    }

    wait(
        list(futures.values()),
        timeout=BATCH_BUDGET,
    )

    executor.shutdown(
        wait=False,
        cancel_futures=True,
    )

    items = []

    for name, future in futures.items():
        if future.done():
            try:
                data = future.result()
            except Exception:
                data = {
                    "error": "분석 중 문제가 생겼어요."
                }
        else:
            data = {
                "error": (
                    "시간 초과: 이 앱만 다시 분석해 주세요."
                )
            }

        items.append({
            "query": name,
            "data": data,
        })

    items.sort(
        key=lambda item: -(
            item["data"].get("priority")
            if isinstance(
                item["data"].get("priority"),
                (int, float),
            )
            else -1
        )
    )

    return {"items": items}


# ---------- 기존 분석 검수·통계 ----------

@app.post("/api/review")
def review(
    r: Review,
    x_reviewer_token: str | None = Header(default=None),
):
    if sb is None:
        return JSONResponse(
            {"error": "Supabase 미연결"},
            status_code=503,
        )

    denied = reviewer_denied(x_reviewer_token)

    if denied is not None:
        return denied

    try:
        sb.table("reviews").insert(
            r.model_dump()
        ).execute()

    except Exception:
        traceback.print_exc()

        return JSONResponse(
            {
                "error": (
                    "판정을 저장하지 못했어요. "
                    "분석 번호를 확인해 주세요."
                )
            },
            status_code=400,
        )

    CACHE["at"] = 0.0

    return {"ok": True}


@app.get("/api/stats")
def stats():
    if sb is None:
        return JSONResponse(
            {"error": "Supabase 미연결"},
            status_code=503,
        )

    rows = load_rows()

    report = D.accuracy_report(rows)
    weights = D.compute_weights(rows)

    report["min_n"] = D.MIN_N

    report["weights"] = sorted(
        [
            {
                "key": key,
                "n": value["n"],
                "precision": value["precision"],
                "weight": value["weight"],
            }
            for key, value in weights.items()
        ],
        key=lambda item: -item["n"],
    )[:12]

    return report


# ---------- 제출·보완 요청 형식 ----------

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


NO_DB = {
    "error": (
        "Supabase 미연결: "
        "SUPABASE_URL / SUPABASE_KEY를 확인해 주세요."
    )
}


def sub_call(fn, *args):
    if db is None:
        return JSONResponse(
            NO_DB,
            status_code=503,
        )

    try:
        return fn(db, *args)

    except SUB.SubmissionError as exc:
        return JSONResponse(
            {"error": exc.message},
            status_code=exc.status_code,
        )

    except Exception:
        print("SUBMISSION_FAIL")
        traceback.print_exc()

        return JSONResponse(
            {
                "error": (
                    "저장소 처리 중 문제가 생겼어요. "
                    "잠시 뒤 다시 시도해 주세요."
                )
            },
            status_code=500,
        )


# ---------- 기존 작성자 기능 ----------

@app.post("/api/submissions")
def submission_create(
    s: SubmissionIn,
    request: Request,
):
    if too_many(request):
        return limited()

    def go(st, data):
        sub, code = SUB.create(st, data)

        return {
            "submission": sub,
            "author_code": code,
            "criteria": C.evaluate(sub),
        }

    return sub_call(
        go,
        s.model_dump(),
    )


@app.get("/api/submissions/{sid}/mine")
def submission_mine(
    sid: int,
    request: Request,
    x_author_code: str | None = Header(default=None),
):
    if too_many(request):
        return limited()

    return sub_call(
        SUB.author_view,
        sid,
        x_author_code,
    )


@app.post(
    "/api/submissions/{sid}/revisions/{rid}/answer"
)
def submission_answer(
    sid: int,
    rid: int,
    a: AnswerIn,
    request: Request,
    x_author_code: str | None = Header(default=None),
):
    if too_many(request):
        return limited()

    return sub_call(
        SUB.answer_revision,
        sid,
        x_author_code,
        rid,
        a.value,
    )


# ---------- 기존 제출 심사 기능 ----------

@app.get("/api/submissions")
def submission_list(
    status: str | None = None,
    x_reviewer_token: str | None = Header(default=None),
):
    denied = reviewer_denied(x_reviewer_token)

    if denied is not None:
        return denied

    return sub_call(
        lambda st, value: {
            "items": SUB.reviewer_list(st, value)
        },
        status or None,
    )


@app.get("/api/submissions/{sid}")
def submission_detail(
    sid: int,
    x_reviewer_token: str | None = Header(default=None),
):
    denied = reviewer_denied(x_reviewer_token)

    if denied is not None:
        return denied

    return sub_call(
        SUB.reviewer_view,
        sid,
    )


@app.post("/api/submissions/{sid}/status")
def submission_status(
    sid: int,
    s: StatusIn,
    x_reviewer_token: str | None = Header(default=None),
):
    denied = reviewer_denied(x_reviewer_token)

    if denied is not None:
        return denied

    return sub_call(
        SUB.set_status,
        sid,
        s.status,
        s.note,
    )


@app.post("/api/submissions/{sid}/revisions")
def submission_revision(
    sid: int,
    r: RevisionIn,
    x_reviewer_token: str | None = Header(default=None),
):
    denied = reviewer_denied(x_reviewer_token)

    if denied is not None:
        return denied

    return sub_call(
        SUB.request_revision,
        sid,
        r.field,
        r.reason,
    )


@app.post("/api/revisions/{rid}/resolve")
def revision_resolve(
    rid: int,
    r: ResolveIn,
    x_reviewer_token: str | None = Header(default=None),
):
    denied = reviewer_denied(x_reviewer_token)

    if denied is not None:
        return denied

    return sub_call(
        SUB.resolve_revision,
        rid,
        r.outcome,
    )


@app.post("/api/submissions/{sid}/analyze")
def submission_analyze(
    sid: int,
    request: Request,
    x_reviewer_token: str | None = Header(default=None),
):
    denied = reviewer_denied(x_reviewer_token)

    if denied is not None:
        return denied

    if db is None:
        return JSONResponse(
            NO_DB,
            status_code=503,
        )

    if too_many(request):
        return limited()

    sub = sub_call(
        lambda st: st.get_submission(sid)
    )

    if isinstance(sub, JSONResponse):
        return sub

    if not sub:
        return JSONResponse(
            {"error": "제출 건을 찾을 수 없어요."},
            status_code=404,
        )

    args = SUB.analysis_args(sub)

    if not args.get("query"):
        return JSONResponse(
            {
                "error": (
                    "분석할 앱 이름이나 주소가 없어요."
                )
            },
            status_code=400,
        )

    result = analyze_one(Q(**args))

    logged = sub_call(
        SUB.record_analysis,
        sid,
        result,
    )

    result["linked"] = not isinstance(
        logged,
        JSONResponse,
    )

    result["submission_id"] = sid

    return result


# ---------- 기본 페이지·상태 ----------

@app.get("/robots.txt")
def robots():
    return PlainTextResponse(
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
    )


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "db": sb is not None,
    }


@app.get("/")
def index():
    path = BASE_DIR / "index.html"

    if path.exists():
        return FileResponse(path)

    return {
        "message": "APP TRUST AI API is running."
    }
