"""가맹점 → 카테고리 분류. 로컬 키워드 규칙만 쓴다 (architecture.md "카테고리 키워드 규칙")."""
import re

# 키워드 규칙은 이곳에만 정의한다. 표의 위쪽 카테고리가 같은 길이 키워드에서 우선한다.
RULES = [
    ("식비", "식당, 김밥, 분식, 국밥, 한식, 중식, 일식, 치킨, 피자, 버거, 맥도날드, 롯데리아, 버거킹, 맘스터치, 노브랜드버거, 한솥, 도시락, 샐러디, 떡볶이, 배달의민족, 우아한형제들, 쿠팡이츠, 요기요, 파리바게뜨, 뚜레쥬르, 베이커리"),
    ("카페", "카페, 커피, 스타벅스, 이디야, 투썸, 메가엠지씨, 메가커피, 컴포즈, 빽다방, 할리스, 폴바셋, 파스쿠찌, 엔제리너스, 더벤티, 공차, 배스킨, 설빙"),
    ("교통", "택시, 카카오T, 카카오모빌리티, 티머니, 지하철, 버스, 코레일, SRT, 주유, SK에너지, GS칼텍스, S-OIL, 현대오일뱅크, 하이패스, 고속도로, 주차, 쏘카"),
    ("쇼핑", "쿠팡, 11번가, G마켓, 지마켓, 옥션, 에스에스지닷컴, SSG, 무신사, 올리브영, 백화점, 아울렛, 홈쇼핑, 아디다스, 룰루레몬, 한샘, 이케아"),
    ("의료", "병원, 의원, 치과, 한의원, 약국, 내과, 외과, 피부과, 안과, 이비인후과, 소아과, 검진"),
    ("여가", "CGV, 메가박스, 롯데시네마, 영화, 노래, 볼링, 골프, 헬스, 피트니스, 교보문고, 예스이십사, 도서, 호텔, 숙박, 야놀자, 여기어때, 항공, 대한항공, 아시아나, 인터파크, 티켓, PC방"),
    ("구독", "넷플릭스, NETFLIX, 유튜브, YOUTUBE, 디즈니, 왓챠, 티빙, 웨이브, 멜론, 지니, 스포티파이, APPLE, 애플, GOOGLE, 구글플레이, 밀리의서재, 리디, OPENAI, ANTHROPIC, CLAUDE.AI, GITHUB, NOTION, DROPBOX, VERCEL"),
    ("생활비", "이마트, 홈플러스, 롯데마트, 코스트코, 하나로마트, 노브랜드, 컬리, 과일, GS25, CU, 세븐일레븐, 이마트24, 편의점, 다이소, 관리비, 한국전력, 전기, 가스, 수도, SKT, KT, LG유플러스, 세탁, 미용실, 헤어"),
]
OTHER = "기타"
CATEGORIES = [name for name, _ in RULES] + [OTHER]
CONFIDENCE_THRESHOLD = 0.6

_KEYWORDS = [
    (kw.strip().replace(" ", "").upper(), name, rank)
    for rank, (name, words) in enumerate(RULES)
    for kw in words.split(",")
]
_SHORT_EN = re.compile(r"[A-Z]{2}")

# 강조 토픽: 요청 속 단어 → 카테고리 (architecture.md 0번)
TOPIC_ALIASES = {
    "외식": "식비", "배달": "식비", "커피": "카페", "택시": "교통", "주유": "교통",
    "병원": "의료", "약국": "의료", "영화": "여가", "여행": "여가", "마트": "생활비",
    "편의점": "생활비", "통신비": "생활비",
}


def _matches(keyword, name):
    if _SHORT_EN.fullmatch(keyword):  # KT, CU: 앞뒤가 영문자가 아닐 때만
        return re.search(rf"(?<![A-Z]){keyword}(?![A-Z])", name) is not None
    return keyword in name


def classify(merchant):
    """(카테고리, 신뢰도, 일치 키워드). 규칙 일치 1.0, 불일치 0 → 0.6 미만은 기타."""
    name = merchant.replace(" ", "").upper()
    hits = [(len(kw), -rank, cat, kw) for kw, cat, rank in _KEYWORDS if _matches(kw, name)]
    confidence = 1.0 if hits else 0.0
    if confidence < CONFIDENCE_THRESHOLD:
        return OTHER, confidence, None
    _, _, category, keyword = max(hits)
    return category, confidence, keyword


def find_topic(text):
    """요청에 처음 나오는 카테고리 언급을 카테고리 이름으로 돌려준다. 없으면 None."""
    candidates = {name: name for name in CATEGORIES if name != OTHER}
    candidates.update(TOPIC_ALIASES)
    found = [(text.find(word), cat) for word, cat in candidates.items() if word in text]
    return min(found)[1] if found else None
