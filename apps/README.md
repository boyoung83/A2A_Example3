# apps — K정밀웍스 ↔ K-Logistics 회사 간 업무 시스템

「A2A 프로토콜 실전 가이드」 Part 3(Ch13~17) 실습을 "실제로 쓰는" 회사 간 업무 형태로 옮긴 에이전트 코드입니다. 실행에는 이 저장소의 `apps/`와 `pyproject.toml`만 있으면 됩니다(`uv sync` 후 `uv run`).

```text
apps/
├── common/
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

에이전트를 임시 포트(9860~9862)와 임시 DB로 직접 띄워 시나리오 13개를 돌리고 `13/13 PASS`를 출력합니다. 이미 띄워 둔 서버와 겹치지 않습니다.

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

기존 실습 클라이언트 방식(텍스트 한 줄)도 그대로 받습니다. `call_router.py`의 주소를 9820으로 바꾸면 같은 결과가 나옵니다.

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
