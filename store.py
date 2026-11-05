"""APP TRUST AI - 제출 기능 저장소 계층 (submissions / revision_requests / submission_events)

Supabase 테이블 구조 (사용자 확인 2026-11-05)
  submissions        : id, app_name, developer, store, store_track_id, store_url, website, description,
                       ai_disclosure, ai_note, privacy_url, operator_info, repo, status,
                       author_code_hash, created_at, updated_at
  revision_requests  : id, submission_id(FK, ON DELETE CASCADE), field, reason, old_value, new_value,
                       status, created_at, resolved_at
  submission_events  : id, submission_id(FK, ON DELETE CASCADE), kind, detail, created_at

NULL 허용 여부/기본값은 아직 확인 전이므로, id 를 제외한 시각 컬럼(created_at, updated_at, resolved_at)도
코드에서 직접 채워 넣는다. (기본값이 있어도 명시한 값이 우선하므로 문제없음)
detail / old_value / new_value 는 text 와 jsonb 어느 쪽이어도 저장되도록 문자열로 보낸다.

기존 analyses / reviews 테이블은 여기서 다루지 않는다 (main.py 의 기존 코드 그대로).
"""
import copy
import json
from datetime import datetime, timezone

SUBMISSION_COLS = ("id", "app_name", "developer", "store", "store_track_id", "store_url", "website",
                   "description", "ai_disclosure", "ai_note", "privacy_url", "operator_info", "repo",
                   "status", "author_code_hash", "created_at", "updated_at")
REVISION_COLS = ("id", "submission_id", "field", "reason", "old_value", "new_value", "status",
                 "created_at", "resolved_at")
EVENT_COLS = ("id", "submission_id", "kind", "detail", "created_at")


def now_iso():
    return datetime.now(timezone.utc).isoformat()

def _only(row, cols):
    """정의된 컬럼만 남긴다 (없는 컬럼을 보내 DB 오류가 나지 않게)."""
    return {k: v for k, v in row.items() if k in cols and k != "id"}

def to_text(v):
    if v is None or isinstance(v, str): return v
    return json.dumps(v, ensure_ascii=False)

def parse_detail(v):
    """detail 이 jsonb(dict) 로 오든 text(JSON 문자열) 로 오든 dict 로 맞춘다."""
    if isinstance(v, dict): return v
    if isinstance(v, str):
        try:
            d = json.loads(v)
            if isinstance(d, dict): return d
            if isinstance(d, str): return parse_detail(d) if d.strip().startswith("{") else {"text": d}
        except ValueError:
            return {"text": v}
    return {} if v is None else {"value": v}


class SupabaseStore:
    """supabase-py 클라이언트(main.py 의 sb)를 감싼다."""

    def __init__(self, client):
        self.sb = client

    # ---- submissions ----
    def create_submission(self, row):
        r = self.sb.table("submissions").insert(_only(row, SUBMISSION_COLS)).execute()
        return r.data[0]

    def get_submission(self, sid):
        r = self.sb.table("submissions").select("*").eq("id", sid).limit(1).execute()
        return (r.data or [None])[0]

    def list_submissions(self, status=None, limit=100):
        q = self.sb.table("submissions").select(
            "id, app_name, developer, store, status, ai_disclosure, created_at, updated_at")
        if status: q = q.eq("status", status)
        return q.order("id", desc=True).limit(limit).execute().data or []

    def update_submission(self, sid, fields):
        r = self.sb.table("submissions").update(_only(fields, SUBMISSION_COLS)).eq("id", sid).execute()
        return (r.data or [None])[0]

    # ---- revision_requests ----
    def create_revision(self, row):
        row = dict(row, old_value=to_text(row.get("old_value")), new_value=to_text(row.get("new_value")))
        r = self.sb.table("revision_requests").insert(_only(row, REVISION_COLS)).execute()
        return r.data[0]

    def get_revision(self, rid):
        r = self.sb.table("revision_requests").select("*").eq("id", rid).limit(1).execute()
        return (r.data or [None])[0]

    def list_revisions(self, sid):
        return self.sb.table("revision_requests").select("*").eq("submission_id", sid).order("id").execute().data or []

    def update_revision(self, rid, fields):
        if "new_value" in fields: fields = dict(fields, new_value=to_text(fields["new_value"]))
        r = self.sb.table("revision_requests").update(_only(fields, REVISION_COLS)).eq("id", rid).execute()
        return (r.data or [None])[0]

    # ---- submission_events ----
    def add_event(self, sid, kind, detail=None):
        row = {"submission_id": sid, "kind": kind, "detail": to_text(detail or {}), "created_at": now_iso()}
        r = self.sb.table("submission_events").insert(row).execute()
        return r.data[0]

    def list_events(self, sid):
        rows = self.sb.table("submission_events").select("*").eq("submission_id", sid).order("id").execute().data or []
        return [dict(e, detail=parse_detail(e.get("detail"))) for e in rows]


class MemoryStore:
    """테스트·로컬 개발용. SupabaseStore 와 같은 메서드를 메모리에서 흉내 낸다."""

    def __init__(self):
        self.t = {"submissions": [], "revision_requests": [], "submission_events": []}
        self.seq = {k: 0 for k in self.t}

    def _ins(self, table, row, cols):
        self.seq[table] += 1
        row = {**{c: None for c in cols}, **_only(row, cols), "id": self.seq[table]}
        self.t[table].append(row)
        return copy.deepcopy(row)

    def _get(self, table, rid):
        return next((r for r in self.t[table] if r["id"] == rid), None)

    def _upd(self, table, rid, fields, cols):
        r = self._get(table, rid)
        if r is None: return None
        r.update(_only(fields, cols))
        return copy.deepcopy(r)

    def create_submission(self, row): return self._ins("submissions", row, SUBMISSION_COLS)
    def get_submission(self, sid): return copy.deepcopy(self._get("submissions", sid))

    def list_submissions(self, status=None, limit=100):
        keep = ("id", "app_name", "developer", "store", "status", "ai_disclosure", "created_at", "updated_at")
        rows = [r for r in self.t["submissions"] if not status or r["status"] == status]
        return [{k: r[k] for k in keep} for r in sorted(rows, key=lambda r: -r["id"])[:limit]]

    def update_submission(self, sid, fields): return self._upd("submissions", sid, fields, SUBMISSION_COLS)

    def create_revision(self, row):
        if not self._get("submissions", row.get("submission_id")):
            raise ValueError("foreign key: submission not found")
        row = dict(row, old_value=to_text(row.get("old_value")), new_value=to_text(row.get("new_value")))
        return self._ins("revision_requests", row, REVISION_COLS)

    def get_revision(self, rid): return copy.deepcopy(self._get("revision_requests", rid))

    def list_revisions(self, sid):
        return [copy.deepcopy(r) for r in self.t["revision_requests"] if r["submission_id"] == sid]

    def update_revision(self, rid, fields):
        if "new_value" in fields: fields = dict(fields, new_value=to_text(fields["new_value"]))
        return self._upd("revision_requests", rid, fields, REVISION_COLS)

    def add_event(self, sid, kind, detail=None):
        if not self._get("submissions", sid):
            raise ValueError("foreign key: submission not found")
        return self._ins("submission_events", {"submission_id": sid, "kind": kind,
                                               "detail": to_text(detail or {}), "created_at": now_iso()}, EVENT_COLS)

    def list_events(self, sid):
        return [dict(copy.deepcopy(e), detail=parse_detail(e["detail"]))
                for e in self.t["submission_events"] if e["submission_id"] == sid]
