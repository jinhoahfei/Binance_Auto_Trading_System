# Event–Action Table 구현 검증 — 2026-09-10

이 문서는 수정 전 감사 결과를 보존한다. 이후 수정·재검증·설치 결과는 [불일치 수정 및 검증 결과](/Users/oscar/Desktop/Binance_Auto/Design/Validation/Trading_Logic_Event_Action_Fix_2026-09-10.md)를 참고한다.

현재 상태를 **문서대로 정상 동작한다고 판정할 수 없다.** 실행 앱과 최신 소스의 차이, 내부 평가 중단을 숨기는 화면 표시, Trend Hold 청산 조건의 문서 불일치를 확인했다. 아래 결과는 발견한 문제를 구분한 것이며, 모든 거래 로직이 잘못됐다는 의미는 아니다.

## 검증 기준과 범위

- 기준: [Trading_Logic_Event_Action_Table.md](/Users/oscar/Desktop/Binance_Auto/Design/Trading_Logic/Event_Action_Table/Trading_Logic_Event_Action_Table.md). 직전 요청에 따라 수정된 실시간 하단 접촉 및 터치 순간 BBW 조건을 포함한다.
- 대상: 공통 전이(G), 포지션 소유권(O), Case B/C 신호(B/C), Case B/C 포지션(PB/PC), 시장 평가 진입 경로, backend snapshot과 화면 표시, 실행 앱의 내장 Python 코드.
- 방법: 문서와 소스 대조, 기존 backend 테스트 전체 실행 및 실제 전이 ID 관측, 문서의 수식에서 별도로 작성한 경계·우선순위 입력 665건, 평가 중단 상태의 backend→UI 재현, 실행 파일 내부 코드 읽기.
- 이번 검증에서는 운영 로직 수정, 앱 재빌드·교체, 매매 시작 및 실제 주문 전송을 하지 않았다. 직전 변경과 사용자의 기존 작업은 유지했다.

## 1. 실행 앱에는 최신 하단 접촉 조건이 반영되지 않았다

검증 중 실행 프로세스가 가리키는 앱은 다음 경로였다.

`UI/apps/desktop/src-tauri/target/session8-1e7df5f/release/bundle/macos/Binance Auto Trader.app`

이 앱의 sidecar 실행 파일에서 PyInstaller 내장 코드를 추출하여 읽었다. 파일 이름이나 빌드 시각만으로 추정한 결과가 아니다.

| 확인 지점 | 현재 소스 | 실행 앱 내부 코드 |
|---|---|---|
| 하단 접촉 공통 판정 | 실시간 현재가 접촉 `lower_price` | 확정봉 접촉 `lower_close` 분기 존재 |
| 시장 이벤트 선택 | 실시간 가격과 하단 밴드 비교 | 확정 30분봉 여부와 봉 저가를 이용하는 분기 존재 |
| Case B 최초 활성화 | 터치 순간 `touch_candle_bbw < 0.02` | `touch_candle_low`, `lower_band_at_touch`, `b_touch_low` 추가 검사 존재 |

따라서 **현재 소스를 검증해도 실행 앱에 최신 조건이 적용됐다고 볼 수 없다.** 직전 수정은 소스에만 반영됐고 앱 재빌드·적용은 이루어지지 않은 상태다.

현재 소스의 [공통 접촉 판정](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/domain/trading/transitions/helpers.py:38)과 [Case B 활성화](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/domain/trading/transitions/case_b_signal_transitions.py:56)를 대조했다. 증거: [실행 파일 검사 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_audit_2026-09-10/binary-probe.json), [내장 함수 disassembly](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_audit_2026-09-10/binary-disassembly.txt).

## 2. 내부 평가가 중단돼도 화면에는 ‘정상 작동’으로 표시된다

현재 소스에서 다음 상황을 통제된 입력으로 재현했다.

1. 정상 실행 상태에서 현재가 101, 하단 밴드 100을 평가한다. 지표는 미접촉으로 표시된다.
2. 이벤트 처리 실패 상태를 주입하여 backend 상태를 `reconciliation_required`로 전환한다.
3. 새 시장 가격 99를 전달한다. `99 <= 100`이지만 controller는 실행 상태가 아니므로 새 평가를 받지 않는다.
4. 이 backend snapshot을 실제 UI 검증·변환·표시 경로에 전달한다.

| 항목 | 재현 결과 |
|---|---|
| backend 상태 | `reconciliation_required` |
| 새 하단 접촉 평가 수락 | 거절 |
| 화면 실행 상태 | **정상 작동** |
| 화면 전략 상태 | **하단 밴드 대기** |
| 현재가 지표 | **이전 값 101 / 빨간색** |

backend가 불확실한 상태에서 평가를 중단하는 안전 동작 자체보다, **중단 사실을 화면이 숨기고 마지막 평가값을 정상 실시간 값처럼 보여주는 것이 문제**다.

원인은 연결 여부와 `is_trading`만으로 정상 문구를 정하는 [dashboardPresenter](/Users/oscar/Desktop/Binance_Auto/UI/src/app/presenters/dashboardPresenter.ts:229), 오류 상태보다 기존 `active_logic`를 먼저 표시하는 [tradingLogicState](/Users/oscar/Desktop/Binance_Auto/UI/src/shared/formatting/tradingLogicState.ts:19), 마지막 성공 평가를 사용하는 지표 경로의 조합이다. 새 시장 평가가 차단되는 위치는 [trading_controller](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/trading_controller.py:3536)다.

증거: [backend 재현 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_audit_2026-09-10/runtime-probe.json), [UI 재현 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_audit_2026-09-10/ui-probe.json). 재현용 UI 테스트의 성공은 이 잘못된 표시가 실제로 발생함을 확인했다는 뜻이다.

이는 앞선 스크린샷처럼 차트는 움직이는데 전략 지표는 멈출 수 있는 경로다. 다만 **당시 스크린샷의 직접 원인으로 확정하지는 않는다.** 당시 오류 로그가 없고, 검증 중 확인한 앱은 재시작 후 ‘매매 시작 전/매매 중지’ 상태여서 이전 메모리 상태를 복원할 수 없었다.

## 3. Case B Trend Hold의 청산 조건이 표와 다르다

[표 PB-15/PB-16](/Users/oscar/Desktop/Binance_Auto/Design/Trading_Logic/Event_Action_Table/Trading_Logic_Event_Action_Table.md:226)은 아래 두 약화 조건이 모두 거짓이면 계속 보유하도록 정의한다.

- `realtime_ema_slope <= 0.04`를 5초 유지
- `realtime_pct_b < 0.60`을 5초 유지

그러나 [구현](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/domain/trading/transitions/case_b_position_transitions.py:303)은 두 조건보다 비상손절·확정봉 손절·시간 청산을 먼저 검사한다.

| 입력: 두 약화 조건 모두 거짓인 Trend Hold | 표의 기대 결과 | 실제 반환 결과 |
|---|---|---|
| 진입가 100, 현재가 99 | PB-15: 계속 보유 | PB-16 → `CASE_B_EMERGENCY_STOP` |
| 확정 30분봉 EMA slope = -0.09 | PB-15: 계속 보유 | PB-16 → `CASE_B_STOP` |
| 보유 시간 6시간 | PB-15: 계속 보유 | PB-16 → `CASE_B_TIME_EXIT` |

세 입력 모두 독립 검사에서 불일치를 재현했다. 이후 방어 매도 처리는 표상 `CASE_B_HOLDING`에 적힌 전이 경로를 Trend Hold에서도 사용한다.

이 추가 방어 조건이 투자 정책상 잘못됐다고 단정할 수는 없다. 다만 **표가 실제 동작을 정확하게 설명하지 않으며, PB-16 기록만으로 표의 약화 조건이 성립했다고 판단할 수도 없다.** 방어 조건 유지 여부에 맞춰 문서·코드·전이 기록을 일치시킬 필요가 있다. 검증 요청 범위에서 이 정책을 임의로 바꾸지 않았다.

## 테스트 결과와 일치한 범위

| 검사 | 결과 | 의미 |
|---|---|---|
| 표와 전이 카탈로그 ID 대조 | 각각 109개, 누락·추가 없음 | 전이 이름의 대응 확인 |
| 기존 backend 테스트 | 1,108개 실행, 1,098개 통과, 10개 건너뜀 | 현재 테스트 기준 회귀 실패 없음 |
| 문서에서 작성한 독립 검사 | 665건 중 662건 기대 일치, 3건 불일치 | 위 Trend Hold 차이 3건 재현 |
| 기존 테스트 + 독립 검사에서 전이 관측 | 109개 모두 최소 1회 관측 | 단순 선언만 있고 미실행인 ID는 없음 |
| backend→UI 평가 중단 재현 | 잘못된 정상 표시 재현 | 화면 신뢰성 문제 확인 |

기존 테스트의 주 프로세스에서 관측한 ID는 76개다. 별도 자식 프로세스 내부 호출은 계측에 포함하지 않았으며, 독립 검사와 합쳐 109개를 관측했다. 이 수치는 모든 입력 조합·부작용·실거래를 검증했다는 의미가 아니다.

직전 요청의 실시간 하단 접촉 및 터치 순간 BBW 경계, B 신호의 slope/%B/직전 저가 조건과 3시간 경계, C setup·flush·회복·3분 경계, B 보유 상태의 청산 우선순위, C 익절 구간·추적 청산·handoff, 주문 실패·재시도 전이 등을 대조했다. 독립 검사는 실제 `TradingSTM`을 호출하되 시장 값과 상태를 직접 주입하여 조건·우선순위를 분리해 확인했다. 실시간 데이터 수집부터 거래소 체결까지를 그대로 재현한 검사는 아니다.

증거: [전체 테스트 로그](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_audit_2026-09-10/suite.log), [독립 검사 입력·결과](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_audit_2026-09-10/independent.json), [109개 전이 관측 목록](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_audit_2026-09-10/transition-observations.md). 검증 대상 파일의 해시는 [audited-source-sha256.json](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_audit_2026-09-10/audited-source-sha256.json)에 보관했다.

## 후속 조치 우선순위

1. 평가 중단·확인 필요 상태가 정상 실행으로 보이지 않도록 상태 문구와 지표 유효성 표시를 수정한다.
2. 최신 소스로 앱을 재빌드하고 실행 파일의 소스 버전을 확인한다. 새 앱에서 실제 주문 없이 실시간 평가 갱신과 접촉 직후 B/C 추적 시작을 확인한다.
3. Trend Hold에도 공통 방어 청산을 유지할지 정책을 정하고, 표와 전이 구현·기록을 일치시킨다.

이번 결과만으로 장시간 실행 안정성, 실제 거래소 체결, 재접속·복구의 모든 조합, 수익성을 보증할 수 없다. 현재 발견한 배포 차이와 표시 오류가 해소되기 전에는 화면의 ‘정상 작동’을 투자 로직 정상 동작의 증거로 사용하기 어렵다.
