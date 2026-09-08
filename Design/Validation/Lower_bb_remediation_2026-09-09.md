# Lower BB · TradingSTM 감사 후 코드 보완

수정일: 2026-09-09 KST. 기준 HEAD: `12c2de12f848b7347a83d259ca2b3db73c6585d4`의 작업 트리.

Validation의 두 감사에서 확인한 **중복 제외 8개 코드 결함을 수정했다.** 아래 결과는 소스와 오프라인 실행 경로에 대한 검증이다. 설치된 앱/DMG의 반영 여부나 실제 계좌의 실주문 준비 완료 판정은 포함하지 않는다.

## 수정 내역

| 감사 항목 | 변경한 동작 | 주요 코드 |
|---|---|---|
| F1 / EA-01 | owner·pending·C 회복 잠금 때문에 새 scope를 열 수 없으면 일반 시장 평가를 전달한다. 직접 STM에 도달한 미처리 NEW lower 이벤트도 시장 평가로 정규화해 확정봉 손절을 보존한다. | `application/trading_controller.py`, `domain/trading/stm.py` |
| F2 | Trend Hold에서도 비상손절 → 확정봉 손절 → 6시간 제한을 우선 평가한다. 청산 실패·재시도 시 Trend Hold 복귀 상태와 같은 주문 의도를 보존한다. 화면에도 세 방어 지표를 표시한다. | `domain/trading/transitions/case_b_position_transitions.py`, `application/trading_indicator_snapshot.py` |
| F3 / EA-02 | 정상 30분봉 교체에서 참인 조건의 monotonic 시작시각과 표시 timer ID를 유지한다. 조건 불충족·stream reset/rebase 때는 기존처럼 초기화한다. | `application/market_evaluation_builder.py`, `application/market_condition_timers.py` |
| F4 / EA-04 | 새로운 진행봉의 실시간 lower 재터치도 G-03 후보가 된다. owner·pending·C consumed/recovery guard와 동일 봉 제한은 유지한다. | `application/trading_controller.py` |
| EA-03 | 최초 주문 의도 %B는 주문에 보존하고, 종료 %B는 실제 마지막 fill의 시각·가격과 해당 봉 직전 19개 확정봉으로 따로 계산한다. 근거가 없으면 적극 인계를 허용하지 않는다. | `application/trading_controller.py`, `application/market_evaluation_builder.py` |
| EA-05 | 일반 STOP의 G-06P는 pending BUY만 취소하고 SELL은 같은 ID로 조회·조정한다. G-07 상단 안전 종료와 수동 `CANCEL_AND_LIQUIDATE`의 취소 정책은 별도로 유지한다. STOPPING 중 수동 긴급 종료를 요청해도 cleanup을 이어간다. | `domain/trading/transitions/global_transitions.py`, `application/trading_controller.py` |
| EA-06 | 확정 30분봉 평가에 원본 마감 경계시각을 실어 B-05 `signal_time`에 저장한다. 내부 후속 이벤트에서도 경과시간을 다시 결합해 지연된 signal 생성 직후 만료를 판단한다. | `domain/trading/context.py`, `application/market_evaluation_builder.py`, `domain/trading/transitions/case_b_signal_transitions.py`, `application/trading_controller.py` |
| EA-07 | PB-23의 C reset에서 마지막 setup candle ID를 보존한다. 같은 봉의 중복 setup은 막고 다음 봉의 setup은 허용한다. | `domain/trading/action_requests.py`, `domain/trading/context.py`, `domain/trading/transitions/case_b_position_transitions.py` |

위 코드 경로는 모두 `backend/src/binance_auto_trader/` 기준이다.

## 명세 적용과 체결 근거의 기준

### Trend Hold 공통 방어

Lower BB 명세의 공통 손절·시간 제한을 Trend Hold에도 적용했다. Event-Action Table의 PB-15~PB-17은 약화 조건 청산만 명시하므로, 이 부분은 고정된 표에 없는 공통 방어를 코드에 추가한 것이다. 기존 PB 청산·실패·재시도 액션과 ID를 재사용했으며 109개 전이 ID 집합은 유지했다. 따라서 **두 문서와 모든 상태 전이가 완전히 동일해졌다는 뜻은 아니다.**

사용자 지시에 따라 `Trading_Logic_Event_Action_Table.md`는 수정하지 않았다. 수정 전후 SHA-256은 같다.

```text
3a6e58ef6ed50a141b677b9b620603b401963aaf7f22b7d37737a21fd0d395b6
```

### Case C 매도 체결 %B

- 전량 청산을 완성한 주문의 마지막 실제 fill을 기준으로 한다. 같은 밀리초의 Binance fill은 숫자 trade ID 순으로 구분하며, 비숫자 ID gateway에서는 누적 수신 순서를 보조 기준으로 삼는다.
- 체결 시각을 UTC 30분봉에 배정하고, 그 봉 직전의 **연속된 확정봉 19개 + 해당 체결가**로 20기간·2σ 후보 BB와 %B를 복원한다. 조회 시점의 최신 가격/밴드를 과거 체결에 대입하지 않는다.
- 이것은 체결가를 후보 현재가로 사용하는 기준이다. 과거 public Kline이 마지막으로 표시했던 가격의 저장값이나 주문 평균 체결가를 사용하는 기준과 구분한다.
- 이력 부족·비연속 이력·밴드 폭 0 등으로 계산할 수 없으면 `case_c_exit_pct_b=None`으로 고정한다. 회복 확인 뒤 PC-28 wait-only 인계가 되며, 값을 추측해 PC-27 적극 인계를 허용하지 않는다.
- `None`도 확정 결과로 취급하므로 중복 게시나 동일 프로세스 내 저장 재시도에서 뒤늦은 시세로 바뀌지 않는다. 프로세스 재시작 후 전략 인계를 복원하는 신규 영속화 기능은 추가하지 않았고 기존 주문·잔여 포지션 recovery 정책을 유지한다.

### Signal 시각

Production 시장 builder는 원본 `open_time + 30분`을 전달한다. 원본 시각 필드가 없는 기존 synthetic snapshot의 호환 경로에서만 event 발생시각을 사용한다. 실제 builder→Controller 경로는 봉 마감시각을 사용하며, 지연 수신한 3시간 초과 신호에서 B-05 직후 B-08이 발생하고 주문이 제출되지 않는 것을 검증했다.

## 검증

| 검증 | 결과/증거 |
|---|---|
| Lower BB 감사 재현 | 6/6 통과 — `lower_bb_remediation_spec_result.txt` |
| Event-Action 감사 재현 | 10/10 통과 — `lower_bb_remediation_event_action_result.txt` |
| UI 지표·타이머 | 2개 파일, 14/14 통과 — `lower_bb_remediation_ui_result.txt` |
| 백엔드 전체 suite | 1,085개 중 1,075 통과, opt-in 외부 연동 10개 skip, 실패/오류 0 (36.116초) — `lower_bb_remediation_backend_result.txt` |
| 공백/patch 검증 | `git diff --check` 통과 |
| 기준 표 무변경 | 위 SHA-256 재확인 |

새 회귀 테스트:

- `backend/tests/unit/trading/test_prelive_regressions.py`: Trend Hold 세 방어의 주문·실패·재시도, 보유/회복 잠금 중 확정봉 전달, BUY/SELL STOP 분리, PB-23 동일 봉 중복 차단.
- `backend/tests/unit/market/test_execution_pct_b.py`: 독립 Decimal 산식과의 일치, 늦은 조회와 다음 봉의 시세로부터 독립성, 과거 이력 부족/0폭, 정확한 30분 경계, 지연 close의 원본 시각.
- `backend/tests/integration/test_prelive_signal_time_flow.py`: 실제 Controller queue에서 지연된 signal을 내부 매수 후속 이벤트 전에 폐기.
- `backend/tests/integration/test_prelive_exit_provenance_flow.py`: 실제 partial→terminal 흐름에서 최초 exit 근거를 고정하고, 근거 없음의 PC-28 인계를 확인. 계산기 반환값을 주입하는 이 검사는 게시·인계 경계를 검증하며, 계산기 자체는 앞의 시장 단위 테스트가 검증한다.

기존 public Case C 주문 테스트는 의도 %B 보존을 인계 기준으로 보던 기대값을 실제 체결 기준으로 바꿨다. fake의 이미 관측한 partial fill은 그대로 보존하고, 새 fill만 전진한 실제 fake 시각/가격으로 생성한다. 기존 STOP·timer 테스트도 바뀐 계약에 맞춰 기대값을 수정했다. 기존 수동 긴급 종료 테스트는 유지해 일반 STOP 정책 변경의 부작용을 검사했다.

감사 스크립트에서는 B-05 fixture에 원본 마감시각을 명시하고, PB-15 유지 대조군은 공통 6시간 제한 전인 5시간으로 변경했다. 수정 전 실패 로그는 보존하고 수정 후 결과를 별도 파일에 기록했다.

실행 명령:

```sh
backend/.venv/bin/python Design/Validation/lower_bb_spec_audit.py
backend/.venv/bin/python Design/Validation/trading_event_action_audit.py

cd backend
BINANCE_RUN_TESTNET=0 BINANCE_RUN_TESTNET_ORDERS=0 \
BINANCE_RUN_PHASE13_PUBLIC_CASE2=0 BINANCE_RUN_PHASE13_RECOVERY_ONLY=0 \
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py'

cd ../UI
./node_modules/.bin/vitest run \
  src/features/recent-orders/indicatorTimer.test.tsx \
  src/features/recent-orders/tradingIndicatorPresenter.test.tsx
```

## 이번 수정의 범위 밖

기존 Lower BB 감사 §3의 시장가 fallback 체결가격 보장, 시세 관측 해상도, 원본 백테스트 EMA slope 산식, C timer `current_open_pct_b` 의미, touch BBW의 기준시점은 코드 결함으로 확정되지 않은 실행·명세 해석 차이다. 이번에 임의의 주문 유형이나 지표 정의로 변경하지 않았다. 원본 백테스트와 수익률 동치도 검증하지 않았다.

실제 주문·Testnet 주문·키 조회·계좌 변경·live 프로필 변경은 실행하지 않았다. 전체 backend suite의 loopback 테스트에만 샌드박스 외 로컬 소켓 권한을 사용했다. 로그의 `phase13-readiness/soak GAP` 등은 suite 내부 fixture 출력이며 실제 계좌 readiness 결과가 아니다. 설치된 앱/DMG 재빌드와 배포는 수행하지 않았다.
