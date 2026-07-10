# Korean Law Data Tools

국가법령정보센터에서 법령 본문, 원본 JSON, 별표/별지/서식 파일을 수집하고 갱신하는 도구 모음입니다.

## 주요 기능

- 법령 목록 조회 및 본문 수집
- CSV, Markdown, TXT 기반 대상 법령 수집
- 전체 수집, 대상 수집, 개정 수집, 업데이트 수집
- 본문 저장 형식 선택: Markdown, TXT, PDF
- 별표/별지/서식 다운로드 및 HWP/HWPX 변환
- Rich 기반 진행률, 로그, 일시정지, 설정 UI

## 구조

```text
korean_law_data_tools/
└── cli.py
src/scraper/
├── LawScraper.py
└── kordoc_parse.mjs
tests/
└── test_law_scraper_update_mode.py
docs/
└── LawScraper.md
```

## 준비

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install
```

법제처 Open API 인증키가 필요합니다.

```bash
cp .env.example .env
```

## 실행

```bash
python -m korean_law_data_tools.cli --mode target --oc-id "$LAW_OPEN_API_OC"
```

## 검증

```bash
pytest tests/test_law_scraper_update_mode.py -q
pytest tests/test_public_api.py -q
python -m py_compile korean_law_data_tools/cli.py src/scraper/LawScraper.py
```

## Python API

```python
from korean_law_data_tools import LawScraper

scraper = LawScraper(oc_id="your-open-api-key")
```

## 데이터 정책

실제 수집 결과, `.env`, 로컬 설정 JSON, `node_modules/`, 캐시 파일은 Git에 포함하지 않습니다.
