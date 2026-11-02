"""입력 읽기•헤더 탐지•카드사 식별•거래 정제 (architecture.md 데이터 정제 규칙)와
재시도•시간 한도•원자적 쓰기 유틸 (retry_policy.md, memory_policy.md)."""
import csv
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

from merchant_normalizer import mask_holder, mask_text

HEADER_SCAN_ROWS = 15
RETRY_WAITS = (0.5, 1, 2)
CANCEL_STATUSES = {"취소", "취소전표접수"}
EXCEL_EPOCH = dt.date(1899, 12, 30)
SIGNATURE_FILE = "issuer_signatures.json"

COLUMN_ALIASES = {
    "date": ("승인일", "승인일자", "이용일", "이용일자", "거래일", "거래일자", "날짜"),
    "time": ("승인시각", "승인시간", "이용시간", "거래시간", "시각"),
    "holder": ("카드구분", "이용자", "사용자", "본인구분"),
    "merchant": ("가맹점명", "가맹점", "이용가맹점", "이용처", "사용처"),
    "amount": ("승인금액", "이용금액", "거래금액", "결제금액", "금액"),
    "usage": ("이용구분",),
    "installment": ("할부개월", "할부"),
    "status": ("승인구분", "거래상태", "상태"),
}
REQUIRED_COLUMNS = ("date", "merchant", "amount")

def signature_key(titles, columns):
    """제목 행은 이름 등 PII가 있을 수 있어 해시로만 남긴다 (memory_policy.md)."""
    title_hash = hashlib.sha256("/".join(titles).encode("utf-8")).hexdigest()[:16]
    return f"{title_hash}|{','.join(columns)}"


# 사용자가 확인해 준 카드사 헤더 어댑터 (memory/card/card-issuer-map.md)
BUILTIN_SIGNATURES = {
    signature_key(
        ["실시간 이용내역", "이용내역"],
        ["승인일", "승인시각", "카드구분", "가맹점명", "승인금액", "이용구분", "할부개월", "취소일", "승인구분"],
    ): "신한카드",
}


class TimeLimitExceeded(Exception):
    """요청당 시간 한도 초과 (종료코드 4)."""


class Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def remaining(self):
        return self.end - time.monotonic()

    def check(self, stage):
        if self.remaining() <= 0:
            raise TimeLimitExceeded(stage)


def with_retry(fn, exceptions, deadline):
    """일시적 I/O 오류만 0.5s → 1s → 2s 간격으로 최대 3회 재시도. 대기가 한도를 넘기면 바로 실패."""
    for wait in RETRY_WAITS + (None,):
        try:
            return fn()
        except exceptions:
            if wait is None or deadline.remaining() <= wait:
                raise
            time.sleep(wait)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def atomic_write_json(path, data, deadline):
    """임시 파일에 쓴 뒤 교체한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def write():
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    with_retry(write, (PermissionError, OSError), deadline)


def save_issuer_signatures(memory_dir, signatures, deadline):
    """사용자가 --issuer로 확인해 준 시그니처 → 카드사. 정정하면 덮어쓴다."""
    path = Path(memory_dir) / SIGNATURE_FILE
    data = read_json(path)
    data.update(signatures)
    atomic_write_json(path, data, deadline)


def _cell(value):
    return "" if value is None else str(value).strip()


def _read_rows(path):
    if path.suffix.lower() == ".csv":
        for encoding in ("utf-8-sig", "cp949"):
            try:
                with open(path, newline="", encoding=encoding) as f:
                    return [tuple(row) for row in csv.reader(f)]
            except UnicodeDecodeError:
                continue
        raise ValueError("csv 인코딩을 판별하지 못함")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return [tuple(row) for row in wb.active.iter_rows(values_only=True)]
    finally:
        wb.close()


def find_header(rows):
    """상단 15행 안에서 날짜•가맹점•금액 컬럼이 모두 있는 첫 행."""
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        names = [_cell(v) for v in row]
        columns = {}
        for key, aliases in COLUMN_ALIASES.items():
            for alias in aliases:
                if alias in names:
                    columns[key] = names.index(alias)
                    break
        if all(k in columns for k in REQUIRED_COLUMNS):
            return i, columns
    return None, None


def signature(rows, header_index):
    """(시그니처 키, 제목 줄 수, 컬럼명 목록)."""
    titles = [" ".join(_cell(v) for v in row if _cell(v)) for row in rows[:header_index]]
    titles = [t for t in titles if t]
    columns = [_cell(v) for v in rows[header_index] if _cell(v)]
    return signature_key(titles, columns), len(titles), columns


def parse_date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return EXCEL_EPOCH + dt.timedelta(days=int(value)) if 20000 < value < 80000 else None
    if isinstance(value, str):
        m = re.fullmatch(r"(\d{4})[-./]?\s*(\d{1,2})[-./]?\s*(\d{1,2})\.?", value.strip())
        if m:
            try:
                return dt.date(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                return None
    return None


def parse_time(value):
    if isinstance(value, dt.datetime):
        value = value.time()
    if isinstance(value, dt.time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, float) and 0 <= value < 1:
        seconds = round(value * 86400)
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return _cell(value)


def parse_amount(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    text = re.sub(r"[,\s원]", "", _cell(value))
    return int(round(float(text))) if re.fullmatch(r"-?\d+(\.\d+)?", text) else None


def parse_int(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = _cell(value)
    return int(text) if text.isdigit() else 0


def load_inputs(input_dir, memory_dir, deadline):
    """입력 폴더의 xlsx/csv를 읽어 정제된 거래 목록과 파일별 처리 정보를 돌려준다."""
    result = {"files": [], "unknown": [], "transactions": [], "warnings": [], "duplicates": 0}
    known = {**BUILTIN_SIGNATURES, **read_json(Path(memory_dir) / SIGNATURE_FILE)}
    paths = sorted(
        p for p in Path(input_dir).glob("*")
        if p.suffix.lower() in (".xlsx", ".csv") and not p.name.startswith("~$")
    )
    seen = set()
    for path in paths:
        deadline.check("입력 읽기")
        try:
            rows = with_retry(lambda: _read_rows(path), (PermissionError, OSError), deadline)
        except OSError:
            result["warnings"].append(f"{path.name}: 읽을 수 없어 건너뜀 (다른 프로그램에서 열려 있는지 확인)")
            continue
        except (zipfile.BadZipFile, InvalidFileException, ValueError, KeyError):
            result["warnings"].append(f"{path.name}: 파일 형식을 읽을 수 없어 건너뜀")
            continue

        header_index, columns = find_header(rows)
        if header_index is None:
            result["warnings"].append(f"{path.name}: 상단 {HEADER_SCAN_ROWS}행 안에서 헤더를 찾지 못해 건너뜀")
            continue

        sig, title_lines, header_columns = signature(rows, header_index)
        info = {
            "file": path.name, "header_row": header_index + 1, "signature": sig,
            "title_lines": title_lines, "columns": [mask_text(c) for c in header_columns],
            "issuer": known.get(sig), "parsed": 0, "cancel_excluded": 0, "non_data_excluded": 0,
        }
        if info["issuer"] is None:
            result["unknown"].append(info)

        for row in rows[header_index + 1:]:
            if all(_cell(v) == "" for v in row):
                continue

            def get(key, row=row):
                index = columns.get(key)
                return row[index] if index is not None and index < len(row) else None

            date, amount = parse_date(get("date")), parse_amount(get("amount"))
            if date is None or amount is None:  # 소계•합계 행 등
                info["non_data_excluded"] += 1
                continue
            status = _cell(get("status"))
            if status in CANCEL_STATUSES:  # 원거래와 취소가 한 쌍 → 둘 다 제외
                info["cancel_excluded"] += 1
                continue
            txn = {
                "date": date,
                "time": parse_time(get("time")),
                "holder": mask_holder(_cell(get("holder"))),
                "merchant": mask_text(_cell(get("merchant"))),
                "amount": amount,
                "usage": _cell(get("usage")),
                "installment": parse_int(get("installment")),
                "status": status,
                "file": path.name,
            }
            key = (txn["date"], txn["time"], txn["merchant"], txn["amount"], status)
            if key in seen:
                result["duplicates"] += 1
                continue
            seen.add(key)
            result["transactions"].append(txn)
            info["parsed"] += 1
        result["files"].append(info)
    return result
