"""PII 마스킹, 가맹점명 정규화, 외부 호출용 익명화 토큰 (guardrails.md 2·3번)."""
import re

# 카드번호: 숫자 4-4-4-4 (일부 * 포함, 구분자 - 또는 공백 허용)
CARD_RE = re.compile(r"(?<![\d*])[\d*]{4}[- ]?[\d*]{4}[- ]?[\d*]{4}[- ]?[\d*]{4}(?![\d*])")
# 전화번호: 0으로 시작하는 2~3 / 3~4 / 4자리
PHONE_RE = re.compile(r"(?<!\d)0\d{1,2}[- .]?\d{3,4}[- .]?\d{4}(?!\d)")
# 이름: 라벨 뒤 한글 2~4자
NAME_RE = re.compile(r"(성명|이름|회원명|예금주|명의)(\s*[:：]?\s*)[가-힣]{2,4}")
HOLDER_ALLOWED = {"본인", "가족", "-", ""}

PG_PREFIXES = ("KCP", "KG이니시스", "이니시스", "토스페이먼츠", "나이스페이", "다날", "페이코")
CORP_RE = re.compile(r"\(주\)|㈜|주식회사|유한회사|유한회|\s+")


def mask_text(text):
    """카드번호•전화번호•라벨이 붙은 이름을 가린다."""
    text = CARD_RE.sub("****-****-****-****", text)
    text = PHONE_RE.sub("***-****-****", text)
    return NAME_RE.sub(r"\1\2***", text)


def mask_holder(value):
    """카드구분/이용자 컬럼: 본인•가족•- 이외의 값(사람 이름일 수 있음)은 가린다."""
    return value if value.strip() in HOLDER_ALLOWED else "***"


def normalize_merchant(name):
    """주 사용처 집계용 가맹점 이름. 지점명(_ 뒤)을 떼고, 결제대행 표기면 법인명을 쓴다."""
    head, sep, tail = name.partition(" - ")
    base = head.split("_")[0].strip()
    if sep and (not base or base.upper().startswith(PG_PREFIXES)):
        base = CORP_RE.sub("", tail) or base
    return base or name.strip()


class PrivacyViolation(Exception):
    """외부로 나갈 문자열에 원문 값(가맹점명•금액•날짜)이 남아 있음."""


class Anonymizer:
    """외부 호출용 토큰 치환. 현재 main.py는 외부 호출을 하지 않는다 (guardrails.md 3번)."""

    PREFIX = {"merchant": "M", "amount": "A", "date": "D"}

    def __init__(self):
        self._forward = {}
        self._reverse = {}
        self._count = dict.fromkeys(self.PREFIX, 0)

    def token(self, kind, value):
        key = (kind, str(value))
        if key not in self._forward:
            self._count[kind] += 1
            tok = f"[{self.PREFIX[kind]}{self._count[kind]:03d}]"
            self._forward[key] = tok
            self._reverse[tok] = str(value)
        return self._forward[key]

    def assert_clean(self, payload):
        leaked = [value for (_, value) in self._forward if value and value in payload]
        if leaked:
            raise PrivacyViolation(f"원문 값 {len(leaked)}개가 남아 있음")

    def restore(self, text):
        """토큰 → 원문. 로컬에서만 쓴다."""
        for tok, value in self._reverse.items():
            text = text.replace(tok, value)
        return text
