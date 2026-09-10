# 하단 BB Case별 단계·실시간 지표 대조 및 수정 결과

2026-09-10. 기준 소스 commit: `74b54bb` 이후 이번 작업의 변경분.

기준 문서는 [하단 BB 전략 기술서](../Specification/Lower_bb_logic_specification.md)와 [Trading Logic Event-Action Table](../Trading_Logic/Event_Action_Table/Trading_Logic_Event_Action_Table.md)이다. 원래 전략 조건과 문서의 전이 ID를 유지하고 구현 및 표시를 수정했다.

## 1. 확인 결과

Case B의 `touch_candle_bbw`는 공통 하단 터치 순간의 실시간 30분봉 BBW로 고정한다. `B-03`에서 `< 0.02`를 판정한 뒤 `B_WAIT_SIGNAL`에서는 BBW를 다시 검사하지 않는다.

운영 로그의 20:04:44와 21:03:43 KST에서도 하단 이벤트 시작 직후 `B-03`을 거쳐 `B_WAIT_SIGNAL`에 진입했다. 현재 소스와 해당 로그에서 BBW를 계속 매수 조건으로 재검사하는 오류는 재현되지 않았다. 기존 UI는 두 Case를 제목 하나로 합치고 현재 단계를 표시하지 않아, 어떤 단계의 조건인지 확인하기 어려웠다. 사진 원본은 이번 입력에 첨부되지 않아 당시 표시값과 개별 publication까지 대조하지는 못했다.

백엔드의 지표 목록 선택에는 별도 불일치가 있었다. Case C가 포지션을 보유하면 Case B의 신호 지표를 제거했지만, `B-12/B-13`은 Case B의 **매수만 중지**하며 신호 감시는 유지한다. 이 때문에 실제로 평가되는 병렬 Case B 단계와 그 지표를 화면에서 볼 수 없었다.

## 2. 단계별 지표와 전환 기준

### 2.1 Case B

| 현재 단계 | 사용하는 값과 판정 시점 | 다음 단계·Action | 표 ID |
|---|---|---|---|
| B_WAIT_TOUCH | 터치 순간 저장한 30분봉 BBW `< 0.02` | 충족 시 WAIT_SIGNAL, 미충족 시 이번 이벤트의 B 종료. 이후 BBW 변화로 재활성화하지 않음 | B-02/03 |
| B_WAIT_SIGNAL | 확정 30분봉 EMA slope `> -0.03`, 종가 %B `> 0.25`, 해당 확정봉 저가 `>=` 직전 확정 3봉 최저가 | 처음 모두 만족한 봉만 signal로 저장하고 WAIT_PULLBACK 진입 | B-04/05 |
| B_WAIT_PULLBACK | 실시간 %B `<= 0.30`, signal 확정 시각부터 `<= 3시간`, owner·pending 주문 없음, B 매수 일시정지 해제 | 매수 요청. 3시간 초과 시 signal 폐기 | B-06~11 |
| B_POSITION_OPEN_SIGNALLED | 주문 결과 및 재시도 시 signal 유효시간 | 실제 체결 반영 후 B 포지션 관리 시작 | O-02~05, B-18/19 |
| CASE_B_HOLDING | 실시간 가격 `<= 매수가 × 0.99`; 확정봉 slope `< -0.08`; %B `>= 0.60` 5초와 실시간 slope; 매수 후 6시간 | 비상손절 → 확정봉 손절 → Trend Hold → 일반 익절 → 시간청산 순서 | PB-02~14 |
| CASE_B_TREND_HOLD | 실시간 slope `<= 0.04` 5초 또는 %B `< 0.60` 5초 | Trend Hold 청산 | PB-15~22 |
| CASE_B_CLOSED | 실제 매도 완료 및 청산 사유·현재가 하단 접촉 | STOP/EMERGENCY_STOP 후 현재가가 하단 이하이면 새 이벤트로 B/C 재판정 | PB-23F/23/24 |

TREND_HOLD의 세부 적용 범위는 Event-Action Table PB-15~22와 기존 수정 기록을 따른다. 이 단계에 일반 보유 상태의 손절·6시간 청산을 추가하지 않았다. 기술서의 일반 손절·시간청산 설명을 모든 하위 상태로 임의 확장하지 않는다. 공통 상단 BB 안전 종료는 유지한다.

### 2.2 Case C

| 현재 단계 | 사용하는 값과 판정 시점 | 다음 단계·Action | 표 ID |
|---|---|---|---|
| C_WAIT_SETUP | 실시간 %B `<= -0.15`와 실시간 30분봉 CCI(20) `<= -140`; 같은 30분봉 중복 setup 금지 | C_SETUP 진입 | C-02~07 |
| C_SETUP · 저점 확인 대기 | 실시간 %B `<= -0.25`; 매수 전 %B `>= 0.25` 종료 | 최초 flush 저장 후 회복 감시 | C-08/09/14 |
| C_SETUP · 반등 회복 대기 | 현재가와 flush_low, `entry_pct_b = timer_base_pct_b + 0.06`, 경과시간 `<= 3분`, 실시간 %B `>= entry_pct_b`, `entry_pct_b < -0.15` | 새로운 저점 갱신 우선. 3분 초과 시 현재 %B로 기준 재설정. 회복 조건과 진입 한도를 모두 만족하면 매수 요청 | C-08~14 |
| C_POSITION_OPEN_SIGNALLED | 주문 결과 및 신규 진입 권한 | 실제 체결 반영 후 C 포지션 관리 시작 | O-06~09, C-16/17 |
| CASE_C_HOLDING | 실시간 %B `>= 0.10`; 실시간 30분봉 slope `<= -0.55` 3분; 매수 후 60분 | 익절권 진입 → 손절 → 시간청산 순서 | PC-02~10 |
| CASE_C_TP_TRAILING | 실시간 %B `< 0.10`; 확정 1분봉 종가를 대입한 **30분봉** EMA slope와 이전 기준; 60분 | fallback → slope 비교 → 시간청산 순서. 이 단계에서는 C 손절 조건을 재적용하지 않음 | PC-11~23 |
| CASE_C_CLOSED | 실시간 %B `>= 0.25` | 회복 전 C 재진입 금지, B 매수 중지 | PC-24~26 |
| CASE_C_RECOVERY_SUCCEEDED | 청산 사유가 TP_TRAIL이고 매도 시점 %B `< 0.40` | 충족 시 B 매수 재개, 나머지는 B 감시만 유지. 같은 이벤트의 C 재진입 금지 | PC-27/28 |

하단 터치 후 두 Case의 setup 감시는 병렬이며 실제 진입은 단일 owner다. 같은 평가에서 두 매수 조건이 동시에 충족하면 표대로 Case C 요청 하나만 선택한다. 주문 중에는 실제 체결을 앞서 owner로 설정하지 않는다. 부분 체결·상태 불명은 같은 주문을 조회·조정한다.

## 3. 수정 내용

| 구분 | 변경 |
|---|---|
| Case별 표시 | `Case_B 실시간 지표`, `Case_C 실시간 지표`를 별도 그룹으로 표시 |
| 현재 단계 | backend가 두 Region의 단계를 `indicators.phases`로 원자적으로 전달. 지표가 없는 주문·종료 단계도 표시 |
| 단계에 맞는 지표 | WAIT_SIGNAL은 확정봉 3개 조건만, WAIT_PULLBACK은 실시간 눌림·유효시간만 표시. 행의 단계가 현재 단계와 다르면 이전 행을 노출하지 않음 |
| C_SETUP 세부 단계 | 저점 확인 대기와 반등 회복 대기를 분리해 표시. setup 이후 CCI를 계속 진입 조건처럼 표시하지 않음 |
| 병렬 감시 | C 보유 중에도 B의 확정봉 평가와 signal 유효시간을 보존·표시. B 매수 일시정지 안내를 B에만 적용 |
| 주문·종료 | 해당 Case 주문 대기, 다른 Case 주문 처리 대기, 후보 탈락, 신호 종료를 구분 |
| 공통 조건 | 상단 밴드 안전 종료 지표를 별도 그룹에서 한 번만 표시 |
| PC-26 회복 Guard | 판정 불가 값을 회복 성공으로 취급하던 분기 수정. 유효한 `%B >= 0.25` 판정이 있어야 인계 진행 |
| C trailing Guard | 판정 불가 EMA를 비증가로 취급해 후속 event를 만들지 않도록 수정. 유효한 시간청산 판정은 계속 확인 |

판정 불가 값 회복 문제는 수정 전 테스트에서 `PC-26`이 잘못 선택되는 것을 재현한 뒤 수정했다. 운영 로그에서 이 잘못된 인계가 발생했다고 확인한 것은 아니다.

## 4. 검증 및 적용

| 검사 | 결과 |
|---|---|
| backend 전체 | 1,131개 실행, 1,121개 통과·10개 건너뜀, 실패·오류 없음 |
| UI 전체 | 49개 파일, 501개 모두 통과 |
| TypeScript 및 배포 빌드 | 통과 |
| 문서 기준 독립 경계·우선순위 검사 | 기존 665건 모두 통과 |
| 앱 내장 코드 검사 | 독립 665건과 회귀 24개 통과. 프로젝트 소스 대신 PyInstaller 내장 모듈을 로드하고 네트워크 연결 차단 |
| 실제 Controller → UI 재생 | B/C 매수 요청·체결, Trend Hold/TP trailing, C 청산 후 B 인계, 타이머 갱신·재연결·종료 검사 통과 |
| C 보유 중 B 신호 | C 단일 owner 유지, B 확정봉 결과 보존, B-05 후 WAIT_PULLBACK 표시, 추가 매수 없음 |
| 화면 검증 | 실제 재생 DTO로 380px 폭에서 Case별 제목·현재 단계·조건·카운트다운 표시 확인 |
| 설치 | macOS 로컬 서명 검증 통과. 기존 설치본 백업 후 `/Applications/Binance Auto Trader.app` 교체. 모든 일반 파일 SHA-256이 검증 빌드와 일치 |

로컬 통신 포트가 차단된 첫 전체 검사 결과는 최종 결과에서 제외했다. 로컬 포트 사용 권한으로 재실행해 전체 통과를 확인했다. 마지막 변경인 다른 Case 주문 대기 안내는 최종 앱 빌드와 내장 코드 회귀 검사에 포함됐다.

실제 매수·매도는 실행하지 않았다. 화면 검증은 가짜 거래소를 사용하는 실제 상태 전이 재생 데이터로 수행했다. 새 설치 앱은 자동 실행하거나 매매를 시작하지 않았으며, 다음 실행부터 수정본이 적용된다.

## 5. 증거

- [backend 전체 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/lower_case_indicators_2026-09-10/backend.log), [UI 전체 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/lower_case_indicators_2026-09-10/ui.log)
- [상태 전이·UI 재생 데이터](/Users/oscar/Desktop/Binance_Auto/Log_History/lower_case_indicators_2026-09-10/replay.json)
- [앱 내장 코드 검증](/Users/oscar/Desktop/Binance_Auto/Log_History/lower_case_indicators_2026-09-10/bundled-verification.json), [설치본 검증](/Users/oscar/Desktop/Binance_Auto/Log_History/lower_case_indicators_2026-09-10/installed-app-verification.json)
- 이전 설치본: `/Users/oscar/Desktop/Binance_Auto/Log_History/lower_case_indicators_2026-09-10/previous-installed.app`
