# 교실 이동 관리 Python 백엔드

FastAPI + PostgreSQL(Neon) / 로컬 SQLite. Notion의 9개 REST API, 교시별 현황, 학생 수 계산, 실시간 SSE, 일일 초기화를 구현했습니다. 디스코드 봇과 전자칠판 프론트엔드는 이 서버에 연결하는 별도 클라이언트입니다.

## 실행 (Windows PowerShell, Python 3.11 이상)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- API 문서: http://127.0.0.1:8000/docs
- OpenAPI: http://127.0.0.1:8000/openapi.json
- 상태 확인: http://127.0.0.1:8000/health
- 데이터는 `data/classroom.db`에 보존됩니다. 실행 위치와 무관하게 프로젝트 내 경로를 사용합니다.

Swagger에는 명세의 9개 REST API와 추가 반 목록·통합 조회 API, SSE 및 상태 확인 경로가 표시됩니다. 성공 응답 모델과 공통 오류 모델도 확인할 수 있습니다. 입력 검증 오류는 실제 동작과 동일하게 `400`으로 문서화하며, 교실 최초 설정의 `201` 및 SSE의 `text/event-stream`도 구분합니다.

API 키와 관리자 키를 같은 값으로 설정하면 권한을 구분할 수 없어 서버 시작 시 오류로 처리합니다. 이동 수정의 `sourcePeriod`에 시간표에 없는 교시를 지정하거나 SSE에 잘못된 반 번호를 보내면 `400 INVALID_REQUEST`를 반환합니다.

이동 수정은 입력한 교시와 겹치는 현재·미래 이동을 입력한 교시와 장소로 변경합니다. 겹치지 않는 이동은 유지하며, 겹치는 이동이 없으면 404를 반환합니다. 여러 이동과 겹치면 해당 이동들을 입력한 범위의 한 건으로 변경합니다. 디스코드에서는 `/수정 교시:9~10 장소:도서관`처럼 입력하며, 멘션이 없으면 본인, 있으면 멘션된 학생만 수정합니다. 기존 API의 `sourcePeriod` 선택 방식은 호환성을 위해 유지합니다.

## 설정

### 데이터베이스 구조

[Notion ERD](https://app.notion.com/p/3e071cdf7d7d8036bf2cef004a619ce6)의 세 테이블 구조를 SQLite에 적용했습니다.

- `classroom`: `guild_id`, `class_number`, `total_students`
- `movement`: 학생·날짜·교시별 한 행. `6~8교시`는 6, 7, 8교시의 세 행으로 저장합니다. 서버·학번·날짜·교시의 UNIQUE 제약으로 중복을 차단합니다.
- `analog_count`: 서버·날짜별 인원. 음수 금지 및 교실 외래키를 적용합니다.

`registration_id`와 `period_string`은 범위 이동 전체의 수정·복귀와 원래 입력 보존을 위해 추가했습니다. `movement_ranges`는 이를 묶어서 기존 API 응답을 만드는 조회 뷰이며, `metadata`는 일일 초기화 기준 날짜를 저장합니다. 반당 서버 하나라는 기존 규칙에 따라 반 번호의 UNIQUE 제약도 유지합니다.

기존 DB는 서버 시작 시 원본을 `<DB 경로>.legacy-backup.db`로 백업한 뒤 트랜잭션으로 변환합니다. 변환 실패 시 원래 구조와 데이터를 유지하며 시작을 중단합니다. 이전 날짜 기록은 변환 후 기존 일일 초기화 규칙에 따라 제거됩니다. 서버를 중지한 상태에서 버전을 교체하세요.

로컬 기본 DB는 SQLite이고 DATABASE_URL을 설정한 배포 환경은 PostgreSQL을 사용합니다. `docs/schema.mysql.sql`은 API 길이 제한과 추가 식별자를 반영한 MySQL 참고 DDL이며, MySQL 연결 기능은 포함하지 않습니다.

Vercel에서는 API_KEY와 ADMIN_API_KEY를 반드시 서로 다른 값으로 설정합니다. 로컬 개발의 인증 생략 옵션은 그대로 유지합니다.

환경변수는 PowerShell에서 `$env:변수명 = '값'`으로 지정합니다. `.env` 자동 로딩은 하지 않습니다.

| 변수 | 기본값 / 설명 |
| --- | --- |
| `DATABASE_URL` | PostgreSQL 연결 문자열. 설정하면 DATABASE_PATH보다 우선. 비밀값이므로 환경변수로만 지정 |
| `DATABASE_PATH` | 프로젝트의 `data/classroom.db` |
| `SCHEDULE_PATH` | 프로젝트의 `config/schedule.json` |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` (쉼표 구분) |
| `API_KEY` | 미설정: 로컬 개발용 인증 비활성화. 설정하면 일반 API의 `X-API-Key` |
| `ADMIN_API_KEY` | 교실 설정용 키. `API_KEY`와 함께 설정해야 함 |

인증 방식은 원문에서 미정이므로 선택형 API 키를 추가했습니다. 운영 시 두 키를 서로 다른 값으로 설정하세요. 관리자 키는 모든 API를 호출할 수 있고, 일반 키는 `PUT /classroom`을 호출할 수 없습니다. `/health`, `/docs`, `/openapi.json`은 공개됩니다. API 키는 서버 간 연결용이며 학생별 본인 인증은 구현하지 않습니다.

## 시간표

사용자가 제공한 시간표를 `config/schedule.json`에 반영했습니다. 모든 날짜 판정은 `Asia/Seoul`입니다.

| 구분 | 시간 |
| --- | --- |
| 5교시 | 13:30~14:20 |
| 쉬는시간 | 14:20~14:30 |
| 6교시 | 14:30~15:20 |
| 쉬는시간 | 15:20~15:30 |
| 7교시 | 15:30~16:20 |
| 쉬는시간 | 16:20~16:40 |
| 8교시 | 16:40~17:30 |
| 석식시간 | 17:30~18:40 |
| 9교시 | 18:40~19:30 |
| 쉬는시간 | 19:30~19:40 |
| 10교시 | 19:40~20:30 |

현재 5~10교시를 지원합니다. 1~4교시는 시간표에 없어 등록할 수 없습니다. 쉬는시간은 표의 각 구간을 따르며 7교시와 8교시 사이는 20분입니다. 명세의 `3교시`는 형식 예시이며 현재 설정에서는 유효하지 않습니다. 시간표를 변경하면 서버를 재시작하세요.

- 시작 시각 포함, 종료 시각 제외. 목록에는 현재 이동과 오늘 아직 끝나지 않은 교시의 예약이 표시됩니다. 쉬는시간·석식시간에도 미래 예약을 표시하며, 현재 디지털 이동 인원은 0명으로 계산합니다. 수업 시작 전에는 오늘 예약 전체, 마지막 교시 종료 후에는 빈 목록을 반환합니다.
- `6교시`, `6~8교시`, `6-8교시`를 지원합니다. 설정에 없는 교시나 역순 범위는 400입니다.
- 23:59:59에 이동 기록과 아날로그 인원만 초기화합니다. 로컬 서버는 백그라운드 작업이 0.5초 간격으로 확인하므로 실제 실행은 약간 늦을 수 있습니다. Vercel은 상시 작업 없이 다음 데이터 요청에서 초기화합니다.
- 서버가 꺼져 있었다면 재시작 또는 첫 데이터 접근 시 이전 날짜 데이터를 제거합니다. 교실 설정과 전체 학생 수는 유지합니다.
- 아날로그 인원은 교시 범위 정보가 없어 수동 감소 또는 일일 초기화까지 유지합니다.

## API 연결

| Method | 경로 | 매개변수 / 본문 |
| --- | --- | --- |
| PUT | `/classroom` | JSON: `guildId`, `classNumber`, `totalStudents` |
| GET | `/classroom` | query: `guildId` |
| POST | `/move` | JSON: `students`, `placeAndPeriod`; query: `guildId` 필수. 기존 `periodString`, `place` 형식도 지원 |
| PATCH | `/move` | query: `guildId`, `studentNumber`, 선택 `sourcePeriod`; JSON: `periodString`, `place` |
| DELETE | `/move` | query: `guildId`, `studentNumber`; 본문 없음. 해당 학생의 당일 이동 전체 삭제 |
| PATCH | `/analog-count` | query: `guildId`; JSON: `change` |
| GET | `/student-counts` | query: `classNumber` |
| GET | `/movedStudents` | query: `classNumber` |
| GET | `/period` | 없음 |
| GET | `/classes` | 없음. 등록된 반 목록 |
| GET | `/classes/{class_number}` | 경로의 반 코드. 해당 반의 통합 현황 |
| GET | `/health` | 없음. 서버 응답 확인 |
| GET | `/events` | query: `classNumber`; SSE 스트림 |

`GET /classroom`, `GET /student-counts`, `GET /movedStudents`는 원문 JSON 본문도 호환하지만 브라우저에서는 쿼리를 사용하세요. 수정/복귀는 `studentNumber=1215&studentNumber=1208`처럼 여러 학생을 지정할 수 있습니다. 수정의 기본 대상은 현재 교시의 이동입니다. 복귀는 교시를 받지 않고 대상 학생의 당일 이동 전체를 삭제하므로 쉬는시간에도 가능합니다. 수정에서 특정 이동을 선택하려면 `sourcePeriod=9`처럼 기존 이동의 교시를 지정합니다. 복귀에는 `sourcePeriod`를 사용하지 않습니다. 한 학생 수정 응답은 원문의 `movement`, 여러 학생은 `movements` 배열입니다.

학번은 숫자 문자열입니다. 이동 등록의 쿼리 `guildId`는 필수입니다. 봇은 명령어가 실행된 디스코드 서버 ID를 매번 보내야 합니다. 백엔드는 교실 설정에 저장된 서버 ID로 반을 찾으며 학번 앞자리로 반을 추정하지 않습니다. 누락·형식 오류는 400, 미등록 서버는 404입니다. 학생의 소속/본인 확인은 하지 않습니다. 서버당 한 반, 반당 한 서버를 지원합니다.

### 호출 예시

Notion 명세의 `placeAndPeriod` 입력을 지원합니다. `8~10교시 도서관`, `8-10교시 도서관`, `8교시~10교시 도서관`, `8교시-10교시 도서관`을 모두 교시 범위와 장소로 분리합니다. 양쪽에 교시를 쓴 범위는 `8~10교시`로 합치고 장소는 `도서관`으로 저장합니다. `8교시 ~ 10교시 도서관`처럼 구분자 앞뒤 공백도 허용합니다. 통합 입력에서 `교시`를 모두 생략하는 형식은 지원하지 않습니다. 종료 교시·장소 누락, 역순 범위와 시간표에 없는 교시는 400입니다. 분리 입력 `periodString`·`place`도 계속 지원하며 두 입력 형식을 섞으면 400입니다.

```json
{
  "students": [{"studentNumber": "1215", "name": "홍길동"}],
  "placeAndPeriod": "8-9교시 임베 2실"
}
```

```powershell
$base = 'http://127.0.0.1:8000'
Invoke-RestMethod "$base/classroom" -Method Put -ContentType 'application/json; charset=utf-8' -Body '{"guildId":"100","classNumber":12,"totalStudents":16}'
Invoke-RestMethod "$base/move?guildId=100" -Method Post -ContentType 'application/json; charset=utf-8' -Body '{"students":[{"studentNumber":"1215","name":"홍길동"}],"placeAndPeriod":"6~8교시 도서관"}'
Invoke-RestMethod "$base/movedStudents?classNumber=12"
Invoke-RestMethod "$base/student-counts?classNumber=12"
Invoke-RestMethod "$base/move?guildId=100&studentNumber=1215" -Method Delete
```

### 실시간 화면 갱신

```javascript
// 인증 비활성화 로컬 개발 예시
const events = new EventSource('http://127.0.0.1:8000/events?classNumber=12');
events.addEventListener('snapshot', (event) => {
  const { period, movements, counts } = JSON.parse(event.data);
  console.log(period, movements.movedStudents, counts);
});
// 화면 해제 시 events.close();
```

연결 직후 전체 스냅샷을 전달하고, 이동·인원·교시·날짜가 바뀌면 약 1초 내 새 스냅샷을 보냅니다. 15초마다 heartbeat를 보내며 재연결 시 최신 전체 상태를 전송합니다. DB를 다시 조회하므로 여러 서버 워커에서도 갱신을 확인할 수 있습니다. API 키 활성화 시 기본 EventSource는 사용자 정의 헤더를 지원하지 않으므로 `fetch` 스트림 또는 같은 출처의 백엔드 프록시로 `X-API-Key`를 전달해야 합니다. 키를 URL에 넣지 않습니다.

## 데이터 처리 규칙

- 여러 학생의 등록·수정·복귀는 하나의 트랜잭션입니다. 한 명이라도 실패하면 전체 요청이 취소됩니다.
- 동일 학생의 겹치는 교시는 409, 겹치지 않는 교시는 여러 건 등록 가능합니다.
- SQLite 쓰기 트랜잭션으로 동시 요청의 중복 등록과 인원 초과를 방지합니다.
- 디지털 + 아날로그가 전체 인원을 넘지 않도록 예약된 모든 교시를 검사합니다. 미래 교시의 인원 초과도 거절합니다.
- 끝난 이동은 표시 목록에서 제외합니다. 다만 그 학생의 다른 미래 예약은 계속 표시합니다. 이동 인원은 현재 교시만 기준으로 집계하며 기록은 일일 초기화 때 삭제합니다.
- 공통 오류: `{"success":false,"error":{"code":"...","message":"..."}}`. 입력 오류는 명세대로 422 대신 400입니다.

## 검증

이 폴더는 GitHub 업로드·배포용입니다. 테스트 코드와 테스트용 패키지 목록은 상위 inp_BE 폴더에서 별도로 관리합니다.

상위 폴더의 테스트를 현재 배포 코드로 실행하려면 move-it 폴더에서 다음 명령을 사용하세요.

```powershell
..\.venv\Scripts\python.exe -m pip install -r ..\requirements-dev.txt
..\.venv\Scripts\python.exe -m pytest ..\tests -q
```

이 저장소에는 코드와 실행 문서를 포함합니다. Notion 원문과 실제 구현 사이의 추가 동작은 이 README의 API·데이터 처리 규칙을 확인하세요.


## 반별 통합 조회

프론트엔드는 `GET /classes/12`로 1학년 2반의 현황을 한 번에 조회할 수 있습니다. 2학년 1반은 `/classes/21`입니다. 반 코드는 `PUT /classroom`의 `classNumber`와 동일합니다.

응답에는 `classNumber`, `date`, `period`, `counts`, `movements`가 포함됩니다. DB의 고유 반 번호로 교실을 찾고 해당 교실의 서버 ID에 연결된 데이터만 반환합니다. 잘못된 반 코드는 400, 미등록 반은 404입니다. API 키 설정 시 기존 API와 동일하게 X-API-Key 헤더가 필요합니다.

```javascript
const response = await fetch('http://192.168.1.19:8000/classes/12');
const data = await response.json();
if (!response.ok) throw new Error(data.error.message);
console.log(data.period, data.counts, data.movements.movedStudents);
```


복귀 예시: DELETE /move?guildId=100&studentNumber=1215 — 교시 입력 없이 해당 서버 학생의 당일 이동 전체를 삭제합니다. 다른 교시의 예약 이동도 함께 삭제되며 다른 학생과 반은 유지합니다.


## 등록된 반 선택 연결

GET /classes는 파라미터 없이 등록된 반 목록을 반환합니다. 응답은 success와 classes 배열이며 각 항목에는 classNumber, className, totalStudents가 있습니다. 반 번호순으로 정렬하고 빈 목록은 HTTP 200과 빈 배열로 반환합니다. API 키가 설정된 경우 기존과 동일하게 인증합니다.

프론트에서는 목록이 하나면 자동 선택하고 여러 개면 선택 UI를 표시하도록 구현한 뒤 GET /classes/{classNumber}를 호출합니다. 선택한 반 코드는 프론트의 서버 주소별 localStorage에 저장하고 재접속 시 현재 목록에 존재하는지 검증하도록 구현할 수 있습니다. 목록 조회와 선택 저장 UI는 백엔드가 자동으로 수행하지 않습니다. 디스코드 서버 ID를 프론트 코드에 고정하거나 GET /classroom을 먼저 호출할 필요가 없습니다.


## 이동 학생 목록 정렬

기존 movements.movedStudents 배열을 시작 교시 → 종료 교시 → 정식 장소명 → 학번 순으로 정렬합니다. 따라서 같은 교시 범위와 장소의 학생이 배열에서 연속으로 나옵니다. /movedStudents, /classes/{classNumber}, /events에 동일하게 적용합니다. 과거 약어 기록도 조회 시 정식 이름으로 변환하되 DB 기록은 변경하지 않습니다.

별도 groups 배열과 GroupStudent, PlaceGroup 모델은 제거했습니다. 프론트는 기존 학생 배열의 순서를 유지하여 표시하면 됩니다. 자체적으로 학번순 재정렬을 하면 장소별 연속 순서가 유지되지 않습니다.


## 장소 약어 변환

app/service.py의 normalize_place()에 약어와 정식 이름을 등록합니다. 약어 키는 공백 없이 작성합니다. 입력을 비교할 때 공백을 제거하므로 임베2실과 임베 2실이 모두 같은 정식 이름으로 변환됩니다. 목록에 없는 장소는 앞뒤 공백만 제거하고 내부 띄어쓰기는 유지합니다.

등록·수정 시 변환된 값을 DB와 응답에 사용하며, 과거 약어 기록은 조회 시에도 변환합니다. 현재 목록의 임베2실은 임베디드 2실로 표시됩니다. 장소 목록은 학교에서 쓰는 실제 이름을 확인하여 관리하세요.

## 같은 Wi-Fi에서 연결

다른 컴퓨터가 접속하도록 하려면 프로젝트 폴더에서 다음과 같이 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

친구는 http://서버PC의IPv4주소:8000/docs에 접속합니다. 127.0.0.1은 접속한 컴퓨터 자신을 뜻합니다. 서버 IP는 변경될 수 있으므로 ipconfig로 확인하세요. 서버 터미널을 열어 두고 Windows 방화벽에서 필요한 네트워크 접근을 허용해야 합니다.

브라우저 프론트는 CORS_ORIGINS에도 실행 주소가 등록되어야 합니다. 기본값은 localhost:3000과 localhost:5173입니다. 다른 주소로 프론트를 열면 기존 허용 주소와 함께 환경변수에 추가하고 서버를 재시작하세요.


## 현재 이동과 미래 예약 표시

/movedStudents, /classes/{classNumber}, /events의 movements.movedStudents는 현재 교시의 이동과 오늘의 미래 예약을 함께 반환합니다. 예를 들어 8교시에는 8~10교시 이동, 9교시 예약, 10교시 예약이 함께 표시됩니다. 쉬는시간·석식에는 아직 종료되지 않은 다음 교시 이후 예약을 반환합니다. 원래 등록한 startPeriod와 endPeriod를 유지하며 끝난 이동은 제외합니다.

counts.movedStudents 및 /student-counts는 현재 교시의 디지털 이동 인원과 아날로그 인원만 계산합니다. 미래 예약은 인원에 포함하지 않습니다. 한 학생의 서로 다른 예약이 목록에 여러 건 나올 수 있으므로 프론트는 목록 길이로 현재 이동 인원을 계산하지 말고 counts를 사용해야 합니다. 미래 예약까지 포함하는 서버 정원 검사는 기존대로 유지합니다.


## Vercel + Neon 배포

API 경로와 요청·응답 형식은 유지합니다. DATABASE_URL이 있으면 PostgreSQL, 없으면 로컬 SQLite를 사용합니다. Vercel에서는 DATABASE_URL과 서로 다른 API_KEY, ADMIN_API_KEY가 필수이며 SQLite로 자동 대체하지 않습니다. 실제 연결 문자열은 GitHub나 채팅에 올리지 마세요.

1. 이 README, requirements.txt, vercel.json, .python-version이 있는 폴더를 GitHub 저장소의 배포 루트로 사용합니다. 현재 로컬 위치는 inp_BE/move-it입니다. .venv, 실제 DB 파일, .env 및 비밀키는 커밋하지 않습니다.
2. Vercel에서 New Project로 GitHub 저장소를 연결합니다. 저장소 루트에 app/가 있으면 Root Directory는 기본값을 사용합니다. move-it/이 저장소 하위 폴더라면 Root Directory를 move-it으로 지정합니다.
3. Framework Preset은 FastAPI입니다. 기존 app/main.py를 자동 인식하며 별도 프론트 빌드·Output Directory를 지정하지 않습니다. 싱가포르 Neon 프로젝트에 맞춰 vercel.json의 region을 sin1로 설정했습니다.
4. 환경변수에 DATABASE_URL(Neon Connect에서 복사한 PostgreSQL URL 전체), API_KEY(일반 봇·조회 키), ADMIN_API_KEY(별도 관리자 키), CORS_ORIGINS(프론트 실행 주소들, 쉼표 구분)를 설정합니다. URL에는 postgresql://부터 sslmode=require 등의 옵션까지 포함하고 psql 명령어 또는 따옴표는 넣지 않습니다. Neon의 pooled connection URL을 사용할 수 있습니다.
5. Deploy 후 /health와 /docs를 확인합니다. /health는 프로세스 응답 확인만 하며 DB 접속 성공을 보장하지 않습니다.
6. /classes를 X-API-Key 헤더와 함께 호출합니다. 첫 DB 요청에서 PostgreSQL 테이블과 뷰를 자동 생성합니다. 빈 DB이면 success=true, classes=[]입니다. 이 요청이 성공해야 DB 연결까지 확인된 것입니다.
7. 관리자 키로 PUT /classroom을 호출해 실제 디스코드 서버 ID·반 번호·전체 인원을 다시 등록합니다. 기존 SQLite 파일은 자동 이전되지 않습니다.
8. 봇과 프론트의 백엔드 주소를 배포된 HTTPS 주소로 바꿉니다. 모든 데이터 API에 X-API-Key가 필요합니다. 관리자 키는 프론트에 넣지 않습니다. 일반 키는 반별 권한 인증이 아니며 배포 보호가 켜져 있으면 외부 봇 접근 가능 여부도 확인해야 합니다.

### Vercel 실행 규칙

Vercel에서는 0.5초 주기 백그라운드 작업을 실행하지 않습니다. 날짜와 23:59:59 초기화 조건은 각 데이터 트랜잭션에서 확인합니다. 요청이 없는 동안 정확한 시각에 물리적으로 삭제하는 방식은 아니지만, 다음 현황 요청은 이전 날짜 이동이 없는 상태를 반환합니다. 교실 설정은 유지합니다.

SSE는 Vercel에서 약 45초마다 연결을 정상 종료합니다. EventSource는 다시 연결할 수 있으며 사용자 정의 헤더가 필요한 인증 환경에서는 프론트의 fetch 스트림 재연결 처리가 필요합니다. 학교 테스트에서는 기존 GET /classes/{반번호}의 5초 주기 조회를 권장합니다. API 주소는 동일하게 유지합니다.

PostgreSQL에서는 트랜잭션 단위 advisory lock으로 날짜 초기화·정원 검사·등록 ID 부여를 여러 인스턴스 사이에서도 직렬화합니다. 소규모 서비스의 정확성을 우선한 설계이며 대규모 처리에서는 반별 잠금 등 추가 개선이 필요합니다. 접속 정보는 응답·로그에 출력하지 않습니다.

### 배포 전 로컬 검증

실제 Neon 연결 문자열을 PowerShell의 DATABASE_URL 환경변수로 설정하고 필수 패키지를 설치한 뒤 기존 Uvicorn 명령으로 실행하면 로컬에서도 PostgreSQL을 사용합니다. .env 파일 자동 로딩은 지원하지 않습니다.

기존 데이터 이전은 별도 작업입니다. 우선 빈 Neon DB에 교실 정보를 새로 등록하는 배포 절차를 제공합니다. Preview와 Production이 같은 Neon DB를 쓰면 미리보기에서도 운영 데이터가 변경되므로 Preview에는 별도 Neon 브랜치를 권장합니다.

선택적으로 상위 폴더의 tests/test_postgres_integration.py는 TEST_POSTGRES_URL에 직접 연결 URL(pooler가 아닌 URL)을 지정한 전용 테스트 DB에서 실행합니다. 임시 스키마를 만들고 제거하며 운영 Neon URL을 테스트 변수에 넣지 마세요. 연결 정보가 없으면 해당 테스트는 건너뜁니다.
