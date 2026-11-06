"""APP TRUST AI: 스토어 검색 및 Supabase 제출 저장소.

Google Play는 URL/패키지 형식만 확인하며 등록 여부를 검증하지 않는다.
"""

import difflib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import collect


SEARCH_LIMIT = 50
CACHE_TTL = 300
CACHE_MAX = 200
_CACHE = {}

PLAY_NOTICE = (
    "Google Play 자동 이름 검색은 현재 지원하지 않습니다. "
    "Play 스토어 주소나 패키지명을 입력하고 앱 정보를 직접 확인해 주세요."
)


def now_iso():
    """현재 시간을 UTC 기준 문자열로 반환한다."""
    return datetime.now(timezone.utc).isoformat()


class SupabaseStore:
    """main.py와 submissions.py가 사용하는 DB 접근 기능.

    생성 시 네트워크 요청을 하지 않는다.

    submission_events.detail의 기존 text 컬럼에는
    JSON 문자열로 저장하고, 읽을 때 복원한다.
    json/jsonb 컬럼에서 dict로 반환되는 경우도 허용한다.
    """

    def __init__(self, client):
        if client is None:
            raise ValueError("Supabase 연결이 필요합니다.")
        self.client = client

    @staticmethod
    def _rows(response):
        return response.data or []

    @classmethod
    def _one(cls, response):
        rows = cls._rows(response)
        return rows[0] if rows else None

    def create_submission(self, row):
        result = self._one(
            self.client.table("submissions")
            .insert(row)
            .execute()
        )
        if result is None:
            raise RuntimeError("제출 저장 결과가 비어 있습니다.")
        return result

    def get_submission(self, sid):
        return self._one(
            self.client.table("submissions")
            .select("*")
            .eq("id", sid)
            .limit(1)
            .execute()
        )

    def list_submissions(self, status=None):
        query = self.client.table("submissions").select("*")

        if status:
            query = query.eq("status", status)

        return self._rows(
            query.order("id", desc=True)
            .limit(200)
            .execute()
        )

    def update_submission(self, sid, values):
        return self._one(
            self.client.table("submissions")
            .update(values)
            .eq("id", sid)
            .execute()
        )

    def create_revision(self, row):
        result = self._one(
            self.client.table("revision_requests")
            .insert(row)
            .execute()
        )
        if result is None:
            raise RuntimeError("보완 요청 저장 결과가 비어 있습니다.")
        return result

    def get_revision(self, rid):
        return self._one(
            self.client.table("revision_requests")
            .select("*")
            .eq("id", rid)
            .limit(1)
            .execute()
        )

    def list_revisions(self, sid):
        return self._rows(
            self.client.table("revision_requests")
            .select("*")
            .eq("submission_id", sid)
            .order("id")
            .execute()
        )

    def update_revision(self, rid, values):
        return self._one(
            self.client.table("revision_requests")
            .update(values)
            .eq("id", rid)
            .execute()
        )

    def add_event(self, sid, kind, detail=None):
        row = {
            "submission_id": sid,
            "kind": kind,
            "detail": json.dumps(
                {} if detail is None else detail,
                ensure_ascii=False,
            ),
            "created_at": now_iso(),
        }

        return self._one(
            self.client.table("submission_events")
            .insert(row)
            .execute()
        )

    def list_events(self, sid):
        rows = self._rows(
            self.client.table("submission_events")
            .select("*")
            .eq("submission_id", sid)
            .order("id")
            .execute()
        )

        events = []

        for original in rows:
            event = dict(original)
            detail = event.get("detail")

            if isinstance(detail, str):
                try:
                    event["detail"] = json.loads(detail)
                except (ValueError, TypeError):
                    # 과거의 일반 텍스트도 삭제하지 않고 유지한다.
                    event["detail"] = detail

            events.append(event)

        return events


def _normalize(value):
    """후보 이름 비교용 문자열."""
    return re.sub(
        r"[\W_]+",
        "",
        str(value or "").casefold(),
    )


def _card(app, country):
    """기존 화면에서 사용하는 앱 카드 형식."""
    description = re.sub(
        r"\s+",
        " ",
        app.get("description") or "",
    ).strip()

    return {
        "store": "apple",
        "track_id": app.get("trackId"),
        "name": app.get("trackName") or "",
        "seller": app.get("sellerName") or "",
        "icon": (
            app.get("artworkUrl100")
            or app.get("artworkUrl60")
            or ""
        ),
        "description": description[:300],
        "url": app.get("trackViewUrl") or "",
        "seller_url": app.get("sellerUrl") or "",
        "rating_count": app.get("userRatingCount"),
        "genre": app.get("primaryGenreName") or "",
        "released": (app.get("releaseDate") or "")[:10],
        "country": country,
    }


def _query_variants(query):
    """원문으로 찾지 못하면 괄호 밖 문자열로 재검색한다."""
    variants = [query]

    short = re.sub(
        r"\([^)]*\)|（[^）]*）",
        " ",
        query,
    )
    short = re.sub(r"\s+", " ", short).strip()

    if short and short != query:
        variants.append(short)

    return variants


def _fetch_apple(query, country):
    params = urlencode({
        "term": query,
        "entity": "software",
        "country": country,
        "limit": SEARCH_LIMIT,
    })

    try:
        data = collect.http_json(
            "https://itunes.apple.com/search?" + params
        )
    except Exception as exc:
        print("STORE_SEARCH_FAIL", type(exc).__name__)
        return None

    if not isinstance(data, dict):
        return None

    if not isinstance(data.get("results"), list):
        return None

    return [
        app
        for app in data["results"]
        if isinstance(app, dict) and app.get("trackId")
    ]


def _rank_key(app, query):
    """정렬만 수행하며 분석 대상을 자동 확정하지 않는다."""
    target = _normalize(query)
    name = _normalize(app.get("trackName"))

    try:
        count = int(app.get("userRatingCount") or 0)
    except (TypeError, ValueError):
        count = 0

    return (
        bool(target and name == target),
        bool(target and target in name),
        difflib.SequenceMatcher(None, target, name).ratio(),
        count,
    )


def search_apple(query):
    """Apple 검색 후보를 최대 50개 반환한다.

    한국 → 미국 순서로 조회하고,
    모두 결과가 없으면 괄호를 제거한 검색어로 재시도한다.
    검색 API가 반환하지 않는 앱까지 발견한다고 보장하지 않는다.
    """
    query = str(query or "").strip()[:100]

    if not query:
        return []

    key = query.casefold()
    cached = _CACHE.get(key)

    if cached and time.monotonic() - cached[0] < CACHE_TTL:
        return [dict(card) for card in cached[1]]

    _CACHE.pop(key, None)

    results = []
    country = "kr"
    matched = query

    for variant in _query_variants(query):
        for cc in ("kr", "us"):
            found = _fetch_apple(variant, cc)

            if found:
                results = found
                country = cc
                matched = variant
                break

        if results:
            break

    # 검색 실패·빈 결과는 캐시에 보관하지 않는다.
    if not results:
        return []

    unique = {}

    for app in results:
        unique.setdefault(str(app["trackId"]), app)

    ordered = sorted(
        unique.values(),
        key=lambda app: _rank_key(app, matched),
        reverse=True,
    )

    cards = [
        _card(app, country)
        for app in ordered[:SEARCH_LIMIT]
    ]

    counts = {}

    for card in cards:
        name = _normalize(card["name"])
        counts[name] = counts.get(name, 0) + 1

    for card in cards:
        card["duplicate"] = counts[_normalize(card["name"])] > 1

    if len(_CACHE) >= CACHE_MAX:
        oldest = min(
            _CACHE,
            key=lambda cache_key: _CACHE[cache_key][0],
        )
        _CACHE.pop(oldest, None)

    _CACHE[key] = (
        time.monotonic(),
        [dict(card) for card in cards],
    )

    return cards


def parse_play(value):
    """Play URL 또는 패키지명 형식만 확인한다.

    Google Play에 접속하지 않으며 실제 등록 여부도 검증하지 않는다.
    """
    value = str(value or "").strip()
    package = None

    if re.match(r"https?://", value, re.I):
        try:
            parsed = urlparse(value)

            if (parsed.hostname or "").lower() in (
                "play.google.com",
                "www.play.google.com",
            ):
                package = (
                    parse_qs(parsed.query).get("id")
                    or [None]
                )[0]

        except ValueError:
            return None

    elif re.fullmatch(
        r"[A-Za-z][\w]*(\.[\w]+)+",
        value,
    ):
        package = value

    if not package or not re.fullmatch(
        r"[A-Za-z][\w]*(\.[\w]+)+",
        package,
    ):
        return None

    return {
        "store": "google_play",
        "package": package,
        "url": (
            "https://play.google.com/store/apps/details?"
            + urlencode({"id": package})
        ),
    }


def search(query):
    """既存の検索候補レスポンス形式を維持する。"""
    play = parse_play(query)

    return {
        "apple": [] if play else search_apple(query),
        "google_play": {
            "supported": False,
            "message": PLAY_NOTICE,
            "parsed": play,
        },
    }
