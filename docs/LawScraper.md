# LawScraper

## 개요

`src/scraper/LawScraper.py`는 국가법령정보센터에서 법령 본문, 원본 JSON, 별표/별지/서식 파일을 수집하는 스크레이퍼이다.

주요 기능은 다음과 같다.

- 법령 목록 조회 및 본문 수집
- CSV 또는 md/txt 대상 파일 기반 수집
- 전체 수집, 대상 수집, 개정 수집, 업데이트 수집
- 본문 저장 형식 선택: `md`, `txt`, `pdf`
- 원본 JSON 저장 옵션
- 별표/별지/서식 수집 옵션
- PDF 모드에서 별표/별지를 별도 원문 파일로 저장하거나 본문 PDF에 병합
- 수집 중 진행률, 로그, 일시정지, 설정 변경을 제공하는 Rich TUI

## 실행 진입점

기본 실행 파일은 `src/scraper/LawScraper.py`이다.

```bash
python src/scraper/LawScraper.py --mode target
```

지원 CLI 옵션:

- `--oc-id`: 법제처 Open API 인증키. 기본값은 코드 내 기본값을 사용한다.
- `--mode`: `full`, `target`, `revision`, `list_only`, `update` 중 하나.
- `--start-date`: 시행일자 검색 시작일, `YYYYMMDD`.
- `--end-date`: 시행일자 검색 종료일, `YYYYMMDD`.
- `--target-md`: target/revision 모드에서 사용할 대상 파일. 기본값은 `src/scraper/국가법령정보센터_법령목록(반출).csv`.
- `--test`: 목록 조회 테스트 모드.
- `--no-annexes`: 별표/별지/서식 수집 비활성화.

실제 실행 설정은 `CollectSettings.load()`로 읽은 저장 설정을 기준으로 하며, CLI의 `--no-annexes`처럼 명시된 오버라이드만 우선 적용된다.

## 저장 위치

기본 저장 루트는 `data/laws_storage`이다.

- `data/laws_storage/total`: full/target/update의 최신 본문 저장 위치
- `data/laws_storage/update/{YYMMDD}`: update 모드에서 갱신된 파일 스냅샷
- `data/laws_storage/revision/{YYYYMMDD}`: revision 모드에서 기준일자별 수집 결과

본문 파일명 형식:

```text
{법령명}_{시행일자}_{법령ID}.{md|txt|pdf}
```

예:

```text
개인정보 보호법 시행령_20251002_011468.pdf
```

소관부처가 여러 개인 법령은 `복수소관부처` 폴더에 저장한다. 파일명 구성 요소는 안전한 길이로 자르고, 파일명에 부적절한 문자는 치환한다.

## 설정

설정은 `src/scraper/.law_scraper_settings.json`에 저장된다.

`CollectSettings` 필드:

| 필드 | 값 | 의미 |
| --- | --- | --- |
| `save_json` | `true` / `false` | 국가법령정보센터 상세 JSON 저장 여부 |
| `save_annexes` | `true` / `false` | 별표/별지/서식 수집 여부 |
| `output_format` | `md` / `txt` / `pdf` | 본문 저장 형식 |
| `annex_pdf_mode` | `separate` / `merge` | PDF 모드에서 별표/별지 처리 방식 |
| `mode` | `target` / `revision` / `full` / `update` | 수집 모드 |

TUI 설정 화면에서 다음 항목을 변경할 수 있다.

- 원본 JSON 저장
- 별지/별표 저장
- 본문 저장 형식
- PDF 별표 처리
- 수집 모드

준비 화면에서 `Tab`을 누르면 수집 모드를 순환한다. 수집 중에는 `Esc`로 일시정지 메뉴에 들어갈 수 있다.

## 수집 모드

### `target`

대상 파일에 적힌 법령만 수집한다.

CSV 입력:

- `법령 ID` 또는 `법령ID`
- `법령명`
- `소관부처`

CSV는 법령ID로 상세 API를 직접 조회한다. 본문이 이미 존재하면 기본적으로 건너뛰지만, `save_annexes=true`인 경우 별표/별지/서식은 추가로 수집한다.

md/txt 입력:

- 각 줄을 법령명으로 읽는다.
- `#`으로 시작하는 줄은 무시한다.
- `-`, `*`, `+` 글머리 기호는 제거한다.
- 법령명으로 목록 검색 후 검색 결과를 수집한다.

### `revision`

대상 법령 중 기준일자 이후 개정된 항목만 수집한다.

흐름:

1. 준비 화면에서 `/start` 선택
2. 기준일자 입력, `YYYYMMDD`
3. `lawSearch.do`의 `efYd` 범위로 후보를 사전 필터링
4. 대상 CSV의 법령ID와 목록 API 결과의 교집합만 상세 조회
5. `revision/{기준일자}` 아래 저장

revision 모드에서는 목록 API에서 확인한 `시행일자`를 파일명과 로그의 기준 시행일자로 사용한다. 상세 API의 최신 `기본정보.시행일자`가 다르더라도 revision 대상 메타데이터를 덮어쓰지 않는다.

오늘 이후 시행일자는 수집 대상에서 제외한다.

### `full`

국가법령정보센터 목록 API에서 전체 법령 목록을 조회해 `total` 아래 저장한다.

### `update`

기존 `total` 폴더의 저장 파일에서 법령ID를 읽고, 상세 API의 시행일자와 비교해 변경된 법령만 재수집한다.

시행일자가 바뀐 경우:

- 기존 본문 파일 삭제
- 기존 JSON 삭제
- 기존 `{파일명}_별표` 폴더 삭제
- 새 본문 저장
- 새 JSON 저장
- `update/{YYMMDD}`에 갱신 스냅샷 복사

시행일자가 같으면 본문은 건너뛴다. 단, 별표 수집 옵션이 켜져 있으면 별표/별지/서식은 누락 보완을 위해 다시 조회할 수 있다.

### `list_only`

법령 목록만 조회해 `data/laws_storage/total/law_list_full.csv`로 저장한다.

## 본문 저장 형식

### `md`

상세 API JSON을 자체 마크다운 빌더로 변환한다.

포함 내용:

- 기본정보
- 조문
- 부칙

### `txt`

현재 구현은 `md`와 같은 본문 빌더를 사용하되 확장자만 `.txt`로 저장한다.

### `pdf`

국가법령정보센터의 웹 원문 저장 엔드포인트에서 원문 PDF bytes를 다운로드해 저장한다.

사용 엔드포인트:

```text
POST https://www.law.go.kr/LSW/lsPdfPrint.do
```

PDF 다운로드에는 `lsiSeq`가 필요하다.

- 목록 API row에 `법령일련번호`, `MST`, `lsiSeq`, `LSI_SEQ`가 있으면 우선 사용한다.
- CSV처럼 `lsiSeq`가 없는 입력은 법령명 목록 검색으로 `lsiSeq`를 보강한다.
- revision 모드에서는 기준 시행일자와 맞는 목록 row의 `lsiSeq`를 우선 사용한다.

본문 PDF 요청은 별표/별지를 포함하지 않는 설정으로 호출한다.

```text
bylChaChk=N
bylAllSeq=
```

따라서 별표/별지를 PDF에 포함하려면 `save_annexes=true`와 `annex_pdf_mode=merge`를 사용해야 한다.

## 별표/별지/서식 수집

별표 목록 조회 순서:

1. `lawSearch.do` `target=licbyl`
2. 해당 법령ID와 `관련법령ID`가 일치하는 항목만 사용
3. licbyl 결과가 없으면 `target=admbyl` 조회

조회된 별표/별지/서식은 API 응답 순서 그대로 쓰지 않고 법령 번호 순서로 자연 정렬한다.

정렬 예:

```text
별표 1
별표 1의2
별표 1의3
별표 1의5
별표 2
```

정렬 기준은 `별표명`과 `별표번호`에서 `별표`, `별지`, `서식`, 주번호, `의` 번호를 파싱해 만든다. 파싱할 수 없는 항목은 원래 응답 순서를 유지한다.

### md/txt 본문 모드의 별표 처리

별표 파일 링크를 다운로드한 뒤 `kordoc_parse.mjs`를 통해 HWP/HWPX를 Markdown으로 변환한다.

저장 위치:

```text
{본문 파일 stem}_별표/{별표번호}_{별표명}.md
```

변환 실패 시 해당 별표는 건너뛰고 로그를 남긴다.

### PDF 본문 모드의 별표 처리

PDF 모드에서는 PDF 링크를 우선 사용한다.

링크 선택 우선순위:

1. `별표서식PDF파일링크`
2. `별표서식파일링크`

`annex_pdf_mode`에 따라 처리 방식이 달라진다.

#### `separate`

별표/별지/서식을 본문과 별개 원문 파일로 저장한다.

저장 위치:

```text
{본문 파일 stem}_별표/{별표번호}_{별표명}.{pdf|hwp|hwpx|...}
```

PDF bytes는 `.pdf`로 저장한다. PDF 링크가 없고 HWP/HWPX 링크만 있으면 해당 원본 확장자로 보존한다. 이 모드에서는 별표를 Markdown으로 파싱하지 않는다.

#### `merge`

본문 PDF 뒤에 별표/별지 PDF 원문을 병합해 하나의 PDF로 저장한다.

동작:

1. 본문 PDF 저장
2. 별표/별지 목록 조회
3. 별표/별지를 법령 번호 순서로 정렬
4. PDF 원문이 있는 항목은 `별표서식PDF파일링크` 다운로드
5. 본문 PDF 뒤에 순서대로 append
6. PDF 원문이 없고 HWP/HWPX 등 원문 파일만 있는 살아있는 항목은 별도 원문 파일로 저장
7. 삭제 이력 항목이나 오래되어 다운로드가 깨진 링크는 실패로 분류하지 않고 건너뜀
8. 임시 파일에 쓴 뒤 기존 본문 PDF를 교체

PDF로 병합된 항목만 있으면 별도 `{본문 파일 stem}_별표` 폴더를 만들지 않는다. PDF 병합이 불가능한 HWP/HWPX 원문이 있으면 해당 폴더에 원문 파일을 보존한다.

HWP/HWPX를 PDF로 변환하지 않는다.

## PDF merge 중복 방지 정책

merge 모드는 기존 PDF에 별표를 덧붙이지 않는다.

병합이 성공하면 본문 PDF 옆에 다음 sidecar 파일을 저장한다.

```text
{본문 PDF 파일명}.merge.json
```

이 manifest에는 병합 상태, 법령ID, 병합된 별표/별지의 번호/제목/PDF 링크 fingerprint가 들어간다. HWP로 별도 보존한 항목은 `unmerged_annexes`, 삭제/오래된 링크로 건너뛴 항목은 `skipped_annexes`에 기록한다.

이미 같은 파일이 있어도 `output_format=pdf`, `save_annexes=true`, `annex_pdf_mode=merge`일 때 manifest가 없거나 현재 별표 목록과 다르면:

1. 본문 PDF를 원문 엔드포인트에서 다시 다운로드
2. 별표/별지 PDF를 새로 다운로드
3. 깨끗한 본문 PDF + 별표 PDF로 재병합
4. 기존 PDF를 교체

반대로 기존 PDF와 `.merge.json`이 있고, 현재 별표 목록 fingerprint가 manifest와 같으면 상세 API와 본문 PDF 재다운로드를 건너뛴다.

이 정책 때문에 기존 파일이 이미 병합된 PDF여도 별표가 중복 append되지 않는다.

대량 수집 중 기존 파일 확인은 저장 루트를 매 항목마다 다시 스캔하지 않고, 시작 시 한 번 만든 법령ID 인덱스를 재사용한다.

## 원본 JSON 저장

`save_json=true`이면 상세 API 응답 JSON을 본문 파일과 같은 stem으로 저장한다.

```text
{법령명}_{시행일자}_{법령ID}.json
```

PDF 원문 저장 모드에서도 JSON 저장 옵션은 독립적으로 동작한다.

## 기존 파일 탐색

기존 파일은 법령ID 기준으로 찾는다.

대상 확장자:

```text
md, txt, pdf
```

파일명 끝의 `_{법령ID}`를 기준으로 매칭하므로 법령명에 `_`가 포함되어도 법령ID를 올바르게 추출한다.

## 의존성

Python:

- `pandas`
- `requests`
- `rich`
- `pypdf`

Node:

- `kordoc`
- `src/scraper/kordoc_parse.mjs`

`pypdf`는 PDF merge 모드에서 필요하다. 설치되어 있지 않으면 PDF 병합 시 `PDF 병합에는 pypdf 패키지가 필요합니다` 오류를 발생시킨다.

`kordoc_parse.mjs`는 HWP/HWPX 별표를 Markdown으로 변환할 때 사용한다.

## 검증 테스트

주요 회귀 테스트는 `tests/test_law_scraper_update_mode.py`에 있다.

검증 명령:

```bash
rtk python -m py_compile src/scraper/LawScraper.py
rtk python tests/test_law_scraper_update_mode.py
```

현재 테스트가 다루는 주요 기능:

- 기존 파일 탐색
- update 모드 갱신/스킵
- 별표 누락 보완 수집
- PDF 원문 본문 저장
- PDF 모드 별표 원문 저장
- PDF merge 모드 병합
- PDF merge 모드 중복 방지
- 별표 `1`, `1의2`, `1의3` 자연 정렬
- revision 모드 기준일자 입력 및 사전 필터링
- settings 기반 실행 흐름
- TUI 일시정지/종료
