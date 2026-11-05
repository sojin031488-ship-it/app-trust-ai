"""앱 제출 기능 테스트. 실행: python -m pytest -q
- criteria / submissions / store(MemoryStore) 테스트는 외부 의존성 없이 동작한다.
- API 테스트는 fastapi + httpx 가 설치된 경우에만 실행된다 (없으면 건너뜀).
실제 Supabase 에는 접속하지 않는다."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import criteria as C
import store as ST
import submissions as SUB

GOOD = {
    "app_name": "마음일기", "developer": "소진랩", "store": "app_store", "store_track_id": "123456789",
    "store_url": "https://apps.apple.com/kr/app/id123456789", "website": "https://example.com",
    "description": "하루의 감정을 기록하고 주간 리포트로 돌아볼 수 있는 감정 일기 앱입니다. 오프라인에서도 동작합니다.",
    "ai_disclosure": "none", "ai_note": "", "privacy_url": "https://example.com/privacy",
    "operator_info": "소진랩 / 대표 임소진 / help@example.com", "repo": "https://github.com/sojin/maeum",
}


def new(st=None, **over):
    st = st or ST.MemoryStore()
    sub, code = SUB.create(st, {**GOOD, **over})
    return st, sub, code


# ---------- criteria ----------
def test_ai_use_is_not_penalized():
    """AI 활용 여부(none/assisted/generated)만 다르면 모든 영역 점수가 같아야 한다 (설명을 충분히 쓴 경우)."""
    note = "Cursor로 화면 코드 초안을 만들고 직접 검토했습니다."
    scores = []
    for v in C.AI_DISCLOSURE:
        r = C.evaluate({**GOOD, "ai_disclosure": v, "ai_note": note})
        assert r["ai_penalty"] is False
        scores.append({k: c["score"] for k, c in r["categories"].items()})
    assert scores[0] == scores[1] == scores[2]

def test_categories_are_separated():
    r = C.evaluate(GOOD)
    assert set(r["categories"]) == {"info", "tech", "transparency"}
    assert all(i["category"] in C.CATEGORIES for i in r["items"])
    assert r["categories"]["info"]["score"] == 100

def test_missing_info_creates_suggestions():
    r = C.evaluate({**GOOD, "privacy_url": "", "operator_info": "짧음"})
    fields = {s["field"] for s in r["suggestions"]}
    assert {"privacy_url", "operator_info"} <= fields
    assert r["categories"]["info"]["score"] < 100

def test_ai_note_only_required_when_ai_used():
    assert C.BY_KEY["ai_note"]["check"]({**GOOD, "ai_disclosure": "none"}, None) is None
    assert C.BY_KEY["ai_note"]["check"]({**GOOD, "ai_disclosure": "generated", "ai_note": ""}, None) is False

def test_analysis_inconsistency_is_flagged_but_disclosed_ai_is_fine():
    hidden = {"quality_score": 80, "declaration_flag": "AI 사용 미공개로 신고했으나 AI 흔적이 발견됨 → 소명 요청 권고"}
    assert C.evaluate(GOOD, hidden)["categories"]["transparency"]["score"] < 100
    # AI 사용을 공개했고 분석도 AI 흔적이 높게 나온 경우: 투명성 만점
    disclosed = {**GOOD, "ai_disclosure": "generated", "ai_note": "Lovable로 대부분의 화면을 만들었습니다."}
    r = C.evaluate(disclosed, {"quality_score": 80, "declaration_flag": None, "ai_score": 95})
    assert r["categories"]["transparency"]["score"] == 100

def test_tech_without_data_is_not_scored_as_zero():
    r = C.evaluate({"app_name": "x", "developer": "y", "ai_disclosure": "none"})
    assert r["categories"]["tech"]["score"] is None


# ---------- submissions: 생성 / 작성자 코드 ----------
def test_create_stores_only_hash_and_long_code():
    st, sub, code = new()
    row = st.t["submissions"][0]
    assert len(code) >= 32
    assert row["author_code_hash"] == SUB.hash_code(code) and len(row["author_code_hash"]) == 64
    assert code not in str(row)                       # 원문 코드는 어디에도 저장되지 않음
    assert "author_code_hash" not in sub              # 응답에서도 해시를 숨김
    assert row["status"] == "submitted" and row["store_track_id"] == 123456789
    assert row["created_at"] and row["updated_at"]
    assert [e["kind"] for e in st.t["submission_events"]] == ["created"]

def test_codes_are_unique():
    assert len({SUB.new_author_code() for _ in range(200)}) == 200

def test_only_known_columns_are_written():
    st, _, _ = new(evil="x", id=999, status="approved")
    row = st.t["submissions"][0]
    assert "evil" not in row and row["id"] == 1 and row["status"] == "submitted"
    assert set(row) == set(ST.SUBMISSION_COLS)

@pytest.mark.parametrize("over, msg", [
    ({"app_name": ""}, "앱 이름"),
    ({"store": "steam"}, "배포처"),
    ({"ai_disclosure": "maybe"}, "AI 활용 여부"),
    ({"privacy_url": "javascript:alert(1)"}, "개인정보처리방침"),
    ({"repo": "https://gitlab.com/a/b"}, "GitHub"),
    ({"store_track_id": "12a"}, "숫자"),
    ({"description": "가" * 4001}, "4000자"),
])
def test_validation(over, msg):
    with pytest.raises(SUB.SubmissionError) as e:
        new(**over)
    assert msg in e.value.message

def test_author_view_requires_correct_code():
    st, sub, code = new()
    assert SUB.author_view(st, sub["id"], code)["submission"]["app_name"] == "마음일기"
    for bad_id, bad_code in [(sub["id"], "wrong"), (sub["id"], ""), (999, code)]:
        with pytest.raises(SUB.SubmissionError) as e:
            SUB.author_view(st, bad_id, bad_code)
        assert e.value.status_code == 403            # 없는 번호와 틀린 코드를 구분하지 않음


# ---------- submissions: 보완 요청 흐름 ----------
def test_revision_flow():
    st, sub, code = new(privacy_url="")
    sid = sub["id"]
    v = SUB.request_revision(st, sid, "privacy_url", "개인정보처리방침 주소를 등록해 주세요.")
    assert v["submission"]["status"] == "revision_requested"
    rev = v["revisions"][0]
    assert rev["status"] == "open" and rev["field"] == "privacy_url" and rev["old_value"] is None

    out = SUB.answer_revision(st, sid, code, rev["id"], "https://example.com/privacy")
    assert out["submission"]["privacy_url"] == "https://example.com/privacy"
    assert out["submission"]["status"] == "resubmitted"      # 남은 요청이 없으면 보완 제출로
    assert out["revisions"][0]["status"] == "answered"
    assert out["revisions"][0]["new_value"] == "https://example.com/privacy"

    with pytest.raises(SUB.SubmissionError):                 # 같은 요청에 두 번 답변 불가
        SUB.answer_revision(st, sid, code, rev["id"], "https://x.com")

    r = SUB.resolve_revision(st, rev["id"], "resolved")
    assert r["revisions"][0]["status"] == "resolved" and r["revisions"][0]["resolved_at"]
    kinds = [e["kind"] for e in st.list_events(sid)]
    assert kinds == ["created", "revision_requested", "status_changed", "revision_answered",
                     "status_changed", "revision_resolved"]

def test_answer_is_validated_and_needs_code():
    st, sub, code = new()
    rev = SUB.request_revision(st, sub["id"], "website", "주소 확인")["revisions"][0]
    with pytest.raises(SUB.SubmissionError):
        SUB.answer_revision(st, sub["id"], code, rev["id"], "not a url")
    with pytest.raises(SUB.SubmissionError) as e:
        SUB.answer_revision(st, sub["id"], "wrong-code", rev["id"], "https://ok.com")
    assert e.value.status_code == 403

def test_answer_cannot_touch_other_submission():
    st, a, code_a = new()
    _, b, _ = new(st)
    rev_b = SUB.request_revision(st, b["id"], "website", "확인")["revisions"][0]
    with pytest.raises(SUB.SubmissionError) as e:
        SUB.answer_revision(st, a["id"], code_a, rev_b["id"], "https://hack.com")
    assert e.value.status_code == 404

def test_status_rules():
    st, sub, _ = new()
    with pytest.raises(SUB.SubmissionError):
        SUB.set_status(st, sub["id"], "whatever")
    SUB.set_status(st, sub["id"], "approved", "문제 없음")
    with pytest.raises(SUB.SubmissionError) as e:
        SUB.request_revision(st, sub["id"], "website", "확인")
    assert e.value.status_code == 409
    ev = st.list_events(sub["id"])[-1]
    assert ev["kind"] == "status_changed" and ev["detail"] == {"from": "submitted", "to": "approved",
                                                               "note": "문제 없음", "by": "reviewer"}

def test_reviewer_list_filter():
    st, a, _ = new()
    new(st, app_name="둘째 앱")
    SUB.set_status(st, a["id"], "in_review")
    assert [s["id"] for s in SUB.reviewer_list(st)] == [2, 1]
    assert [s["id"] for s in SUB.reviewer_list(st, "in_review")] == [1]
    assert all("author_code_hash" not in s for s in SUB.reviewer_list(st))


# ---------- 기존 분석 기능과의 연결 ----------
def test_analysis_args_mapping():
    assert SUB.analysis_args(GOOD) == {"query": "마음일기", "track_id": 123456789, "repo": "https://github.com/sojin/maeum",
                                       "declared_ai": False}
    a = SUB.analysis_args({**GOOD, "store_track_id": None, "ai_disclosure": "assisted"})
    assert a["track_id"] is None and a["declared_ai"] is True
    w = SUB.analysis_args({**GOOD, "store": "web", "store_track_id": 5, "repo": None})
    assert w["query"] == "https://example.com" and w["track_id"] is None and w["repo"] is None

def test_record_analysis_feeds_criteria():
    st, sub, _ = new()
    SUB.record_analysis(st, sub["id"], {"analysis_id": 7, "name": "마음일기", "ai_score": 20, "quality_score": 40,
                                        "verdict": "개발자 직접 제작 가능성 높음", "declaration_flag": None,
                                        "signals": [{"big": "x"}]})
    v = SUB.reviewer_view(st, sub["id"])
    assert v["analysis"]["analysis_id"] == 7 and "signals" not in v["analysis"]
    assert next(i for i in v["criteria"]["items"] if i["key"] == "quality")["ok"] is False
    # 개발자 화면에는 분석 점수가 드러나지 않는다
    assert all("detail" not in e for e in SUB.author_view(st, sub["id"], _code(st, sub))["events"])

def _code(st, sub):
    # 테스트용: 해시만 저장되므로 새 코드를 발급해 바꿔 끼운다
    code = SUB.new_author_code()
    st.update_submission(sub["id"], {"author_code_hash": SUB.hash_code(code)})
    return code

def test_failed_analysis_is_logged_but_not_used():
    st, sub, _ = new()
    SUB.record_analysis(st, sub["id"], {"error": "분석 중 문제가 생겼어요."})
    assert SUB.reviewer_view(st, sub["id"])["analysis"] is None


# ---------- store ----------
def test_detail_parsing_handles_text_and_json():
    assert ST.parse_detail('{"a": 1}') == {"a": 1}
    assert ST.parse_detail({"a": 1}) == {"a": 1}
    assert ST.parse_detail("plain") == {"text": "plain"}
    assert ST.parse_detail(None) == {}

def test_memory_store_foreign_key():
    st = ST.MemoryStore()
    with pytest.raises(ValueError):
        st.add_event(1, "created")
    with pytest.raises(ValueError):
        st.create_revision({"submission_id": 1, "field": "website", "reason": "x"})

class FakeQuery:
    """supabase-py 체이닝 흉내: 호출 기록만 남긴다."""
    def __init__(self, log, table): self.log, self.table = log, table
    def __getattr__(self, name):
        def f(*a, **k):
            self.log.append((self.table, name, a, k)); return self
        return f
    def execute(self):
        class R: data = [{"id": 1, "submission_id": 1, "kind": "created", "detail": "{}"}]
        return R()

class FakeClient:
    def __init__(self): self.log = []
    def table(self, name): return FakeQuery(self.log, name)

def test_supabase_store_uses_real_table_and_column_names():
    fc = FakeClient()
    s = ST.SupabaseStore(fc)
    s.create_submission({"app_name": "a", "bogus": 1, "id": 5})
    s.add_event(1, "created", {"by": "author"})
    s.create_revision({"submission_id": 1, "field": "website", "reason": "r", "old_value": None})
    tables = {t for t, *_ in fc.log}
    assert tables == {"submissions", "submission_events", "revision_requests"}
    inserts = [a[0] for t, name, a, k in fc.log if name == "insert"]
    assert inserts[0] == {"app_name": "a"}                         # 모르는 컬럼과 id 는 보내지 않음
    assert inserts[1]["detail"] == '{"by": "author"}'               # text/jsonb 어느 쪽에도 저장되는 문자열
    assert set(inserts[2]) <= set(ST.REVISION_COLS)


# ---------- API (fastapi 가 설치된 경우에만) ----------
@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setenv("REVIEW_TOKEN", "rv-secret")
    monkeypatch.setattr(main, "db", ST.MemoryStore())
    main.HITS.clear()
    calls = []
    def fake_analyze(q, research=True):
        calls.append(q)
        return {"name": q.query, "ai_score": 30, "quality_score": 70, "verdict": "개발자 직접 제작 가능성 높음",
                "declaration_flag": None, "analysis_id": 42, "signals": []}
    monkeypatch.setattr(main, "analyze_one", fake_analyze)
    c = TestClient(main.app)
    c.calls = calls
    return c

RV = {"x-reviewer-token": "rv-secret"}

def test_api_full_flow(client):
    r = client.post("/api/submissions", json={**GOOD, "privacy_url": ""})
    assert r.status_code == 200, r.text
    j = r.json(); sid, code = j["submission"]["id"], j["author_code"]
    assert "author_code_hash" not in j["submission"] and j["criteria"]["suggestions"]
    assert r.headers["x-frame-options"] == "DENY"                  # 기존 보안 헤더 유지

    assert client.get(f"/api/submissions/{sid}/mine", headers={"x-author-code": "nope"}).status_code == 403
    assert client.get(f"/api/submissions/{sid}/mine", headers={"x-author-code": code}).status_code == 200

    assert client.get("/api/submissions").status_code == 401        # 심사자 코드 없음
    assert client.get("/api/submissions", headers={"x-reviewer-token": "bad"}).status_code == 401
    assert client.get("/api/submissions", headers=RV).json()["items"][0]["id"] == sid

    v = client.post(f"/api/submissions/{sid}/revisions", json={"field": "privacy_url", "reason": "등록해 주세요"}, headers=RV).json()
    rid = v["revisions"][0]["id"]
    a = client.post(f"/api/submissions/{sid}/revisions/{rid}/answer", json={"value": "https://e.com/p"},
                    headers={"x-author-code": code}).json()
    assert a["submission"]["status"] == "resubmitted"
    assert client.post(f"/api/revisions/{rid}/resolve", json={"outcome": "resolved"}, headers=RV).status_code == 200
    assert client.post(f"/api/submissions/{sid}/status", json={"status": "bogus"}, headers=RV).status_code == 400
    assert client.post(f"/api/submissions/{sid}/status", json={"status": "approved"}, headers=RV).json()["submission"]["status"] == "approved"

def test_api_analyze_uses_existing_pipeline(client):
    j = client.post("/api/submissions", json={**GOOD, "ai_disclosure": "assisted", "ai_note": "코드 리뷰에 AI를 썼어요"}).json()
    sid = j["submission"]["id"]
    assert client.post(f"/api/submissions/{sid}/analyze").status_code == 401
    r = client.post(f"/api/submissions/{sid}/analyze", headers=RV).json()
    assert r["analysis_id"] == 42 and r["submission_id"] == sid
    q = client.calls[0]
    assert q.query == "마음일기" and q.track_id == 123456789 and q.declared_ai is True
    d = client.get(f"/api/submissions/{sid}", headers=RV).json()
    assert d["analysis"]["analysis_id"] == 42

def test_api_review_token_missing(client, monkeypatch):
    monkeypatch.delenv("REVIEW_TOKEN")
    r = client.get("/api/submissions", headers=RV)
    assert r.status_code == 503 and "REVIEW_TOKEN" in r.json()["error"]

def test_api_without_db(client, monkeypatch):
    import main
    monkeypatch.setattr(main, "db", None)
    assert client.post("/api/submissions", json=GOOD).status_code == 503
    assert client.get("/api/health").json() == {"ok": True, "db": main.sb is not None}   # 기존 응답 그대로

def test_existing_routes_still_registered(client):
    import main
    paths = {r.path for r in main.app.routes}
    assert {"/api/search", "/api/batch", "/api/review", "/api/stats", "/api/health", "/", "/robots.txt"} <= paths
