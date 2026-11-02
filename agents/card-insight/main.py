"""카드사용내역분석 에이전트 진입점.

사용: python agents/card-insight/main.py "4월 지출 정리해줘" [--issuer 카드사] [--input 폴더]
종료코드: 0 성공 / 1 오류•거부 / 2 입력 없음•요청 월 없음 / 3 카드사 식별 실패 / 4 60초 초과
"""
import argparse
import datetime as dt
import re
import sys
from pathlib import Path

from advisor import (AssertiveExpression, assert_non_assertive, assert_stdout_clean, build_suggestions,
                     detect_request_rejection, rate_words, validate)
from category_classifier import find_topic
from csv_loader import Deadline, TimeLimitExceeded, load_inputs, save_issuer_signatures
from merchant_normalizer import PrivacyViolation, normalize_merchant
from report_writer import DISCLAIMER, compose, render, report_path, text_of
from statistics_engine import analyze, available_months, month_key, prev_month_key, save_monthly_memory

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT.parent / "harness-book-samples" / "card-insight"
OUTPUT_DIR = ROOT / "output" / "card-report"
MEMORY_DIR = ROOT / "memory" / "card"
TIME_LIMIT_SECONDS = 60


def parse_request(text, months, today):
    """자연어 요청 → {month, kind, topic}. 월 지정이 없으면 데이터의 최신 월."""
    if re.search(r"디버그|debug", text, re.IGNORECASE):
        kind = "디버그"
    elif "비교" in text:
        kind = "전월비교"
    else:
        kind = "리포트"

    month = None
    if m := (re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월", text) or re.search(r"(\d{4})[-./](\d{1,2})(?!\d)", text)):
        year, mon = int(m[1]), int(m[2])
        month = f"{year:04d}-{mon:02d}" if 1 <= mon <= 12 else None
    elif m := re.search(r"(?<!\d)(\d{1,2})\s*월", text):
        mon = int(m[1])
        candidates = [k for k in months if int(k[5:]) == mon]
        month = max(candidates) if candidates else (f"{today.year:04d}-{mon:02d}" if 1 <= mon <= 12 else None)
    elif re.search(r"이번\s*달|이달|금월", text):
        month = month_key(today)
    elif re.search(r"지난\s*달|저번\s*달", text):
        month = prev_month_key(month_key(today))
    elif months:
        month = months[-1]
    return {"month": month, "kind": kind, "topic": find_topic(text)}


def print_unknown_issuer(unknown, request):
    print("카드사를 식별하지 못했습니다. 아래 헤더를 보고 어느 카드사인지 알려 주세요.")
    for info in unknown:  # 제목 행은 이름 등 PII가 있을 수 있어 줄 수만 보여준다
        print(f"- {info['file']} (헤더 {info['header_row']}행, 제목 {info['title_lines']}줄 표시 안 함)")
        print(f"  컬럼: {', '.join(info['columns'])}")
    print(f'다시 실행: python agents/card-insight/main.py "{request}" --issuer "카드사명"')


def print_debug(loaded, transactions):
    print("[디버그] 파일별 처리 결과")
    for info in loaded["files"]:
        print(f"- {info['file']}: 헤더 {info['header_row']}행, 카드사 {info['issuer'] or '미확인'}, "
              f"거래 {info['parsed']}건, 취소 제외 {info['cancel_excluded']}건, "
              f"소계 등 제외 {info['non_data_excluded']}건")
    print(f"- 중복 제거: {loaded['duplicates']}건")
    counts = {}
    for t in transactions:
        counts[month_key(t["date"])] = counts.get(month_key(t["date"]), 0) + 1
    print("- 월별 건수: " + ", ".join(f"{k} {v}건" for k, v in sorted(counts.items())))


def stdout_lines(stats, suggestions, path):
    """표준출력: 경로 + 핵심 3줄 + 면책문구. 금액•가맹점명•거래 날짜는 넣지 않는다."""
    key = stats["key"]
    lines = [f"결과파일: {path}", f"분석 월: {key}"]
    if stats["coverage_warning"]:
        lines.append(f"주의: {key} 데이터는 {stats['coverage_warning']} 있습니다.")

    if stats["total_rate"] is None:
        lines.append("총 사용액: 리포트 참조 (전월 데이터 없음)")
    else:
        lines.append(f"총 사용액: 전월 대비 {rate_words(stats['total_rate'])} (금액은 리포트 참조)")

    cats = stats["current"]["categories"]
    if stats["top3_mode"] == "amount":
        total = stats["current"]["total"]
        items = [f"{c}(비중 {cats[c]['sum'] / total * 100:.1f}%)" for c in stats["top3"]]
        lines.append("금액 상위 3개 (전월 데이터 없음): " + ", ".join(items))
    else:
        items = []
        for c in stats["top3"]:
            comp = stats["comparison"][c]
            items.append(f"{c}(전월 없음(신규))" if comp["new"] else f"{c}(전월 대비 {rate_words(comp['rate'])})")
        lines.append("과소비 Top3 (전월 대비, 초과액 순): " + (", ".join(items) or "해당 없음"))

    names = [s["category"] for s in suggestions if s["kind"] in ("compare", "new", "no_baseline", "subscription")]
    parts = [f"{'•'.join(names)} 카테고리 검토 권장"] if names else []
    if any(s["kind"] == "installment" for s in suggestions):
        parts.append("할부 결제 점검 권장")
    lines.append("절약제안: " + (", ".join(parts) + " (절감액(추정)은 리포트 참조)" if parts else "리포트 참조"))
    lines.append(DISCLAIMER)
    return lines


def run(args, deadline):
    request = " ".join(args.request)
    rejection = detect_request_rejection(request)
    if rejection:
        print(rejection)
        return 1

    loaded = load_inputs(Path(args.input), MEMORY_DIR, deadline)
    for warning in loaded["warnings"]:
        print(f"경고: {warning}", file=sys.stderr)
    if not loaded["files"]:
        print(f"입력 파일이 없습니다: {args.input}")
        return 2

    if loaded["unknown"]:
        if not args.issuer:
            print_unknown_issuer(loaded["unknown"], request)
            return 3
        for info in loaded["unknown"]:
            info["issuer"] = args.issuer
        try:
            save_issuer_signatures(MEMORY_DIR, {i["signature"]: args.issuer for i in loaded["unknown"]}, deadline)
        except OSError:
            print("경고: 카드사 시그니처를 메모리에 저장하지 못했습니다.", file=sys.stderr)

    transactions = loaded["transactions"]
    months = available_months(transactions)
    if not months:
        print("분석할 거래가 없습니다.")
        return 2
    intent = parse_request(request, months, dt.date.today())
    if intent["kind"] == "디버그":
        print_debug(loaded, transactions)
        return 0
    if intent["month"] not in months:
        print(f"요청한 월({intent['month'] or '해석 불가'})의 데이터가 없습니다. 사용 가능한 월: {', '.join(months)}")
        return 2

    deadline.check("분석")
    key = intent["month"]
    stats = analyze(transactions, key, MEMORY_DIR)
    suggestions = build_suggestions(stats)
    issuer_by_file = {i["file"]: i["issuer"] for i in loaded["files"]}
    issuers = sorted({issuer_by_file[t["file"]] for t in transactions if month_key(t["date"]) == key})
    blocks = compose(stats, suggestions, intent["topic"], issuers)
    path = report_path(OUTPUT_DIR, key)
    lines = stdout_lines(stats, suggestions, path)

    # 가드레일: 단정 표현 → 중단, 표준출력에 금액•가맹점명 → 중단
    assert_non_assertive([text_of(blocks, include_data=False), *lines])
    merchant_names = {t["merchant"] for t in transactions} | {normalize_merchant(t["merchant"]) for t in transactions}
    assert_stdout_clean(lines, merchant_names)

    failures = validate(stats, suggestions, text_of(blocks))
    if failures:
        print("카드분석검증 실패로 리포트를 만들지 않았습니다.")
        for f in failures:
            print(f"- {f}")
        return 1

    deadline.check("리포트 저장")
    try:
        render(blocks, path, deadline)
    except PermissionError:
        print("리포트 파일이 다른 프로그램에서 열려 있어 저장하지 못했습니다. 파일을 닫고 다시 실행하세요.")
        return 1
    try:
        save_monthly_memory(MEMORY_DIR, key, stats["current"], deadline)
    except OSError:
        print("경고: 월별 요약을 메모리에 저장하지 못했습니다 (리포트는 생성됨).", file=sys.stderr)

    print("\n".join(lines))
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):  # 테스트에서 StringIO로 바꿔 끼울 수 있게
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="카드 이용내역 → docx 리포트 (로컬 전용)")
    parser.add_argument("request", nargs="+", help="자연어 요청 한 문장")
    parser.add_argument("--issuer", help="카드사 식별 실패 시 사용자가 확인한 카드사")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="입력 폴더")
    args = parser.parse_args(argv)
    deadline = Deadline(TIME_LIMIT_SECONDS)
    try:
        return run(args, deadline)
    except TimeLimitExceeded as e:
        print(f"시간 한도 {TIME_LIMIT_SECONDS}초를 넘어 중단했습니다 (단계: {e}). 입력 파일 수를 줄여 다시 실행해 주세요.")
        return 4
    except AssertiveExpression as e:
        print(f"단정 표현({e})이 발견되어 리포트를 만들지 않았습니다 (가드레일 위반).")
        return 1
    except PrivacyViolation as e:
        print(f"개인정보 검사 실패로 중단했습니다: {e}")
        return 1
    except Exception as e:  # 데이터 값이 섞일 수 있으므로 예외 종류만 출력
        print(f"오류로 중단했습니다: {type(e).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
