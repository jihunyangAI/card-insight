"""docx 리포트 구성•저장 (architecture.md 6번 출력 구조). 면책문구는 이곳에만 정의한다."""
from pathlib import Path

from docx import Document

from advisor import TIPS, rate_words
from csv_loader import with_retry
from statistics_engine import days_in_month

DISCLAIMER = (
    "※ 본 리포트는 입력된 카드 이용내역만으로 로컬에서 자동 생성한 참고 자료이며, "
    "금융·세무·투자 자문이 아닙니다. 카테고리 분류와 절감액은 추정치로 실제와 다를 수 있으니, "
    "지출 결정은 본인의 상황에 맞게 직접 판단하시기 바랍니다."
)
TOP3_CRITERION = (
    "선정 기준: 전월 대비, 초과액 순. 전월보다 일평균이 늘어난 카테고리 중 초과액이 큰 순서이며, "
    "이번 달 3만원 미만 또는 총액 3% 미만 카테고리는 제외합니다. "
    "초과액 = (이번 달 일평균 − 전월 일평균) × 커버 일수 (한 달로 외삽하지 않음)."
)


def won(value):
    return f"{int(round(value)):,}원"


def report_path(output_dir, key):
    year, month = key.split("-")
    return Path(output_dir) / f"{year}{month}{days_in_month(key):02d}_report.docx"


def _rate_cell(stats, cat):
    if not stats["baseline"]:
        return "전월 데이터 없음"
    c = stats["comparison"].get(cat)
    if c is None or c["new"]:
        return "전월 없음(신규)"
    return f"{c['rate']:+.1f}%"


def compose(stats, suggestions, topic, issuers):
    """리포트 블록 목록. data=True 블록은 입력 데이터(가맹점명)를 담는다."""
    cur = stats["current"]
    cats = cur["categories"]
    total = cur["total"]
    year, month = stats["key"].split("-")
    blocks = []

    def add(kind, text=None, data=False, **extra):
        blocks.append({"kind": kind, "text": text, "data": data, **extra})

    add("title", f"카드 사용 리포트 — {year}년 {int(month)}월")
    add("para", f"카드사: {', '.join(issuers) or '미확인'} · 분석 기간: {cur['first']:%Y-%m-%d} ~ "
                f"{cur['last']:%Y-%m-%d} (커버 {cur['coverage_days']}일)")
    if stats["coverage_warning"]:
        add("para", f"주의: 이 달 데이터는 {stats['coverage_warning']} 있습니다. 전월 비교는 일평균으로 환산했습니다.")

    if topic:
        if topic in cats:
            share = cats[topic]["sum"] / total * 100
            in_top3 = "포함" if topic in stats["top3"] else "미포함"
            add("heading", f"강조: {topic}")
            add("para", f"{topic} 카테고리 {won(cats[topic]['sum'])} (총액 대비 {share:.1f}%), "
                        f"전월 대비 {_rate_cell(stats, topic)}, 과소비 Top3 {in_top3}.")
        else:
            add("heading", f"강조: {topic}")
            add("para", f"{topic} 카테고리는 이번 달 지출이 없습니다.")

    add("heading", "1. 요약")
    add("bullet", f"총 사용액: {won(total)} ({cur['count']}건)")
    add("bullet", f"일평균 {won(total / cur['coverage_days'])} · 주평균 {won(total / cur['coverage_days'] * 7)} · "
                  f"건당 평균 {won(total / cur['count'])}")
    if stats["total_rate"] is not None:
        base = stats["baseline"]
        add("bullet", f"전월 대비: 일평균 기준 {rate_words(stats['total_rate'])} "
                      f"(전월 {stats['prev_key']}, {base['source']} {base['coverage_days']}일치 기준)")
    else:
        add("bullet", f"전월 대비: 전월 데이터 없음 ({stats['baseline_reason']})")

    add("heading", "2. 카테고리별 지출")
    headers = ["카테고리", "금액", "건수", "비중", "전월 대비"]
    show_z = bool(stats["zscores"])
    if show_z:
        headers.append("z-score(참고)")
    rows = []
    for cat in sorted(cats, key=lambda c: cats[c]["sum"], reverse=True):
        row = [cat, won(cats[cat]["sum"]), f"{cats[cat]['count']}건",
               f"{cats[cat]['sum'] / total * 100:.1f}%", _rate_cell(stats, cat)]
        if show_z:
            z = stats["zscores"].get(cat)
            row.append("-" if z is None else f"{z:+.2f}")
        rows.append(row)
    add("table", headers=headers, rows=rows)

    add("heading", "3. 주 사용처 Top5")
    add("table", data=True, headers=["순위", "가맹점", "금액", "건수"],
        rows=[[str(i), name, won(amount), f"{count}건"]
              for i, (name, (amount, count)) in enumerate(stats["top_merchants"], 1)])

    add("heading", "4. 과소비 Top3")
    add("para", TOP3_CRITERION)
    if stats["top3_mode"] == "amount":
        add("para", f"전월 데이터 없음({stats['baseline_reason']})으로 비교하지 않고, 이번 달 금액 상위 3개 카테고리를 표시합니다.")
        for i, cat in enumerate(stats["top3"], 1):
            add("bullet", f"{i}. {cat}: {won(cats[cat]['sum'])} (총액 대비 {cats[cat]['sum'] / total * 100:.1f}%)")
    elif not stats["top3"]:
        add("para", "전월보다 늘어난 카테고리 중 기준을 넘는 항목이 없습니다.")
    else:
        for i, cat in enumerate(stats["top3"], 1):
            c = stats["comparison"][cat]
            change = "전월 없음(신규)" if c["new"] else f"전월 대비 {c['rate']:.1f}% 증가"
            add("bullet", f"{i}. {cat}: 이번 달 {won(cats[cat]['sum'])}, 일평균 {won(c['cur_daily'])} "
                          f"(전월 {won(c['prev_daily'])}), {change}, 초과액 {won(c['excess'])}. 점검 포인트: {TIPS[cat]}")

    add("heading", "5. 절약 가능 영역")
    if suggestions:
        for s in suggestions:
            add("bullet", s["text"])
    else:
        add("para", "제안할 항목이 없습니다.")

    add("heading", "6. 참고")
    if cur["unclassified"]:
        add("para", "미분류 가맹점 (키워드 규칙에 맞지 않아 '기타'로 분류):")
        add("table", data=True, headers=["가맹점", "건수"],
            rows=[[name, f"{n}건"] for name, n in sorted(cur["unclassified"].items())])
    else:
        add("para", "미분류 가맹점: 없음")
    if stats["big_merchants"]:
        add("para", "단일 가맹점 월 100만원 이상:")
        add("table", data=True, headers=["가맹점", "금액", "전월 패턴"],
            rows=[[name, won(amount), label] for name, amount, label in stats["big_merchants"]])
    else:
        add("para", "단일 가맹점 월 100만원 이상: 해당 없음")
    add("para", "취소•취소전표접수 거래는 원거래와 함께 제외했고, 할부는 승인 시점에 전액 계상했습니다.")

    add("para", DISCLAIMER)  # 마지막 단락
    return blocks


def text_of(blocks, include_data=True):
    parts = []
    for b in blocks:
        if b["data"] and not include_data:
            continue
        if b["text"]:
            parts.append(b["text"])
        if b["kind"] == "table":
            parts.extend(" ".join(r) for r in [b["headers"], *b["rows"]])
    return "\n".join(parts)


def render(blocks, path, deadline):
    doc = Document()
    for b in blocks:
        if b["kind"] == "title":
            doc.add_heading(b["text"], level=0)
        elif b["kind"] == "heading":
            doc.add_heading(b["text"], level=1)
        elif b["kind"] == "bullet":
            doc.add_paragraph(b["text"], style="List Bullet")
        elif b["kind"] == "para":
            doc.add_paragraph(b["text"])
        elif b["kind"] == "table":
            table = doc.add_table(rows=1, cols=len(b["headers"]))
            table.style = "Table Grid"
            for cell, header in zip(table.rows[0].cells, b["headers"]):
                cell.text = header
            for row in b["rows"]:
                for cell, value in zip(table.add_row().cells, row):
                    cell.text = value
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with_retry(lambda: doc.save(str(path)), (PermissionError,), deadline)
    return path
