"""APP TRUST AI 스토어 검색.

Apple:
- 기존 collect.py의 HTTP 요청 함수를 재사용한다.
- iTunes Search API에서 앱 후보를 가져온다.
- 검색만으로 앱을 확정하거나 분석하지 않는다.
- 사용자가 선택할 수 있도록 후보 목록을 반환한다.

Google Play:
- 현재 자동 이름 검색과 앱 설명 수집은 제공하지 않는다.
- 사용자가 입력한 Play URL 또는 패키지명만 확인한다.
"""

import difflib
import re
import time
from urllib.parse import parse_qs, urlencode, urlparse

import collect


# 검색 API에서 가져올 최대 후보 수
SEARCH_LIMIT = 50

# 정상 검색 결과를 저장하는 시간
CACHE_TTL = 300

# 메모리 캐시 최대 검색어 수
CACHE_MAX = 200

_CACHE = {}

PLAY_NOTICE = (
    "Google Play 자동 이름 검색은 현재 지원하지 않습니다. "
    "Play 스토어 주소나 패키지명을 입력하면 "
    "제출 양식에 주소를 채울 수 있습니다. "
    "앱 이름·개발자·설명은 직접 확인해 입력해 주세요."
)


def _normalize(value):
    """비교용 문자열. 화면에 표시할 원래 이름은 바꾸지 않는다."""
    return re.sub(
        r"[\W_]+",
        "",
        str(value or "").casefold()
    )


def _card(app, country):
    """기존 index.html이 사용하는 응답 형식을 유지한다."""
    description = re.sub(
        r"\s+",
        " ",
        app.get("description") or ""
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
    """별칭을 추측하지 않고 입력에 포함된 이름만 재사용한다.

    예:
        Pieceful(피스풀)
        -> ["Pieceful(피스풀)", "Pieceful"]

    첫 검색에 결과가 있으면 추가 검색은 하지 않는다.
    """
    variants = [query]

    without_parentheses = re.sub(
        r"\([^)]*\)|（[^）]*）",
        " ",
        query
    )

    without_parentheses = re.sub(
        r"\s+",
        " ",
        without_parentheses
    ).strip()

    if (
        without_parentheses
        and without_parentheses != query
    ):
        variants.append(without_parentheses)

    return variants


def _fetch_apple(query, country):
    """기존 collect.http_json으로 공식 검색 API를 호출한다.

    None:
        요청 실패 또는 잘못된 응답

    []:
        정상 응답이지만 검색 결과가 없음
    """
    params = urlencode({
        "term": query,
        "entity": "software",
        "country": country,
        "limit": SEARCH_LIMIT,
    })

    url = "https://itunes.apple.com/search?" + params

    try:
        data = collect.http_json(url)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    results = data.get("results")

    if not isinstance(results, list):
        return None

    # 앱 ID가 있는 정상 후보만 사용한다.
    return [
        item
        for item in results
        if isinstance(item, dict)
        and item.get("trackId")
    ]


def _rank_key(app, query):
    """후보를 정렬할 뿐, 특정 앱으로 확정하지 않는다."""
    target = _normalize(query)
    name = _normalize(app.get("trackName"))

    exact_match = bool(target and name == target)
    contains = bool(target and target in name)

    similarity = difflib.SequenceMatcher(
        None,
        target,
        name
    ).ratio()

    try:
        rating_count = int(
            app.get("userRatingCount") or 0
        )
    except (TypeError, ValueError):
        rating_count = 0

    return (
        exact_match,
        contains,
        similarity,
        rating_count,
    )


def search_apple(query):
    """앱 후보 목록을 반환한다.

    검색 순서:
    1. 입력한 검색어로 한국 스토어 조회
    2. 결과가 없으면 미국 스토어 조회
    3. 모두 결과가 없으면 괄호를 제거한 검색어로 재시도

    검색 결과는 존재 여부를 보장하는 전체 앱 목록이 아니다.
    Apple 검색 API가 반환하는 후보만 표시한다.
    """
    query = str(query or "").strip()[:100]

    if not query:
        return []

    cache_key = query.casefold()
    cached = _CACHE.get(cache_key)

    if cached:
        saved_at, cards = cached

        if time.monotonic() - saved_at < CACHE_TTL:
            # 호출한 쪽에서 캐시 원본을 변경하지 못하도록 복사한다.
            return [dict(card) for card in cards]

        _CACHE.pop(cache_key, None)

    results = []
    result_country = "kr"
    matched_query = query

    for variant in _query_variants(query):
        for country in ("kr", "us"):
            found = _fetch_apple(variant, country)

            if found:
                results = found
                result_country = country
                matched_query = variant
                break

        if results:
            break

    # 빈 결과나 요청 실패는 캐시에 저장하지 않는다.
    if not results:
        return []

    # 같은 앱 ID가 중복되면 한 번만 표시한다.
    unique = {}

    for app in results:
        app_id = str(app["trackId"])

        if app_id not in unique:
            unique[app_id] = app

    ordered = sorted(
        unique.values(),
        key=lambda app: _rank_key(app, matched_query),
        reverse=True
    )

    cards = [
        _card(app, result_country)
        for app in ordered[:SEARCH_LIMIT]
    ]

    # 이름이 같은 후보는 기존 화면에서 구분 표시할 수 있다.
    name_counts = {}

    for card in cards:
        name = _normalize(card["name"])
        name_counts[name] = name_counts.get(name, 0) + 1

    for card in cards:
        name = _normalize(card["name"])
        card["duplicate"] = name_counts[name] > 1

    if len(_CACHE) >= CACHE_MAX:
        oldest_key = min(
            _CACHE,
            key=lambda key: _CACHE[key][0]
        )
        _CACHE.pop(oldest_key, None)

    _CACHE[cache_key] = (
        time.monotonic(),
        [dict(card) for card in cards]
    )

    return cards


def parse_play(value):
    """Google Play URL 또는 패키지명을 확인한다.

    이 함수는 Google Play에 접속하지 않는다.
    앱이 실제 등록되어 있는지까지 검증하지는 않는다.
    """
    value = str(value or "").strip()
    package = None

    if re.match(r"https?://", value, re.I):
        try:
            parsed = urlparse(value)
        except ValueError:
            return None

        hostname = (parsed.hostname or "").lower()

        if hostname in (
            "play.google.com",
            "www.play.google.com"
        ):
            package = (
                parse_qs(parsed.query).get("id")
                or [None]
            )[0]

    elif re.fullmatch(
        r"[A-Za-z][\w]*(\.[\w]+)+",
        value
    ):
        package = value

    if not package:
        return None

    if not re.fullmatch(
        r"[A-Za-z][\w]*(\.[\w]+)+",
        package
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
    """기존 main.py와 index.html의 응답 형식을 유지한다."""
    play = parse_play(query)

    return {
        "apple": [] if play else search_apple(query),
        "google_play": {
            "supported": False,
            "message": PLAY_NOTICE,
            "parsed": play,
        },
    }
