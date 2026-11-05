"""APP TRUST AI - 제출 앱 점검 기준 (외부 의존성 없음, 단독 테스트 가능)

세 가지 영역으로 나눠 점검한다.
  info         정보 완성도      : 심사에 필요한 정보가 빠짐없이 적혀 있는가
  tech         기술적 완성도    : 웹사이트·저장소·분석 결과로 본 기본 완성도
  transparency AI 활용 투명성   : AI 활용 여부를 정확하게 밝혔는가

원칙: AI를 활용했다는 사실 자체로는 절대 감점하지 않는다.
      투명성 영역은 '밝혔는가'와 '밝힌 내용이 분석 결과와 어긋나지 않는가'만 본다.

각 기준의 check(sub, analysis) 는 True(충족) / False(미충족) / None(평가 불가, 점수에서 제외) 를 돌려준다.
  sub      : submissions 테이블의 한 행(dict)
  analysis : 기존 앱 분석 결과 요약(dict, 선택) - quality_score, declaration_flag 등
"""
import re
from urllib.parse import urlparse

CATEGORIES = {
    "info": "정보 완성도",
    "tech": "기술적 완성도",
    "transparency": "AI 활용 투명성",
}

# ai_disclosure 에 저장하는 값 (DB에서는 text). 어느 값이든 점수는 같다.
AI_DISCLOSURE = {
    "none": "AI를 사용하지 않음",
    "assisted": "개발 일부에 AI 활용 (코드 보조·리뷰 등)",
    "generated": "주로 AI 도구로 제작 (바이브코딩 등)",
}
AI_USED = ("assisted", "generated")

PASS_SCORE = 70    # 영역 점수가 이 값 미만이면 '보완 필요'로 표시


def _txt(sub, k):
    return str(sub.get(k) or "").strip()

def is_http_url(v, https_only=False):
    v = (v or "").strip()
    try:
        u = urlparse(v)
    except ValueError:
        return False
    ok_scheme = u.scheme == "https" if https_only else u.scheme in ("http", "https")
    return ok_scheme and bool(u.netloc) and "." in u.netloc

def is_github_repo(v):
    return bool(re.match(r"^https://github\.com/[\w.-]+/[\w.-]+/?$", (v or "").strip()))

def declared_ai_of(sub):
    """ai_disclosure 값을 기존 분석 기능의 declared_ai(True/False/None)로 바꾼다."""
    v = _txt(sub, "ai_disclosure")
    if v in AI_USED: return True
    if v == "none": return False
    return None


def _quality(sub, a):
    q = (a or {}).get("quality_score")
    return None if q is None else q >= 60

def _declaration_consistent(sub, a):
    if not a or "declaration_flag" not in a: return None
    flag = a.get("declaration_flag") or ""
    # '공개했으나 흔적이 적음'은 정상일 수 있으므로 문제 삼지 않는다. '미공개인데 흔적 발견'만 미충족.
    return not flag.startswith("AI 사용 미공개")

def _ai_note(sub, a):
    if declared_ai_of(sub) is not True: return None       # AI를 쓰지 않았거나 미선택이면 평가하지 않음
    return len(_txt(sub, "ai_note")) >= 10


# field: 미충족 시 개발자에게 보완을 요청할 submissions 컬럼 (None이면 자동 요청 대상 아님)
CRITERIA = [
    # ---- 정보 완성도 ----
    {"key": "app_name", "category": "info", "weight": 2, "field": "app_name",
     "label": "앱 이름 기재", "check": lambda s, a: bool(_txt(s, "app_name"))},
    {"key": "developer", "category": "info", "weight": 2, "field": "developer",
     "label": "개발자(개발사) 이름 기재", "check": lambda s, a: bool(_txt(s, "developer"))},
    {"key": "description", "category": "info", "weight": 2, "field": "description",
     "label": "앱 설명 50자 이상", "check": lambda s, a: len(_txt(s, "description")) >= 50},
    {"key": "where", "category": "info", "weight": 1, "field": "store_url",
     "label": "스토어 주소 또는 웹사이트 등록",
     "check": lambda s, a: is_http_url(_txt(s, "store_url")) or is_http_url(_txt(s, "website"))},
    {"key": "privacy_url", "category": "info", "weight": 2, "field": "privacy_url",
     "label": "개인정보처리방침 주소 등록", "check": lambda s, a: is_http_url(_txt(s, "privacy_url"))},
    {"key": "operator_info", "category": "info", "weight": 2, "field": "operator_info",
     "label": "운영자 정보(상호·연락처 등) 10자 이상", "check": lambda s, a: len(_txt(s, "operator_info")) >= 10},
    # ---- 기술적 완성도 ----
    {"key": "https", "category": "tech", "weight": 1, "field": "website",
     "label": "웹사이트 HTTPS 사용",
     "check": lambda s, a: is_http_url(_txt(s, "website"), https_only=True) if _txt(s, "website") else None},
    {"key": "privacy_https", "category": "tech", "weight": 1, "field": "privacy_url",
     "label": "개인정보처리방침 HTTPS 제공",
     "check": lambda s, a: is_http_url(_txt(s, "privacy_url"), https_only=True) if _txt(s, "privacy_url") else None},
    {"key": "repo", "category": "tech", "weight": 1, "field": None,
     "label": "공개 GitHub 저장소 (선택, 있으면 가점)",
     "check": lambda s, a: True if is_github_repo(_txt(s, "repo")) else None},
    {"key": "quality", "category": "tech", "weight": 3, "field": None,
     "label": "자동 분석 엔지니어링 완성도 60점 이상", "check": _quality},
    # ---- AI 활용 투명성 (AI 사용 여부 자체는 점수에 영향 없음) ----
    {"key": "ai_disclosure", "category": "transparency", "weight": 3, "field": "ai_disclosure",
     "label": "AI 활용 여부 선택", "check": lambda s, a: _txt(s, "ai_disclosure") in AI_DISCLOSURE},
    {"key": "ai_note", "category": "transparency", "weight": 2, "field": "ai_note",
     "label": "AI를 활용했다면 어디에 썼는지 10자 이상 설명", "check": _ai_note},
    {"key": "consistent", "category": "transparency", "weight": 3, "field": "ai_note",
     "label": "AI 활용 신고와 자동 분석 결과가 어긋나지 않음", "check": _declaration_consistent},
]
BY_KEY = {c["key"]: c for c in CRITERIA}


def evaluate(sub, analysis=None):
    """제출 정보(+선택: 분석 결과)를 기준별로 점검한다.
    반환: {"categories": {영역: {...}}, "items": [...], "suggestions": [{field, reason}], "ai_penalty": False}"""
    items, cats = [], {}
    for k, label in CATEGORIES.items():
        cats[k] = {"label": label, "earned": 0, "possible": 0, "score": None, "status": "평가 불가"}
    for c in CRITERIA:
        try:
            r = c["check"](sub or {}, analysis)
        except Exception:
            r = None
        r = None if r is None else bool(r)
        items.append({"key": c["key"], "category": c["category"], "label": c["label"], "ok": r, "field": c["field"]})
        if r is None: continue
        cat = cats[c["category"]]
        cat["possible"] += c["weight"]
        cat["earned"] += c["weight"] if r else 0
    for cat in cats.values():
        if cat["possible"]:
            cat["score"] = round(100 * cat["earned"] / cat["possible"])
            cat["status"] = "양호" if cat["score"] >= PASS_SCORE else "보완 필요"
    seen, sugg = set(), []
    for it in items:
        if it["ok"] is False and it["field"] and it["field"] not in seen:
            seen.add(it["field"])
            sugg.append({"field": it["field"], "reason": f"'{it['label']}' 기준을 충족하지 못했어요."})
    return {"categories": cats, "items": items, "suggestions": sugg, "ai_penalty": False}
