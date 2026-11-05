"""detector.py 기준 케이스 테스트 (외부 의존성 없음). 실행: python -m pytest -q"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import detector as D

def ev(sigs, checks=None, sources=("store", "site", "repo"), weights=None, declared=None):
    return D.evaluate(sigs, checks or [], set(sources), None, declared, weights)

def test_old_big_app_is_human():
    """ChatGPT 이전 출시 + 평가 수 많은 앱 -> 직접 개발"""
    meta = {"releaseDate": "2012-01-01T00:00:00Z", "currentVersionReleaseDate": "2026-01-01T00:00:00Z",
            "userRatingCount": 500000, "languageCodesISO2A": ["KO"] * 12, "description": ""}
    sigs, _ = D.store_signals(meta, [])
    assert ev(sigs)["ai_score"] <= 20

def test_ai_builder_host_is_ai():
    site = {"html": "<title>x</title>", "text": "", "headers": {}, "bundles": [], "probe": {"robots": False, "404": False}}
    sigs, _ = D.site_signals("https://myapp.lovable.app", site)
    r = ev(sigs)
    assert r["ai_score"] >= 85 and "AI" in r["verdict"]

def test_no_evidence_is_hold():
    r = ev([], sources=("store",))
    assert r["verdict"].startswith("판단 보류")

def test_both_strong_is_mixed():
    sigs = [D.S("ai", "strong", "Cursor 설정 파일 발견: .cursorrules", "repo"),
            D.S("human", "strong", "ChatGPT 공개 이전 출시", "store")]
    assert ev(sigs)["ai_score"] == 55

def test_bad_strong_signal_loses_its_fixed_score():
    """심사자 판정으로 자주 틀린 강한 근거(배수 0.5)는 85점 고정이 풀린다"""
    s = D.S("ai", "strong", "AI 코딩 에이전트 설정 파일 발견: AGENTS.md", "repo")
    assert ev([s])["ai_score"] >= 85
    r = ev([s], weights={D.sig_key(s): 0.5})
    assert r["ai_score"] < 85

def test_weights_need_enough_data():
    s = D.S("ai", "strong", "AI 코딩 에이전트 설정 파일 발견: AGENTS.md", "repo")
    few = [{"label": "human", "signals": [s]}] * (D.MIN_N - 1)
    assert D.compute_weights(few)[D.sig_key(s)]["weight"] == 1.0
    many = [{"label": "human", "signals": [s]}] * (D.MIN_N + 5)
    assert D.compute_weights(many)[D.sig_key(s)]["weight"] < 1.0

def test_accuracy_ignores_hold():
    rows = [{"label": "ai", "ai_score": 90, "verdict": "AI 도구 사용 가능성 높음"},
            {"label": "human", "ai_score": 10, "verdict": "개발자 직접 제작 가능성 높음"},
            {"label": "ai", "ai_score": 50, "verdict": "판단 보류 (근거 부족)"}]
    rep = D.accuracy_report(rows)
    assert rep["evaluated"] == 2 and rep["hold"] == 1 and rep["accuracy"] == 1.0
