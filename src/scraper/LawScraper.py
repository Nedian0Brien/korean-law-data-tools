import pandas as pd
import os
import time
import re
import subprocess
import tempfile
import signal
import threading
import sys
import platform
import dataclasses
import select
from io import BytesIO
from xml.etree import ElementTree
import json
from datetime import datetime
from pathlib import Path
import requests

# Windows/Unix 환경 감지
IS_WINDOWS = sys.platform == 'win32' or platform.system() == 'Windows'

if IS_WINDOWS:
    import msvcrt
else:
    import tty
    import termios

from rich.console import Console, Group
from rich.progress import (
    Progress, SpinnerColumn, BarColumn, TextColumn,
    TimeElapsedColumn, TimeRemainingColumn, TaskProgressColumn, MofNCompleteColumn,
)
from rich.table import Table, Column
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout
from rich.text import Text
from rich import box
from collections import deque

console = Console()

# Windows/Linux 모두 지원: 절대 경로 사용
KORDOC_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "kordoc_parse.mjs"))

# --- [사용자 정의 예외] ---
class IPBlockedError(Exception):
    """서버로부터 IP 접근 제한 메시지를 받았을 때 발생하는 예외"""
    pass

# --- [전역 상수] ---
DEFAULT_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
    'Connection': 'keep-alive',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
}
BASE_URL_SEARCH = "http://www.law.go.kr/DRF/lawSearch.do"
BASE_URL_SERVICE = "http://www.law.go.kr/DRF/lawService.do"
BASE_OUTPUT_DIR = os.path.join("data", "laws_storage")
BASE_TOTAL_DIR = os.path.join(BASE_OUTPUT_DIR, "total")
BASE_UPDATE_DIR = os.path.join(BASE_OUTPUT_DIR, "update")
BASE_REVISION_DIR = os.path.join(BASE_OUTPUT_DIR, "revision")
PDF_DOWNLOAD_URL = "https://www.law.go.kr/LSW/lsPdfPrint.do"
BODY_OUTPUT_EXTENSIONS = ("md", "txt", "pdf")


# --- [공통 헬퍼 함수] ---


def _read_target_file(file_path):
    """
    수집 대상 파일을 읽어 법령 목록을 반환합니다.

    - .csv (EUC-KR, 컬럼: 소관부처 / 법령명 / 법령 ID):
        [{'법령ID': '001372', '법령명': '감사원법', '소관부처': '감사원'}, ...]
    - .md / .txt:
        [{'법령명': '감사원법'}, ...]  ← 법령명으로 검색
    """
    import csv as _csv

    if not os.path.exists(file_path):
        print(f"🚨 수집 대상 파일 '{file_path}'를 찾을 수 없습니다.")
        return []

    if file_path.lower().endswith('.csv'):
        rows = []
        for enc in ('utf-8-sig', 'utf-8', 'euc-kr'):
            try:
                with open(file_path, 'r', encoding=enc) as f:
                    reader = _csv.DictReader(f)
                    for row in reader:
                        law_id  = (row.get('법령 ID') or row.get('법령ID') or '').strip()
                        law_name = (row.get('법령명') or '').strip()
                        dept     = (row.get('소관부처') or '').strip()
                        if law_id or law_name:
                            rows.append({'법령ID': law_id, '법령명': law_name, '소관부처': dept})
                return rows
            except (UnicodeDecodeError, KeyError):
                continue
        print(f"🚨 CSV 파일 인코딩을 인식할 수 없습니다: {file_path}")
        return []

    # md / txt: 법령명 목록
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    return [
        {'법령명': re.sub(r'^\s*[-*+]\s*', '', line).strip(), '법령ID': '', '소관부처': ''}
        for line in lines
        if line.strip() and not line.strip().startswith('#')
    ]


# --- [메인 스크레이퍼 클래스] ---

class LawScraper:
    """국가법령정보센터 API 수집 핵심 로직 클래스"""

    def __init__(self, oc_id, request_delay=0.5, max_retries=5):
        self.oc_id = oc_id
        self.request_delay = request_delay
        self.max_retries = max_retries
        os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)

    def _make_request(self, session, method, url, params=None):
        """API 요청 및 재시도 제어"""
        time.sleep(self.request_delay)
        for attempt in range(self.max_retries):
            try:
                response = session.request(method, url, params=params, timeout=30)
                response.raise_for_status()
                content = response.content
                if b'IP' in content and b'alert' in content:
                    if '접근제한된 IP 입니다' in content.decode('utf-8', errors='ignore'):
                        raise IPBlockedError("IP 접근 제한 발생")
                return content
            except requests.exceptions.HTTPError as e:
                # 404는 법령이 존재하지 않는 경우이므로 재시도하지 않음
                if e.response.status_code == 404:
                    law_id = params.get('ID', 'unknown') if params else 'unknown'
                    console.log(f"⚠️  법령 없음 (ID: {law_id}): 해당 법령이 존재하지 않거나 삭제되었습니다")
                    return None
                # 다른 HTTP 에러는 재시도
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                else:
                    console.log(f"❌ 요청 최종 실패: {e}")
            except Exception as e:
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                else:
                    console.log(f"❌ 요청 최종 실패: {e}")
        return None

    def _parse_law_xml(self, content):
        """XML 응답 파싱"""
        if not content: return [], 0
        try:
            root = ElementTree.fromstring(content)
            data = [{child.tag: child.text for child in item} for item in root.findall('law')]
            total_count = int(root.findtext('totalCnt', 0))
            return data, total_count
        except:
            return [], 0

    def fetch_law_list(self, session, query='', date_range_str=None, test_mode=False):
        """법령 목록 수집 (테스트 모드 대응)"""
        # 테스트 모드 시 1페이지만, 일반 모드 시 100개씩 수집
        display = 3 if test_mode else 100
        params = {
            'OC': self.oc_id, 'target': 'law', 'type': 'xml',
            'display': display, 'page': 1, 'query': query, 'sort': 'efdesc'
        }
        if date_range_str:
            params['efYd'] = date_range_str

        content = self._make_request(session, 'GET', BASE_URL_SEARCH, params=params)
        data, total_count = self._parse_law_xml(content)
        if not data: return pd.DataFrame()

        all_data = data
        total_pages = (total_count + display - 1) // display
        # 테스트 모드 시 최대 1페이지로 제한
        max_pages = 1 if test_mode else total_pages

        if max_pages > 1:
            for i in range(2, max_pages + 1):
                params['page'] = i
                p_content = self._make_request(session, 'GET', BASE_URL_SEARCH, params=params)
                p_data, _ = self._parse_law_xml(p_content)
                all_data.extend(p_data)
        
        return pd.DataFrame(all_data)

    def _extract_lsi_seq(self, row) -> str | None:
        """목록 응답/타겟 메타데이터에서 원문 다운로드용 법령일련번호를 꺼낸다."""
        for key in ("법령일련번호", "MST", "lsiSeq", "LSI_SEQ"):
            value = row.get(key) if hasattr(row, "get") else None
            if value is None:
                continue
            text = str(value).strip()
            if not text or text.lower() == "nan":
                continue
            if text.endswith(".0") and text[:-2].isdigit():
                text = text[:-2]
            return text
        return None

    def _resolve_lsi_seq_for_pdf(
        self,
        session,
        law_id: str,
        law_name: str,
        effective_efyd: str | None = None,
    ) -> str | None:
        """CSV처럼 법령일련번호가 없는 입력에서 원문 PDF용 lsiSeq를 목록 API로 보강한다."""
        df = self.fetch_law_list(session, query=law_name, date_range_str=None, test_mode=False)
        if df.empty or "법령ID" not in df.columns:
            return self._resolve_lsi_seq_from_law_page(
                session,
                law_id,
                effective_efyd,
            )

        padded = str(law_id).strip().zfill(6)
        fallback = None
        for _, row in df.iterrows():
            row_id = str(row.get("법령ID", "")).strip().zfill(6)
            if row_id != padded:
                continue
            lsi_seq = self._extract_lsi_seq(row)
            if not lsi_seq:
                continue
            if fallback is None:
                fallback = lsi_seq
            row_efyd = str(row.get("시행일자", "")).strip()
            if effective_efyd and row_efyd == str(effective_efyd):
                return lsi_seq
        return fallback or self._resolve_lsi_seq_from_law_page(
            session,
            law_id,
            effective_efyd,
        )

    def _resolve_lsi_seq_from_law_page(
        self,
        session,
        law_id: str,
        effective_efyd: str | None = None,
    ) -> str | None:
        """lawSearch에서 못 찾은 PDF용 lsiSeq를 웹 본문 화면의 hidden input에서 보강한다."""
        padded = str(law_id).strip().zfill(6)
        params = {
            "lsId": padded,
            "chrClsCd": "010202",
            "efGubun": "Y",
        }
        if effective_efyd:
            params["efYd"] = str(effective_efyd)

        try:
            response = session.get(
                "https://www.law.go.kr/LSW/lsInfoP.do",
                params=params,
                timeout=30,
            )
            response.raise_for_status()
        except Exception as e:
            console.log(f"PDF 원문 lsiSeq 웹 보강 실패(lsId={padded}): {e}")
            return None

        html = response.text or ""
        for tag in re.findall(r"<input\b[^>]*>", html, flags=re.IGNORECASE):
            id_match = re.search(r"\bid=[\"']lsiSeq[\"']", tag, flags=re.IGNORECASE)
            if not id_match:
                continue
            value_match = re.search(r"\bvalue=[\"']([^\"']+)", tag, flags=re.IGNORECASE)
            if value_match and value_match.group(1).strip().isdigit():
                return value_match.group(1).strip()

        return None

    def _download_original_pdf(self, session, lsi_seq: str, *, efyd: str | None = None) -> bytes | None:
        """국가법령정보센터 웹 원문 저장 엔드포인트에서 PDF bytes를 내려받는다."""
        payload = {
            "lsiSeq": str(lsi_seq),
            "chrClsCd": "010202",
            "joAllCheck": "Y",
            "joEfOutPutYn": "on",
            "efYd": efyd or "",
            "mokChaChk": "N",
            "bylChaChk": "N",
            "arSeqs": ",",
            "arIds": "",
            "bylAllSeq": "",
            "coverDpYn": "1",
            "lsNmFont": "goThic",
            "lsJoSize": "10",
            "lsJoFont": "smyoungjo",
            "spaceCls": "2",
        }

        for attempt in range(self.max_retries):
            try:
                response = session.post(PDF_DOWNLOAD_URL, data=payload, timeout=60)
                response.raise_for_status()
                content = response.content
                content_type = response.headers.get("Content-Type", "")
                if content.startswith(b"%PDF") or "pdf" in content_type.lower():
                    return content
                console.log(f"PDF 원문 응답이 PDF가 아님: content-type={content_type}")
                return None
            except Exception as e:
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                else:
                    console.log(f"PDF 원문 다운로드 실패(lsiSeq={lsi_seq}): {e}")
        return None

    def _append_pdf_bytes(self, writer, pdf_bytes: bytes, label: str) -> None:
        """PDF bytes를 writer에 추가한다."""
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError("PDF 병합에는 pypdf 패키지가 필요합니다") from e

        try:
            reader = PdfReader(BytesIO(pdf_bytes))
            for page in reader.pages:
                writer.add_page(page)
        except Exception as e:
            raise RuntimeError(f"PDF 병합 실패: {label}") from e

    def _pdf_merge_manifest_entries(self, annexes: list[dict]) -> list[dict]:
        """병합 완료 여부를 정확히 판단하기 위한 별표/별지 fingerprint를 만든다."""
        entries = []
        for annex in annexes:
            entries.append({
                "number": str(annex.get("별표번호") or "").strip(),
                "title": str(annex.get("별표명") or "").strip(),
                "pdf_link": str(annex.get("별표서식PDF파일링크") or "").strip(),
                "file_link": str(annex.get("별표서식파일링크") or "").strip(),
                "serial": str(annex.get("별표일련번호") or "").strip(),
            })
        return entries

    def _pdf_merge_manifest_path(self, body_pdf_path: str) -> str:
        return f"{body_pdf_path}.merge.json"

    def _write_pdf_merge_manifest(
        self,
        body_pdf_path: str,
        law_id: str,
        annexes: list[dict],
        status: str,
        *,
        merged_count: int = 0,
        unmerged_annexes: list[dict] | None = None,
        skipped_annexes: list[dict] | None = None,
        error: str | None = None,
    ) -> None:
        manifest = {
            "status": status,
            "law_id": str(law_id).strip().zfill(6),
            "annexes": self._pdf_merge_manifest_entries(annexes),
            "merged_count": merged_count,
            "unmerged_annexes": unmerged_annexes or [],
            "skipped_annexes": skipped_annexes or [],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        if error:
            manifest["error"] = error
        with open(self._pdf_merge_manifest_path(body_pdf_path), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    def _is_pdf_merge_manifest_complete(
        self,
        body_pdf_path: str,
        law_id: str,
        annexes: list[dict],
    ) -> bool:
        if not os.path.exists(body_pdf_path):
            return False
        manifest_path = self._pdf_merge_manifest_path(body_pdf_path)
        if not os.path.exists(manifest_path):
            return False
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            return False

        return (
            manifest.get("status") in {"complete", "complete_with_warnings"}
            and str(manifest.get("law_id") or "").zfill(6) == str(law_id).strip().zfill(6)
            and manifest.get("annexes") == self._pdf_merge_manifest_entries(annexes)
        )

    def _get_pdf_merge_completion(
        self,
        session,
        law_name: str,
        law_id: str,
        body_pdf_path: str,
    ) -> tuple[bool, list[dict]]:
        annexes = self._fetch_annex_list(session, law_name, law_id)
        return self._is_pdf_merge_manifest_complete(body_pdf_path, law_id, annexes), annexes

    def _is_deleted_annex(self, annex: dict) -> bool:
        """삭제 이력으로만 남은 별표/별지는 병합/원문 저장 대상에서 제외한다."""
        title = str(annex.get("별표명") or "").strip()
        return bool(re.search(r"삭제\s*<[^>]+>", title))

    def _save_unmerged_annex_original(
        self,
        session,
        body_pdf_path: str,
        annex: dict,
    ) -> dict:
        """PDF 병합이 불가능한 살아있는 별표/별지는 원문 파일로 별도 보존한다."""
        title = annex.get('별표명', '').strip() or '제목없음'
        byl_seq = annex.get('별표번호', '').strip()
        file_link = (annex.get('별표서식파일링크') or '').strip()
        if not file_link:
            return {"title": title, "reason": "원문 링크 없음", "saved_path": None}

        raw = self._make_request(session, 'GET', f"https://www.law.go.kr{file_link}")
        if not raw:
            return {"title": title, "reason": "원문 다운로드 실패", "saved_path": None}

        ext = self._infer_annex_original_extension(file_link, raw)
        if ext == "bin":
            ext = "hwp"
        body_dir = os.path.dirname(body_pdf_path)
        body_stem = os.path.splitext(os.path.basename(body_pdf_path))[0]
        annex_dir = os.path.join(body_dir, f"{body_stem}_별표")
        os.makedirs(annex_dir, exist_ok=True)
        original_path = os.path.join(annex_dir, f"{byl_seq}_{self._sanitize(title)}.{ext}")
        if not os.path.exists(original_path):
            with open(original_path, 'wb') as f:
                f.write(raw)
        return {"title": title, "reason": "PDF 원문 없음", "saved_path": original_path}

    def fetch_and_merge_annex_pdfs(
        self,
        session,
        law_name: str,
        law_id: str,
        body_pdf_path: str,
        annexes: list[dict] | None = None,
    ) -> int:
        """본문 PDF 뒤에 별표/별지 PDF 원문을 병합한다."""
        try:
            from pypdf import PdfWriter
        except ImportError as e:
            raise RuntimeError("PDF 병합에는 pypdf 패키지가 필요합니다") from e

        annexes = annexes if annexes is not None else self._fetch_annex_list(session, law_name, law_id)
        if not annexes:
            return 0

        if self._is_pdf_merge_manifest_complete(body_pdf_path, law_id, annexes):
            return 0

        writer = PdfWriter()
        try:
            with open(body_pdf_path, "rb") as f:
                self._append_pdf_bytes(writer, f.read(), "본문")

            merged = 0
            unmerged_annexes = []
            skipped_annexes = []
            for annex in annexes:
                title = annex.get('별표명', '').strip() or '제목없음'
                if self._is_deleted_annex(annex):
                    skipped_annexes.append({"title": title, "reason": "삭제 항목"})
                    continue

                pdf_link = (annex.get('별표서식PDF파일링크') or '').strip()
                if not pdf_link:
                    unmerged_annexes.append(
                        self._save_unmerged_annex_original(session, body_pdf_path, annex)
                    )
                    continue

                raw = self._make_request(session, 'GET', f"https://www.law.go.kr{pdf_link}")
                if not raw:
                    skipped_annexes.append({"title": title, "reason": "별표 PDF 다운로드 실패"})
                    continue
                if not raw.startswith(b"%PDF"):
                    skipped_annexes.append({"title": title, "reason": "별표 원문이 PDF가 아님"})
                    continue
                self._append_pdf_bytes(writer, raw, title)
                merged += 1

            tmp_path = f"{body_pdf_path}.tmp"
            with open(tmp_path, "wb") as f:
                writer.write(f)
            os.replace(tmp_path, body_pdf_path)
            status = "complete_with_warnings" if unmerged_annexes or skipped_annexes else "complete"
            self._write_pdf_merge_manifest(
                body_pdf_path,
                law_id,
                annexes,
                status,
                merged_count=merged,
                unmerged_annexes=unmerged_annexes,
                skipped_annexes=skipped_annexes,
            )
            return merged
        except Exception as e:
            self._write_pdf_merge_manifest(
                body_pdf_path,
                law_id,
                annexes,
                "failed",
                error=str(e),
            )
            raise

    def collect_annexes_for_body(
        self,
        session,
        law_name: str,
        law_id: str,
        dept_dir: str,
        fname_prefix: str,
        body_path: str,
        output_format: str,
        settings: "CollectSettings | None" = None,
        on_saved=None,
        annexes: list[dict] | None = None,
    ) -> int:
        """설정에 따라 별표/별지를 별도 저장하거나 본문 PDF에 병합한다."""
        if output_format == "pdf" and _should_merge_annex_pdfs(settings):
            merged = self.fetch_and_merge_annex_pdfs(
                session,
                law_name,
                law_id,
                body_path,
                annexes=annexes,
            )
            if merged and on_saved:
                on_saved(f"PDF 병합 {merged}건", body_path)
            return merged

        return self.fetch_and_save_annexes(
            session,
            law_name,
            law_id,
            dept_dir,
            fname_prefix,
            output_format=output_format,
            on_saved=on_saved,
        )

    def _write_law_body_file(
        self,
        session,
        output_path: str,
        output_format: str,
        js: dict,
        law_name: str,
        law_id: str,
        effective_efyd: str | None = None,
        lsi_seq: str | None = None,
    ) -> None:
        """설정된 본문 형식에 따라 본문 파일을 저장한다."""
        if output_format == "pdf":
            resolved_lsi_seq = lsi_seq or self._resolve_lsi_seq_for_pdf(
                session, law_id, law_name, effective_efyd
            )
            if not resolved_lsi_seq:
                raise RuntimeError("원문 PDF 다운로드용 법령일련번호(lsiSeq)를 찾을 수 없습니다")
            pdf_bytes = self._download_original_pdf(session, resolved_lsi_seq, efyd=effective_efyd)
            if not pdf_bytes:
                raise RuntimeError("원문 PDF 다운로드 실패")
            with open(output_path, "wb") as f:
                f.write(pdf_bytes)
            return

        body = self._build_markdown(js, law_name, effective_efyd=effective_efyd)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(body)

    def _sanitize(self, text):
        if not text:
            return "알수없음"
        # 쉼표를 하이픈으로 치환
        sanitized = str(text).replace(',', '-')
        # Windows 파일명 금지 문자 제거
        sanitized = re.sub(r'[\\/*?:"<>|]', "", sanitized)
        return sanitized.strip()

    def _normalize_content(self, content, indent=""):
        """내용을 문자열로 정규화 (리스트일 경우 재귀적으로 join)"""
        if isinstance(content, list):
            # 중첩 리스트를 평탄화하면서 문자열로 변환
            result = []
            for i, item in enumerate(content):
                if isinstance(item, list):
                    # 중첩 리스트인 경우 재귀 처리 (들여쓰기 추가)
                    nested = self._normalize_content(item, indent + "  ")
                    if nested:
                        result.append(nested)
                elif item:
                    item_str = str(item).strip()
                    # 첫 번째 항목은 그대로, 나머지는 들여쓰기 추가
                    if i == 0:
                        result.append(item_str)
                    else:
                        result.append(indent + item_str)
            return '\n'.join(result) if result else ''
        return str(content).strip() if content else ''

    def _parse_hierarchical_structure(self, data, content_key, child_key, indent_level=0):
        """
        재귀적으로 계층 구조(항-호-목 등)를 파싱하는 범용 함수

        Args:
            data: 파싱할 데이터 (dict, list, 또는 기타)
            content_key: 내용 키 이름 (예: '항내용', '호내용', '목내용')
            child_key: 하위 항목 키 이름 (예: '호', '목')
            indent_level: 들여쓰기 레벨 (0: 들여쓰기 없음, 1: 2칸, 2: 4칸)

        Returns:
            list: 파싱된 텍스트 라인들
        """
        lines = []
        indent = "  " * indent_level

        if not data:
            return lines

        # 데이터를 리스트로 정규화
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = [data]
        else:
            return lines

        for item in items:
            if not isinstance(item, dict):
                continue

            # 현재 레벨의 내용 처리
            content = self._normalize_content(item.get(content_key, ''))
            if content:
                lines.append(f"{indent}{content}")

            # 하위 항목이 있는 경우 재귀 처리
            if child_key:
                child_data = item.get(child_key)
                if child_data:
                    # 하위 키 결정 (항->호, 호->목, 목->None)
                    next_content_key = None
                    next_child_key = None

                    if child_key == '호':
                        next_content_key = '호내용'
                        next_child_key = '목'
                    elif child_key == '목':
                        next_content_key = '목내용'
                        next_child_key = None  # 목 이하는 없음

                    if next_content_key:
                        child_lines = self._parse_hierarchical_structure(
                            child_data,
                            next_content_key,
                            next_child_key,
                            indent_level + 1
                        )
                        lines.extend(child_lines)

        return lines

    def _parse_article_recursive(self, article_data):
        """조문 데이터를 재귀적으로 파싱하여 마크다운 형식으로 변환"""
        lines = []

        # 조문내용 (제목 및 본문)
        content = self._normalize_content(article_data.get('조문내용', ''))
        if content:
            # 조문 제목을 #### 헤더로 표시 (제N조로 시작하는 경우)
            if content.startswith('제') and '조(' in content:
                lines.append(f"#### {content}")
            else:
                lines.append(content)

        # 항 처리
        hang_data = article_data.get('항')
        if hang_data:
            # 항이 딕셔너리이고 '항내용'이 없고 바로 '호'가 있는 경우 (특수 케이스)
            if isinstance(hang_data, dict) and '항내용' not in hang_data and '호' in hang_data:
                # 항 레벨을 건너뛰고 바로 호 처리
                ho_lines = self._parse_hierarchical_structure(hang_data.get('호'), '호내용', '목', indent_level=1)
                lines.extend(ho_lines)
            else:
                # 일반적인 항 처리
                hang_lines = self._parse_hierarchical_structure(hang_data, '항내용', '호', indent_level=0)
                lines.extend(hang_lines)

        # 호가 항 밖에 직접 있는 경우 처리 (조문 바로 아래 호가 있는 경우)
        ho_data = article_data.get('호')
        if ho_data and not hang_data:  # 항이 없을 때만
            ho_lines = self._parse_hierarchical_structure(ho_data, '호내용', '목', indent_level=1)
            lines.extend(ho_lines)

        return '\n'.join(lines) if lines else ''

    # --- [별표 수집] ---

    def _fetch_annex_list(self, session, law_name: str, law_id: str) -> list[dict]:
        """법령의 별표/서식 목록 조회 (licbyl → admbyl 순으로 시도)"""
        padded_id = str(law_id).zfill(6)

        # 1차: 법령 별표 (licbyl)
        params = {
            'OC': self.oc_id, 'target': 'licbyl', 'type': 'JSON',
            'query': law_name, 'search': '2', 'display': '100',
        }
        content = self._make_request(session, 'GET', BASE_URL_SEARCH, params=params)
        if content:
            try:
                data = json.loads(content)
                items = data.get('licBylSearch', {}).get('licbyl', [])
                if items:
                    items = items if isinstance(items, list) else [items]
                    # 해당 법령ID만 필터
                    matched = [i for i in items if i.get('관련법령ID', '').lstrip('0') == str(law_id).lstrip('0')]
                    if matched:
                        return self._sort_annexes(matched)
            except Exception:
                pass

        # 2차: 행정규칙 별표 (admbyl)
        params['target'] = 'admbyl'
        content = self._make_request(session, 'GET', BASE_URL_SEARCH, params=params)
        if content:
            try:
                data = json.loads(content)
                items = data.get('admRulBylSearch', {}).get('admbyl', [])
                if items:
                    items = items if isinstance(items, list) else [items]
                    return self._sort_annexes(items)
            except Exception:
                pass

        return []

    def _annex_sort_key(self, annex: dict, original_index: int) -> tuple:
        """별표 1, 별표 1의2, 별표 1의3 같은 법령식 번호를 자연 정렬한다."""
        title = str(annex.get('별표명') or '')
        seq = str(annex.get('별표번호') or '')
        text = f"{title} {seq}"
        kind_order = {"별표": 0, "별지": 1, "서식": 2}

        match = re.search(
            r'(별표|별지|서식)\s*(?:제)?\s*(\d+)(?:\s*호)?(?:\s*의\s*(\d+))?',
            text,
        )
        if match:
            kind, main, sub = match.groups()
            return (kind_order.get(kind, 99), int(main), int(sub or 0), original_index)

        match = re.search(r'(\d+)(?:\s*의\s*(\d+))?', seq or title)
        if match:
            main, sub = match.groups()
            return (99, int(main), int(sub or 0), original_index)

        return (999, float("inf"), float("inf"), original_index)

    def _sort_annexes(self, annexes: list[dict]) -> list[dict]:
        """API 응답 순서 대신 법령 별표 번호 순서로 정렬한다."""
        return [
            annex
            for _, annex in sorted(
                enumerate(annexes),
                key=lambda item: self._annex_sort_key(item[1], item[0]),
            )
        ]

    def _parse_annex_file(self, file_bytes: bytes) -> str | None:
        """kordoc를 통해 HWP/HWPX 바이너리를 Markdown으로 변환"""
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                ['node', KORDOC_SCRIPT, tmp_path],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                return result.stdout.strip() or None
            err_msg = result.stderr.strip() or "unknown"
            console.log(f"  kordoc 오류 (code={result.returncode}): {err_msg[:1000]}")
        except subprocess.TimeoutExpired:
            console.log("  kordoc 타임아웃")
        except Exception as e:
            console.log(f"  kordoc 실행 실패: {e}")
        finally:
            os.unlink(tmp_path)
        return None

    def _select_annex_link(self, annex: dict, prefer_pdf: bool = False) -> str:
        """별표/서식 파일 링크를 고른다. PDF 모드에서는 PDF 링크를 최우선으로 사용한다."""
        pdf_link = (annex.get('별표서식PDF파일링크') or '').strip()
        file_link = (annex.get('별표서식파일링크') or '').strip()
        if prefer_pdf:
            return pdf_link or file_link
        return file_link or pdf_link

    def _infer_annex_original_extension(self, file_link: str, raw: bytes) -> str:
        """원문 별표 저장 확장자를 링크/콘텐츠에서 추정한다."""
        if raw.startswith(b"%PDF"):
            return "pdf"
        path = file_link.split("?", 1)[0].split("#", 1)[0]
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext in {"pdf", "hwp", "hwpx", "doc", "docx", "xls", "xlsx", "html", "htm"}:
            return ext
        return "bin"

    def fetch_and_save_annexes(
        self,
        session,
        law_name: str,
        law_id: str,
        dept_dir: str,
        fname_prefix: str,
        output_format: str = "md",
        on_saved=None,
    ) -> int:
        """별표/서식 목록 조회 → 다운로드 → 설정 형식에 맞게 저장. 저장된 건수 반환."""
        annexes = self._fetch_annex_list(session, law_name, law_id)
        if not annexes:
            return 0

        annex_dir = os.path.join(dept_dir, f"{fname_prefix}_별표")
        os.makedirs(annex_dir, exist_ok=True)

        saved = 0
        for annex in annexes:
            title    = annex.get('별표명', '').strip() or '제목없음'
            byl_seq  = annex.get('별표번호', '').strip()
            file_link = self._select_annex_link(annex, prefer_pdf=output_format == "pdf")
            if not file_link:
                continue

            raw = self._make_request(session, 'GET', f"https://www.law.go.kr{file_link}")
            if not raw:
                console.log(f"[yellow]별표 다운로드 실패[/]: {title}")
                continue

            if output_format == "pdf":
                ext = self._infer_annex_original_extension(file_link, raw)
                original_path = os.path.join(annex_dir, f"{byl_seq}_{self._sanitize(title)}.{ext}")
                if os.path.exists(original_path):
                    continue
                with open(original_path, 'wb') as f:
                    f.write(raw)
                if on_saved:
                    on_saved(title, original_path)
                saved += 1
                continue

            md_path = os.path.join(annex_dir, f"{byl_seq}_{self._sanitize(title)}.md")
            if os.path.exists(md_path):
                continue

            markdown = self._parse_annex_file(raw)
            if not markdown:
                console.log(f"[yellow]별표 파싱 실패[/]: {title}")
                continue

            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(f"# {title}\n\n")
                f.write(markdown)
            if on_saved:
                on_saved(title, md_path)
            saved += 1

        return saved

    def _find_existing_file(
        self,
        dept_dir: str,
        law_id: str,
        extensions: tuple[str, ...] | None = None,
    ) -> tuple[str | None, str | None]:
        """
        법령ID로 기존 저장 파일을 찾아 (파일경로, 시행일자) 반환.
        파일명 형식: {법령명}_{시행일자}_{법령ID}.{md|txt|pdf}
        """
        padded = law_id.zfill(6)
        allowed_extensions = extensions or BODY_OUTPUT_EXTENSIONS
        if not os.path.isdir(dept_dir):
            return None, None
        for fname in os.listdir(dept_dir):
            stem, ext = os.path.splitext(fname)
            if ext.lower().lstrip(".") not in allowed_extensions:
                continue
            # 파일명 끝이 _{법령ID}.{ext} 인 것 매칭
            parts = stem.rsplit('_', 2)
            if len(parts) == 3:
                stored_id = parts[2].zfill(6)
                if stored_id == padded:
                    stored_efyd = parts[1]
                    return os.path.join(dept_dir, fname), stored_efyd
        return None, None

    def _build_existing_file_index(
        self,
        root_dir: str,
        extensions: tuple[str, ...] | None = None,
    ) -> dict[str, list[dict]]:
        """저장 루트를 한 번만 스캔해 법령ID별 기존 파일 인덱스를 만든다."""
        index: dict[str, list[dict]] = {}
        allowed_extensions = set(extensions or BODY_OUTPUT_EXTENSIONS)
        if not root_dir or not os.path.isdir(root_dir):
            return index

        for dirpath, _, filenames in os.walk(root_dir):
            for fname in filenames:
                stem, ext = os.path.splitext(fname)
                normalized_ext = ext.lower().lstrip(".")
                if normalized_ext not in allowed_extensions:
                    continue
                parts = stem.rsplit('_', 2)
                if len(parts) != 3:
                    continue
                law_id = parts[2].zfill(6)
                index.setdefault(law_id, []).append({
                    "path": os.path.join(dirpath, fname),
                    "efyd": parts[1],
                    "dept_dir": os.path.abspath(dirpath),
                    "ext": normalized_ext,
                })
        return index

    def _find_existing_in_index(
        self,
        index: dict[str, list[dict]],
        law_id: str,
        extensions: tuple[str, ...] | None = None,
        dept_dir: str | None = None,
    ) -> tuple[str | None, str | None]:
        """사전 구축 인덱스에서 기존 저장 파일을 찾는다."""
        allowed_extensions = set(extensions) if extensions else None
        wanted_dept = os.path.abspath(dept_dir) if dept_dir else None
        for record in index.get(str(law_id).zfill(6), []):
            if allowed_extensions and record["ext"] not in allowed_extensions:
                continue
            if wanted_dept and record["dept_dir"] != wanted_dept:
                continue
            return record["path"], record["efyd"]
        return None, None

    def _add_existing_index_record(
        self,
        index: dict[str, list[dict]] | None,
        law_id: str,
        file_path: str,
        efyd: str,
    ) -> None:
        """같은 실행 안의 후속 중복 검사를 위해 저장 직후 인덱스를 보강한다."""
        if index is None:
            return
        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        record = {
            "path": file_path,
            "efyd": str(efyd),
            "dept_dir": os.path.abspath(os.path.dirname(file_path)),
            "ext": ext,
        }
        key = str(law_id).zfill(6)
        records = index.setdefault(key, [])
        for idx, existing in enumerate(records):
            if existing["path"] == file_path:
                records[idx] = record
                return
        records.append(record)

    def process_laws(self, session, df, total_dir, collect_annexes: bool = False,
                     update_mode: bool = False, update_snapshot_dir: str | None = None,
                     settings: "CollectSettings | None" = None):
        """
        본문 다운로드 및 부처별 저장 (마크다운 형식)

        - total_dir: 전체 법령 저장 루트 (항상 최신본 유지)
        - update_snapshot_dir: update 모드 시 갱신본을 복사할 날짜별 스냅샷 폴더
        """
        import shutil
        if df.empty:
            return

        stats = {'total': len(df), 'saved': 0, 'skipped': 0, 'failed': 0, 'annexes': 0}
        existing_index = self._build_existing_file_index(total_dir)

        with _make_progress() as progress:
            task = progress.add_task("", total=len(df))

            for _, row in df.iterrows():
                law_name = row['법령명한글']
                law_id   = str(row['법령ID']).zfill(6)
                progress.update(task, description=f"[cyan]{law_name[:30]}[/]")

                dept_name = row.get('소관부처명', '기타')
                dept = "복수소관부처" if ',' in str(dept_name) else self._sanitize(dept_name)
                dept_dir = os.path.join(total_dir, dept)
                os.makedirs(dept_dir, exist_ok=True)

                fname   = _build_law_filename_stem(law_name, row['시행일자'], law_id)
                body_ext = _resolve_body_extension(settings)
                body_path = os.path.join(dept_dir, f"{fname}.{body_ext}")
                pending_annexes = None

                if update_mode:
                    existing_path, stored_efyd = self._find_existing_in_index(
                        existing_index,
                        law_id,
                        dept_dir=dept_dir,
                    )
                    if existing_path:
                        existing_stem = os.path.splitext(os.path.basename(existing_path))[0]
                        merge_existing_pdf = (
                            collect_annexes
                            and _should_merge_annex_pdfs(settings)
                            and existing_path.lower().endswith(".pdf")
                        )
                        if stored_efyd == str(row['시행일자']):
                            if merge_existing_pdf:
                                is_complete, pending_annexes = self._get_pdf_merge_completion(
                                    session,
                                    law_name,
                                    law_id,
                                    existing_path,
                                )
                                if is_complete:
                                    stats['skipped'] += 1
                                    progress.advance(task)
                                    continue
                                body_path = existing_path
                                fname = existing_stem
                            else:
                                if collect_annexes:
                                    n = self.collect_annexes_for_body(
                                        session,
                                        law_name,
                                        law_id,
                                        dept_dir,
                                        existing_stem,
                                        existing_path,
                                        body_ext,
                                        settings,
                                    )
                                    stats['annexes'] += n
                                stats['skipped'] += 1
                                progress.advance(task)
                                continue
                        else:
                            old_stem = os.path.splitext(os.path.basename(existing_path))[0]
                            for ext in [*(f".{ext}" for ext in BODY_OUTPUT_EXTENSIONS), ".json"]:
                                old_file = os.path.join(dept_dir, old_stem + ext)
                                if os.path.exists(old_file):
                                    os.remove(old_file)
                            old_annex_dir = os.path.join(dept_dir, f"{old_stem}_별표")
                            if os.path.isdir(old_annex_dir):
                                shutil.rmtree(old_annex_dir)
                            console.log(f"[yellow]갱신[/] {law_name} ({stored_efyd} → {row['시행일자']})")
                else:
                    existing_path, _ = self._find_existing_in_index(
                        existing_index,
                        law_id,
                        extensions=(body_ext,),
                        dept_dir=dept_dir,
                    )
                    if existing_path:
                        existing_stem = os.path.splitext(os.path.basename(existing_path))[0]
                        merge_existing_pdf = (
                            collect_annexes
                            and _should_merge_annex_pdfs(settings)
                            and existing_path.lower().endswith(".pdf")
                        )
                        if not merge_existing_pdf:
                            if collect_annexes:
                                n = self.collect_annexes_for_body(
                                    session,
                                    law_name,
                                    law_id,
                                    dept_dir,
                                    existing_stem,
                                    existing_path,
                                    output_format=body_ext,
                                    settings=settings,
                                )
                                stats['annexes'] += n
                            stats['skipped'] += 1
                            progress.advance(task)
                            continue
                        is_complete, pending_annexes = self._get_pdf_merge_completion(
                            session,
                            law_name,
                            law_id,
                            existing_path,
                        )
                        if is_complete:
                            stats['skipped'] += 1
                            progress.advance(task)
                            continue
                        body_path = existing_path
                        fname = existing_stem

                p_params = {'OC': self.oc_id, 'target': 'law', 'type': 'JSON', 'ID': law_id}
                content = self._make_request(session, 'GET', BASE_URL_SERVICE, params=p_params)
                if not content:
                    stats['failed'] += 1
                    progress.advance(task)
                    continue

                try:
                    js = json.loads(content)
                    lsi_seq = self._extract_lsi_seq(row)
                    self._write_law_body_file(
                        session,
                        body_path,
                        body_ext,
                        js,
                        law_name,
                        law_id,
                        str(row['시행일자']),
                        lsi_seq,
                    )
                    with open(os.path.join(dept_dir, f"{fname}.json"), 'w', encoding='utf-8') as f:
                        f.write(json.dumps(js, indent=4, ensure_ascii=False))

                    if update_mode and update_snapshot_dir:
                        snap_dept_dir = os.path.join(update_snapshot_dir, dept)
                        os.makedirs(snap_dept_dir, exist_ok=True)
                        shutil.copy2(body_path, os.path.join(snap_dept_dir, f"{fname}.{body_ext}"))
                        shutil.copy2(
                            os.path.join(dept_dir, f"{fname}.json"),
                            os.path.join(snap_dept_dir, f"{fname}.json"),
                        )

                    if collect_annexes:
                        n = self.collect_annexes_for_body(
                            session,
                            law_name,
                            law_id,
                            dept_dir,
                            fname,
                            body_path,
                            output_format=body_ext,
                            settings=settings,
                            annexes=pending_annexes,
                        )
                        stats['annexes'] += n

                    self._add_existing_index_record(
                        existing_index,
                        law_id,
                        body_path,
                        str(row['시행일자']),
                    )
                    stats['saved'] += 1

                except Exception as e:
                    console.log(f"[red]저장 실패[/] ({law_id}): {e}")
                    stats['failed'] += 1

                progress.advance(task)

        _print_summary(stats)

    def _build_markdown(self, js: dict, law_name: str, effective_efyd: str | None = None) -> str:
        """법령 JSON → 마크다운 본문 생성"""
        md_body = f"# {law_name}\n\n"

        basic_info = js.get('법령', {}).get('기본정보', {})
        if basic_info:
            md_body += "## 기본정보\n\n"
            md_body += f"- **법령ID**: {basic_info.get('법령ID', '')}\n"
            md_body += f"- **공포일자**: {basic_info.get('공포일자', '')}\n"
            md_body += f"- **시행일자**: {effective_efyd or basic_info.get('시행일자', '')}\n"
            dept_info = basic_info.get('소관부처', {})
            if isinstance(dept_info, dict):
                md_body += f"- **소관부처**: {dept_info.get('content', '')}\n"
            md_body += "\n---\n\n"

        articles = js.get('법령', {}).get('조문', {}).get('조문단위', [])
        if not isinstance(articles, list):
            articles = [articles]

        for article in articles:
            if not isinstance(article, dict):
                continue
            article_type = article.get('조문여부', '')
            if article_type == '전문':
                title_raw = article.get('조문내용', '')
                title = self._normalize_content(title_raw) if isinstance(title_raw, list) else str(title_raw).strip()
                if title:
                    title = title.lstrip()
                    if '장' in title and title.startswith('제'):
                        md_body += f"## {title}\n\n"
                    elif '절' in title and title.startswith('제'):
                        md_body += f"### {title}\n\n"
                    elif '관' in title and title.startswith('제'):
                        md_body += f"#### {title}\n\n"
                    else:
                        md_body += f"{title}\n\n"
            else:
                parsed = self._parse_article_recursive(article)
                if parsed:
                    md_body += f"{parsed}\n\n"

        appendix_data = js.get('법령', {}).get('부칙', {})
        if appendix_data:
            md_body += "\n---\n\n## 부칙\n\n"
            appendix_list = appendix_data.get('부칙단위', [])
            if not isinstance(appendix_list, list):
                appendix_list = [appendix_list]
            for appendix in appendix_list:
                if isinstance(appendix, dict):
                    for content_group in appendix.get('부칙내용', []):
                        if isinstance(content_group, list):
                            for line in content_group:
                                md_body += f"{line}\n"
                        md_body += "\n"

        return md_body


# --- [설정 모델] ---

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), ".law_scraper_settings.json")

@dataclasses.dataclass
class CollectSettings:
    save_json:    bool = True    # 원본 JSON 저장 여부
    save_annexes: bool = True    # 별지/별표 저장 여부
    output_format: str = "md"    # 본문 저장 형식: md | txt | pdf
    annex_pdf_mode: str = "separate"  # PDF 모드 별표 처리: separate | merge
    mode:         str  = "target"  # 수집 모드: target | revision | full | update

    _OUTPUT_FORMAT_OPTIONS = BODY_OUTPUT_EXTENSIONS
    _ANNEX_PDF_MODE_OPTIONS = ("separate", "merge")
    _MODE_OPTIONS = ("target", "revision", "full", "update")

    def save(self):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(self), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls) -> "CollectSettings":
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, encoding="utf-8") as f:
                    d = json.load(f)
                return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
            except Exception:
                pass
        return cls()


def _resolve_collect_annexes(settings: CollectSettings, cli_collect_annexes: bool | None) -> bool:
    """CLI 오버라이드가 있으면 우선하고, 없으면 저장된 설정값을 따른다."""
    if cli_collect_annexes is None:
        return settings.save_annexes
    return cli_collect_annexes


def _resolve_body_extension(settings: CollectSettings | None) -> str:
    """본문 저장 확장자를 설정에서 결정한다."""
    if settings and settings.output_format in CollectSettings._OUTPUT_FORMAT_OPTIONS:
        return settings.output_format
    return "md"


def _should_merge_annex_pdfs(settings: "CollectSettings | None") -> bool:
    """PDF 모드에서 별표/별지를 본문 PDF에 병합할지 결정한다."""
    return bool(
        settings
        and settings.output_format == "pdf"
        and getattr(settings, "annex_pdf_mode", "separate") == "merge"
    )


def _normalize_revision_cutoff_date(raw: str | None) -> str | None:
    """사용자 입력 날짜를 YYYYMMDD 형태로 정규화한다."""
    if raw is None:
        return None
    normalized = raw.strip().replace("-", "")
    if not normalized:
        return None
    if re.fullmatch(r"\d{8}", normalized):
        return normalized
    return None


def _prompt_revision_cutoff_date() -> str | None:
    """키보드로 개정 기준일자를 입력받는다."""
    while True:
        console.print(Panel("[bold cyan]revision 모드[/] 기준일자를 입력하세요.", border_style="cyan"))
        raw = input("  개정 기준일자 (YYYYMMDD): ")
        cutoff = _normalize_revision_cutoff_date(raw)
        if cutoff is not None:
            return cutoff
        console.print("[red]날짜 형식은 YYYYMMDD로 입력해주세요.[/]")


def _resolve_target_output_root(revision_cutoff_date: str | None) -> str:
    """target 수집의 저장 루트를 결정한다."""
    if revision_cutoff_date:
        return os.path.join(BASE_REVISION_DIR, revision_cutoff_date)
    return BASE_TOTAL_DIR


def _safe_filename_component(text: str, max_bytes: int = 120) -> str:
    """파일명 한 조각을 UTF-8 바이트 한도 내에서 안전하게 잘라낸다."""
    sanitized = re.sub(r'[\\/*?:"<>|,]', "_", str(text)).strip()
    if len(sanitized.encode("utf-8")) <= max_bytes:
        return sanitized

    data = sanitized.encode("utf-8")[:max_bytes]
    while True:
        try:
            return data.decode("utf-8").rstrip(" ._-")
        except UnicodeDecodeError:
            data = data[:-1]


def _build_law_filename_stem(law_name: str, efyd: str, law_id: str, max_name_bytes: int = 80) -> str:
    """법령 파일명 stem을 안전한 길이로 생성한다."""
    safe_name = _safe_filename_component(law_name, max_name_bytes)
    return f"{safe_name}_{efyd}_{law_id}"


def _today_yyyymmdd() -> str:
    """오늘 날짜를 YYYYMMDD로 반환한다."""
    return datetime.now().strftime("%Y%m%d")


def _is_within_revision_window(efyd: str | None, revision_cutoff_date: str | None, today: str | None = None) -> bool:
    """법령 시행일자가 기준일자부터 오늘 사이인지 확인한다."""
    if not revision_cutoff_date:
        return True
    efyd_text = str(efyd or "").strip()
    if not re.fullmatch(r"\d{8}", efyd_text):
        return False
    window_end = today or _today_yyyymmdd()
    return revision_cutoff_date <= efyd_text <= window_end


def _format_revision_window_skip_reason(
    efyd: str | None,
    revision_cutoff_date: str,
    today: str | None = None,
) -> str:
    """revision 범위 밖 스킵 사유를 사용자에게 보일 문구로 만든다."""
    today_text = today or _today_yyyymmdd()
    efyd_text = str(efyd or "").strip() or "알수없음"
    return (
        f"  (시행일자 {efyd_text}, 기준일자 {revision_cutoff_date} ~ 현재일 {today_text} 범위 밖)"
    )


# --- [설정 화면 TUI] ---

def _getch() -> str:
    """단일 키 읽기 (raw mode). Windows와 Unix 모두 지원. ESC 시퀀스도 처리."""
    if IS_WINDOWS:
        return _getch_windows()
    else:
        return _getch_unix()


def _getch_windows() -> str:
    """Windows에서 단일 키 읽기."""
    ch = msvcrt.getch().decode('utf-8', errors='ignore')

    # Windows에서 화살표 키는 2바이트 시퀀스 (0x00 또는 0xE0 프리픽스)
    if ch == '\x00' or ch == '\xe0':
        ch2 = msvcrt.getch().decode('utf-8', errors='ignore')
        # Windows 화살표 키 코드
        return {
            'H': 'UP',      # Up arrow
            'P': 'DOWN',    # Down arrow
            'M': 'RIGHT',   # Right arrow
            'K': 'LEFT',    # Left arrow
        }.get(ch2, 'ESC')

    return ch


def _getch_unix() -> str:
    """Unix/Linux에서 단일 키 읽기."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":          # ESC 또는 방향키 시퀀스
            ch1 = sys.stdin.read(1)
            ch2 = sys.stdin.read(1)
            if ch1 == "[":
                return {
                    "A": "UP",
                    "B": "DOWN",
                    "C": "RIGHT",
                    "D": "LEFT",
                    "Z": "SHIFT_TAB",
                }.get(ch2, "ESC")
            return "ESC"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _getch_nonblocking() -> str | None:
    """입력 가능한 키가 있을 때만 한 글자를 읽고, 없으면 None을 반환한다."""
    if IS_WINDOWS:
        if not msvcrt.kbhit():
            return None
        return _getch_windows()

    try:
        ready = select.select([sys.stdin], [], [], 0)[0]
    except (OSError, ValueError):
        return None
    except Exception:
        # pytest capture 같은 fileno 미지원 입력에서는 비차단 입력을 비활성화한다.
        return None

    if not ready:
        return None
    try:
        return _getch_unix()
    except Exception:
        # pytest/unittest capture나 파이프 입력처럼 TTY가 아닌 환경에서는 키 입력을 무시한다.
        return None


def show_settings_ui(current: CollectSettings) -> CollectSettings | None:
    """
    풀스크린 설정 TUI.
    - ↑/↓: 항목 이동
    - Space / ←/→: 값 변경
    - Shift+Tab: 수집 모드 변경
    - Enter: 저장 후 반환
    - Esc: 취소 (None 반환)
    """
    import copy
    cfg = copy.copy(current)

    ITEMS = [
        {"key": "save_json",    "label": "원본 JSON 저장",   "type": "bool"},
        {"key": "save_annexes", "label": "별지/별표 저장",   "type": "bool"},
        {"key": "output_format","label": "본문 저장 형식",  "type": "choice",
         "choices": CollectSettings._OUTPUT_FORMAT_OPTIONS},
        {"key": "annex_pdf_mode","label": "PDF 별표 처리",  "type": "choice",
         "choices": CollectSettings._ANNEX_PDF_MODE_OPTIONS},
        {"key": "mode",         "label": "수집 모드",        "type": "choice",
         "choices": CollectSettings._MODE_OPTIONS},
    ]
    cursor = 0

    def render():
        table = Table(box=box.ROUNDED, show_header=False, padding=(0, 2), min_width=44)
        table.add_column(style="bold", width=18)
        table.add_column(width=24)
        for i, item in enumerate(ITEMS):
            prefix = "[bold cyan]▶ [/]" if i == cursor else "  "
            label  = f"{prefix}{item['label']}"
            val    = getattr(cfg, item["key"])
            if item["type"] == "bool":
                display = "[green]ON[/]" if val else "[red]OFF[/]"
            else:
                opts = item["choices"]
                parts = []
                for o in opts:
                    parts.append(f"[bold cyan underline]{o}[/]" if o == val else f"[dim]{o}[/]")
                display = "  ".join(parts)
            table.add_row(label, display)

        hint = Text.assemble(
            ("↑↓", "bold"), " 이동   ",
            ("Space/←→", "bold"), " 변경   ",
            ("Shift+Tab", "bold"), " 모드 변경   ",
            ("Enter", "bold green"), " 저장   ",
            ("Esc", "bold yellow"), " 취소",
        )
        console.clear()
        console.print(Panel(table, title="[bold]⚙ 설정[/]", border_style="cyan", padding=(1, 2)))
        console.print(hint)

    while True:
        render()
        key = _getch()

        if key in ("UP", "\x10"):       # ↑ or Ctrl+P
            cursor = (cursor - 1) % len(ITEMS)
        elif key in ("DOWN", "\x0e"):   # ↓ or Ctrl+N
            cursor = (cursor + 1) % len(ITEMS)
        elif key in (" ", "RIGHT", "LEFT", "SHIFT_TAB"):
            item = ITEMS[cursor]
            if item["type"] == "bool":
                if key == " ":
                    setattr(cfg, item["key"], not getattr(cfg, item["key"]))
            else:
                choices = item["choices"]
                idx = list(choices).index(getattr(cfg, item["key"]))
                delta = -1 if key == "LEFT" else 1
                setattr(cfg, item["key"], choices[(idx + delta) % len(choices)])
        elif key == "\r":               # Enter
            cfg.save()
            console.clear()
            return cfg
        elif key in ("ESC", "\x1b"):    # Esc
            console.clear()
            return None


# --- [UI 헬퍼] ---

LOG_BOX_HEIGHT = 12   # 로그 패널에 표시할 최근 항목 수


def _format_log_path(file_path: str | None) -> str | None:
    """TUI 로그에 보여줄 경로를 상대경로 우선으로 정리한다."""
    if not file_path:
        return None

    path = Path(file_path)
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _format_display_root(path: str) -> str:
    """상태 화면에 보여줄 저장 루트를 사람이 읽기 좋게 정리한다."""
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except ValueError:
        return path

_MODE_DESC = {
    "target": ("목록 수집",  "CSV 파일의 법령 목록을 ID로 직접 조회"),
    "revision": ("개정 수집", " /start 후 기준일자를 입력하고 수집"),
    "full":   ("전체 수집",  "법제처 전체 법령을 날짜 범위로 수집"),
    "update": ("최신화",     "기수집 법령의 시행일자 비교 후 변경분만 갱신"),
}

_READY_COMMANDS   = ["/start", "/settings", "/exit"]
_COLLECT_COMMANDS = ["/resume", "/settings", "/exit"]


def _command_button_label(command: str) -> str:
    """내부 명령어를 화면용 버튼 라벨로 변환한다."""
    return {
        "/start": "시작",
        "/settings": "설정",
        "/pause": "일시정지",
        "/resume": "재개",
        "/exit": "종료",
    }.get(command, command.lstrip("/"))


def _build_command_ui(
    all_commands: list[tuple[str, str]],
    active_commands: list[str],
    cursor: int = 0,
) -> Group:
    """명령 영역에 들어갈 선택형 명령 리스트와 안내 문구를 만든다."""
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column(style="bold", width=14)
    table.add_column(style="dim")
    selected_description = ""
    active_lookup = {cmd for cmd in active_commands}
    active_index_lookup = [cmd for cmd, _ in all_commands if cmd in active_lookup]
    selected_command = active_index_lookup[cursor] if active_index_lookup else None

    for command, description in all_commands:
        enabled = command in active_lookup
        selected = command == selected_command
        if selected:
            selected_description = description
        prefix = "[bold cyan]>[/]" if selected else " "
        label = _command_button_label(command)
        if enabled:
            command_style = {
                "/start": "green",
                "/settings": "cyan",
                "/pause": "yellow",
                "/resume": "yellow",
                "/exit": "red",
            }.get(command, "white")
            table.add_row(f"{prefix} [{command_style}]{label}[/]", description)
        else:
            table.add_row(f"{prefix} [dim]{label}[/]", f"[dim]{description}[/]")

    hint = Text.assemble(
        ("↑↓", "bold"), " 이동   ",
        ("Enter", "bold green"), " 선택   ",
        ("Esc", "bold yellow"), " 종료",
    )
    return Group(
        table,
        Text(f"선택: {selected_description}", style="dim") if selected_description else Text(""),
        hint,
    )


def _cycle_mode(current_mode: str) -> str:
    """다음 수집 모드를 순환한다."""
    choices = list(CollectSettings._MODE_OPTIONS)
    idx = choices.index(current_mode)
    return choices[(idx + 1) % len(choices)]


def _select_command_from_screen(render_screen, options: list[tuple[str, str]], on_tab=None) -> str | None:
    """화면 렌더 함수 안의 선택 UI에서 명령을 선택한다."""
    cursor = 0
    active_commands = [command for command, _ in options]
    with Live(render_screen(cursor, active_commands), console=console, refresh_per_second=12, transient=False) as live:
        while True:
            key = _getch()
            if key in ("UP", "\x10"):
                cursor = (cursor - 1) % len(options)
            elif key in ("DOWN", "\x0e"):
                cursor = (cursor + 1) % len(options)
            elif key == "\t" and on_tab is not None:
                on_tab()
            elif key in ("\r", "\n"):
                return options[cursor][0]
            elif key in ("ESC", "\x1b"):
                return None
            live.update(render_screen(cursor, active_commands))


def _build_ready_screen(
    settings: "CollectSettings",
    total: int,
    revision_cutoff_date: str | None = None,
    output_root: str | None = None,
    command_cursor: int = 0,
    active_commands: list[str] | None = None,
):
    """수집 시작 전 안내 화면 renderable 생성."""
    from rich.console import Group

    mode_key   = settings.mode
    mode_name, mode_desc = _MODE_DESC.get(mode_key, (mode_key, ""))

    cfg_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    cfg_table.add_column(style="dim", width=14)
    cfg_table.add_column()
    cfg_table.add_row("수집 모드",
                      f"[bold cyan]{mode_name}[/]  [dim]{mode_desc}[/]")
    cfg_table.add_row("대상 법령",   f"[bold]{total:,}[/]건")
    if revision_cutoff_date:
        cfg_table.add_row("개정 기준일", f"[bold cyan]{revision_cutoff_date}[/]")
    if output_root:
        cfg_table.add_row("저장 폴더",  f"[bold]{_format_display_root(output_root)}[/]")
    cfg_table.add_row("JSON 저장",   "[green]ON[/]"  if settings.save_json    else "[red]OFF[/]")
    cfg_table.add_row("별표 저장",   "[green]ON[/]"  if settings.save_annexes else "[red]OFF[/]")
    cfg_table.add_row("본문 형식",   f"[bold]{settings.output_format}[/]")
    if settings.output_format == "pdf":
        pdf_annex_label = "본문 PDF에 병합" if settings.annex_pdf_mode == "merge" else "별도 원문 파일"
        cfg_table.add_row("PDF 별표", f"[bold]{settings.annex_pdf_mode}[/]  [dim]{pdf_annex_label}[/]")

    command_ui = _build_command_ui(
        [
            ("/start", "수집 시작"),
            ("/settings", "설정 변경  (JSON저장 / 별표저장 / 본문형식 / PDF별표 / 수집모드)"),
            ("/exit", "종료"),
        ],
        active_commands or ["/start", "/settings", "/exit"],
        cursor=command_cursor,
    )
    extra_hint = Text.assemble(("Tab", "bold"), " 수집 모드 변경")

    return Panel(
        Group(cfg_table, Text(""), command_ui, extra_hint),
        title="[bold]국가법령정보센터 법령 데이터 크롤러[/]",
        border_style="cyan",
        padding=(1, 2),
    )


def _print_ready_screen(
    settings: "CollectSettings",
    total: int,
    revision_cutoff_date: str | None = None,
    output_root: str | None = None,
    command_cursor: int = 0,
    active_commands: list[str] | None = None,
):
    """수집 시작 전 안내 화면 출력 (Live 없이 일반 print)."""
    console.print(_build_ready_screen(
        settings,
        total,
        revision_cutoff_date,
        output_root,
        command_cursor,
        active_commands,
    ))
    console.print()


class CollectLive:
    """
    수집 중 Live 레이아웃 (로그 박스 + 진행바).
    대기 단계는 _print_ready_screen() + wait_for_start() 로 분리.
    """

    def __init__(self, total: int, settings: "CollectSettings", summary_stats: dict | None = None):
        self._log: deque[Text] = deque(maxlen=LOG_BOX_HEIGHT)
        self._orig_sigint  = None
        self._stdin_fd = None
        self._orig_tty = None
        self.settings      = settings
        self.paused        = False
        self._total        = total
        self._summary_stats = summary_stats

        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}", table_column=Column(width=32, no_wrap=True)),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TextColumn("남은"),
            TimeRemainingColumn(),
            console=console,
            transient=False,
        )
        self.task_id = self.progress.add_task("", total=total)
        self._layout = Layout()
        self._layout.split_column(
            Layout(name="summary", size=7),
            Layout(name="log", ratio=LOG_BOX_HEIGHT),
            Layout(name="bar", ratio=3),
        )
        self._layout["bar"].update(self.progress)
        self._live = Live(self._layout, console=console, refresh_per_second=8, transient=False)

    # ── Esc → Live 중단 후 입력 루프 진입 ─────────────────────────────────
    def _handle_sigint(self, signum, frame):
        self._live.stop()
        self.paused = True
        self._enter_cmd_loop()

    def poll_pause_key(self):
        """수집 중 Esc 입력을 감지하면 일시정지 메뉴로 진입한다."""
        if self.paused:
            return
        key = _getch_nonblocking()
        if key == "ESC":
            self._live.stop()
            self.paused = True
            self._enter_cmd_loop()

    def _enter_cmd_loop(self):
        """Live를 멈추고 기존 안내 문구 아래에서 명령을 선택하는 루프."""
        while True:
            def render_paused_screen(cursor: int, active_commands: list[str]):
                command_ui = _build_command_ui(
                    [
                        ("/settings", "설정 변경  (JSON저장 / 별표저장 / 본문형식 / PDF별표 / 수집모드)"),
                        ("/resume", "재개"),
                        ("/exit", "종료"),
                    ],
                    active_commands,
                    cursor=cursor,
                )
                console.print()
                console.print(Panel(
                    Group(
                        Text.from_markup("[bold yellow]⏸ 일시정지[/]  — 아래 버튼에서 작업을 선택하세요."),
                        Text(""),
                        command_ui,
                    ),
                    border_style="yellow",
                    padding=(1, 2),
                ))

            cmd = _select_command_from_screen(
                render_paused_screen,
                [
                    ("/settings", "설정 변경"),
                    ("/resume", "수집 재개"),
                    ("/exit", "종료"),
                ],
            )
            if cmd is None:
                console.print("[bold red]종료[/]")
                raise SystemExit(0)

            if cmd == "/resume":
                self.paused = False
                self._log.append(Text("▶ 재개", style="green"))
                self._live.start()
                self._refresh()
                return

            elif cmd in ("/settings", "/config"):
                new_cfg = show_settings_ui(self.settings)
                if new_cfg is not None:
                    self.settings = new_cfg
                    console.print("[bold green]✓ 설정 저장됨[/]")
                else:
                    console.print("[dim]설정 취소[/]")

            elif cmd == "/exit":
                console.print("[bold red]종료[/]")
                raise SystemExit(0)

    # ── 진입/종료 ────────────────────────────────────────────────────────────
    def __enter__(self):
        self._orig_sigint = signal.signal(signal.SIGINT, self._handle_sigint)
        if not IS_WINDOWS:
            try:
                if sys.stdin.isatty():
                    self._stdin_fd = sys.stdin.fileno()
                    self._orig_tty = termios.tcgetattr(self._stdin_fd)
                    tty.setcbreak(self._stdin_fd)
            except Exception:
                self._stdin_fd = None
                self._orig_tty = None
        self._live.__enter__()
        self._refresh()
        return self

    def __exit__(self, *args):
        signal.signal(signal.SIGINT, self._orig_sigint or signal.SIG_DFL)
        if not IS_WINDOWS and self._stdin_fd is not None and self._orig_tty is not None:
            try:
                termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._orig_tty)
            except Exception:
                pass
        self._live.__exit__(*args)

    def _refresh(self):
        from rich.console import Group
        lines = list(self._log) or [Text("(아직 수집된 항목 없음)", style="dim")]
        while len(lines) < LOG_BOX_HEIGHT:
            lines.append(Text(""))
        self._layout["summary"].update(self._build_summary_panel())
        title = Text.from_markup("[bold]수집 완료 목록[/]")
        if self.paused:
            title = Text.from_markup("[bold yellow]⏸ 일시정지[/]  수집 완료 목록")
        self._layout["log"].update(
            Panel(Group(*lines), title=title, border_style="blue", padding=(0, 1))
        )

    def _build_summary_panel(self):
        from rich.console import Group
        if not self._summary_stats:
            return Panel("")

        stats = self._summary_stats
        rows = [
            ("총 대상", f"[cyan]{stats.get('total', 0)}[/]"),
            ("저장",   f"[green]{stats.get('saved', 0)}[/]"),
            ("건너뜀", f"[dim]{stats.get('skipped', 0)}[/]"),
            ("실패",   f"[red]{stats.get('failed', 0)}[/]" if stats.get('failed', 0) else "[dim]0[/]"),
        ]
        if "annexes" in stats:
            rows.append(("별표/서식", f"[cyan]{stats.get('annexes', 0)}[/]"))

        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        table.add_column(style="bold")
        table.add_column(justify="right")
        for label, value in rows:
            table.add_row(label, value)
        return Panel(
            Group(table),
            title="[bold]상단 요약[/]",
            border_style="blue",
            padding=(0, 1),
        )

    def log_saved(self, law_name: str, efyd: str, file_path: str | None = None):
        t = Text()
        t.append("✓ ", style="bold green")
        t.append(law_name, style="green")
        t.append(f"  {efyd}", style="dim")
        if file_path:
            t.append("\n  ↳ ", style="dim")
            t.append(_format_log_path(file_path) or file_path, style="dim")
        self._log.append(t)
        self._refresh()

    def log_annex(self, title: str, file_path: str | None = None):
        t = Text()
        t.append("  ↳ 별표 ", style="cyan")
        t.append(title, style="cyan dim")
        if file_path:
            t.append("\n    ")
            t.append(_format_log_path(file_path) or file_path, style="dim")
        self._log.append(t)
        self._refresh()

    def log_skip(self, law_name: str, reason: str = ""):
        self._log.append(Text.assemble(
            ("— ", "dim"), (law_name, "dim"), (reason, "dim")
        ))
        self._refresh()

    def log_fail(self, law_name: str, reason: str = ""):
        self._log.append(Text.assemble(
            ("✗ ", "bold red"), (law_name, "red"),
            (f"  {reason[:40]}" if reason else "", "dim red")
        ))
        self._refresh()

    def update_desc(self, desc: str):
        self.progress.update(self.task_id, description=f"[cyan]{desc[:30]}[/]")

    def advance(self):
        self.progress.advance(self.task_id)


def _make_progress() -> Progress:
    """rich Progress 인스턴스 생성 — 법령명 길이와 무관하게 바 위치 고정"""
    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", table_column=Column(width=32, no_wrap=True)),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TextColumn("남은"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )


def _print_summary(stats: dict):
    """수집 완료 요약 테이블 출력"""
    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(justify="right")
    if "total" in stats:
        table.add_row("총 대상", f"[cyan]{stats['total']}[/]")
    table.add_row("저장",     f"[green]{stats['saved']}[/]")
    table.add_row("건너뜀",   f"[dim]{stats['skipped']}[/]")
    table.add_row("실패",     f"[red]{stats['failed']}[/]" if stats['failed'] else "[dim]0[/]")
    if 'annexes' in stats:
        table.add_row("별표/서식", f"[cyan]{stats['annexes']}[/]")
    console.print(Panel(table, title="[bold]수집 완료[/]", border_style="green"))


# --- [모드별 실행 함수] ---

def _build_date_range_str(start_date: str | None, end_date: str | None) -> str | None:
    """start_date, end_date (YYYYMMDD) → API efYd 파라미터 문자열. 둘 다 None이면 None."""
    if not start_date and not end_date:
        return None
    s = start_date or '19000101'
    e = end_date or datetime.now().strftime('%Y%m%d')
    return f"{s}~{e}"


def _build_revision_date_range_str(revision_cutoff_date: str | None) -> str | None:
    """revision 모드용 시행일자 범위 문자열을 만든다."""
    if not revision_cutoff_date:
        return None
    return _build_date_range_str(revision_cutoff_date, _today_yyyymmdd())


def _fetch_revision_target_candidates(scraper, session, targets, test_mode: bool,
                                      revision_cutoff_date: str | None):
    """revision 범위 목록 API와 타겟 ID의 교집합만 남긴다."""
    if not revision_cutoff_date:
        return list(targets), 0, False

    target_ids = {
        str(t.get('법령ID', '')).strip().zfill(6)
        for t in targets
        if str(t.get('법령ID', '')).strip()
    }
    if not target_ids:
        return list(targets), 0, False

    revision_df = scraper.fetch_law_list(
        session,
        query='',
        date_range_str=_build_revision_date_range_str(revision_cutoff_date),
        test_mode=test_mode,
    )
    if revision_df.empty or '법령ID' not in revision_df.columns:
        return list(targets), 0, False

    candidate_rows = {}
    for _, row in revision_df.iterrows():
        law_id = str(row.get('법령ID', '')).strip().zfill(6)
        if law_id:
            candidate_rows[law_id] = row.to_dict()

    matched_targets = []
    for t in targets:
        law_id = str(t.get('법령ID', '')).strip().zfill(6)
        row = candidate_rows.get(law_id)
        if not row:
            continue
        enriched = dict(t)
        enriched['시행일자'] = str(row.get('시행일자', '')).strip()
        enriched['목록법령명'] = str(row.get('법령명한글', '')).strip()
        enriched['목록소관부처'] = str(row.get('소관부처명', '')).strip()
        enriched['법령일련번호'] = str(row.get('법령일련번호', '')).strip()
        matched_targets.append(enriched)
    return matched_targets, len(targets) - len(matched_targets), True


def run_update_mode(scraper, session, collect_annexes, settings: "CollectSettings | None" = None):
    """기수집 법령 최신화: total 법령ID → API 시행일자 비교 → 변경분만 재수집."""
    if settings is None:
        settings = CollectSettings.load()
    total_dir = BASE_TOTAL_DIR
    if not os.path.isdir(total_dir):
        console.print("[red]total 폴더 없음 — 먼저 full 모드로 수집하세요.[/]")
        return

    console.print(Panel("[bold]업데이트 모드[/] — 기수집 법령 최신화", border_style="blue"))

    law_ids = [
        os.path.splitext(f.name)[0].rsplit('_', 2)[2]
        for dept in os.scandir(total_dir) if dept.is_dir()
        for f in os.scandir(dept.path)
        if (
            os.path.splitext(f.name)[1].lower().lstrip(".") in BODY_OUTPUT_EXTENSIONS
            and len(os.path.splitext(f.name)[0].rsplit('_', 2)) == 3
        )
    ]
    if not law_ids:
        console.print("[yellow]수집된 법령 없음[/]")
        return

    console.print(f"대상: [bold]{len(law_ids)}[/]건")

    rows = []
    with _make_progress() as progress:
        task = progress.add_task("[cyan]시행일자 조회 중…[/]", total=len(law_ids))
        for law_id in law_ids:
            padded = law_id.zfill(6)
            content = scraper._make_request(
                session, 'GET', BASE_URL_SERVICE,
                params={'OC': scraper.oc_id, 'target': 'law', 'type': 'JSON', 'ID': padded}
            )
            if content:
                try:
                    js = json.loads(content)
                    basic = js.get('법령', {}).get('기본정보', {})
                    dept  = basic.get('소관부처', {})
                    rows.append({
                        '법령ID':    padded,
                        '법령명한글': basic.get('법령명_한글', ''),
                        '시행일자':  basic.get('시행일자', ''),
                        '소관부처명': dept.get('content', '') if isinstance(dept, dict) else str(dept),
                    })
                except Exception:
                    pass
            progress.advance(task)

    if not rows:
        console.print("[yellow]조회된 법령 없음[/]")
        return

    today_str    = datetime.now().strftime('%y%m%d')
    snapshot_dir = os.path.join(BASE_UPDATE_DIR, today_str)
    os.makedirs(snapshot_dir, exist_ok=True)

    scraper.process_laws(
        session, pd.DataFrame(rows), BASE_TOTAL_DIR,
        collect_annexes=collect_annexes,
        update_mode=True,
        update_snapshot_dir=snapshot_dir,
        settings=settings,
    )

    if not any(os.scandir(snapshot_dir)):
        os.rmdir(snapshot_dir)
        console.print("[green]✓ 갱신된 법령 없음 — 모두 최신 상태[/]")
    else:
        updated = sum(len(files) for _, _, files in os.walk(snapshot_dir))
        console.print(f"[green]✓ 갱신 완료[/] — {updated}개 파일 → [bold]update/{today_str}/[/]")


def run_target_mode(scraper, session, target_md_path, date_range_str, test_mode,
                    collect_annexes, settings: "CollectSettings | None" = None,
                    revision_cutoff_date: str | None = None,
                    output_root: str | None = None):
    """
    특정 법령 리스트 수집 모드.
    - CSV 입력: 법령ID로 직접 조회 (빠름)
    - md/txt 입력: 법령명으로 검색 후 수집
    """
    if settings is None:
        settings = CollectSettings.load()

    targets = _read_target_file(target_md_path)
    if not targets:
        return
    if output_root is None and settings.mode != "revision":
        output_root = BASE_TOTAL_DIR
    if output_root is not None:
        os.makedirs(output_root, exist_ok=True)

    def ensure_revision_context() -> None:
        nonlocal revision_cutoff_date, output_root
        if settings.mode != "revision":
            return
        revision_cutoff_date = _prompt_revision_cutoff_date()
        output_root = _resolve_target_output_root(revision_cutoff_date)
        os.makedirs(output_root, exist_ok=True)

    if target_md_path.lower().endswith('.csv'):
        stats = {
            'total': len(targets),
            'saved': 0,
            'skipped': 0,
            'failed': 0,
            'annexes': 0,
        }

        # ── 대기 단계: 기존 안내 화면 + 화살표 선택 UI ──────────────────────
        while True:
            cmd = _select_command_from_screen(
                lambda cursor, active_commands: _print_ready_screen(
                    settings,
                    len(targets),
                    revision_cutoff_date,
                    output_root,
                    command_cursor=cursor,
                    active_commands=active_commands,
                ),
                [
                    ("/start", "수집 시작"),
                    ("/settings", "설정 변경"),
                    ("/exit", "종료"),
                ],
                on_tab=lambda: setattr(settings, "mode", _cycle_mode(settings.mode)),
            )
            if cmd is None:
                console.print("[bold red]종료[/]")
                return
            if cmd == "/start":
                console.print()
                break
            elif cmd in ("/settings", "/config"):
                new_cfg = show_settings_ui(settings)
                if new_cfg is not None:
                    settings = new_cfg
            elif cmd == "/exit":
                console.print("[bold red]종료[/]")
                return

        ensure_revision_context()
        active_targets = targets
        revision_prefilter_applied = False
        if revision_cutoff_date:
            active_targets, prefiltered_skips, revision_prefilter_applied = _fetch_revision_target_candidates(
                scraper,
                session,
                targets,
                test_mode,
                revision_cutoff_date,
            )
            stats['skipped'] += prefiltered_skips
        existing_index = scraper._build_existing_file_index(
            output_root,
            extensions=(_resolve_body_extension(settings),),
        )

        with CollectLive(total=len(active_targets), settings=settings, summary_stats=stats) as ui:
            for t in active_targets:
                ui.poll_pause_key()
                # ── pause 대기 (Ctrl+C → _enter_cmd_loop이 paused=False로 풀어줌) ──
                while ui.paused:
                    time.sleep(0.1)

                law_id = t['법령ID']
                if not law_id:
                    ui.log_skip(t.get('법령명', '법령명 없음'), "법령ID 없음: CSV에서 ID를 입력하세요")
                    ui.advance()
                    continue

                ui.update_desc(t['법령명'])
                padded   = law_id.zfill(6)
                dept_key = scraper._sanitize(t['소관부처']) if t['소관부처'] else None
                pending_annexes = None

                # ── 기수집 여부 확인 (API 호출 전) ──────────────────────────
                if dept_key:
                    dept_dir_check = os.path.join(output_root, dept_key)
                    existing_path, _ = scraper._find_existing_in_index(
                        existing_index,
                        padded,
                        extensions=(_resolve_body_extension(ui.settings),),
                        dept_dir=dept_dir_check,
                    )
                else:
                    existing_path, _ = scraper._find_existing_in_index(
                        existing_index,
                        padded,
                        extensions=(_resolve_body_extension(ui.settings),),
                    )

                refresh_existing_pdf = (
                    existing_path
                    and ui.settings.save_annexes
                    and _should_merge_annex_pdfs(ui.settings)
                    and existing_path.lower().endswith(".pdf")
                )

                if refresh_existing_pdf:
                    is_complete, pending_annexes = scraper._get_pdf_merge_completion(
                        session,
                        t['법령명'],
                        padded,
                        existing_path,
                    )
                    if is_complete:
                        stats['skipped'] += 1
                        ui.log_skip(t['법령명'], "기존 수집본 존재 (PDF 병합 완료)")
                        ui.advance()
                        continue

                if existing_path and not refresh_existing_pdf:
                    if ui.settings.save_annexes:
                        annex_stem = os.path.splitext(os.path.basename(existing_path))[0]
                        annex_dir = os.path.dirname(existing_path)
                        n = scraper.collect_annexes_for_body(
                            session,
                            law_name=t['법령명'],
                            law_id=padded,
                            dept_dir=annex_dir,
                            fname_prefix=annex_stem,
                            body_path=existing_path,
                            output_format=_resolve_body_extension(ui.settings),
                            settings=ui.settings,
                            on_saved=ui.log_annex,
                        )
                        stats['annexes'] += n
                    else:
                        n = 0
                    ui.log_skip(
                        t['법령명'],
                        f"기존 수집본 존재 (별표 수집 {n}건)" if ui.settings.save_annexes else "기존 수집본 존재",
                    )
                    stats['skipped'] += 1
                    ui.advance()
                    continue

                # ── API 조회 ─────────────────────────────────────────────────
                content = scraper._make_request(
                    session, 'GET', BASE_URL_SERVICE,
                    params={'OC': scraper.oc_id, 'target': 'law', 'type': 'JSON', 'ID': padded}
                )
                if not content:
                    stats['failed'] += 1
                    ui.log_fail(t['법령명'], "API 응답 없음")
                    ui.advance()
                    continue

                try:
                    js        = json.loads(content)
                    basic     = js.get('법령', {}).get('기본정보', {})
                    dept_raw  = basic.get('소관부처', {})
                    law_name  = basic.get('법령명_한글', '') or t.get('목록법령명') or t['법령명']
                    detail_efyd = basic.get('시행일자', '')
                    effective_efyd = (
                        str(t.get('시행일자', '')).strip()
                        if revision_prefilter_applied and str(t.get('시행일자', '')).strip()
                        else detail_efyd
                    )
                    dept_name = (
                        dept_raw.get('content', '')
                        if isinstance(dept_raw, dict) and dept_raw.get('content', '')
                        else t.get('목록소관부처') or t['소관부처']
                    )

                    if (
                        revision_cutoff_date
                        and not revision_prefilter_applied
                        and not _is_within_revision_window(detail_efyd, revision_cutoff_date)
                    ):
                        stats['skipped'] += 1
                        ui.log_skip(
                            law_name,
                            _format_revision_window_skip_reason(detail_efyd, revision_cutoff_date),
                        )
                        ui.advance()
                        continue

                    body_ext = _resolve_body_extension(ui.settings)
                    if refresh_existing_pdf:
                        dept_dir = os.path.dirname(existing_path)
                        fname = os.path.splitext(os.path.basename(existing_path))[0]
                        body_path = existing_path
                    else:
                        dept = "복수소관부처" if ',' in dept_name else scraper._sanitize(dept_name)
                        dept_dir = os.path.join(output_root, dept)
                        os.makedirs(dept_dir, exist_ok=True)
                        fname = _build_law_filename_stem(law_name, effective_efyd, padded)
                        body_path = os.path.join(dept_dir, f"{fname}.{body_ext}")

                    scraper._write_law_body_file(
                        session,
                        body_path,
                        body_ext,
                        js,
                        law_name,
                        padded,
                        effective_efyd,
                        scraper._extract_lsi_seq(t),
                    )

                    # settings.save_json: 원본 JSON 저장 여부
                    if ui.settings.save_json:
                        with open(os.path.join(dept_dir, f"{fname}.json"), 'w', encoding='utf-8') as f:
                            f.write(json.dumps(js, indent=4, ensure_ascii=False))

                    ui.log_saved(law_name, effective_efyd, file_path=body_path)

                    # settings.save_annexes: 별지/별표 저장 여부
                    if ui.settings.save_annexes:
                        n = scraper.collect_annexes_for_body(
                            session,
                            law_name,
                            padded,
                            dept_dir,
                            fname,
                            body_path,
                            output_format=body_ext,
                            settings=ui.settings,
                            on_saved=ui.log_annex,
                            annexes=pending_annexes,
                        )
                        stats['annexes'] += n

                    scraper._add_existing_index_record(
                        existing_index,
                        padded,
                        body_path,
                        effective_efyd,
                    )
                    stats['saved'] += 1

                except Exception as e:
                    ui.log_fail(t['법령명'], str(e))
                    stats['failed'] += 1

                ui.advance()

        _print_summary(stats)

    else:
        ensure_revision_context()
        # md/txt: 법령명 검색 방식
        effective_date_range_str = (
            _build_revision_date_range_str(revision_cutoff_date)
            if revision_cutoff_date else date_range_str
        )
        for t in targets:
            df = scraper.fetch_law_list(
                session,
                query=t['법령명'],
                date_range_str=effective_date_range_str,
                test_mode=test_mode,
            )
            if revision_cutoff_date and not df.empty and '시행일자' in df.columns:
                filtered = df.copy()
                filtered['시행일자'] = filtered['시행일자'].astype(str)
                filtered = filtered[
                    filtered['시행일자'].str.fullmatch(r'\d{8}')
                    & (filtered['시행일자'] >= revision_cutoff_date)
                    & (filtered['시행일자'] <= _today_yyyymmdd())
                ]
                df = filtered
            scraper.process_laws(session, df, output_root, collect_annexes=settings.save_annexes, settings=settings)


def run_revision_mode(scraper, session, target_md_path, date_range_str, test_mode,
                      collect_annexes, settings: "CollectSettings | None" = None):
    """목록에서 지정된 법령 중 기준일 이후 개정된 항목만 revision/<날짜>에 저장한다."""
    if settings is None:
        settings = CollectSettings.load()
    settings.mode = "revision"
    run_target_mode(
        scraper,
        session,
        target_md_path,
        date_range_str,
        test_mode,
        collect_annexes,
        settings=settings,
    )


def run_full_mode(scraper, session, date_range_str, test_mode, collect_annexes,
                  settings: "CollectSettings | None" = None):
    """전체 법령 수집 모드 — total 폴더에 저장"""
    print("🌐 전체 법령 수집 시작...")
    df = scraper.fetch_law_list(session, query='', date_range_str=date_range_str, test_mode=test_mode)
    scraper.process_laws(
        session,
        df,
        BASE_TOTAL_DIR,
        collect_annexes=collect_annexes,
        settings=settings,
    )


def run_list_only_mode(scraper, session, date_range_str):
    """목록만 수집하여 CSV 저장하는 모드"""
    print("📋 목록 전용 수집 시작...")
    df = scraper.fetch_law_list(session, query='', date_range_str=date_range_str, test_mode=False)
    os.makedirs(BASE_TOTAL_DIR, exist_ok=True)
    df.to_csv(os.path.join(BASE_TOTAL_DIR, "law_list_full.csv"), index=False, encoding='utf-8-sig')
    print(f"💾 목록 저장 완료: law_list_full.csv ({len(df)}건)")


# --- [메인 래퍼 함수] ---

def main(
    oc_id,
    mode: str = 'full',
    start_date: str | None = None,
    end_date: str | None = None,
    target_md_path: str = "target_laws.md",
    test_mode: bool = False,
    collect_annexes: bool | None = None,
):
    """
    모드별 수집 제어 메인 함수

    :param mode: 'full' | 'target' | 'revision' | 'list_only' | 'update'
    :param start_date: 시행일자 범위 시작 (YYYYMMDD). None이면 제한 없음
    :param end_date:   시행일자 범위 종료 (YYYYMMDD). None이면 오늘
    :param collect_annexes: True이면 별표/서식도 함께 수집
    """
    settings = CollectSettings.load()
    settings.mode = mode
    resolved_collect_annexes = _resolve_collect_annexes(settings, collect_annexes)
    settings.save_annexes = resolved_collect_annexes

    info = (f"모드: [bold]{mode}[/]  테스트: {test_mode}"
            f"  JSON저장: {settings.save_json}  별표수집: {settings.save_annexes}"
            f"  본문형식: {settings.output_format}")
    if settings.output_format == "pdf" and settings.save_annexes:
        info += f"  PDF별표: {settings.annex_pdf_mode}"
    if start_date or end_date:
        info += f"\n시행일자: {start_date or '처음'} ~ {end_date or '오늘'}"
    console.print(Panel(info, title="[bold green]법령 수집기[/]", border_style="green"))

    date_range_str = _build_date_range_str(start_date, end_date)
    os.makedirs(BASE_TOTAL_DIR, exist_ok=True)

    with requests.Session() as session:
        session.headers.update(DEFAULT_HEADERS)
        scraper = LawScraper(oc_id=oc_id)

        if mode == 'target':
            run_target_mode(scraper, session, target_md_path, date_range_str, test_mode,
                            resolved_collect_annexes, settings=settings)
        elif mode == 'revision':
            run_revision_mode(scraper, session, target_md_path, date_range_str, test_mode,
                              resolved_collect_annexes, settings=settings)
        elif mode == 'full':
            run_full_mode(scraper, session, date_range_str, test_mode, resolved_collect_annexes, settings=settings)
        elif mode == 'list_only':
            run_list_only_mode(scraper, session, date_range_str)
        elif mode == 'update':
            run_update_mode(scraper, session, resolved_collect_annexes, settings=settings)
        else:
            console.print(f"[red]알 수 없는 모드: {mode}[/]")

    console.print("[bold green]✓ 완료[/]")


if __name__ == "__main__":
    import argparse

    _default_csv = os.path.join(os.path.dirname(__file__), "국가법령정보센터_법령목록(반출).csv")

    parser = argparse.ArgumentParser(description="국가법령정보센터 법령 수집기")
    parser.add_argument("--oc-id",      default="leegy76",    help="법제처 Open API 인증키")
    parser.add_argument("--mode",       default="target",     choices=["full", "target", "revision", "list_only", "update"],
                        help="수집 모드 (기본값: target)")
    parser.add_argument("--start-date", default=None,         help="시행일자 범위 시작 (YYYYMMDD)")
    parser.add_argument("--end-date",   default=None,         help="시행일자 범위 종료 (YYYYMMDD)")
    parser.add_argument("--target-md",  default=_default_csv, help="target 모드용 법령 목록 파일")
    parser.add_argument("--test",       action="store_true",  help="테스트 모드 (법령당 3건만 수집)")
    parser.add_argument("--no-annexes", action="store_true",  help="별표/서식 수집 안 함")

    args = parser.parse_args()

    main(
        oc_id=args.oc_id,
        mode=args.mode,
        start_date=args.start_date,
        end_date=args.end_date,
        target_md_path=args.target_md,
        test_mode=args.test,
        collect_annexes=False if args.no_annexes else None,
    )
    
