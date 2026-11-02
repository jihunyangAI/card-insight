"""통계 계산 — 파이썬만, LLM에 산수를 맡기지 않는다 (architecture.md 지표 정의•4번)
와 월별 요약 메모리 읽기/쓰기 (memory_policy.md)."""
import calendar
import statistics
from collections import defaultdict
from pathlib import Path

from category_classifier import CATEGORIES, OTHER, classify
from csv_loader import atomic_write_json, read_json
from merchant_normalizer import normalize_merchant

MIN_BASELINE_DAYS = 7
COVERAGE_WARN_RATIO = 0.8
TOP3_MIN_AMOUNT = 30_000
TOP3_MIN_SHARE = 0.03
BIG_MERCHANT_AMOUNT = 1_000_000
ZSCORE_MIN_MONTHS = 3
MEMORY_KEEP_MONTHS = 12
SUMMARY_FILE = "monthly_summary.json"


def month_key(date):
    return f"{date.year:04d}-{date.month:02d}"


def prev_month_key(key):
    year, month = map(int, key.split("-"))
    return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


def days_in_month(key):
    year, month = map(int, key.split("-"))
    return calendar.monthrange(year, month)[1]


def available_months(transactions):
    return sorted({month_key(t["date"]) for t in transactions})


def summarize(transactions):
    """한 달 거래 → 집계. 분석 월은 승인일 기준."""
    dates = [t["date"] for t in transactions]
    amounts_by_cat = defaultdict(list)
    merchants = defaultdict(lambda: [0, 0])
    unclassified = defaultdict(int)
    installment = [0, 0]
    for t in transactions:
        category, _, _ = classify(t["merchant"])
        amounts_by_cat[category].append(t["amount"])
        entry = merchants[normalize_merchant(t["merchant"])]
        entry[0] += t["amount"]
        entry[1] += 1
        if category == OTHER:
            unclassified[t["merchant"]] += 1
        if t["installment"] > 1 or t["usage"] == "할부":
            installment[0] += 1
            installment[1] += t["amount"]
    categories = {
        cat: {"sum": sum(v), "count": len(v), "mean": sum(v) / len(v), "std": statistics.pstdev(v)}
        for cat, v in amounts_by_cat.items()
    }
    return {
        "total": sum(t["amount"] for t in transactions),
        "count": len(transactions),
        "first": min(dates),
        "last": max(dates),
        "coverage_days": (max(dates) - min(dates)).days + 1,
        "categories": categories,
        "merchants": dict(merchants),
        "unclassified": dict(unclassified),
        "installment": {"count": installment[0], "amount": installment[1]},
    }


def memory_record(summary):
    """메모리에 남기는 월별 집계: 총액•건수•커버 일수•카테고리별 합계만."""
    return {
        "total": summary["total"],
        "count": summary["count"],
        "coverage_days": summary["coverage_days"],
        "categories": {cat: v["sum"] for cat, v in summary["categories"].items()},
    }


def load_monthly_memory(memory_dir):
    return read_json(Path(memory_dir) / SUMMARY_FILE)


def save_monthly_memory(memory_dir, key, summary, deadline):
    data = load_monthly_memory(memory_dir)
    data[key] = memory_record(summary)
    kept = sorted(data)[-MEMORY_KEEP_MONTHS:]
    atomic_write_json(Path(memory_dir) / SUMMARY_FILE, {k: data[k] for k in kept}, deadline)


def _daily(record, category=None):
    value = record["total"] if category is None else record["categories"].get(category, 0)
    return value / record["coverage_days"]


def analyze(transactions, key, memory_dir):
    by_month = defaultdict(list)
    for t in transactions:
        by_month[month_key(t["date"])].append(t)
    current = summarize(by_month[key])
    cur_record = memory_record(current)
    memory = load_monthly_memory(memory_dir)

    # 비교 기준 = 전월. 입력 데이터가 메모리보다 우선한다.
    prev_key = prev_month_key(key)
    prev_input = summarize(by_month[prev_key]) if by_month.get(prev_key) else None
    baseline, baseline_reason = None, f"전월({prev_key}) 데이터 없음"
    if prev_input:
        baseline = {**memory_record(prev_input), "source": "입력 데이터"}
    elif prev_key in memory:
        baseline = {**memory[prev_key], "source": "메모리 월별 요약"}
    if baseline and baseline["coverage_days"] < MIN_BASELINE_DAYS:
        baseline_reason = f"전월({prev_key}) 커버 일수 {baseline['coverage_days']}일 (7일 미만)"
        baseline = None

    coverage = current["coverage_days"]
    comparison, total_rate = {}, None
    if baseline:
        for cat in CATEGORIES:
            cur_sum = cur_record["categories"].get(cat, 0)
            prev_sum = baseline["categories"].get(cat, 0)
            if cur_sum == 0 and prev_sum == 0:
                continue
            cur_daily, prev_daily = _daily(cur_record, cat), _daily(baseline, cat)
            comparison[cat] = {
                "cur_daily": cur_daily,
                "prev_daily": prev_daily,
                "new": prev_daily == 0,
                "rate": None if prev_daily == 0 else (cur_daily / prev_daily - 1) * 100,
                "excess": round((cur_daily - prev_daily) * coverage),  # 외삽하지 않음
            }
        if baseline["total"] > 0:
            total_rate = (_daily(cur_record) / _daily(baseline) - 1) * 100

    # 과소비 Top3: 전월보다 높은 카테고리 중 초과액 순. 3만원 미만•총액 3% 미만 제외.
    total = current["total"]
    if baseline:
        candidates = [
            cat for cat, c in comparison.items()
            if c["excess"] > 0
            and cur_record["categories"].get(cat, 0) >= TOP3_MIN_AMOUNT
            and cur_record["categories"].get(cat, 0) >= total * TOP3_MIN_SHARE
        ]
        top3 = sorted(candidates, key=lambda cat: comparison[cat]["excess"], reverse=True)[:3]
        top3_mode = "excess"
    else:  # 전월 비교 불가 → 이번 달 금액 상위 3개
        top3 = sorted(cur_record["categories"], key=cur_record["categories"].get, reverse=True)[:3]
        top3_mode = "amount"

    # 자기평균 대비 z-score: 이전 달 요약이 3개월 이상일 때만 (입력 데이터 우선)
    history = {k: v for k, v in memory.items() if k < key and v.get("coverage_days")}
    history.update({k: memory_record(summarize(v)) for k, v in by_month.items() if k < key and v})
    zscores = {}
    if len(history) >= ZSCORE_MIN_MONTHS:
        for cat in cur_record["categories"]:
            values = [_daily(h, cat) for h in history.values()]
            spread = statistics.pstdev(values)
            if spread > 0:
                zscores[cat] = (_daily(cur_record, cat) - statistics.mean(values)) / spread

    # 단일 가맹점 월 100만원 이상: 입력 데이터의 전월 거래로 패턴 확인
    big_merchants = []
    for name, (amount, _) in current["merchants"].items():
        if amount < BIG_MERCHANT_AMOUNT:
            continue
        if prev_input is None:
            label = "전월 데이터 없음"
        elif prev_input["merchants"].get(name, [0])[0] >= BIG_MERCHANT_AMOUNT:
            label = "전월에도 동일 패턴"
        else:
            label = "전월 대비 신규 패턴"
        big_merchants.append((name, amount, label))

    month_days = days_in_month(key)
    coverage_warning = (
        f"{month_days}일 중 {coverage}일치만" if coverage < month_days * COVERAGE_WARN_RATIO else None
    )
    top_merchants = sorted(current["merchants"].items(), key=lambda kv: kv[1][0], reverse=True)[:5]

    return {
        "key": key,
        "prev_key": prev_key,
        "current": current,
        "baseline": baseline,
        "baseline_reason": baseline_reason,
        "comparison": comparison,
        "total_rate": total_rate,
        "top3": top3,
        "top3_mode": top3_mode,
        "zscores": zscores,
        "big_merchants": sorted(big_merchants, key=lambda m: m[1], reverse=True),
        "top_merchants": top_merchants,
        "coverage_warning": coverage_warning,
    }
