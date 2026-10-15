# apps — K정밀웍스 ↔ K-Logistics 회사 간 업무 시스템

「A2A 프로토콜 실전 가이드」 Part 3(Ch13~17) 실습을 "실제로 쓰는" 회사 간 업무 형태로 옮긴 에이전트 코드입니다. 실행에는 이 저장소의 `apps/`와 `pyproject.toml`만 있으면 됩니다(`uv sync` 후 `uv run`).

```text
apps/
├── common/
│   ├── auth.py        # 파트너 API Key 인증(Ch18) — 키 확인·카드 표시·Task 조회 차단
│   ├── keystore.py    # 발급한 키 장부(해시 저장, 교체·폐기 즉시 반영)
│   ├── keys_web.py    # 키 장부 화면 API (콘솔·포털 공통) + static/keys.js
│   ├── contracts.py   # 스킬별 요청·응답 계약 (pydantic)
│   ├── a2a_io.py      # 서버: SkillExecutor(스킬 → 업무 함수 → Task 상태) / 클라이언트: call_skill
│   └── db.py          # 회사별 SQLite (WAL)
├── klogistics/        # K-Logistics
│   ├── service.py     # 업무 로직 + DB (견적·출고·취소·배송 조회·상태 진행, 업무키 멱등)
│   ├── pricing.py     # 요금 규칙
│   ├── agent.py       # A2A 에이전트 9810   ← 회사 간 연동에 필요한 것은 이것뿐
│   ├── cli.py         # 운영 CLI
│   └── web.py         # 운영 콘솔 9811 (선택) + static/console.html
├── kprecision/        # K정밀웍스
│   ├── service.py     # 업무 로직 + DB (발주·견적·승인·출고 요청·추적·재시도)
│   ├── agent.py       # A2A 구매 에이전트 9820 (외부 배송 문의 위임, 기존 call_router 호환)
│   ├── cli.py         # 구매 CLI
│   └── web.py         # 구매 포털 9821 (선택) + static/portal.html
├── keys.py            # 파트너 API Key 발급·교체·폐기 도구
├── scenarios/
│   ├── run_all.py     # 시나리오 S0~S3, E1~E9 자동 판정 (웹·관제 없이)
│   └── bad_partner.py # E6 계약 위반 실험용 물류사
└── data/              # 기본 DB 파일 (처음 실행 시 생김, 지워도 시드로 다시 생김)
```

## 구조 한 줄 요약

웹 화면과 CLI는 같은 `service.py`를 부르고, 회사 사이는 A2A(`agent.py`)로만 오갑니다. 그래서 웹 화면(9811·9821)이나 관제 허브(9900)를 꺼도 연동은 그대로 동작합니다.

## 자동 검증 (먼저 해 보기)

```cmd
cd A2A_Example3
uv sync
uv run apps\scenarios\run_all.py
```

에이전트를 임시 포트(9860~9862)와 임시 DB로 직접 띄워 시나리오 20개(인증·권한 4개, 키 교체·폐기 3개 포함)를 돌리고 `20/20 PASS`를 출력합니다. 이미 띄워 둔 서버와 겹치지 않습니다.

## 손으로 해 보기 — 웹 화면 없이

```cmd
:: 터미널 1, 2 — 회사마다 A2A 에이전트 하나
uv run apps\klogistics\agent.py
uv run apps\kprecision\agent.py

:: 터미널 3 — K정밀웍스 구매 담당자 (발주번호를 생략하면 자동 채번, `order next-no`로 미리 보기)
uv run apps\kprecision\cli.py order create ORDER-2001 --item 기어박스 --qty 10 --weight 120 --from 창원 --due 2026-10-25
uv run apps\kprecision\cli.py quote request ORDER-2001         :: 도착지가 없어 물류사가 되묻는다 (INPUT_REQUIRED)
uv run apps\kprecision\cli.py quote input ORDER-2001 --to 평택   :: 같은 Task로 이어 보낸다
uv run apps\kprecision\cli.py order approve ORDER-2001
uv run apps\kprecision\cli.py shipment request ORDER-2001

:: 터미널 4 — K-Logistics 운영 담당자
uv run apps\klogistics\cli.py shipment list
uv run apps\klogistics\cli.py shipment advance SHP-0003 --memo "창원 집하"

:: 터미널 3
uv run apps\kprecision\cli.py delivery check ORDER-2001         :: 집하 완료로 반영
uv run apps\kprecision\cli.py sync                              :: 대기 요청 재전송 + 배송 중 발주 전부 동기화 (변경 없는 조회는 기록 안 함)
uv run apps\kprecision\cli.py cities                            :: 물류사 운행 도시
uv run apps\kprecision\cli.py request list                      :: 물류사에 보낸 요청(업무키·taskId·상태)
```

기존 실습 클라이언트 방식(텍스트 한 줄)도 그대로 받습니다. 단, 구매 에이전트(9820)는 사내 시스템 키(`KP_API_KEY`)가 있어야 합니다.

## 웹 화면으로

```cmd
uv run apps\kprecision\web.py     :: http://127.0.0.1:9821  구매 포털
uv run apps\klogistics\web.py     :: http://127.0.0.1:9811  운영 콘솔
```

- 포털: "새 발주"를 누르면 발주번호가 자동 채번(`ORDER-` 뒤 가장 큰 번호 + 1)되어 채워지고, 고쳐 써도 됩니다(중복이면 거부). 출발지·도착지는 물류사가 A2A `service-area`로 알려 준 운행 도시 콤보박스(물류사가 꺼져 있으면 마지막 목록). 발주 등록·수정 → 견적 요청 → (되물으면 입력 폼) → 승인 → 출고 요청 → 배송 조회·입고 확인, 물류사 요청 내역과 "다시 보내기", 자동 동기화(60초)
- 콘솔: 출고 건 목록·상세, 다음 단계로 진행(메모), 접수된 견적과 만료 처리
- 주소 뒤에 `#ORDER-2001`·`#SHP-0003`을 붙이면 그 건을 바로 엽니다.
- 요청 내역의 taskId를 누르면 관제 화면(`?snapshot=1&select=…`)으로 갑니다(허브가 켜져 있을 때).

## 관제 화면과 함께 (선택)

관제 허브 `obs/`는 이 저장소에 없습니다. 원본 실습 폴더(`part3_orchestration/obs`)를 옆에 두고 쓸 때의 방법입니다. 에이전트만 `obs_run.py`로 감쌉니다. 웹·CLI는 감싸지 않습니다(웹은 A2A 에이전트가 아니라서 허브가 '꺼짐'으로 표시함).

```cmd
cd obs & uv run obs_hub.py & cd ..
uv run obs\obs_run.py apps\klogistics\agent.py
uv run obs\obs_run.py apps\kprecision\agent.py
```

포털·CLI에서 보낸 A2A 요청도 관제 화면에 나오게 하려면 CLI만 감쌀 수 있습니다: `uv run obs\obs_run.py apps\kprecision\cli.py quote request ORDER-2001`.

## 인증 (Ch18 방식 — 파트너 API Key)

회사 간 A2A 호출은 상대가 발급한 키를 `X-Partner-Key` 헤더에 실어야 합니다. Agent Card에 이 요구가 적혀 있고(`securitySchemes`), 카드 조회 자체는 인증 없이 열려 있습니다.

| 호출 | 받는 쪽이 확인하는 키 | 보내는 쪽 키 |
|---|---|---|
| K정밀웍스 → K-Logistics (9810) | `KL_PARTNER_KEYS` (화주 이름=키, …) | `KL_API_KEY` |
| 사내 시스템 → K정밀웍스 구매 에이전트 (9820) | `KP_CLIENT_KEYS` | `KP_API_KEY` |

- 키가 없거나 틀리거나 폐기·만료됐으면 일을 시작하기 전에 **REJECTED** `{"reason": "unauthenticated"}`. `tasks/get`·`cancel`·`subscribe`도 인증 없이는 "없는 Task".
- **권한**: 키로 확인된 화주는 자기 출고 건·견적만 조회·사용·취소합니다. 남의 주문은 "결과 없음", 남의 견적·출고 건은 "없는 건"으로 답해 존재 여부도 알려 주지 않습니다. 화주 이름은 요청 본문이 아니라 키로 정하고, 업무키도 화주별로 따로 셉니다. Task도 화주별로 나뉘어 남의 Task를 조회할 수 없습니다.
- 구매 에이전트는 받은 키를 물류사로 넘기지 않고 자기 키(`KL_API_KEY`)를 씁니다. 키는 상대 카드가 요구할 때만 보냅니다.
- 환경변수가 없으면 로컬 개발용 `apps/data/secrets.json`을 씁니다(처음 실행 때 무작위 키로 생성, git 제외). 한 PC에서 "발급한 키를 전달했다"를 흉내 낸 것이므로, 실제로 회사를 나눠 띄울 때는 환경변수로 넣습니다.
- 포털 상단에 "파트너 키 인증 / 키 없음"이 표시되고, 인증 실패는 요청 내역에 "물류사 인증 실패"로 남습니다.
- 웹 화면(9811·9821)은 각 회사 내부용이라 이번 범위에서는 로그인을 두지 않았습니다(127.0.0.1에서만 열림).

```cmd
:: 예) 회사를 나눠 띄울 때
set KL_PARTNER_KEYS=K정밀웍스=kl-여기에-발급한-키
uv run apps\klogistics\agent.py
:: (다른 PC/터미널) K정밀웍스
set KL_API_KEY=kl-여기에-발급한-키
uv run apps\kprecision\web.py
```

### 키 발급·교체·폐기 (`apps/keys.py`)

키를 발급한 회사가 자기 DB의 **키 장부**로 관리합니다. 장부에는 키 원문 대신 SHA-256 해시만 남고, 에이전트는 요청마다 장부를 확인하므로 **재시작 없이 바로 반영**됩니다. 장부는 처음 비어 있을 때만 환경변수·`secrets.json`에서 키를 들여옵니다.

```cmd
uv run apps\keys.py kl list                                   :: K-Logistics가 화주에게 발급한 키 (kp: K정밀웍스가 사내 시스템에 발급한 키)
uv run apps\keys.py kl issue K전자 --expires-hours 720         :: 새 화주에게 30일짜리 키 발급
uv run apps\keys.py kl rotate K정밀웍스 --grace-hours 24 --deliver   :: 교체: 새 키 발급, 옛 키는 24시간 뒤 만료
uv run apps\keys.py kl revoke KLK-001 --reason "교체 완료"       :: 키 하나 즉시 폐기
uv run apps\keys.py kl revoke --partner K전자 --reason "계약 종료"  :: 그 화주의 키 전부 폐기
```

- **화면에서도 같은 일을 합니다**: 운영 콘솔(9811) 맨 아래 "파트너 키"(= `kl`), 구매 포털(9821) 맨 아래 "사내 시스템 키"(= `kp`).
  대상 이름을 넣고 [키 교체](유예 시간 지정) · [새 키 발급](유효 시간 지정) · [대상 키 전부 폐기], 표의 [폐기]로 키 하나 폐기.
  새 키 원문은 창에 한 번만 뜨고 [복사]할 수 있습니다. "새 키를 받는 쪽에 전달(개발용)"은 `--deliver`와 같습니다.
  화면은 각 회사 내부용(127.0.0.1)이라 로그인이 없습니다 — 외부에 열려면 관리자 로그인을 먼저 붙여야 합니다.
- 상태: 유효 · 만료 예정(교체 유예기간 또는 유효기간 지정 — 그때까지는 통함) · 만료 · 폐기. `list`에 최근 사용 시각이 나와 옛 키가 아직 쓰이는지 볼 수 있습니다.
- 키 원문은 발급할 때 한 번만 출력됩니다. `--deliver`는 로컬 개발용으로 받는 쪽 키 파일(`secrets.json`)에 넣어 주며, K정밀웍스는 요청마다 키를 다시 읽어 바로 새 키를 씁니다(`KL_API_KEY` 환경변수가 있으면 그쪽이 우선).
- 에이전트 콘솔에 `인증 통과: K정밀웍스 → quote` / `인증되지 않은 요청 거부`가 찍힙니다.

## 설정 (환경변수)

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `KL_AGENT_PORT` / `KP_AGENT_PORT` | 9810 / 9820 | A2A 에이전트 포트 |
| `KL_WEB_PORT` / `KP_WEB_PORT` | 9811 / 9821 | 웹 화면 포트 |
| `KL_AGENT_URL` | http://127.0.0.1:9810 | K정밀웍스가 부를 물류사 주소 |
| `KL_DB` / `KP_DB` | apps/data/*.db | 회사별 DB 파일 |
| `KL_SEED` / `KP_SEED` | 1 | 0이면 ORDER-1001·1002 시드를 넣지 않음 |
| `KP_PARTNER_TIMEOUT` | 15 | 물류사 응답 대기(초), 넘으면 PENDING_RETRY |
| `KL_FAIL_ORDERS` | (없음) | 실험용: 이 주문번호는 물류사 내부 오류(E8) |
| `KL_PARTNER_KEYS` / `KL_API_KEY` | secrets.json | 물류사가 받을 키 목록 / K정밀웍스가 보낼 키 |
| `KP_CLIENT_KEYS` / `KP_API_KEY` | secrets.json | 구매 에이전트가 받을 키 목록 / 사내 시스템이 보낼 키 |
| `A2A_SECRETS_FILE` | apps/data/secrets.json | 로컬 개발용 키 파일 위치 |

## 회사 간 계약 요약

메시지마다 DataPart `{"skill": "...", ...}` + 사람이 읽을 TextPart 한 줄. 업무키 `request_id`가 같으면 물류사는 처음 결과를 그대로 돌려줍니다.

| 스킬 (K-Logistics) | 완료 | 그 외 |
|---|---|---|
| `check-delivery` `{order_no}` (텍스트도 가능) | `{found, status, eta, history}` | — |
| `quote` `{request_id, order_no, weight_kg, origin, destination, due_date, express}` | `{quote_id, price_krw, eta_days, valid_until}` | INPUT_REQUIRED `{missing}`, REJECTED `unsupported_route` |
| `create-shipment` `{request_id, quote_id, order_no, contact}` | `{shipment_id, status}` | REJECTED `quote_expired` · `unknown_quote` · `order_mismatch` |
| `cancel-shipment` `{request_id, shipment_id, reason}` | `{status: CANCELLED}` | REJECTED `already_picked_up` · `unknown_shipment` |
| `service-area` `{}` | `{carrier, cities}` | — (포털 출발지·도착지 콤보박스, `cli.py cities`) |

K정밀웍스 발주 상태: 작성 → 견적 수신 → 승인 → 출고 요청 → 배송 중 → 배송 완료 → 입고 완료 (+ 취소)
