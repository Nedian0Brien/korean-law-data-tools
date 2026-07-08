"""
LawScraper 업데이트 모드 검증 테스트

테스트 대상:
  1. _find_existing_file  — 법령ID로 기존 파일 탐색
  2. process_laws (update_mode=True)
       a. 시행일자 동일 → 스킵
       b. 시행일자 변경 → 구 파일 삭제 + 신 파일 저장 + 스냅샷 복사
       c. 파일 없음 → 신규 저장
  3. run_update_mode — total 폴더 스캔 → API 조회 → 갱신
"""

import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile
import shutil
import pandas as pd
from pypdf import PdfReader, PdfWriter

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# rich 의존 없이 임포트하기 위한 최소 stub
import importlib

def _import_scraper():
    """LawScraper 모듈을 임포트 (rich, tty, termios 있는 환경 가정)"""
    from src.scraper.LawScraper import (
        LawScraper,
        CollectLive,
        CollectSettings,
        run_target_mode,
        run_revision_mode,
        run_update_mode,
        BASE_TOTAL_DIR,
        BASE_UPDATE_DIR,
        BASE_REVISION_DIR,
        _resolve_collect_annexes,
        _format_revision_window_skip_reason,
    )
    return (
        LawScraper,
        CollectLive,
        CollectSettings,
        run_target_mode,
        run_revision_mode,
        run_update_mode,
        BASE_TOTAL_DIR,
        BASE_UPDATE_DIR,
        BASE_REVISION_DIR,
        _resolve_collect_annexes,
        _format_revision_window_skip_reason,
    )


# ─────────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────────

def _make_scraper():
    LawScraper, *_ = _import_scraper()
    return LawScraper(oc_id="test_oc", request_delay=0)


def _make_law_json(law_id: str, law_name: str, efyd: str, dept: str = "테스트부처") -> dict:
    """법령 API 응답 JSON 구조 (최소 필드)"""
    return {
        "법령": {
            "기본정보": {
                "법령ID": law_id,
                "법령명_한글": law_name,
                "시행일자": efyd,
                "공포일자": efyd,
                "소관부처": {"content": dept},
            },
            "조문": {"조문단위": []},
            "부칙": {},
        }
    }


def _pdf_bytes(page_count: int = 1) -> bytes:
    from io import BytesIO

    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=100, height=100)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _pdf_page_count(path: str | Path) -> int:
    return len(PdfReader(str(path)).pages)


# ─────────────────────────────────────────────────────────────────
# 1. _find_existing_file
# ─────────────────────────────────────────────────────────────────

class TestFindExistingFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scraper = _make_scraper()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _touch(self, name: str) -> str:
        p = os.path.join(self.tmp, name)
        Path(p).touch()
        return p

    def test_finds_file_by_law_id(self):
        """법령ID가 일치하는 파일을 반환해야 한다"""
        self._touch("감사원법_20240101_001372.md")
        path, efyd = self.scraper._find_existing_file(self.tmp, "1372")
        self.assertIsNotNone(path)
        self.assertEqual(efyd, "20240101")

    def test_finds_file_by_zero_padded_id(self):
        """0-패딩된 ID도 매칭해야 한다"""
        self._touch("국가재정법_20230601_000042.md")
        path, efyd = self.scraper._find_existing_file(self.tmp, "42")
        self.assertIsNotNone(path)
        self.assertEqual(efyd, "20230601")

    def test_returns_none_when_no_match(self):
        """일치 파일 없으면 (None, None) 반환"""
        self._touch("국가재정법_20230601_000042.md")
        path, efyd = self.scraper._find_existing_file(self.tmp, "9999")
        self.assertIsNone(path)
        self.assertIsNone(efyd)

    def test_returns_none_for_missing_dir(self):
        """존재하지 않는 디렉토리도 (None, None) 반환"""
        path, efyd = self.scraper._find_existing_file("/nonexistent/dir", "001372")
        self.assertIsNone(path)
        self.assertIsNone(efyd)

    def test_ignores_non_md_files(self):
        """본문 저장 파일이 아닌 파일은 무시해야 한다"""
        self._touch("감사원법_20240101_001372.json")
        path, efyd = self.scraper._find_existing_file(self.tmp, "1372")
        self.assertIsNone(path)

    def test_finds_pdf_file_by_law_id(self):
        """PDF 원문 저장본도 법령ID로 기존 파일 탐색 대상이어야 한다"""
        self._touch("감사원법_20240101_001372.pdf")
        path, efyd = self.scraper._find_existing_file(self.tmp, "1372")
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith(".pdf"))
        self.assertEqual(efyd, "20240101")

    def test_filename_with_multiple_underscores_in_name(self):
        """법령명에 밑줄이 포함되어도 ID가 올바르게 추출되어야 한다"""
        self._touch("가축_전염병_예방법_20231001_456789.md")
        path, efyd = self.scraper._find_existing_file(self.tmp, "456789")
        self.assertIsNotNone(path)
        self.assertEqual(efyd, "20231001")


class TestPdfOriginalResolution(unittest.TestCase):
    def setUp(self):
        self.scraper = _make_scraper()

    def test_resolves_lsi_seq_from_law_page_when_list_search_misses(self):
        """목록 검색에서 lsiSeq를 못 찾으면 웹 본문 화면 hidden input으로 보강해야 한다"""
        class Response:
            text = '<html><input type="hidden" id="lsiSeq" value="267531" /></html>'

            def raise_for_status(self):
                return None

        session = MagicMock()
        session.get.return_value = Response()
        with patch.object(self.scraper, "fetch_law_list", return_value=pd.DataFrame()):
            lsi_seq = self.scraper._resolve_lsi_seq_for_pdf(
                session,
                "000593",
                "대한민국과 아메리카합중국 간의 상호방위조약 제4조에 의한 시설과 구역 및 대한민국에서의 합중국군대의 지위에 관한 협정의 실시에 따른 관세법 등의 임시특례에 관한 법률",
                "20250101",
            )

        self.assertEqual(lsi_seq, "267531")
        session.get.assert_called_once()
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["lsId"], "000593")
        self.assertEqual(kwargs["params"]["efYd"], "20250101")


class TestRevisionSkipReason(unittest.TestCase):
    def test_skip_reason_includes_effective_date_and_today(self):
        """revision 스킵 로그는 시행일자와 현재 날짜를 함께 보여줘야 한다"""
        *_, format_skip_reason = _import_scraper()
        reason = format_skip_reason("20260701", "20260107", today="20260414")
        self.assertIn("시행일자 20260701", reason)
        self.assertIn("기준일자 20260107", reason)
        self.assertIn("현재일 20260414", reason)


# ─────────────────────────────────────────────────────────────────
# 2. process_laws (update_mode)
# ─────────────────────────────────────────────────────────────────

class TestProcessLawsUpdateMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.total_dir = os.path.join(self.tmp, "total")
        self.snap_dir = os.path.join(self.tmp, "snapshot")
        os.makedirs(self.total_dir)
        self.scraper = _make_scraper()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _make_df(self, law_id, law_name, efyd, dept="테스트부처"):
        return pd.DataFrame([{
            "법령ID": law_id,
            "법령명한글": law_name,
            "시행일자": efyd,
            "소관부처명": dept,
        }])

    def _mock_api_response(self, law_id, law_name, efyd, dept="테스트부처"):
        """_make_request가 반환할 bytes 객체 생성"""
        js = _make_law_json(law_id, law_name, efyd, dept)
        return json.dumps(js, ensure_ascii=False).encode("utf-8")

    # ── 2-a. 시행일자 동일 → 스킵 ───────────────────────────────────
    def test_skips_when_efyd_unchanged(self):
        """시행일자가 동일하면 파일을 건드리지 않고 스킵해야 한다"""
        dept_dir = os.path.join(self.total_dir, "테스트부처")
        os.makedirs(dept_dir)
        existing = os.path.join(dept_dir, "감사원법_20240101_001372.md")
        Path(existing).write_text("기존내용", encoding="utf-8")

        df = self._make_df("001372", "감사원법", "20240101")
        with patch.object(self.scraper, "_make_request") as mock_req:
            self.scraper.process_laws(
                session=MagicMock(), df=df,
                total_dir=self.total_dir,
                update_mode=True,
                update_snapshot_dir=self.snap_dir,
            )
            # API 호출이 없어야 한다 (스킵)
            mock_req.assert_not_called()

        # 기존 파일 내용이 유지되어야 한다
        self.assertEqual(
            Path(existing).read_text(encoding="utf-8"), "기존내용"
        )

    def test_skips_when_efyd_unchanged_but_collects_annex_if_enabled(self):
        """시행일자 동일 스킵은 유지되지만 별표 저장 옵션이면 별지 조회는 수행"""
        dept_dir = os.path.join(self.total_dir, "테스트부처")
        os.makedirs(dept_dir)
        existing = os.path.join(dept_dir, "감사원법_20240101_001372.md")
        Path(existing).write_text("기존내용", encoding="utf-8")

        df = self._make_df("1372", "감사원법", "20240101")
        with patch.object(self.scraper, "fetch_and_save_annexes", return_value=2) as mock_annex:
            with patch.object(self.scraper, "_make_request") as mock_req:
                self.scraper.process_laws(
                    session=MagicMock(), df=df,
                    total_dir=self.total_dir,
                    collect_annexes=True,
                    update_mode=True,
                    update_snapshot_dir=self.snap_dir,
                )
                mock_annex.assert_called_once()
                mock_req.assert_not_called()

    def test_collects_annex_when_saving_skipped_in_full_mode(self):
        """중복 파일이 있어도 별표 옵션이면 별표를 조회해 저장한다"""
        dept_dir = os.path.join(self.total_dir, "테스트부처")
        os.makedirs(dept_dir)
        existing = os.path.join(dept_dir, "감사원법_20240101_001372.md")
        Path(existing).write_text("기존내용", encoding="utf-8")

        df = self._make_df("1372", "감사원법", "20240101")
        with patch.object(self.scraper, "fetch_and_save_annexes", return_value=1) as mock_annex:
            with patch.object(self.scraper, "_make_request") as mock_req:
                self.scraper.process_laws(
                    session=MagicMock(), df=df,
                    total_dir=self.total_dir,
                    collect_annexes=True,
                )
                mock_annex.assert_called_once()
                mock_req.assert_not_called()

    def test_pdf_merge_mode_refreshes_existing_pdf_before_merging(self):
        """기존 PDF가 있어도 merge 모드는 깨끗한 본문 PDF를 다시 받은 뒤 별표를 병합한다"""
        _, _, CollectSettings, *_ = _import_scraper()
        dept_dir = os.path.join(self.total_dir, "테스트부처")
        os.makedirs(dept_dir)
        existing = os.path.join(dept_dir, "감사원법_20240101_001372.pdf")
        Path(existing).write_bytes(_pdf_bytes(3))

        df = self._make_df("1372", "감사원법", "20240101")
        df["법령일련번호"] = "188135"
        settings = CollectSettings(
            save_json=True,
            save_annexes=True,
            output_format="pdf",
            annex_pdf_mode="merge",
        )
        annexes = [
            {
                "별표명": "별표 1",
                "별표번호": "1",
                "별표서식PDF파일링크": "/pdf/annex1.pdf",
            }
        ]
        api_bytes = self._mock_api_response("001372", "감사원법", "20240101")

        def mock_request(session, method, url, params=None):
            if url.endswith("annex1.pdf"):
                return _pdf_bytes(2)
            return api_bytes

        with patch.object(self.scraper, "_download_original_pdf", return_value=_pdf_bytes(1)) as mock_pdf:
            with patch.object(self.scraper, "_fetch_annex_list", return_value=annexes):
                with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                    self.scraper.process_laws(
                        session=MagicMock(),
                        df=df,
                        total_dir=self.total_dir,
                        collect_annexes=True,
                        settings=settings,
                    )

        mock_pdf.assert_called_once()
        self.assertEqual(_pdf_page_count(existing), 3)
        self.assertFalse(list(Path(dept_dir).rglob("*_별표")), "merge 모드는 별도 별표 폴더를 만들지 않아야 한다")

    def test_pdf_merge_mode_skips_existing_completed_manifest_without_refresh(self):
        """완성 manifest가 있으면 기존 병합 PDF를 API/본문 재다운로드 없이 스킵한다"""
        _, _, CollectSettings, *_ = _import_scraper()
        dept_dir = os.path.join(self.total_dir, "테스트부처")
        os.makedirs(dept_dir)
        existing = os.path.join(dept_dir, "감사원법_20240101_001372.pdf")
        Path(existing).write_bytes(_pdf_bytes(3))

        annexes = [
            {
                "별표명": "별표 1",
                "별표번호": "1",
                "별표서식PDF파일링크": "/pdf/annex1.pdf",
            }
        ]
        manifest_path = f"{existing}.merge.json"
        Path(manifest_path).write_text(
            json.dumps(
                {
                    "status": "complete",
                    "law_id": "001372",
                    "annexes": self.scraper._pdf_merge_manifest_entries(annexes),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        df = self._make_df("1372", "감사원법", "20240101")
        df["법령일련번호"] = "188135"
        settings = CollectSettings(
            save_json=True,
            save_annexes=True,
            output_format="pdf",
            annex_pdf_mode="merge",
        )

        with patch.object(self.scraper, "_fetch_annex_list", return_value=annexes):
            with patch.object(self.scraper, "_make_request") as mock_request:
                with patch.object(self.scraper, "_download_original_pdf") as mock_pdf:
                    self.scraper.process_laws(
                        session=MagicMock(),
                        df=df,
                        total_dir=self.total_dir,
                        collect_annexes=True,
                        settings=settings,
                    )

        mock_request.assert_not_called()
        mock_pdf.assert_not_called()
        self.assertEqual(_pdf_page_count(existing), 3)

    # ── 2-b. 시행일자 변경 → 구 파일 삭제 + 신 파일 저장 ──────────
    def test_updates_when_efyd_changed(self):
        """시행일자가 변경되면 구 파일 삭제 후 새 파일 저장해야 한다"""
        dept_dir = os.path.join(self.total_dir, "테스트부처")
        os.makedirs(dept_dir)
        old_md = os.path.join(dept_dir, "감사원법_20230601_001372.md")
        old_js = os.path.join(dept_dir, "감사원법_20230601_001372.json")
        Path(old_md).write_text("옛날내용", encoding="utf-8")
        Path(old_js).write_text("{}", encoding="utf-8")

        new_efyd = "20240101"
        df = self._make_df("1372", "감사원법", new_efyd)
        api_bytes = self._mock_api_response("001372", "감사원법", new_efyd)

        with patch.object(self.scraper, "_make_request", return_value=api_bytes):
            self.scraper.process_laws(
                session=MagicMock(), df=df,
                total_dir=self.total_dir,
                update_mode=True,
                update_snapshot_dir=self.snap_dir,
            )

        # 구 파일 삭제 확인
        self.assertFalse(os.path.exists(old_md), "구 .md 파일이 삭제되어야 한다")
        self.assertFalse(os.path.exists(old_js), "구 .json 파일이 삭제되어야 한다")

        # 신 파일 존재 확인 (법령ID는 df에 전달된 "1372" 그대로 파일명에 사용됨)
        dept_files = os.listdir(dept_dir)
        new_mds = [f for f in dept_files if new_efyd in f and f.endswith(".md")]
        self.assertTrue(new_mds, f"새 시행일자({new_efyd})가 포함된 .md 파일이 저장되어야 한다 (found: {dept_files})")

    def test_snapshot_copied_on_update(self):
        """갱신 시 스냅샷 디렉토리에 파일이 복사되어야 한다"""
        dept_dir = os.path.join(self.total_dir, "테스트부처")
        os.makedirs(dept_dir)
        Path(os.path.join(dept_dir, "감사원법_20230601_001372.md")).write_text("구내용")

        new_efyd = "20240101"
        df = self._make_df("1372", "감사원법", new_efyd)
        api_bytes = self._mock_api_response("001372", "감사원법", new_efyd)

        os.makedirs(self.snap_dir)
        with patch.object(self.scraper, "_make_request", return_value=api_bytes):
            self.scraper.process_laws(
                session=MagicMock(), df=df,
                total_dir=self.total_dir,
                update_mode=True,
                update_snapshot_dir=self.snap_dir,
            )

        snap_dept = os.path.join(self.snap_dir, "테스트부처")
        self.assertTrue(os.path.isdir(snap_dept), "스냅샷 부처 폴더가 생성되어야 한다")
        snap_files = os.listdir(snap_dept)
        self.assertTrue(
            any(new_efyd in f for f in snap_files),
            f"스냅샷에 새 시행일자 파일이 있어야 한다 (found: {snap_files})"
        )

    # ── 2-c. 파일 없음 → 신규 저장 ────────────────────────────────
    def test_saves_new_law_when_not_existing(self):
        """기존 파일이 없으면 신규 저장해야 한다"""
        new_efyd = "20240601"
        df = self._make_df("5000", "새로운법", new_efyd)
        api_bytes = self._mock_api_response("005000", "새로운법", new_efyd)

        with patch.object(self.scraper, "_make_request", return_value=api_bytes):
            self.scraper.process_laws(
                session=MagicMock(), df=df,
                total_dir=self.total_dir,
                update_mode=True,
            )

        dept_dir = os.path.join(self.total_dir, "테스트부처")
        files = os.listdir(dept_dir)
        self.assertTrue(any("5000" in f or "005000" in f for f in files),
                        f"새 파일이 저장되어야 한다 (found: {files})")

    def test_old_annex_dir_removed_on_update(self):
        """시행일자 변경 시 구 별표 폴더도 삭제되어야 한다"""
        dept_dir = os.path.join(self.total_dir, "테스트부처")
        os.makedirs(dept_dir)
        old_stem = "감사원법_20230601_001372"
        Path(os.path.join(dept_dir, f"{old_stem}.md")).write_text("구내용")
        old_annex = os.path.join(dept_dir, f"{old_stem}_별표")
        os.makedirs(old_annex)
        Path(os.path.join(old_annex, "별표1.md")).write_text("별표내용")

        new_efyd = "20240101"
        df = self._make_df("1372", "감사원법", new_efyd)
        api_bytes = self._mock_api_response("001372", "감사원법", new_efyd)

        with patch.object(self.scraper, "_make_request", return_value=api_bytes):
            self.scraper.process_laws(
                session=MagicMock(), df=df,
                total_dir=self.total_dir,
                update_mode=True,
            )

        self.assertFalse(os.path.isdir(old_annex), "구 별표 폴더가 삭제되어야 한다")


class TestAnnexOriginalFileMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scraper = _make_scraper()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_pdf_mode_saves_annex_pdf_original_without_markdown_parsing(self):
        """PDF 모드 별표/별지는 PDF 링크를 우선 사용해 원문 bytes를 .pdf로 저장해야 한다"""
        annexes = [
            {
                "별표명": "수수료 서식",
                "별표번호": "1",
                "별표서식파일링크": "/hwp/annex.hwp",
                "별표서식PDF파일링크": "/pdf/annex.pdf",
            }
        ]

        with patch.object(self.scraper, "_fetch_annex_list", return_value=annexes):
            with patch.object(self.scraper, "_parse_annex_file") as mock_parse:
                with patch.object(self.scraper, "_make_request", return_value=b"%PDF-1.4\nannex") as mock_request:
                    saved = self.scraper.fetch_and_save_annexes(
                        MagicMock(),
                        "테스트법",
                        "001372",
                        self.tmp,
                        "테스트법_20240101_001372",
                        output_format="pdf",
                    )

        self.assertEqual(saved, 1)
        mock_parse.assert_not_called()
        self.assertIn("/pdf/annex.pdf", mock_request.call_args.args[2])

        saved_pdfs = sorted(Path(self.tmp).rglob("*.pdf"))
        saved_mds = sorted(Path(self.tmp).rglob("*.md"))
        self.assertEqual(len(saved_pdfs), 1, f"별표 원문 PDF가 저장되어야 한다: {saved_pdfs}")
        self.assertFalse(saved_mds, f"PDF 모드 별표는 markdown으로 저장하지 않아야 한다: {saved_mds}")
        self.assertEqual(saved_pdfs[0].read_bytes(), b"%PDF-1.4\nannex")

    def test_pdf_mode_preserves_non_pdf_annex_original_when_pdf_link_missing(self):
        """PDF 링크가 없으면 별표 원문 파일 링크를 파싱하지 않고 원래 확장자로 보존한다"""
        annexes = [
            {
                "별표명": "원본 한글 서식",
                "별표번호": "2",
                "별표서식파일링크": "/hwp/annex.hwpx",
            }
        ]

        with patch.object(self.scraper, "_fetch_annex_list", return_value=annexes):
            with patch.object(self.scraper, "_parse_annex_file") as mock_parse:
                with patch.object(self.scraper, "_make_request", return_value=b"HWPX-BYTES"):
                    saved = self.scraper.fetch_and_save_annexes(
                        MagicMock(),
                        "테스트법",
                        "001372",
                        self.tmp,
                        "테스트법_20240101_001372",
                        output_format="pdf",
                    )

        self.assertEqual(saved, 1)
        mock_parse.assert_not_called()
        saved_hwpxs = sorted(Path(self.tmp).rglob("*.hwpx"))
        self.assertEqual(len(saved_hwpxs), 1, f"PDF 링크가 없으면 원본 확장자로 보존해야 한다: {saved_hwpxs}")
        self.assertEqual(saved_hwpxs[0].read_bytes(), b"HWPX-BYTES")

    def test_pdf_merge_mode_appends_annex_pdfs_into_single_body_pdf(self):
        """merge 모드는 본문 PDF 뒤에 별표 PDF를 붙이고 별도 별표 폴더를 만들지 않는다"""
        body_path = Path(self.tmp) / "테스트법_20240101_001372.pdf"
        body_path.write_bytes(_pdf_bytes(1))
        annexes = [
            {
                "별표명": "별표 1",
                "별표번호": "1",
                "별표서식PDF파일링크": "/pdf/annex1.pdf",
            },
            {
                "별표명": "별지 2",
                "별표번호": "2",
                "별표서식PDF파일링크": "/pdf/annex2.pdf",
            },
        ]

        def mock_request(session, method, url, params=None):
            if url.endswith("annex1.pdf"):
                return _pdf_bytes(2)
            if url.endswith("annex2.pdf"):
                return _pdf_bytes(1)
            raise AssertionError(f"unexpected url: {url}")

        with patch.object(self.scraper, "_fetch_annex_list", return_value=annexes):
            with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                merged = self.scraper.fetch_and_merge_annex_pdfs(
                    MagicMock(),
                    "테스트법",
                    "001372",
                    str(body_path),
                )

        self.assertEqual(merged, 2)
        self.assertEqual(_pdf_page_count(body_path), 4)
        manifest = json.loads(Path(f"{body_path}.merge.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["law_id"], "001372")
        self.assertEqual(len(manifest["annexes"]), 2)
        self.assertFalse(list(Path(self.tmp).rglob("*_별표")), "merge 모드에서는 별표 폴더를 만들지 않아야 한다")

    def test_pdf_merge_mode_orders_annexes_by_legal_number(self):
        """별표 1의3 같은 번호는 문자열/응답 순서가 아니라 법령 번호 순서로 병합해야 한다"""
        body_path = Path(self.tmp) / "테스트법_20240101_001372.pdf"
        body_path.write_bytes(_pdf_bytes(1))
        annexes = [
            {
                "별표명": "별표 1의3",
                "별표번호": "1의3",
                "관련법령ID": "001372",
                "별표서식PDF파일링크": "/pdf/annex1_3.pdf",
            },
            {
                "별표명": "별표 1",
                "별표번호": "1",
                "관련법령ID": "001372",
                "별표서식PDF파일링크": "/pdf/annex1.pdf",
            },
            {
                "별표명": "별표 1의2",
                "별표번호": "1의2",
                "관련법령ID": "001372",
                "별표서식PDF파일링크": "/pdf/annex1_2.pdf",
            },
            {
                "별표명": "별표 1의5",
                "별표번호": "1의5",
                "관련법령ID": "001372",
                "별표서식PDF파일링크": "/pdf/annex1_5.pdf",
            },
        ]
        requested_urls = []

        def mock_request(session, method, url, params=None):
            requested_urls.append(url)
            return _pdf_bytes(1)

        with patch.object(self.scraper, "_fetch_annex_list", return_value=self.scraper._sort_annexes(annexes)):
            with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                merged = self.scraper.fetch_and_merge_annex_pdfs(
                    MagicMock(),
                    "테스트법",
                    "001372",
                    str(body_path),
                )

        self.assertEqual(merged, 4)
        self.assertEqual(_pdf_page_count(body_path), 5)
        self.assertEqual(
            requested_urls,
            [
                "https://www.law.go.kr/pdf/annex1.pdf",
                "https://www.law.go.kr/pdf/annex1_2.pdf",
                "https://www.law.go.kr/pdf/annex1_3.pdf",
                "https://www.law.go.kr/pdf/annex1_5.pdf",
            ],
        )

    def test_pdf_merge_mode_preserves_unmergeable_original_without_failing(self):
        """merge 모드에서도 PDF 병합 불가 별표는 원문으로 보존하고 수집 실패로 만들지 않는다"""
        body_path = Path(self.tmp) / "테스트법_20240101_001372.pdf"
        body_path.write_bytes(_pdf_bytes(1))
        annexes = [
            {
                "별표명": "한글 전용 별표",
                "별표번호": "1",
                "별표서식파일링크": "/hwp/annex.hwp",
            }
        ]

        with patch.object(self.scraper, "_fetch_annex_list", return_value=annexes):
            with patch.object(self.scraper, "_make_request", return_value=b"HWP-DATA"):
                merged = self.scraper.fetch_and_merge_annex_pdfs(
                    MagicMock(),
                    "테스트법",
                    "001372",
                    str(body_path),
                )

        self.assertEqual(merged, 0)
        self.assertEqual(_pdf_page_count(body_path), 1)
        saved = sorted(Path(self.tmp).rglob("*.hwp"))
        self.assertEqual(len(saved), 1, f"병합 불가 원문이 별도 보존되어야 한다: {saved}")
        self.assertEqual(saved[0].read_bytes(), b"HWP-DATA")
        manifest = json.loads(Path(f"{body_path}.merge.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "complete_with_warnings")
        self.assertEqual(len(manifest["unmerged_annexes"]), 1)
        self.assertEqual(len(manifest["skipped_annexes"]), 0)

    def test_pdf_merge_mode_skips_deleted_annex_without_pdf_link(self):
        """삭제된 별표는 PDF/HWP 다운로드 없이 무시하고 merge 전체를 실패시키지 않는다"""
        body_path = Path(self.tmp) / "테스트법_20240101_001372.pdf"
        body_path.write_bytes(_pdf_bytes(1))
        annexes = [
            {
                "별표명": "삭제 <2018. 1. 2.>",
                "별표번호": "2",
                "별표서식파일링크": "/hwp/deleted.hwp",
            }
        ]

        with patch.object(self.scraper, "_fetch_annex_list", return_value=annexes):
            with patch.object(self.scraper, "_make_request") as mock_request:
                merged = self.scraper.fetch_and_merge_annex_pdfs(
                    MagicMock(),
                    "테스트법",
                    "001372",
                    str(body_path),
                )

        self.assertEqual(merged, 0)
        mock_request.assert_not_called()
        self.assertFalse(list(Path(self.tmp).rglob("*_별표")), "삭제 항목은 별도 원문도 만들지 않는다")
        manifest = json.loads(Path(f"{body_path}.merge.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "complete_with_warnings")

    def test_pdf_merge_mode_treats_broken_annex_download_as_warning(self):
        """별표 PDF 링크가 404 등으로 실패해도 법령 전체 저장 실패로 올리지 않는다"""
        body_path = Path(self.tmp) / "테스트법_20240101_001372.pdf"
        body_path.write_bytes(_pdf_bytes(1))
        annexes = [
            {
                "별표명": "오래된 별표",
                "별표번호": "1",
                "별표서식PDF파일링크": "/pdf/missing.pdf",
                "별표서식파일링크": "/hwp/missing.hwp",
            }
        ]

        with patch.object(self.scraper, "_fetch_annex_list", return_value=annexes):
            with patch.object(self.scraper, "_make_request", return_value=None):
                merged = self.scraper.fetch_and_merge_annex_pdfs(
                    MagicMock(),
                    "테스트법",
                    "001372",
                    str(body_path),
                )

        self.assertEqual(merged, 0)
        self.assertEqual(_pdf_page_count(body_path), 1)
        manifest = json.loads(Path(f"{body_path}.merge.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "complete_with_warnings")
        self.assertEqual(len(manifest["unmerged_annexes"]), 0)
        self.assertEqual(len(manifest["skipped_annexes"]), 1)


# ─────────────────────────────────────────────────────────────────
# 3. run_update_mode — total 폴더 스캔 + API 조회 통합
# ─────────────────────────────────────────────────────────────────

class TestRunUpdateMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scraper = _make_scraper()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _patch_dirs(self, total_dir, update_dir):
        """BASE_TOTAL_DIR / BASE_UPDATE_DIR 를 임시 경로로 교체"""
        import src.scraper.LawScraper as mod
        self._orig_total = mod.BASE_TOTAL_DIR
        self._orig_update = mod.BASE_UPDATE_DIR
        mod.BASE_TOTAL_DIR = total_dir
        mod.BASE_UPDATE_DIR = update_dir

    def _restore_dirs(self):
        import src.scraper.LawScraper as mod
        mod.BASE_TOTAL_DIR = self._orig_total
        mod.BASE_UPDATE_DIR = self._orig_update

    def test_aborts_when_total_dir_missing(self):
        """total 폴더가 없으면 아무것도 하지 않아야 한다"""
        _, _, _, _, _, run_update_mode, *_ = _import_scraper()
        total_dir = os.path.join(self.tmp, "nonexistent_total")
        update_dir = os.path.join(self.tmp, "update")
        self._patch_dirs(total_dir, update_dir)
        try:
            with patch.object(self.scraper, "_make_request") as mock_req:
                run_update_mode(self.scraper, MagicMock(), collect_annexes=False)
                mock_req.assert_not_called()
        finally:
            self._restore_dirs()

    def test_collects_law_ids_from_total_dir(self):
        """total 폴더의 .md 파일에서 법령ID를 올바르게 수집해야 한다"""
        total_dir = os.path.join(self.tmp, "total")
        update_dir = os.path.join(self.tmp, "update")
        dept_dir = os.path.join(total_dir, "테스트부처")
        os.makedirs(dept_dir)
        Path(os.path.join(dept_dir, "감사원법_20240101_001372.md")).write_text("내용")
        Path(os.path.join(dept_dir, "국가재정법_20231001_000042.md")).write_text("내용")

        _, _, _, _, _, run_update_mode, *_ = _import_scraper()
        self._patch_dirs(total_dir, update_dir)
        try:
            called_ids = []

            def mock_request(session, method, url, params=None):
                if params and 'ID' in params:
                    called_ids.append(params['ID'])
                # 빈 응답 → rows 없음 → 종료
                return None

            with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                run_update_mode(self.scraper, MagicMock(), collect_annexes=False)

            self.assertIn("001372", called_ids, "001372이 조회되어야 한다")
            self.assertIn("000042", called_ids, "000042이 조회되어야 한다")
        finally:
            self._restore_dirs()

    def test_no_snapshot_dir_when_nothing_updated(self):
        """갱신 없으면 snapshot 폴더가 생성되지 않거나 비어서 삭제되어야 한다"""
        total_dir = os.path.join(self.tmp, "total")
        update_dir = os.path.join(self.tmp, "update")
        dept_dir = os.path.join(total_dir, "테스트부처")
        os.makedirs(dept_dir)
        Path(os.path.join(dept_dir, "감사원법_20240101_001372.md")).write_text("내용")

        _, _, _, _, _, run_update_mode, *_ = _import_scraper()
        self._patch_dirs(total_dir, update_dir)
        try:
            # API가 동일한 시행일자를 반환 → 갱신 없음
            api_response = json.dumps(_make_law_json(
                "001372", "감사원법", "20240101"
            ), ensure_ascii=False).encode("utf-8")

            with patch.object(self.scraper, "_make_request", return_value=api_response):
                run_update_mode(self.scraper, MagicMock(), collect_annexes=False)

            # update 폴더가 없거나 빈 날짜 폴더가 삭제되어야 함
            if os.path.isdir(update_dir):
                subdirs = [d for d in os.listdir(update_dir)
                           if os.path.isdir(os.path.join(update_dir, d))]
                for sd in subdirs:
                    snap = os.path.join(update_dir, sd)
                    files = list(Path(snap).rglob("*"))
                    self.assertEqual(len(files), 0,
                                     f"갱신 없으면 snapshot 폴더가 비어야 한다: {files}")
        finally:
            self._restore_dirs()


# ─────────────────────────────────────────────────────────────────
# 4. 설정/로그 동작
# ─────────────────────────────────────────────────────────────────

class TestAnnexSettingsAndTuiLogging(unittest.TestCase):
    def test_cli_override_absent_uses_saved_setting(self):
        """CLI 오버라이드가 없으면 저장된 설정값을 따라야 한다"""
        _, _, CollectSettings, _, _, _, _, _, _, resolve_collect_annexes, _ = _import_scraper()

        settings = CollectSettings(save_json=True, save_annexes=False, mode="target")
        self.assertFalse(resolve_collect_annexes(settings, None))

    def test_cli_override_can_force_annex_collection_on(self):
        """CLI 오버라이드가 있으면 저장된 설정보다 우선해야 한다"""
        _, _, CollectSettings, _, _, _, _, _, _, resolve_collect_annexes, _ = _import_scraper()

        settings = CollectSettings(save_json=True, save_annexes=False, mode="target")
        self.assertTrue(resolve_collect_annexes(settings, True))

    def test_tui_log_shows_saved_path(self):
        """TUI 저장 로그에 저장 경로가 표시되어야 한다"""
        _, CollectLive, CollectSettings, _, _, _, _, _, _, _, _ = _import_scraper()

        live = CollectLive(total=1, settings=CollectSettings())
        with patch.object(live, "_refresh", return_value=None):
            live.log_saved(
                "감사원법",
                "20240101",
                "/home/ubuntu/project/data_job/data/laws_storage/total/감사원/감사원법_20240101_001372.md",
            )

        self.assertIn(
            "감사원법_20240101_001372.md",
            live._log[-1].plain,
            "저장된 파일 경로가 로그에 보여야 한다",
        )


class TestSettingsUiModeSwitching(unittest.TestCase):
    def test_shift_tab_cycles_mode_in_settings_ui(self):
        """설정 화면에서 Shift+Tab으로 수집 모드를 다음 값으로 전환해야 한다"""
        import src.scraper.LawScraper as mod

        tmp = tempfile.mkdtemp()
        settings_path = os.path.join(tmp, "settings.json")
        orig_settings_file = mod.SETTINGS_FILE
        mod.SETTINGS_FILE = settings_path
        try:
            current = mod.CollectSettings(save_json=True, save_annexes=True, mode="target")

            with patch.object(mod.console, "clear", return_value=None), \
                 patch.object(mod.console, "print", return_value=None), \
                 patch.object(mod, "_getch", side_effect=["DOWN", "DOWN", "DOWN", "DOWN", "SHIFT_TAB", "\r"]):
                updated = mod.show_settings_ui(current)

            self.assertIsNotNone(updated)
            self.assertEqual(updated.mode, "revision")
            self.assertTrue(os.path.exists(settings_path), "설정 저장 파일이 생성되어야 한다")
        finally:
            mod.SETTINGS_FILE = orig_settings_file
            shutil.rmtree(tmp)


class TestExitCommand(unittest.TestCase):
    def test_exit_quits_on_menu_selection(self):
        """일시정지 메뉴에서 종료를 선택하면 바로 종료되어야 한다"""
        _, CollectLive, CollectSettings, _, _, _, _, _, _, _, _ = _import_scraper()

        live = CollectLive(total=1, settings=CollectSettings())
        with patch("src.scraper.LawScraper._getch", side_effect=["DOWN", "DOWN", "\r"]):
            with patch("src.scraper.LawScraper.console.clear", return_value=None):
                with patch("src.scraper.LawScraper.console.print", return_value=None):
                    with self.assertRaises(SystemExit):
                        live._enter_cmd_loop()

    def test_esc_pauses_collection(self):
        """수집 중 Esc 입력이 들어오면 일시정지 상태가 되어야 한다"""
        _, CollectLive, CollectSettings, _, _, _, _, _, _, _, _ = _import_scraper()

        live = CollectLive(total=1, settings=CollectSettings())
        with patch("src.scraper.LawScraper._getch_nonblocking", return_value="ESC"):
            with patch.object(live, "_enter_cmd_loop", return_value=None) as mock_loop:
                with patch.object(live._live, "stop", return_value=None):
                    live.poll_pause_key()

        self.assertTrue(live.paused)
        mock_loop.assert_called_once()


class TestRevisionTargetMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.tmp, "targets.csv")
        self.total_dir = os.path.join(self.tmp, "total")
        self.revision_dir = os.path.join(self.tmp, "revision")
        self.scraper = _make_scraper()
        self.settings = _import_scraper()[2](save_json=True, save_annexes=False, mode="target")

        Path(self.csv_path).write_text(
            "소관부처,법령명,법령 ID\n"
            "테스트부처,감사원법,001372\n"
            "테스트부처,국가재정법,000042\n",
            encoding="utf-8-sig",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _patch_roots(self):
        import src.scraper.LawScraper as mod
        self._orig_total = mod.BASE_TOTAL_DIR
        self._orig_revision = mod.BASE_REVISION_DIR
        mod.BASE_TOTAL_DIR = self.total_dir
        mod.BASE_REVISION_DIR = self.revision_dir

    def _restore_roots(self):
        import src.scraper.LawScraper as mod
        mod.BASE_TOTAL_DIR = self._orig_total
        mod.BASE_REVISION_DIR = self._orig_revision

    def test_target_csv_existing_file_collects_annex_when_enabled(self):
        """CSV 타겟 모드에서 본문이 이미 있어도 별지 옵션 ON이면 별표만 수집"""
        _, _, CollectSettings, run_target_mode, *_ = _import_scraper()
        self._patch_roots()
        try:
            single_target = Path(self.tmp) / "single_target.csv"
            single_target.write_text(
                "소관부처,법령명,법령 ID\n"
                "테스트부처,감사원법,001372\n",
                encoding="utf-8-sig",
            )
            dept_dir = os.path.join(self.total_dir, "테스트부처")
            os.makedirs(dept_dir, exist_ok=True)
            Path(os.path.join(dept_dir, "감사원법_20240101_001372.md")).write_text("기존내용", encoding="utf-8")

            with patch.object(self.scraper, "fetch_and_save_annexes", return_value=2) as mock_annex:
                with patch.object(self.scraper, "_make_request") as mock_req:
                    with patch("src.scraper.LawScraper._getch", side_effect=["\r"]):
                        with patch("src.scraper.LawScraper.console.clear", return_value=None):
                            with patch("src.scraper.LawScraper.console.print", return_value=None):
                                run_target_mode(
                                    self.scraper,
                                    MagicMock(),
                                    str(single_target),
                                    None,
                                    False,
                                    collect_annexes=False,
                                    settings=CollectSettings(save_json=True, save_annexes=True, mode="target"),
                                )

            mock_annex.assert_called_once()
            mock_req.assert_not_called()
        finally:
            self._restore_roots()

    def test_target_csv_existing_file_logs_skip_instead_of_empty_placeholder(self):
        """이미 수집된 본문은 건너뜀 로그를 남겨서 빈 목록 프레임을 방지"""
        _, CollectLive, CollectSettings, run_target_mode, *_ = _import_scraper()
        self._patch_roots()
        try:
            single_target = Path(self.tmp) / "single_target.csv"
            single_target.write_text(
                "소관부처,법령명,법령 ID\n"
                "테스트부처,감사원법,001372\n",
                encoding="utf-8-sig",
            )
            dept_dir = os.path.join(self.total_dir, "테스트부처")
            os.makedirs(dept_dir, exist_ok=True)
            Path(os.path.join(dept_dir, "감사원법_20240101_001372.md")).write_text("기존내용", encoding="utf-8")

            with patch.object(CollectLive, "log_skip", return_value=None) as mock_log_skip:
                with patch("src.scraper.LawScraper._getch", side_effect=["\r"]):
                    with patch("src.scraper.LawScraper.console.clear", return_value=None):
                        with patch("src.scraper.LawScraper.console.print", return_value=None):
                            run_target_mode(
                                self.scraper,
                                MagicMock(),
                                str(single_target),
                                None,
                                False,
                                collect_annexes=False,
                                settings=CollectSettings(save_json=True, save_annexes=False, mode="target"),
                            )

            mock_log_skip.assert_called_once()
        finally:
            self._restore_roots()

    def test_revision_target_mode_filters_and_saves_to_date_folder(self):
        """기준일자 이후 개정된 법령만 revision/<날짜> 아래 저장해야 한다"""
        _, _, CollectSettings, _, run_revision_mode, *_ = _import_scraper()
        self._patch_roots()
        try:
            def mock_request(session, method, url, params=None):
                if params and params.get("ID") == "001372":
                    return json.dumps(
                        _make_law_json("001372", "감사원법", "20240110"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                if params and params.get("ID") == "000042":
                    return json.dumps(
                        _make_law_json("000042", "국가재정법", "20240201"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                return None

            prompts = []

            def fake_input(prompt=""):
                prompts.append(prompt)
                return "20240115"

            with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                with patch("src.scraper.LawScraper._getch", side_effect=["\r"]):
                    with patch("src.scraper.LawScraper.console.clear", return_value=None):
                        with patch("src.scraper.LawScraper.console.print", return_value=None):
                            with patch("builtins.input", side_effect=fake_input):
                                run_revision_mode(
                                    self.scraper,
                                    MagicMock(),
                                    self.csv_path,
                                    None,
                                    False,
                                    collect_annexes=False,
                                    settings=CollectSettings(save_json=True, save_annexes=False, mode="target"),
                                )

            self.assertEqual(len(prompts), 1, f"revision 모드는 기준일자만 입력받아야 한다: {prompts}")
            self.assertIn("개정 기준일자", prompts[0])

            save_root = Path(self.revision_dir) / "20240115"
            self.assertTrue(save_root.is_dir(), "기준일자 폴더가 생성되어야 한다")

            saved_mds = sorted(save_root.rglob("*.md"))
            self.assertEqual(len(saved_mds), 1, f"기준일자 이후 법령만 저장되어야 한다: {saved_mds}")
            self.assertIn("000042", saved_mds[0].name)
            self.assertFalse(any("001372" in p.name for p in save_root.rglob("*.md")))
        finally:
            self._restore_roots()

    def test_revision_mode_can_save_txt_files_when_configured(self):
        """저장 형식을 txt로 고르면 본문이 .txt로 저장되어야 한다"""
        _, _, CollectSettings, _, run_revision_mode, *_ = _import_scraper()
        self._patch_roots()
        try:
            def mock_request(session, method, url, params=None):
                if params and params.get("ID") == "000042":
                    return json.dumps(
                        _make_law_json("000042", "국가재정법", "20240201"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                return None

            with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                with patch("src.scraper.LawScraper._getch", side_effect=["\r"]):
                    with patch("src.scraper.LawScraper._getch_nonblocking", return_value=None):
                        with patch("src.scraper.LawScraper.console.clear", return_value=None):
                            with patch("src.scraper.LawScraper.console.print", return_value=None):
                                with patch("builtins.input", side_effect=["20240115"]):
                                    run_revision_mode(
                                        self.scraper,
                                        MagicMock(),
                                        self.csv_path,
                                        None,
                                        False,
                                        collect_annexes=False,
                                        settings=CollectSettings(
                                            save_json=True,
                                            save_annexes=False,
                                            mode="revision",
                                            output_format="txt",
                                        ),
                                    )

            save_root = Path(self.revision_dir) / "20240115"
            saved_txts = sorted(save_root.rglob("*.txt"))
            saved_mds = sorted(save_root.rglob("*.md"))
            self.assertEqual(len(saved_txts), 1, f"txt 설정이면 .txt 파일이 저장되어야 한다: {saved_txts}")
            self.assertFalse(saved_mds, f"txt 설정이면 .md 파일은 저장되지 않아야 한다: {saved_mds}")
            self.assertIn("000042", saved_txts[0].name)
        finally:
            self._restore_roots()

    def test_revision_mode_can_save_original_pdf_files_when_configured(self):
        """저장 형식을 pdf로 고르면 국가법령정보센터 원문 PDF bytes를 .pdf로 저장해야 한다"""
        _, _, CollectSettings, _, run_revision_mode, *_ = _import_scraper()
        self._patch_roots()
        try:
            def fake_fetch_law_list(session, query="", date_range_str=None, test_mode=False):
                return pd.DataFrame(
                    [
                        {
                            "법령ID": "000042",
                            "법령명한글": "국가재정법",
                            "시행일자": "20240201",
                            "소관부처명": "기획재정부",
                            "법령일련번호": "268785",
                        }
                    ]
                )

            def mock_request(session, method, url, params=None):
                if params and params.get("ID") == "000042":
                    return json.dumps(
                        _make_law_json("000042", "국가재정법", "20240201", dept="기획재정부"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                return None

            with patch.object(self.scraper, "fetch_law_list", side_effect=fake_fetch_law_list):
                with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                    with patch.object(self.scraper, "_download_original_pdf", return_value=b"%PDF-1.4\noriginal") as mock_pdf:
                        with patch("src.scraper.LawScraper._today_yyyymmdd", return_value="20240401"):
                            with patch("src.scraper.LawScraper._getch", side_effect=["\r"]):
                                with patch("src.scraper.LawScraper._getch_nonblocking", return_value=None):
                                    with patch("src.scraper.LawScraper.console.clear", return_value=None):
                                        with patch("src.scraper.LawScraper.console.print", return_value=None):
                                            with patch("builtins.input", side_effect=["20240115"]):
                                                run_revision_mode(
                                                    self.scraper,
                                                    MagicMock(),
                                                    self.csv_path,
                                                    None,
                                                    False,
                                                    collect_annexes=False,
                                                    settings=CollectSettings(
                                                        save_json=True,
                                                        save_annexes=False,
                                                        mode="revision",
                                                        output_format="pdf",
                                                    ),
                                                )

            save_root = Path(self.revision_dir) / "20240115"
            saved_pdfs = sorted(save_root.rglob("*.pdf"))
            saved_mds = sorted(save_root.rglob("*.md"))
            self.assertEqual(len(saved_pdfs), 1, f"pdf 설정이면 .pdf 파일이 저장되어야 한다: {saved_pdfs}")
            self.assertFalse(saved_mds, f"pdf 설정이면 .md 파일은 저장되지 않아야 한다: {saved_mds}")
            self.assertEqual(saved_pdfs[0].read_bytes(), b"%PDF-1.4\noriginal")
            mock_pdf.assert_called_once()
            self.assertEqual(mock_pdf.call_args.args[1], "268785")
            self.assertEqual(mock_pdf.call_args.kwargs["efyd"], "20240201")
        finally:
            self._restore_roots()

    def test_revision_mode_prefilters_csv_targets_with_efyd_list_api(self):
        """revision CSV 모드는 efYd 목록 API 결과와 타겟 ID 교집합만 상세 조회해야 한다"""
        _, _, CollectSettings, _, run_revision_mode, *_ = _import_scraper()
        self._patch_roots()
        try:
            list_calls = []
            detail_calls = []

            def fake_fetch_law_list(session, query="", date_range_str=None, test_mode=False):
                list_calls.append(
                    {
                        "query": query,
                        "date_range_str": date_range_str,
                        "test_mode": test_mode,
                    }
                )
                return pd.DataFrame(
                    [
                        {
                            "법령ID": "000042",
                            "법령명한글": "국가재정법",
                            "시행일자": "20240201",
                            "소관부처명": "기획재정부",
                        },
                        {
                            "법령ID": "999999",
                            "법령명한글": "타겟외법령",
                            "시행일자": "20240210",
                            "소관부처명": "기타부처",
                        },
                    ]
                )

            def mock_request(session, method, url, params=None):
                if params and params.get("target") == "law" and params.get("type") == "JSON":
                    detail_calls.append(params.get("ID"))
                if params and params.get("ID") == "000042":
                    return json.dumps(
                        _make_law_json("000042", "국가재정법", "20240201", dept="기획재정부"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                if params and params.get("ID") == "001372":
                    return json.dumps(
                        _make_law_json("001372", "감사원법", "20240110", dept="감사원"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                return None

            with patch.object(self.scraper, "fetch_law_list", side_effect=fake_fetch_law_list):
                with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                    with patch("src.scraper.LawScraper._today_yyyymmdd", return_value="20240401"):
                        with patch("src.scraper.LawScraper._getch", side_effect=["\r"]):
                            with patch("src.scraper.LawScraper.console.clear", return_value=None):
                                with patch("src.scraper.LawScraper.console.print", return_value=None):
                                    with patch("builtins.input", side_effect=["20240115"]):
                                        run_revision_mode(
                                            self.scraper,
                                            MagicMock(),
                                            self.csv_path,
                                            None,
                                            False,
                                            collect_annexes=False,
                                            settings=CollectSettings(save_json=True, save_annexes=False, mode="revision"),
                                        )

            self.assertEqual(
                list_calls,
                [{"query": "", "date_range_str": "20240115~20240401", "test_mode": False}],
            )
            self.assertEqual(detail_calls, ["000042"])

            save_root = Path(self.revision_dir) / "20240115"
            saved_mds = sorted(save_root.rglob("*.md"))
            self.assertEqual(len(saved_mds), 1, f"범위+타겟 교집합 1건만 저장되어야 한다: {saved_mds}")
            self.assertIn("000042", saved_mds[0].name)
        finally:
            self._restore_roots()

    def test_revision_mode_collects_all_in_range_target_revisions_without_missing_any(self):
        """revision CSV 모드는 범위 내 타겟 법령 개정을 누락 없이 모두 수집해야 한다"""
        _, _, CollectSettings, _, run_revision_mode, *_ = _import_scraper()
        self._patch_roots()
        try:
            def fake_fetch_law_list(session, query="", date_range_str=None, test_mode=False):
                self.assertEqual(query, "")
                self.assertEqual(date_range_str, "20240115~20240401")
                return pd.DataFrame(
                    [
                        {
                            "법령ID": "000042",
                            "법령명한글": "국가재정법",
                            "시행일자": "20240201",
                            "소관부처명": "기획재정부",
                        },
                        {
                            "법령ID": "001372",
                            "법령명한글": "감사원법",
                            "시행일자": "20240315",
                            "소관부처명": "감사원",
                        },
                        {
                            "법령ID": "999999",
                            "법령명한글": "타겟외법령",
                            "시행일자": "20240210",
                            "소관부처명": "기타부처",
                        },
                    ]
                )

            detail_calls = []

            def mock_request(session, method, url, params=None):
                if params and params.get("target") == "law" and params.get("type") == "JSON":
                    detail_calls.append(params.get("ID"))
                if params and params.get("ID") == "000042":
                    return json.dumps(
                        _make_law_json("000042", "국가재정법", "20240201", dept="기획재정부"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                if params and params.get("ID") == "001372":
                    return json.dumps(
                        _make_law_json("001372", "감사원법", "20240315", dept="감사원"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                raise AssertionError(f"unexpected detail request: {params}")

            with patch.object(self.scraper, "fetch_law_list", side_effect=fake_fetch_law_list):
                with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                    with patch("src.scraper.LawScraper._today_yyyymmdd", return_value="20240401"):
                        with patch("src.scraper.LawScraper._getch", side_effect=["\r"]):
                            with patch("src.scraper.LawScraper.console.clear", return_value=None):
                                with patch("src.scraper.LawScraper.console.print", return_value=None):
                                    with patch("builtins.input", side_effect=["20240115"]):
                                        run_revision_mode(
                                            self.scraper,
                                            MagicMock(),
                                            self.csv_path,
                                            None,
                                            False,
                                            collect_annexes=False,
                                            settings=CollectSettings(save_json=True, save_annexes=False, mode="revision"),
                                        )

            self.assertEqual(detail_calls, ["001372", "000042"])

            save_root = Path(self.revision_dir) / "20240115"
            saved_ids = sorted(
                path.stem.rsplit("_", 1)[-1]
                for path in save_root.rglob("*.md")
            )
            self.assertEqual(saved_ids, ["000042", "001372"])
        finally:
            self._restore_roots()

    def test_revision_mode_keeps_prefiltered_target_even_if_detail_basic_efyd_is_outside(self):
        """목록 API가 범위 내로 잡은 타겟은 그 시행일자로 저장되어야 한다"""
        _, _, CollectSettings, _, run_revision_mode, *_ = _import_scraper()
        self._patch_roots()
        try:
            special_csv = Path(self.tmp) / "revision-targets.csv"
            special_csv.write_text(
                "소관부처,법령명,법령 ID\n"
                "경찰청,도로교통법,1638\n",
                encoding="utf-8",
            )

            def fake_fetch_law_list(session, query="", date_range_str=None, test_mode=False):
                return pd.DataFrame(
                    [
                        {
                            "법령ID": "001638",
                            "법령명한글": "도로교통법",
                            "시행일자": "20260402",
                            "소관부처명": "경찰청",
                        },
                    ]
                )

            def mock_request(session, method, url, params=None):
                if params and params.get("ID") == "001638":
                    return json.dumps(
                        _make_law_json("001638", "도로교통법", "20260701", dept="경찰청"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                raise AssertionError(f"unexpected request: {params}")

            with patch.object(self.scraper, "fetch_law_list", side_effect=fake_fetch_law_list):
                with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                    with patch("src.scraper.LawScraper._today_yyyymmdd", return_value="20260414"):
                        with patch("src.scraper.LawScraper._getch", side_effect=["\r"]):
                            with patch("src.scraper.LawScraper._getch_nonblocking", return_value=None):
                                with patch("src.scraper.LawScraper.console.clear", return_value=None):
                                    with patch("src.scraper.LawScraper.console.print", return_value=None):
                                        with patch("builtins.input", side_effect=["20260107"]):
                                            run_revision_mode(
                                                self.scraper,
                                                MagicMock(),
                                                str(special_csv),
                                                None,
                                                False,
                                                collect_annexes=False,
                                                settings=CollectSettings(save_json=True, save_annexes=False, mode="revision"),
                                            )

            save_root = Path(self.revision_dir) / "20260107"
            saved_mds = sorted(save_root.rglob("*.md"))
            self.assertEqual(len(saved_mds), 1, f"prefilter된 범위 내 타겟은 저장되어야 한다: {saved_mds}")
            self.assertIn("001638", saved_mds[0].name)
            self.assertIn("20260402", saved_mds[0].name, "파일명은 목록 API 시행일자를 사용해야 한다")
            body = saved_mds[0].read_text(encoding="utf-8")
            self.assertIn("- **시행일자**: 20260402", body)
            self.assertNotIn("- **시행일자**: 20260701", body)
        finally:
            self._restore_roots()

    def test_revision_mode_skips_future_effective_dates(self):
        """revision 모드는 오늘 이후 시행 법령을 수집하면 안 된다"""
        _, _, CollectSettings, _, run_revision_mode, *_ = _import_scraper()
        self._patch_roots()
        try:
            future_date = "20990101"

            def mock_request(session, method, url, params=None):
                if params and params.get("ID") == "001372":
                    return json.dumps(
                        _make_law_json("001372", "감사원법", future_date),
                        ensure_ascii=False,
                    ).encode("utf-8")
                if params and params.get("ID") == "000042":
                    return json.dumps(
                        _make_law_json("000042", "국가재정법", "20240201"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                return None

            with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                with patch("src.scraper.LawScraper._getch", side_effect=["\r"]):
                    with patch("builtins.input", side_effect=["20240115"]):
                        run_revision_mode(
                            self.scraper,
                            MagicMock(),
                            self.csv_path,
                            None,
                            False,
                            collect_annexes=False,
                            settings=CollectSettings(save_json=True, save_annexes=False, mode="revision"),
                        )

            save_root = Path(self.revision_dir) / "20240115"
            saved_mds = sorted(save_root.rglob("*.md"))
            self.assertEqual(len(saved_mds), 1, f"오늘 이후 시행 법령은 제외되어야 한다: {saved_mds}")
            self.assertIn("000042", saved_mds[0].name)
            self.assertFalse(any("001372" in p.name for p in saved_mds))
        finally:
            self._restore_roots()

    def test_revision_mode_prompts_cutoff_on_start_when_settings_mode_is_revision(self):
        """settings.mode가 revision이면 /start 이후 기준일자를 물어봐야 한다"""
        _, _, CollectSettings, run_target_mode, _, *_ = _import_scraper()
        self._patch_roots()
        try:
            def mock_request(session, method, url, params=None):
                if params and params.get("ID") == "000042":
                    return json.dumps(
                        _make_law_json("000042", "국가재정법", "20240201"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                return None

            input_calls = []

            def fake_input(prompt=""):
                input_calls.append(prompt)
                return "20240115"

            with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                with patch("src.scraper.LawScraper._getch", side_effect=["\r"]):
                    with patch("src.scraper.LawScraper.console.clear", return_value=None):
                        with patch("src.scraper.LawScraper.console.print", return_value=None):
                            with patch("builtins.input", side_effect=fake_input):
                                run_target_mode(
                                    self.scraper,
                                    MagicMock(),
                                    self.csv_path,
                                    None,
                                    False,
                                    collect_annexes=False,
                                    settings=CollectSettings(save_json=True, save_annexes=False, mode="revision"),
                                )

            self.assertEqual(len(input_calls), 1, f"/start 이후 기준일자만 입력받아야 한다: {input_calls}")
            self.assertIn("개정 기준일자", input_calls[0])

            save_root = Path(self.revision_dir) / "20240115"
            self.assertTrue(save_root.is_dir(), "새 기준일자 폴더가 생성되어야 한다")
        finally:
            self._restore_roots()

    def test_tab_cycles_to_revision_mode_before_start(self):
        """준비 화면에서 Tab으로 revision 모드로 바꾼 뒤 시작할 수 있어야 한다"""
        _, _, CollectSettings, run_target_mode, _, *_ = _import_scraper()
        self._patch_roots()
        try:
            def mock_request(session, method, url, params=None):
                if params and params.get("ID") == "000042":
                    return json.dumps(
                        _make_law_json("000042", "국가재정법", "20240201"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                return None

            prompts = []

            def fake_input(prompt=""):
                prompts.append(prompt)
                return "20240115"

            with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                with patch("src.scraper.LawScraper._getch", side_effect=["\t", "\r"]):
                    with patch("builtins.input", side_effect=fake_input):
                        run_target_mode(
                            self.scraper,
                            MagicMock(),
                            self.csv_path,
                            None,
                            False,
                            collect_annexes=False,
                            settings=CollectSettings(save_json=True, save_annexes=False, mode="target"),
                        )

            self.assertEqual(len(prompts), 1, "Tab으로 revision 모드 변경 후 기준일자를 물어봐야 한다")
            save_root = Path(self.revision_dir) / "20240115"
            self.assertTrue(save_root.is_dir(), "Tab으로 바꾼 revision 모드의 저장 폴더가 생성되어야 한다")
        finally:
            self._restore_roots()

    def test_main_routes_revision_mode(self):
        """main(mode='revision')는 revision 실행기로 라우팅해야 한다"""
        import src.scraper.LawScraper as mod
        with patch.object(mod, "run_revision_mode") as mock_revision:
            with patch("builtins.input", side_effect=["20240115"]):
                mod.main(
                    oc_id="test_oc",
                    mode="revision",
                    target_md_path=self.csv_path,
                    test_mode=False,
                    collect_annexes=False,
                )

        mock_revision.assert_called_once()

    def test_long_law_name_is_truncated_for_filename(self):
        """긴 법령명도 파일명 길이 한도 안에서 저장되어야 한다"""
        _, _, CollectSettings, _, run_revision_mode, *_ = _import_scraper()
        self._patch_roots()
        try:
            long_name = "대한민국과 아메리카합중국의 자유무역협정에 따른 부속서류와 개정사항이 매우 길게 이어지는 예시 법령명 " * 5

            def mock_request(session, method, url, params=None):
                if params and params.get("ID") == "000042":
                    return json.dumps(
                        _make_law_json("000042", long_name, "20240201"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                return None

            with patch.object(self.scraper, "_make_request", side_effect=mock_request):
                with patch("src.scraper.LawScraper._getch", side_effect=["\r"]):
                    with patch("src.scraper.LawScraper.console.clear", return_value=None):
                        with patch("src.scraper.LawScraper.console.print", return_value=None):
                            with patch("builtins.input", side_effect=["20240115"]):
                                run_revision_mode(
                                    self.scraper,
                                    MagicMock(),
                                    self.csv_path,
                                    None,
                                    False,
                                    collect_annexes=False,
                                    settings=CollectSettings(save_json=True, save_annexes=False, mode="revision"),
                                )

            save_root = Path(self.revision_dir) / "20240115"
            saved_mds = list(save_root.rglob("*.md"))
            self.assertEqual(len(saved_mds), 1, f"긴 법령명도 1개 파일로 저장되어야 한다: {saved_mds}")
            self.assertLess(len(saved_mds[0].name.encode("utf-8")), 200, "파일명은 너무 길면 안 된다")
        finally:
            self._restore_roots()


if __name__ == "__main__":
    unittest.main(verbosity=2)
