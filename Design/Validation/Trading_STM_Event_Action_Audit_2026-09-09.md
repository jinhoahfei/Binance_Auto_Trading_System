# TradingSTM Event-Action Table 코드 보완 기록

검토일: 2026-09-09 KST. 대상 HEAD: `12c2de12f848b7347a83d259ca2b3db73c6585d4`.

> 후속 코드 수정 및 재검증: [Lower BB · TradingSTM 감사 후 코드 보완](Lower_bb_remediation_2026-09-09.md). 아래 판정과 실패 로그는 수정 전 감사 기록이다.

기준: `Design/Trading_Logic/Event_Action_Table/Trading_Logic_Event_Action_Table.md`의 실행 계약 및 G/O/PB/PC/B/C 전체 109개 행. 이 문서는 코드 보완이 필요한 항목만 기록한다. 기준 명세와 production 코드는 수정하지 않았다.

**판정: Event-Action Table에 완전히 부합한다고 볼 수 없다.** 전이 ID 109개는 모두 있지만, 이벤트 전달·시간 측정·Controller 액션 반영을 포함한 실행에서 아래 7건을 확인했다. EA-01/EA-02는 손절 누락·지연과 직접 관련되므로 실주문 전에 우선 보완해야 한다.

## EA-01 / P1 — 새 하단 터치 이벤트 때문에 포지션의 확정봉 평가가 누락됨

**해당 계약:** §0.1의 동일 평가 시점 전달, PB-03/PB-05/PB-08, G-03.

**코드 위치:**

- `backend/src/binance_auto_trader/application/trading_controller.py:3718`
- `backend/src/binance_auto_trader/domain/trading/transitions/global_transitions.py:288`
- `backend/src/binance_auto_trader/domain/trading/stm.py:438`

Controller는 기존 터치봉과 다른 확정 30분봉의 low가 lower 이하이면 포지션 보유 중에도 `NEW_30M_LOWER_BAND_TOUCHED`로 분류한다. G-03은 owner가 있으면 실행할 수 없고, 포지션 Region은 이 이벤트를 조건 검사로 변환하지 않는다. 따라서 같은 close에서 PB-05가 골라야 하는 일반 손절이 누락된다.

**재현:** 매수가 100, 현재가 99.5, lower 99.4, 확정 low 99.3, 확정 slope -0.09. 실제 classifier→STM 결과는 transition 없음이다. 동일 snapshot을 일반 `MARKET_DATA_UPDATED`로 넣은 대조군은 `CASE_B_STOP`을 선택한다. 이후 진행봉에서는 confirmed flag가 내려가므로 누락된 종가 손절이 자동 복원되지 않는다.

**코드 보완:** 새 scope를 열 수 없는 상태에서는 현재 시장 평가를 포지션·신호 Region에 전달해야 한다. G-03 적용 여부와 별개로 확정봉 판단을 보존하고, 보유·pending·C 회복 잠금 상태의 close 전달을 통합 테스트로 검증한다.

## EA-02 / P1 — 정상적인 30분봉 교체가 연속 유지시간을 초기화함

**해당 계약:** PC-02~PC-06의 slope `<= -0.55` 3분 연속 유지, PB-02~PB-17의 5초 유지 조건.

**코드 위치:** `backend/src/binance_auto_trader/application/market_evaluation_builder.py:845`.

봉 ID가 달라지면 `_condition_started_at`을 승계하지 않는다. 표는 연속 유지조건을 요구하며 정상 봉 교체 시 누적시간을 버리도록 정의하지 않았다.

**재현:** 12:28부터 slope -0.60, 12:30 새 봉에서도 -0.60, 12:31까지 계속 유지. 총 180초가 지났지만 실제 builder의 손절 flag는 false다. 현재 동작은 12:30을 새 시작점으로 삼아 12:33까지 기다린다. 재현은 actual MarketSnapshot과 builder `__call__`을 사용하며, %B/slope 두 입력만 fixture에서 고정한다.

**코드 보완:** 조건이 계속 참이고 시장 데이터가 연속이면 정상 봉 교체에서도 타이머를 유지한다. 조건 불충족과 disconnect/gap/rebase에 따른 리셋은 유지한다. 표시용 timer 시작시각도 같은 기준으로 맞춘다.

## EA-03 / P2 — 매도 체결 %B 대신 최초 주문 의도 %B로 Case B 인계함

**해당 계약:** §0.3 매도 정상 체결, §1.5 `case_c_exit_pct_b`, PC-23F/PC-27/PC-28, B-14~B-17.

**코드 위치:**

- `backend/src/binance_auto_trader/application/trading_controller.py:7303`
- `backend/src/binance_auto_trader/application/trading_controller.py:7334`
- `backend/src/binance_auto_trader/domain/trading/transitions/helpers.py:235`

표는 `case_c_exit_pct_b`를 **Case C 매도 체결 시점**의 realtime %B로 정의한다. `_publish_case_c_exit_result()`는 `order.exit_pct_b_at_intent`를 저장한다. 부분 체결·재조회·재시도로 체결이 늦어져도 최초 의도 값을 유지하므로 PC-27의 `< 0.40` 판단이 표와 달라질 수 있다.

**재현:** production 시장 계산→STM→Controller→fake REST→실제 임시 JSONL 이력을 사용한다. TP_TRAIL 요청 당시 %B는 약 0.124175, 나머지 fill 시점 시장 %B는 약 0.717945인데, runtime에는 0.124175가 저장되고 `PC-27`이 선택된다. 실제 fill 시각/시장가격을 fake에서 명시적으로 전진시키고 이미 반영한 partial은 그대로 보존했다. 따라서 단순히 늦은 query 시각을 과거 체결 시각으로 오해한 사례가 아니다. 이 테스트는 인계 분기의 오류를 검증하며 실제 거래소 주문이나 후속 BUY가 실행됐다는 뜻은 아니다.

기존 `test_case_c_terminal_sell_keeps_intent_pct_b_for_active_handoff`는 현재 의도 시점 값 보존을 정답으로 검사하므로, 기존 suite가 통과해도 표 준수의 증거가 되지 않는다.

**코드 보완:** 주문 의도 provenance와 표의 exit provenance를 서로 다른 필드로 관리한다. actual execution time에 대응하는 시장 근거로 exit 값을 확정하고, 중복 결과·persistence retry에서도 그 값을 안정적으로 보존한다. 단순히 query 수신 순간의 최신 시세로 바꾸는 것만으로는 체결 시점 계약을 충족하지 못한다. 값 확보가 안 되면 추측으로 active 인계를 허용하지 않는다.

## EA-04 / P2 — G-03에 표에 없는 종가 확정 제한이 추가됨

**해당 계약:** G-03의 새 30분봉 lower touch, PC-27/PC-28 이후 다음 lower touch 재판정.

**코드 위치:** `backend/src/binance_auto_trader/application/trading_controller.py:3718`.

표의 G-03 Guard는 새 candle ID, low <= lower, 무포지션·무pending, consumed/recovery 상태를 요구한다. `confirmed_30m_close=True` 조건은 없다. 하지만 Controller는 다른 30분봉의 **종가 확정 시점에만** 이 이벤트를 생성한다.

**재현:** C consumed와 recovery confirmed가 true, owner/pending 없음, B_WAIT_SIGNAL/C Final 상태에서 **다른 진행봉**의 현재가가 lower 아래로 내려간다. G-03 Guard가 충족되지만 분류 결과는 `MARKET_DATA_UPDATED`이고 새 C 감시가 켜지지 않는다. 같은 봉 1회 제한과 충돌하지 않도록 서로 다른 봉을 사용했다.

**코드 보완:** G-03의 표상 Guard를 만족하는 새 진행봉 재터치도 전달한다. EA-01과 함께 수정하여, 재진입할 수 없는 상태에서는 포지션/신호 평가만 계속하고 유효한 무포지션 재터치에서만 scope를 바꾼다.

## EA-05 / P2 — G-06P가 pending 매도 주문에도 취소 요청을 생성함

**해당 계약:** G-06P Action 2. pending 진입 주문은 취소·조회하고, pending 청산 주문은 terminal까지 조회·조정한다.

**코드 위치:**

- `backend/src/binance_auto_trader/domain/trading/transitions/global_transitions.py:136`
- `backend/src/binance_auto_trader/application/trading_controller.py:7951`

STM은 `pending_order_side is not None`이면 BUY/SELL 구분 없이 `CancelPendingOrder`를 추가한다. Controller도 query 결과가 active이면 side 구분 없이 cancel한다. 표가 요구하는 pending SELL의 조회·조정 유지와 다르다.

**재현:** owner Case B, pending side SELL인 상태에서 STOP → `G-06P`, `PatchRuntimeContext`, `CancelPendingOrder`, `ReconcileOrder`가 반환된다. 표 기준 기대는 pending SELL 취소 없이 동일 주문 조정이다.

**코드 보완:** STOP_CONFIRMED의 G-06P에서는 pending BUY만 취소하고 SELL은 동일 ID로 조정한다. pending 종류에 관계없이 취소를 명시한 **G-07 상단 안전 종료**와는 정책을 구분해야 한다. 이미 terminal인 주문에 불필요한 cancel을 보내지 않는 현 query-first 방어는 유지한다.

## EA-06 / P2 — B-05의 signal_time이 봉 마감시각이 아닌 로컬 관측시각임

**해당 계약:** B-05 Action 2의 `signal_time = candle_close_time`, B-06/B-08/B-09/B-11 및 O-04의 3시간 유효기간.

**코드 위치:**

- `backend/src/binance_auto_trader/domain/trading/transitions/case_b_signal_transitions.py:105`
- `backend/src/binance_auto_trader/application/trading_controller.py:3543`

Controller가 `occurred_at=self._clock()`으로 시장 이벤트를 만들고, B-05는 이 값을 signal_time에 저장한다. 원본 candle close time은 별도 전달되지 않는다. 네트워크·처리 지연만큼 3시간 유효기간이 늘어난다.

**재현:** 00:00에 열린 30분봉의 close가 00:30:02에 전달되면 signal_time은 00:30:02다. 표상 기준은 00:30:00이다. 두 시각의 차이가 클수록 만료 이후에도 매수 가능한 구간이 길어진다.

**코드 보완:** 원본 30분봉의 종료 경계시각을 immutable market event/snapshot에 보존하고 B-05에 사용한다. 로컬 관측시각은 지연 측정용으로 따로 유지한다. 정상 수신과 지연 수신 모두에서 정확히 3시간/3시간 초과 경계를 검증한다.

## EA-07 / P2 — PB-23의 Context reset이 같은 봉 Case C 중복 방지를 해제함

**해당 계약:** C-03/C-05의 `last_case_c_setup_candle_id != current_30m_candle_id`, §2.4의 “한 30분봉에서 Case C setup은 한 번만 인정”, PB-23.

**코드 위치:**

- `backend/src/binance_auto_trader/domain/trading/transitions/case_b_position_transitions.py:121`
- `backend/src/binance_auto_trader/domain/trading/context.py:960`

같은 봉에서 C setup이 먼저 기록된 뒤 Case B가 진입하면 C-15로 C 진입이 종료된다. 이어 B가 손절해 PB-23으로 새 lower event를 열 때 `ResetCaseCContext()`가 마지막 setup candle ID까지 None으로 만든다. PB-23 표상 초기화 플래그에는 이 중복방지 ID의 삭제가 없다.

**재현:** 마지막 C setup ID와 현재 candle ID가 같은 상태에서 B 매도 완료→PB-23→반환된 typed Context 액션 적용→ACTIVATE. C-03이 다시 발생하여 같은 봉의 두 번째 setup을 인정한다.

**코드 보완:** lower event 단위 setup/flush/회복 플래그와 candle 단위 중복방지 이력을 분리한다. PB-23으로 새 이벤트를 열어도 해당 봉에서 이미 사용한 C setup ID는 보존하고, 다음 봉에서는 정상 setup을 허용한다.

## 재현 및 검증 기록

재현 스크립트와 결과는 이 디렉터리에만 추가했다.

```sh
backend/.venv/bin/python Design/Validation/trading_event_action_audit.py
```

- 10개 검사: 대조군 3개 통과, 위 7개 코드 보완 기대값 검사 실패, 실행 오류 0.
- 결과: `trading_event_action_audit_result.txt`. 명세 위반을 숨기지 않기 위해 현 구현에서 exit code 1을 반환한다.
- EA-01/02/04는 같은 디렉터리의 이전 감사 재현 함수를 재사용한다. EA-03은 local fake 주문 통합 경로, EA-05/06/07은 production STM/Context를 사용하는 통제된 재현이다.
- 관련 기존 테스트를 별도 실행한 결과 108개 모두 통과했다: STM, queue, Context, 30분 시장 builder, public Case C 흐름, session, 주문 관측시각 스케줄링. 결과: `trading_event_action_existing_tests.txt`.
- 109개 ID 존재 검사는 동작 전체의 109행 완전 검증을 뜻하지 않는다. 위 재현이 기존 성공 검사에서 빠지는 동작 차이를 보여준다.

기준 명세 SHA-256 (검토 전·후 동일):

```text
3a6e58ef6ed50a141b677b9b620603b401963aaf7f22b7d37737a21fd0d395b6
```

이번 기록은 기준 표 변경이나 실주문 실행 승인이 아니다. 실제 주문·키 조회·계좌 변경은 실행하지 않았다.
