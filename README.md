# Hang on 신용잔고 → GitHub Pages → Notion 대시보드

`https://www.hangon.co.kr/credit-balance` 페이지가 실제 차트에 사용하는 네트워크 응답을 GitHub Actions의 Chromium으로 읽어 `data.json`에 저장합니다.

## 화면 구성

- 상단: **예탁금 대비 신용 비율**
- 차트 오른쪽 위: **신용융자 잔고**, **고객예탁금** 최신 수치
- 차트 전환 탭
  - 신용비율
  - 신용 VS 예탁금
- 하단: 지표 읽는 법
- 모바일·노션 임베드 크기에 맞춘 반응형 다크 디자인

## 1. GitHub에 올리기

1. GitHub에서 새 저장소를 만듭니다. 예: `credit-balance-dashboard`
2. 이 ZIP 파일을 풀고, 폴더 안의 파일과 폴더를 **전부** 저장소 최상단에 업로드합니다.
3. 특히 `.github/workflows/update.yml`이 빠지지 않게 확인합니다.
4. 저장소의 **Actions** 탭으로 들어갑니다.
5. 왼쪽에서 **신용잔고 데이터 자동 갱신**을 선택합니다.
6. **Run workflow → Run workflow**를 눌러 최초 데이터를 수집합니다.
7. 실행이 끝나면 `data.json`에 시계열 데이터가 자동 커밋됩니다.

> 첫 실행 전에는 화면에 “아직 수집된 데이터가 없습니다”라고 표시되는 것이 정상입니다.

## 2. GitHub Pages 켜기

1. 저장소 **Settings → Pages**로 이동합니다.
2. **Build and deployment**의 Source를 `Deploy from a branch`로 설정합니다.
3. Branch는 `main`, 폴더는 `/(root)`를 선택하고 저장합니다.
4. 잠시 후 다음 형태의 주소가 생성됩니다.

```text
https://사용자명.github.io/저장소명/
```

## 3. Notion에 넣기

1. GitHub Pages 주소를 복사합니다.
2. Notion에서 `/임베드` 또는 `/embed`를 입력합니다.
3. 주소를 붙여넣고 **링크 임베드**를 선택합니다.
4. 임베드 블록의 높이를 약 900~1,100px로 늘리면 상단 지표·차트·설명까지 한 화면에서 보기 좋습니다.

## 자동 갱신 시각

`update.yml`은 평일 한국시간 기준으로 다음 시각에 실행됩니다.

- 09:17
- 15:17
- 21:17

GitHub Actions의 예약 실행은 서버 사정에 따라 몇 분 이상 늦어질 수 있습니다. 수동 갱신은 Actions에서 언제든 실행할 수 있습니다.

## 데이터 수집 실패 시

1. **Actions → 실패한 실행**을 엽니다.
2. 하단 Artifacts의 `credit-balance-debug`를 내려받습니다.
3. 사이트의 API 필드명이나 구조가 변경된 경우 `scripts/update_data.py`의 패턴을 조정해야 합니다.
4. 실패 시 기존 `data.json`은 지우지 않으므로, GitHub Pages에는 마지막 정상 데이터가 계속 표시됩니다.

## 주요 파일

- `index.html` — 대시보드 구조
- `style.css` — 노션용 반응형 다크 디자인
- `app.js` — 차트 전환과 수치 표시
- `data.json` — 자동 수집 데이터
- `scripts/update_data.py` — Hang on 페이지 데이터 추출기
- `.github/workflows/update.yml` — 자동 갱신 설정
