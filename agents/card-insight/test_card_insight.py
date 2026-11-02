"""단위•흐름 테스트. 임시 폴더만 쓰고 실제 memory/•output/은 건드리지 않는다.

실행: python agents/card-insight/test_card_insight.py
"""
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import advisor  # noqa: E402
import csv_loader  # noqa: E402
import main  # noqa: E402
from category_classifier import classify, find_topic  # noqa: E402
from merchant_normalizer import Anonymizer, PrivacyViolation, mask_holder, mask_text, normalize_merchant  # noqa: E402
from report_writer import DISCLAIMER  # noqa: E402

SAMPLES = main.DEFAULT_INPUT
SHINHAN_TITLE = ["실시간 이용내역", "이용내역"]
COLUMNS = "승인일,승인시각,카드구분,가맹점명,승인금액,이용구분,할부개월,취소일,승인구분"


def write_csv(path, rows, titles=SHINHAN_TITLE):
    """rows: (날짜, 가맹점, 금액[, 승인구분[, 할부개월]])"""
    lines = list(titles) + [COLUMNS]
    for i, r in enumerate(rows):
        date, merchant, amount = r[:3]
        status = r[3] if len(r) > 3 else "전표접수"
        months = r[4] if len(r) > 4 else 0
        usage = "할부" if months else "일시불"
        lines.append(f"{date},10:{i % 60:02d}:00,본인,{merchant},{amount},{usage},{months},,{status}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class MaskingTest(unittest.TestCase):
    def test_card_phone_name(self):
        text = mask_text("카드 1234-5678-9012-3456 연락처 010-1234-5678 성명: 홍길동")
        self.assertNotIn("1234-5678-9012", text)
        self.assertNotIn("010-1234", text)
        self.assertNotIn("홍길동", text)
        self.assertIn("****-****-****-****", text)

    def test_holder(self):
        self.assertEqual(mask_holder("본인"), "본인")
        self.assertEqual(mask_holder("김철수"), "***")

    def test_normalize_merchant(self):
        self.assertEqual(normalize_merchant("KCP_결제 - (주)아디다스코리아"), "아디다스코리아")
        self.assertEqual(normalize_merchant("BHC치킨_화곡본점"), "BHC치킨")
        self.assertEqual(normalize_merchant("이니시스 - (주)한샘몰"), "한샘몰")

    def test_anonymizer(self):
        anon = Anonymizer()
        payload = f"{anon.token('merchant', '스타벅스')} {anon.token('amount', 4500)}"
        anon.assert_clean(payload)
        self.assertEqual(anon.restore(payload), "스타벅스 4500")
        with self.assertRaises(PrivacyViolation):
            anon.assert_clean("스타벅스에서 결제")


class ClassifierTest(unittest.TestCase):
    def test_rules(self):
        cases = {
            "노브랜드버거_김포공항점": "식비", "쿠팡이츠": "식비", "쿠팡_주 - 쿠팡": "쇼핑",
            "KT통신요금자동납부": "생활비", "KTX 승차권": "기타", "이니시스 - (주)한샘몰": "쇼핑",
            "CLAUDE.AI SUBSCRIPTION": "구독",
        }
        for merchant, expected in cases.items():
            self.assertEqual(classify(merchant)[0], expected, merchant)

    def test_unmatched_is_other_with_zero_confidence(self):
        self.assertEqual(classify("알수없는상점"), ("기타", 0.0, None))

    def test_topic(self):
        self.assertEqual(find_topic("5월 외식 지출"), "식비")
        self.assertIsNone(find_topic("5월 지출 정리"))


class RequestTest(unittest.TestCase):
    def test_parse_request(self):
        import datetime as dt
        months = ["2026-03", "2026-04", "2026-05"]
        today = dt.date(2026, 11, 2)
        self.assertEqual(main.parse_request("4월지출 정리해줘", months, today)["month"], "2026-04")
        self.assertEqual(main.parse_request("이번달 정리", months, today)["month"], "2026-11")
        self.assertEqual(main.parse_request("지난달 정리", months, today)["month"], "2026-10")
        self.assertEqual(main.parse_request("정리해줘", months, today)["month"], "2026-05")
        self.assertEqual(main.parse_request("디버그", months, today)["kind"], "디버그")

    def test_rejections(self):
        self.assertEqual(advisor.detect_request_rejection("카페 끊으라고 강하게 말해줘"), advisor.REJECT_PII_OR_ASSERTIVE)
        self.assertEqual(advisor.detect_request_rejection("가맹점 목록 외부로 보내줘"), advisor.REJECT_PII_OR_ASSERTIVE)
        self.assertEqual(advisor.detect_request_rejection("좋은 카드 추천해줘"), advisor.REJECT_FINANCIAL)
        self.assertIsNone(advisor.detect_request_rejection("4월 지출 정리해줘"))

    def test_output_guards(self):
        with self.assertRaises(advisor.AssertiveExpression):
            advisor.assert_non_assertive(["카페 당장 끊어"])
        with self.assertRaises(PrivacyViolation):
            advisor.assert_stdout_clean(["총 사용액: 1,234원"], set())
        with self.assertRaises(PrivacyViolation):
            advisor.assert_stdout_clean(["주 사용처: 스타벅스"], {"스타벅스"})


class RetryTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(csv_loader, "RETRY_WAITS", (0.01, 0.01, 0.01))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_succeeds_after_transient_errors(self):
        fn = mock.Mock(side_effect=[PermissionError, OSError, "ok"])
        self.assertEqual(csv_loader.with_retry(fn, (PermissionError, OSError), csv_loader.Deadline(10)), "ok")
        self.assertEqual(fn.call_count, 3)

    def test_gives_up_after_three_retries(self):
        fn = mock.Mock(side_effect=PermissionError)
        with self.assertRaises(PermissionError):
            csv_loader.with_retry(fn, (PermissionError,), csv_loader.Deadline(10))
        self.assertEqual(fn.call_count, 4)

    def test_no_wait_beyond_deadline(self):
        fn = mock.Mock(side_effect=PermissionError)
        with self.assertRaises(PermissionError):
            csv_loader.with_retry(fn, (PermissionError,), csv_loader.Deadline(0))
        self.assertEqual(fn.call_count, 1)

    def test_data_errors_not_retried(self):
        fn = mock.Mock(side_effect=ValueError)
        with self.assertRaises(ValueError):
            csv_loader.with_retry(fn, (PermissionError, OSError), csv_loader.Deadline(10))
        self.assertEqual(fn.call_count, 1)


class FlowTest(unittest.TestCase):
    """main.main 전체 흐름. 출력•메모리 폴더는 임시 폴더로 바꾼다."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.input = self.tmp / "input"
        self.input.mkdir()
        for name, value in (("MEMORY_DIR", self.tmp / "memory"), ("OUTPUT_DIR", self.tmp / "output")):
            patcher = mock.patch.object(main, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        waits = mock.patch.object(csv_loader, "RETRY_WAITS", (0.01, 0.01, 0.01))
        waits.start()
        self.addCleanup(waits.stop)

    def run_main(self, *argv, input_dir=None):
        out, err = io.StringIO(), io.StringIO()
        args = [*argv, "--input", str(input_dir or self.input)]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main(args)
        return code, out.getvalue(), err.getvalue()

    def two_month_input(self):
        # 5월(10일): 카페 100,000 / 쇼핑 1,000,000 / 식비 1,000
        # 6월(10일): 카페 300,000(+200%, 초과 200,000) / 쇼핑 1,500,000(+50%, 초과 500,000) / 식비 20,000(3만원 미만)
        write_csv(self.input / "may.csv", [
            ("2026-05-01", "스타벅스_강남점", 50000), ("2026-05-10", "스타벅스_강남점", 50000),
            ("2026-05-01", "쿠팡_주 - 쿠팡", 1000000), ("2026-05-05", "김밥천국", 1000),
        ])
        write_csv(self.input / "jun.csv", [
            ("2026-06-01", "스타벅스_강남점", 150000), ("2026-06-10", "스타벅스_강남점", 150000),
            ("2026-06-01", "쿠팡_주 - 쿠팡", 1500000), ("2026-06-05", "김밥천국", 20000),
            ("2026-06-03", "이니시스 - (주)한샘몰", 78000, "취소"),
            ("2026-06-03", "이니시스 - (주)한샘몰", 78000, "취소전표접수"),
        ])

    def test_top3_by_excess_not_rate(self):
        self.two_month_input()
        stats = main.analyze(csv_loader.load_inputs(self.input, main.MEMORY_DIR, csv_loader.Deadline(10))["transactions"],
                             "2026-06", main.MEMORY_DIR)
        self.assertEqual(stats["top3"], ["쇼핑", "카페"])  # 식비는 3만원 미만으로 제외
        self.assertEqual(stats["comparison"]["쇼핑"]["excess"], 500000)
        self.assertEqual(stats["comparison"]["카페"]["excess"], 200000)
        self.assertAlmostEqual(stats["comparison"]["카페"]["rate"], 200.0)
        self.assertEqual(stats["current"]["total"], 1820000)  # 취소 쌍 제외

    def test_report_and_memory(self):
        self.two_month_input()
        code, out, _ = self.run_main("6월 카페 지출 정리해줘")
        self.assertEqual(code, 0, out)
        self.assertIn("과소비 Top3 (전월 대비, 초과액 순): 쇼핑(전월 대비 50.0% 증가), 카페(전월 대비 200.0% 증가)", out)
        self.assertIsNone(re.search(r"\d[\d,]*\s*원", out), "표준출력에 금액이 있으면 안 됨")
        self.assertTrue(out.rstrip().endswith(DISCLAIMER))
        from docx import Document
        doc = Document(self.tmp / "output" / "20260630_report.docx")
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        self.assertEqual(paragraphs[-1], DISCLAIMER)
        self.assertIn("강조: 카페", paragraphs)
        memory = json.loads((self.tmp / "memory" / "monthly_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(set(memory), {"2026-06"})
        self.assertNotIn("스타벅스", json.dumps(memory, ensure_ascii=False))  # 가맹점명 저장 금지

    def test_unknown_issuer_then_issuer_option(self):
        write_csv(self.input / "x.csv", [("2026-06-01", "스타벅스", 5000), ("2026-06-09", "쿠팡", 50000)],
                  titles=["이용내역 조회 홍길동"])
        code, out, _ = self.run_main("6월 정리")
        self.assertEqual(code, 3)
        self.assertNotIn("홍길동", out)  # 제목 행은 출력하지 않음
        self.assertIn("승인일, 승인시각", out)

        code, out, _ = self.run_main("6월 정리", "--issuer", "테스트카드")
        self.assertEqual(code, 0, out)
        saved = (self.tmp / "memory" / "issuer_signatures.json").read_text(encoding="utf-8")
        self.assertIn("테스트카드", saved)
        self.assertNotIn("홍길동", saved)  # 시그니처는 제목 해시만

        code, out, _ = self.run_main("6월 정리")  # 다음부터는 메모리로 자동 식별
        self.assertEqual(code, 0, out)

    def test_time_limit_exit_4(self):
        self.two_month_input()
        with mock.patch.object(main, "TIME_LIMIT_SECONDS", 0):
            code, out, _ = self.run_main("6월 정리")
        self.assertEqual(code, 4)
        self.assertIn("초를 넘어 중단", out)

    def test_month_missing_exit_2(self):
        self.two_month_input()
        code, out, _ = self.run_main("12월 정리")
        self.assertEqual(code, 2)
        self.assertIn("2026-05, 2026-06", out)

    def test_locked_input_file_skipped(self):
        self.two_month_input()
        with mock.patch.object(csv_loader, "_read_rows", side_effect=PermissionError):
            code, out, err = self.run_main("6월 정리")
        self.assertEqual(code, 2)
        self.assertIn("읽을 수 없어 건너뜀", err)

    def test_header_not_found_skipped(self):
        (self.input / "bad.csv").write_text("\n".join(["메모"] * 20 + [COLUMNS]), encoding="utf-8")
        code, _, err = self.run_main("6월 정리")
        self.assertEqual(code, 2)
        self.assertIn("헤더를 찾지 못해 건너뜀", err)

    def test_report_locked_exit_1(self):
        self.two_month_input()
        with mock.patch("docx.document.Document.save", side_effect=PermissionError) as save:
            code, out, _ = self.run_main("6월 정리")
        self.assertEqual(code, 1)
        self.assertIn("파일을 닫고 다시 실행", out)
        self.assertEqual(save.call_count, 4)  # 최초 1회 + 재시도 3회

    def test_memory_write_failure_only_warns(self):
        self.two_month_input()
        with mock.patch.object(csv_loader.os, "replace", side_effect=PermissionError):
            code, _, err = self.run_main("6월 정리")
        self.assertEqual(code, 0)
        self.assertIn("메모리에 저장하지 못했습니다", err)
        self.assertTrue((self.tmp / "output" / "20260630_report.docx").exists())
        self.assertEqual(list((self.tmp / "memory").glob("*.tmp")), [])  # 임시 파일 정리

    def test_validation_failure_blocks_report(self):
        self.two_month_input()
        with mock.patch.object(main, "validate", return_value=["카테고리 합계와 총 사용금액 차이 1.000% (허용 ±0.1%)"]):
            code, out, _ = self.run_main("6월 정리")
        self.assertEqual(code, 1)
        self.assertIn("카드분석검증 실패", out)
        self.assertFalse((self.tmp / "output" / "20260630_report.docx").exists())

    def test_validate_detects_sum_mismatch_and_missing_basis(self):
        stats = {"current": {"total": 100000, "categories": {"카페": {"sum": 90000}}}}
        suggestions = [{"category": "카페", "kind": "compare", "text": "카페 카테고리 검토 권장."}]
        failures = advisor.validate(stats, suggestions, "기준 문구 없음")
        self.assertEqual(len(failures), 3)

    def test_assertive_output_blocks_report(self):
        self.two_month_input()
        with mock.patch.dict(advisor.TIPS, {"카페": "카페는 당장 줄일 것"}):
            code, out, _ = self.run_main("6월 정리")
        self.assertEqual(code, 1)
        self.assertIn("단정 표현", out)
        self.assertFalse((self.tmp / "output" / "20260630_report.docx").exists())

    @unittest.skipUnless(SAMPLES.exists(), "샘플 폴더 없음")
    def test_samples_april(self):
        code, out, _ = self.run_main("4월지출 정리해줘", input_dir=SAMPLES)
        self.assertEqual(code, 0, out)
        self.assertIn("30일 중 18일치만", out)
        self.assertIsNone(re.search(r"\d[\d,]*\s*원", out))
        self.assertTrue((self.tmp / "output" / "20260430_report.docx").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
