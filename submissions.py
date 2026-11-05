"""APP TRUST AI - 앱 제출 / 보완 요청 흐름 (외부 의존성 없음, 단독 테스트 가능)

흐름
  개발자  : 앱 정보 제출 -> 작성자 코드 발급(화면에 한 번만 표시, DB에는 SHA-256 해시만 저장)
           -> 제출 번호 + 작성자 코드로 내 제출 조회 -> 보완 요청에 답변
  심사자  : REVIEW_TOKEN 으로 인증(main.py) -> 목록/상세 -> 보완 요청 / 상태 변경 / 기존 앱 분석 실행
  모든 변경은 submission_events 에 기록한다.

DB의 상태·구분 컬럼은 text 이고 CHECK 제약이 없으므로, 허용값은 이 파일에서 관리한다.
"""
import hashlib
import hmac
import re
import secrets

import criteria as C
import store as ST

# ---- 허용값 (DB에는 영문 코드로 저장, 화면에는 한글 라벨) ----
STATUSES = {
    "submitted": "제출됨",
    "in_review": "심사 중",
    "revision_requested": "보완 요청",
    "resubmitted": "보완 제출",
    "approved": "승인",
    "rejected": "반려",
}
FINAL_STATUSES = ("approved", "rejected")
REVISION_STATUSES = {"open": "답변 대기", "answered": "답변 완료", "resolved": "확인 완료", "dismissed": "철회"}
EVENT_KINDS = {
    "created": "제출",
    "status_changed": "상태 변경",
    "revision_requested": "보완 요청",
    "revision_answered": "보완 답변",
    "revision_resolved": "보완 확인",
    "analyzed": "자동 분석",
}
STORES = {"app_store": "App Store", "google_play": "Google Play", "web": "웹 서비스", "other": "기타"}

# 개발자가 입력하고, 심사자가 보완을 요청할 수 있는 컬럼과 최대 길이
FIELDS = {
    "app_name": ("앱 이름", 100),
    "developer": ("개발자(개발사)", 100),
    "store": ("배포처", 20),
    "store_track_id": ("App Store 앱 번호", 20),
    "store_url": ("스토어 주소", 500),
    "website": ("웹사이트", 500),
    "description": ("앱 설명", 4000),
    "ai_disclosure": ("AI 활용 여부", 20),
    "ai_note": ("AI 활용 설명", 2000),
    "privacy_url": ("개인정보처리방침 주소", 500),
    "operator_info": ("운영자 정보", 1000),
    "repo": ("GitHub 저장소", 500),
}
REQUIRED = ("app_name", "developer", "store", "ai_disclosure")
URL_FIELDS = ("store_url", "website", "privacy_url", "repo")
CODE_BYTES = 24    # secrets.token_urlsafe(24) -> 32자, 192비트


class SubmissionError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message, self.status_code = message, status_code


# ---------- 작성자 코드 ----------
def new_author_code():
    return secrets.token_urlsafe(CODE_BYTES)

def hash_code(code):
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()

def code_matches(sub, code):
    stored = (sub or {}).get("author_code_hash") or ""
    if not stored or not code: return False
    return hmac.compare_digest(stored.encode(), hash_code(code).encode())


# ---------- 입력 검증 ----------
def clean_fields(data, partial=False):
    """허용된 컬럼만 골라 공백 정리·길이·형식을 검사한다. partial=True 면 필수값 검사를 하지 않는다."""
    out = {}
    for k, v in (data or {}).items():
        if k not in FIELDS: continue
        label, mx = FIELDS[k]
        v = "" if v is None else str(v).strip()
        if len(v) > mx:
            raise SubmissionError(f"{label}은(는) {mx}자까지 입력할 수 있어요.")
        out[k] = v or None
    if not partial:
        for k in REQUIRED:
            if not out.get(k):
                raise SubmissionError(f"{FIELDS[k][0]}을(를) 입력해 주세요.")
    if out.get("store") and out["store"] not in STORES:
        raise SubmissionError("배포처 값이 올바르지 않아요.")
    if out.get("ai_disclosure") and out["ai_disclosure"] not in C.AI_DISCLOSURE:
        raise SubmissionError("AI 활용 여부 값이 올바르지 않아요.")
    for k in URL_FIELDS:
        if out.get(k) and not C.is_http_url(out[k]):
            raise SubmissionError(f"{FIELDS[k][0]}은(는) http:// 또는 https:// 로 시작하는 주소여야 해요.")
    if out.get("repo") and not C.is_github_repo(out["repo"]):
        raise SubmissionError("GitHub 저장소는 https://github.com/아이디/저장소 형식이어야 해요.")
    if "store_track_id" in out and out["store_track_id"] is not None:
        if not re.fullmatch(r"\d{1,15}", out["store_track_id"]):
            raise SubmissionError("App Store 앱 번호는 숫자만 입력해 주세요.")
        out["store_track_id"] = int(out["store_track_id"])
    return out

def public(sub):
    """응답에 내보낼 때 작성자 코드 해시는 빼고, 한글 라벨을 붙인다."""
    if not sub: return sub
    d = {k: v for k, v in sub.items() if k != "author_code_hash"}
    d["status_label"] = STATUSES.get(d.get("status"), d.get("status"))
    return d


# ---------- 기존 앱 분석과의 연결 ----------
def analysis_args(sub):
    """제출 정보를 collect.run() 인자(query, track_id, repo, declared_ai)로 바꾼다."""
    store, web = sub.get("store"), sub.get("website") or ""
    query = web if (store == "web" and web) else (sub.get("app_name") or web or sub.get("store_url") or "")
    tid = sub.get("store_track_id")
    try:
        tid = int(tid) if (store == "app_store" and tid not in (None, "")) else None
    except (TypeError, ValueError):
        tid = None
    repo = sub.get("repo") if C.is_github_repo(sub.get("repo")) else None
    return {"query": query, "track_id": tid, "repo": repo, "declared_ai": C.declared_ai_of(sub)}

SUMMARY_KEYS = ("analysis_id", "name", "ai_score", "quality_score", "confidence", "verdict", "triage", "declaration_flag")

def analysis_summary(result):
    return {k: result.get(k) for k in SUMMARY_KEYS if k in result}

def latest_analysis(events):
    for e in reversed(events or []):
        if e.get("kind") == "analyzed" and isinstance(e.get("detail"), dict) and "error" not in e["detail"]:
            return e["detail"]
    return None


# ---------- 개발자 ----------
def create(st, data):
    """반환: (공개용 제출 정보, 작성자 코드 원문). 원문은 다시 알 수 없으니 화면에 한 번만 보여 준다."""
    fields = clean_fields(data)
    code, now = new_author_code(), ST.now_iso()
    row = {**{k: None for k in FIELDS}, **fields, "status": "submitted",
           "author_code_hash": hash_code(code), "created_at": now, "updated_at": now}
    sub = st.create_submission(row)
    st.add_event(sub["id"], "created", {"by": "author"})
    return public(sub), code

def _load(st, sid):
    sub = st.get_submission(sid)
    if not sub: raise SubmissionError("제출 건을 찾을 수 없어요.", 404)
    return sub

def _load_for_author(st, sid, code):
    sub = st.get_submission(sid)
    if not sub or not code_matches(sub, code):            # 없는 번호와 틀린 코드를 구분하지 않는다
        raise SubmissionError("제출 번호 또는 작성자 코드가 맞지 않아요.", 403)
    return sub

def author_view(st, sid, code):
    """개발자용 화면. 자동 분석 점수는 심사 내부 자료라서 보여 주지 않는다."""
    sub = _load_for_author(st, sid, code)
    events = [{"kind": e["kind"], "label": EVENT_KINDS.get(e["kind"], e["kind"]), "created_at": e.get("created_at")}
              for e in st.list_events(sid)]
    return {"submission": public(sub), "revisions": _revisions(st, sid), "events": events,
            "criteria": C.evaluate(sub)}

def answer_revision(st, sid, code, rid, value):
    sub = _load_for_author(st, sid, code)
    rev = st.get_revision(rid)
    if not rev or rev.get("submission_id") != sub["id"]:
        raise SubmissionError("보완 요청을 찾을 수 없어요.", 404)
    if rev.get("status") != "open":
        raise SubmissionError("이미 답변했거나 닫힌 보완 요청이에요.", 409)
    if sub.get("status") in FINAL_STATUSES:
        raise SubmissionError("심사가 끝난 제출 건은 수정할 수 없어요.", 409)
    field = rev.get("field")
    if field not in FIELDS: raise SubmissionError("수정할 수 없는 항목이에요.", 400)
    cleaned = clean_fields({field: value}, partial=True)
    if field in REQUIRED and not cleaned.get(field):
        raise SubmissionError(f"{FIELDS[field][0]}을(를) 입력해 주세요.")
    now = ST.now_iso()
    st.update_submission(sid, {field: cleaned.get(field), "updated_at": now})
    st.update_revision(rid, {"new_value": "" if cleaned.get(field) is None else str(cleaned.get(field)),
                             "status": "answered"})
    st.add_event(sid, "revision_answered", {"revision_id": rid, "field": field, "by": "author"})
    still_open = [r for r in st.list_revisions(sid) if r.get("status") == "open"]
    if not still_open and sub.get("status") == "revision_requested":
        _change_status(st, sub, "resubmitted", "모든 보완 요청에 답변함", by="author")
    return author_view(st, sid, code)


# ---------- 심사자 (인증은 main.py 에서 REVIEW_TOKEN 으로 처리) ----------
def _revisions(st, sid):
    return [dict(r, status_label=REVISION_STATUSES.get(r.get("status"), r.get("status")),
                 field_label=FIELDS.get(r.get("field"), (r.get("field"),))[0]) for r in st.list_revisions(sid)]

def reviewer_list(st, status=None):
    if status and status not in STATUSES: raise SubmissionError("상태 값이 올바르지 않아요.")
    return [public(s) for s in st.list_submissions(status)]

def reviewer_view(st, sid):
    sub = _load(st, sid)
    events = st.list_events(sid)
    for e in events: e["label"] = EVENT_KINDS.get(e["kind"], e["kind"])
    analysis = latest_analysis(events)
    return {"submission": public(sub), "revisions": _revisions(st, sid), "events": events,
            "analysis": analysis, "criteria": C.evaluate(sub, analysis)}

def _change_status(st, sub, new, note="", by="reviewer"):
    old = sub.get("status")
    st.update_submission(sub["id"], {"status": new, "updated_at": ST.now_iso()})
    st.add_event(sub["id"], "status_changed", {"from": old, "to": new, "note": note, "by": by})
    sub["status"] = new

def set_status(st, sid, status, note=""):
    if status not in STATUSES: raise SubmissionError("상태 값이 올바르지 않아요.")
    sub = _load(st, sid)
    if sub.get("status") != status:
        _change_status(st, sub, status, (note or "").strip()[:500])
    return reviewer_view(st, sid)

def request_revision(st, sid, field, reason):
    if field not in FIELDS: raise SubmissionError("보완을 요청할 수 없는 항목이에요.")
    reason = (reason or "").strip()
    if not reason: raise SubmissionError("보완 요청 사유를 적어 주세요.")
    if len(reason) > 1000: raise SubmissionError("사유는 1000자까지 적을 수 있어요.")
    sub = _load(st, sid)
    if sub.get("status") in FINAL_STATUSES:
        raise SubmissionError("승인/반려된 제출 건에는 보완을 요청할 수 없어요. 먼저 상태를 바꿔 주세요.", 409)
    old = sub.get(field)
    rev = st.create_revision({"submission_id": sid, "field": field, "reason": reason,
                              "old_value": None if old is None else str(old), "new_value": None,
                              "status": "open", "created_at": ST.now_iso(), "resolved_at": None})
    st.add_event(sid, "revision_requested", {"revision_id": rev["id"], "field": field, "by": "reviewer"})
    if sub.get("status") != "revision_requested":
        _change_status(st, sub, "revision_requested", f"{FIELDS[field][0]} 보완 요청")
    return reviewer_view(st, sid)

def resolve_revision(st, rid, outcome="resolved"):
    if outcome not in ("resolved", "dismissed"): raise SubmissionError("처리 결과 값이 올바르지 않아요.")
    rev = st.get_revision(rid)
    if not rev: raise SubmissionError("보완 요청을 찾을 수 없어요.", 404)
    if rev.get("status") in ("resolved", "dismissed"):
        raise SubmissionError("이미 처리된 보완 요청이에요.", 409)
    st.update_revision(rid, {"status": outcome, "resolved_at": ST.now_iso()})
    st.add_event(rev["submission_id"], "revision_resolved", {"revision_id": rid, "outcome": outcome, "by": "reviewer"})
    return reviewer_view(st, rev["submission_id"])

def record_analysis(st, sid, result):
    """기존 분석 결과(analyses 테이블 저장은 main.analyze_one 이 그대로 처리)를 제출 이력에 연결한다."""
    detail = {"error": str(result.get("error"))[:200]} if result.get("error") else analysis_summary(result)
    st.add_event(sid, "analyzed", detail)
