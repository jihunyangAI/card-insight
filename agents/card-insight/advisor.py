"""요청 거부 판정, 절약 제안 생성(로컬 문장 템플릿, LLM 미사용), 단정 표현 검사, 카드분석검증
(guardrails.md 4번•카드분석 가드레일, retry_policy.md 카드분석검증)."""
import re

from merchant_normalizer import PrivacyViolation

REJECT_PII_OR_ASSERTIVE = "이 요청은 PII외부전송 또는 단정표현을 포함합니다. 에이전트는 익명화된 분석과 제안만 제공합니다."
REJECT_FINANCIAL = "이 요청은 특정 금융상품 추천을 포함합니다. 에이전트는 지출 분석과 절약 제안만 제공하며, 금융상품 추천은 하지 않습니다."

BANNED_EXPRESSIONS = ("끊어", "끊으세요", "안돼", "안 돼", "사지마", "사지 마", "하지 마", "그만", "절대", "반드시 줄", "당장")
TONE_WORDS = ("끊어", "끊으", "단호", "강하게", "세게", "사지마", "사지 마", "하지마", "하지 마")
SEND_WORDS = ("외부", "전송", "보내", "업로드", "공유")
SENSITIVE_WORDS = ("카드번호", "이름", "전화번호", "가맹점", "금액", "날짜", "거래내역", "이용내역")
FINANCIAL_WORDS = ("대출", "금융상품", "적금", "펀드", "주식", "투자상품", "보험상품")
RECOMMEND_TARGETS = ("카드", "대출", "보험", "상품")

TIPS = {
    "식비": "배달•외식 횟수",
    "카페": "방문 빈도와 음료 가격대",
    "교통": "택시•주유 이용",
    "쇼핑": "온라인 주문과 계획에 없던 구매",
    "의료": "정기 진료 외 지출",
    "여가": "문화•여가 예산",
    "구독": "중복•미사용 구독",
    "생활비": "장보기 빈도와 생활용품 구매 주기",
    "기타": "미분류 가맹점 지출 내역",
}
RATE_REQUIRED_KINDS = {"compare"}  # 나머지(신규•전월 없음•구독•할부)는 금액만 있어도 통과
AMOUNT_RE = re.compile(r"\d[\d,]*\s*원")
PERCENT_RE = re.compile(r"\d+(\.\d+)?%")


class AssertiveExpression(Exception):
    """리포트•출력에 단정 표현이 들어감 → 리포트를 만들지 않고 중단."""


def detect_request_rejection(text):
    """거부 대상 요청이면 거부 메시지, 아니면 None."""
    if any(w in text for w in FINANCIAL_WORDS) or (
        "추천" in text and any(w in text for w in RECOMMEND_TARGETS)
    ):
        return REJECT_FINANCIAL
    if any(w in text for w in TONE_WORDS):
        return REJECT_PII_OR_ASSERTIVE
    if any(w in text for w in SEND_WORDS) and any(w in text for w in SENSITIVE_WORDS):
        return REJECT_PII_OR_ASSERTIVE
    return None


def assert_non_assertive(texts):
    found = sorted({w for text in texts for w in BANNED_EXPRESSIONS if w in text})
    if found:
        raise AssertiveExpression(", ".join(found))


def assert_stdout_clean(lines, merchant_names):
    """표준출력은 외부 전송으로 본다: 금액•가맹점명이 있으면 중단 (architecture.md 표준출력 규칙)."""
    text = "\n".join(lines)
    if AMOUNT_RE.search(text) or any(name and name in text for name in merchant_names):
        raise PrivacyViolation("표준출력에 금액 또는 가맹점명이 포함됨")


def rate_words(rate):
    if abs(rate) < 0.05:
        return "변동 없음"
    return f"{abs(rate):.1f}% {'증가' if rate > 0 else '감소'}"


def build_suggestions(stats):
    cur = stats["current"]
    cats = cur["categories"]
    suggestions = []
    for cat in stats["top3"]:
        amount = cats[cat]["sum"]
        tip = TIPS[cat]
        if stats["top3_mode"] == "amount":
            share = amount / cur["total"] * 100
            text = (f"{cat} 카테고리 지출이 {amount:,}원(총액 대비 {share:.1f}%)으로 이번 달 금액 상위 항목입니다. "
                    f"{tip} 위주로 우선 줄여볼 만함. 전월 데이터 없음으로 절감액(추정)은 산정하지 않았습니다.")
            suggestions.append({"category": cat, "kind": "no_baseline", "text": text, "saving": None})
            continue
        c = stats["comparison"][cat]
        if c["new"]:
            text = (f"{cat} 카테고리 지출 {amount:,}원은 전월 없음(신규) 항목입니다. {tip} 검토 권장. "
                    f"전월 기준이 없어 절감액(추정)은 산정하지 않았습니다.")
            suggestions.append({"category": cat, "kind": "new", "text": text, "saving": None})
            continue
        text = (f"{cat} 카테고리 지출이 전월 대비 {c['rate']:.1f}% 증가"
                f"(이번 달 {amount:,}원, 일평균 {round(c['cur_daily']):,}원 / 전월 {round(c['prev_daily']):,}원). "
                f"{tip} 검토 권장. 전월 일평균 수준으로 줄이면 약 {c['excess']:,}원 절감(추정).")
        suggestions.append({"category": cat, "kind": "compare", "text": text, "saving": c["excess"]})

    if "구독" in cats and "구독" not in stats["top3"]:
        sub = cats["구독"]
        text = f"구독 카테고리 {sub['count']}건 {sub['sum']:,}원. 이용하지 않는 정기결제가 있는지 검토 권장."
        suggestions.append({"category": "구독", "kind": "subscription", "text": text, "saving": None})
    inst = cur["installment"]
    if inst["count"]:
        text = (f"할부 결제 {inst['count']}건 {inst['amount']:,}원(승인 시점 전액 기준). "
                f"남은 할부와 새 할부 계획 검토 권장.")
        suggestions.append({"category": "할부", "kind": "installment", "text": text, "saving": None})
    return suggestions


def validate(stats, suggestions, report_text):
    """리포트를 만들기 전 카드분석검증. 실패 항목 목록(금액 없이)을 돌려준다."""
    failures = []
    cur = stats["current"]
    category_sum = sum(v["sum"] for v in cur["categories"].values())
    if cur["total"] and abs(category_sum - cur["total"]) > cur["total"] * 0.001:
        diff = abs(category_sum - cur["total"]) / cur["total"] * 100
        failures.append(f"카테고리 합계와 총 사용금액 차이 {diff:.3f}% (허용 ±0.1%)")
    if "전월 대비, 초과액 순" not in report_text:
        failures.append("과소비 Top3 선정 기준 문구('전월 대비, 초과액 순')가 리포트에 없음")
    for s in suggestions:
        has_amount = AMOUNT_RE.search(s["text"]) is not None
        has_rate = PERCENT_RE.search(s["text"]) is not None and "전월 대비" in s["text"]
        if not has_amount or (s["kind"] in RATE_REQUIRED_KINDS and not has_rate):
            failures.append(f"{s['category']} 절약제안에 정량근거(금액•전월 대비 %) 부족")
    return failures
