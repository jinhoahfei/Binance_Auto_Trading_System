# Backend 운영 로그

LIVE와 Testnet runtime을 만들 때 이 디렉터리에 로그 파일이 자동 생성됩니다.
소스 실행은 현재 작업 디렉터리와 무관하게 프로젝트 루트의 `Log_History`를 사용합니다.
이 Mac의 저장 위치는 `/Users/oscar/Desktop/Binance_Auto/Log_History`입니다.
패키지 실행도 `~/Desktop/Binance_Auto`가 있으면 같은 위치를 사용하며,
프로젝트가 없는 설치에서는 `~/Binance_Auto/Log_History`를 사용합니다.

파일 이름은 `2026-09-09_14-30-00-123456_KST_live_<pid>_<run_id>_part0001.log` 형식입니다.
새 실행마다 다른 파일을 만들고, 한국 자정 또는 약 16 MiB에서 새 파일로 분할합니다.
이전 파일은 자동 삭제하지 않습니다. 단일 사건이 16 MiB보다 크면 그 사건을 한 파일에 보존합니다.
로그는 UTF-8 JSON Lines이며 한 줄이 한 사건입니다. Decimal 수치와 초 단위 경과 시간은
정밀도를 유지하기 위해 문자열로 저장합니다.

## 하루 흐름 추적

1. 파일 이름의 `live`와 `timestamp_kst`로 원하는 날의 실제 실행을 찾습니다.
2. `run_id`와 `sequence`로 파일 분할 전후의 전체 사건을 시간 순서대로 읽습니다.
3. `details.session_id`로 시작부터 종료까지 묶고, `lower_event_id`로 하단 터치를 구분합니다.
4. `event_id`와 `decision_id`로 시장 입력 → 판단 → action을 연결합니다.
5. 주문의 `intent_id` → `client_order_id` → `exchange_order_id`를 따라 제출·조회·체결·저장을 확인합니다.
   `order_step.trace.order_id`는 동일한 거래소 주문 ID이며, 최초 응답 전에는 null일 수 있습니다.

| 사건 | 확인할 내용 |
| --- | --- |
| `logging_started`, `runtime_configured` | 실제 파일 경로, 실행 환경, 주문 허용 여부, 주문 상한·위험 정책 |
| `application_state_changed`, `startup_step` | 시작 단계, 시장·계좌·이력 복원, READY/FAILED/CLOSED 및 실패 코드 |
| `session_command_completed` | REGIME 선택, 분할 비율, 시작·중지 명령과 처리 결과 |
| `session_state_changed` | STM 처리 이후 공개 running/stopping/terminated 상태 동기화 완료 |
| `evaluation_started`, `decision_started` | 외부 효과 실행 전의 시장 입력·상태·지표·조건 비교 |
| `market_input_observed`, `market_boundary`, `market_evaluation_deferred` | 원본 봉, 지표 계산 완료와 queue 입력의 연결; 복합 봉 경계에서의 평가 대기 |
| `strategy_evaluated` | 실제 전이 ID, 이전·이후 상태, 판단 입력, 요청 action, 변경 후 runtime·포지션 |
| `action_requested`, `action_blocked` | 수행할 작업 및 시장 단절·주문 gate에 따른 실행 차단 |
| `risk_evaluated` | 주문 예산·누적 노출·손실 기준과 위험 정책 판정 |
| `order_submit_started`, `order_result_received` | 주문 수량·방향·전략·청산 이유, NEW/UNKNOWN/부분 체결/완료, fill 가격·수수료 |
| `order_query_scheduled`, `order_query_started` | 같은 주문의 조회 횟수, 대기 시간, 다음 조회 시각 |
| `order_cancel_started`, `order_cancel_received` | 취소 요청과 잠정 취소 결과; 후속 조회로 최종 체결을 확인 |
| `order_step` | 단계별 호출자/수신자, Context version, 처리 성공·실패 및 주문 수치 |
| `trade_committed` | 실제 저장된 거래, 수수료 포함 실현손익·수익률과 당일·누적 성과 |
| `reconciliation_required`, `operation_failed` | 차단 원인, 실패 작업, 예외 타입·원인 chain과 내부 module/함수/행 |
| `stream_unavailable`, `stream_reconciled`, `stream_recovery_*` | 시장·계좌 연결 장애와 복구 시도·대기·완료 |
| `runtime_heartbeat` | background worker 생존 여부와 해당 시점 세션 상태, 약 60초 간격 |

`order_step`의 주요 단계는 `6/6.1` 제출, `7/9` 주문 응답 반영,
`8/8.1/8.2` 동일 주문 조회, `11/12` 포지션 반영, `13.5/13.5.1` 이력 파일 저장,
`14` 체결 결과의 STM 전달입니다. 전체 caller/receiver도 함께 기록됩니다.

## 전략 판단에 남는 값

`evaluation.market`에는 현재가, 상·하단 BB, realtime %B, 터치봉 BBW,
30분 확정 EMA slope·종가 %B·직전 3봉 저점, realtime EMA slope·CCI,
1분 마감 판단에 사용하는 30분 EMA slope와 5초·3분 유지 판정 및 timer 정보가 담깁니다.
`evaluation.runtime`에는 touch/signal/flush 시각, 터치봉에 고정된 BBW,
flush_low, timer 기준과 entry %B, TP 기준가·이전 trailing slope,
Case별 활성·진입 중지·소모·회복·소유권·청산 사유가 담깁니다.

`condition_comparisons`는 실제 전략과 **같은 순수 비교 함수**로 만든
값·비교 연산·기준·유지 시간·충족 여부입니다. 단락 평가 때문에 모든 비교가 실제로
실행되었다는 뜻은 아니며, 어떤 전이가 선택되었는지는 `transition_ids`가 기준입니다.
확정되지 않은 30분/1분봉 조건은 `applicable=false`, `satisfied=null`로 기록합니다.
조건이 충족되지 않아 대기하는 평가도 저장합니다.

- Case B: 터치 → 최초 회복 신호 → 3시간 눌림 → 진입 → 익절/TREND_HOLD,
  30분봉 손절·비상손절·6시간 청산의 판단 입력을 추적합니다.
- Case C: setup → flush 갱신 → 3분 회복·timer 재설정 → 진입 → TP_TRAILING,
  fallback·추세 종료·3분 손절·1시간 청산과 이후 B 인계를 추적합니다.
- 통합: 단일 owner, 상대 전략 진입 중지, C 소모·0.25 회복,
  B 손절 후 새 하단 이벤트, 상단 BB 안전 종료는 runtime과 전이/action으로 확인합니다.

시장 `evaluated_at`·봉 마감 시각과 파일 `timestamp_kst/utc`는 다를 수 있습니다.
파일 시각은 실제 관측 시각이고 시장 시각은 판단 입력의 원본 시각입니다.
`state_after`는 STM 전이 상태, `runtime_after`는 action 적용까지 끝난 Context입니다.
`action_requested`는 실행 시도이며, 실행 성공은 후속 결과나 `strategy_evaluated`로 확인합니다.

## 오류와 보관

API 키, secret, token, 서명, HTTP header/body/URL, raw payload와 예외 원문은 저장하지 않습니다.
거래소 오류는 기존 adapter가 정규화한 고정 분류와 정수 API code만 보존합니다.
WebSocket 주문 거부 사유는 2026-09-09에 확인한
[Binance 공식 Order Reject Reason](https://developers.binance.com/en/docs/products/spot/user-data-stream#order-reject-reason)
표의 고정 값도 보존합니다. 문서에 없는 자유 형식 사유는 미분류로 처리합니다.
주문·잔액·체결 관련 정보가 있으므로 로그 파일은 사용자 읽기/쓰기 권한으로 생성합니다.

파일은 매 사건마다 flush하고 닫습니다. 시작 시 파일 생성이 실패하면 runtime 조립이 실패합니다.
운영 중 저장 장애는 기존 주문 조정을 계속할 수 있도록 격리하고 stderr에
`DIAGNOSTIC_LOG_WRITE_FAILED`를 표시합니다. 복구 후 첫 사건의
`dropped_records_before`로 누락 건수를 알립니다. 이 값이 0보다 크면 해당 구간 로그는 불완전합니다.
진단 파일은 거래 이력·pending-order의 fsync 복구 저널을 대체하지 않습니다.

## 재검증

프로젝트 루트에서 다음을 실행합니다.

```sh
python3 scripts/verify_runtime_logging.py
```

네트워크 연결을 막고 memory 거래소로 실제 Controller·STM·주문·background worker를 실행합니다.
Case B/C 진입·종료, C timer 재설정, 상단 안전 종료, 응답 유실과 동일 ID 조회,
이력 저장 실패, 처리 중 예외, 정밀도·정보 제외·동시 쓰기·파일 분할·저장 장애 복구를 검사합니다.
`verification_<한국 날짜와 시각>_KST` 하위에 `fake` 로그와 `verification_report.json`을 보존합니다.
검증 파일의 ERROR 사건은 의도적으로 주입한 장애이며 LIVE 주문 증거가 아닙니다.
