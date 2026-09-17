# 2026-09-17 자동매매 종료 및 실시간 지표 공백 분석

분석 범위: 첨부 화면의 실행에 대응하는 live PID 46388, run_id `e7b7439dadd848ae8d986b67dd91c6d5`, 세션 `dd9a782a-7e92-40f6-9f0c-82f06a605e03`. 시각은 모두 KST다.

## 결론

03:06:20.052에 `G-07 / UPPER_BAND_SAFE_TERMINATION`이 실행되어 자동매매 세션이 종료됐다. 당시 실시간 ETHUSDT 가격 2,419.61이 실시간 30분봉 볼린저밴드 상단 2,419.557348577058… 이상이었다. 포지션과 미결 주문이 없어 즉시 `LOGIC_TERMINATED`가 됐다.

현재 구현은 이 조건에서 다음 하단 접촉을 기다리는 상태로 돌아가지 않는다. 세션 평가와 타이머를 정리하고 계속 종료 상태로 남는다. 앱 자체와 공개 시세 수신은 별도로 살아 있었다.

종료 후 backend는 `active_logic = null`을 제공한다. 화면은 이 경우 전략 지표를 표시하지 않고 ‘자동매매 종료’, ‘실행 중인 전략이 없습니다.’를 표시한다. 첨부 화면은 이 경로와 일치한다.

## 상태 변경 이력

| 시각 | 실제 동작 | 상태 |
| --- | --- | --- |
| 01:15:03–01:15:06 | 앱 시작, 시세 연결 및 잔고 대조 완료 | 앱 `CREATED → STARTING → READY`, 매매 `not_started` |
| 01:15:10.788 | TYPE_0 선택 | 매매 시작 전 |
| 01:15:13.835 | 자동매매 시작, G-01 | `running / LOWER_TOUCH_WATCH` |
| 02:35:40.059 | 가격 2,378.06 ≤ 하단 2,379.255257…; G-02로 하단 이벤트 생성 | `TRADE_MANAGEMENT`, 포지션 없음 |
| 02:35:40.060 | 터치 시 BBW 0.01613747… < 0.02; B-03 | B: `B_WAIT_SIGNAL`, C: `C_WAIT_SETUP` |
| 03:00:00.067 | 30분봉 마감에서 B 조건 불충족; B-04 | B/C 대기 유지 |
| 03:00:28.051 | 새 30분봉에서 가격 2,378.53 ≤ 하단 2,379.660691…; G-03 | 새 하단 이벤트로 초기화 후 B 신호 대기/C 설정 대기 |
| 03:06:20.052 | 가격 2,419.61 ≥ 상단 2,419.557348…; G-07 | `LOGIC_TERMINATED / terminated` |
| 03:06:20–11:26:23 | 재시작 없음; 시장 입력은 계속 수신 | 매매 종료 상태 유지, 지표 없음 |
| 11:27:31–11:27:33 | 화면 경로의 앱 종료 요청, 잔고 확인, 앱 종료 | 앱 `READY → SHUTTING_DOWN → CLOSED`, backend exit code 0 |

자동매매 실제 실행 시간은 약 **1시간 51분 6초**다. 스크린샷 파일명 시각 11:26:23 기준으로 이미 **8시간 20분 3초** 동안 매매가 종료돼 있었다. 11:27의 앱 종료는 03:06의 전략 종료와 별개이며, 이 분석 중 에이전트가 실행한 동작이 아니다.

## 매수·매도가 없었던 이유

이 실행에는 `SubmitOrder`, `ForceSellAll`, 주문 제출/체결 이벤트가 없다. 전략 평가 기록의 포지션은 0이며 마지막 세션 상태도 `has_open_position = false`다. 따라서 이 세션의 자동매매 주문·체결은 0건으로 판단한다. 계좌의 외부 수동 거래 전체를 조사했다는 뜻은 아니다.

### Case B

하단 접촉 시 BBW 조건은 통과했다. 그러나 회복 신호는 30분봉 마감 때 세 조건을 모두 충족해야 한다. 03:00 마감 평가 결과:

| 조건 | 실제 값 | 기준 | 결과 |
| --- | --- | --- | --- |
| EMA9 기울기 | -0.03296212 | > -0.03 | 미충족 |
| 종가 %B | 0.255683574… | > 0.25 | 충족 |
| 신호봉 저가 | 2,371.62 | ≥ 직전 3봉 최저가 2,386.51 | 미충족 |

따라서 `B_WAIT_PULLBACK`으로 진입하지 못했다. 03:00:28에 새 하단 이벤트가 생긴 뒤에도 다음 30분봉 마감 전에 상단 접촉 종료가 발생했다.

### Case C

초기 설정은 `%B ≤ -0.15`와 `CCI ≤ -140`을 동시에 요구한다. Case C 조건이 기록된 평가 931건에서 두 조건 동시 충족은 0건이다. 평가 시점의 최저 %B는 02:49:52의 **-0.136915678…**였다. 이때 CCI는 -179.656369…로 통과했으나 %B는 -0.15 이하가 아니었다. 따라서 `C_WAIT_SETUP`을 벗어나지 못했다.

## 직접 원인과 표시상의 문제

1. **매매 종료 정책**: G-07은 `TRADE_MANAGEMENT` 중 상단 접촉 시 무포지션이라도 전체 매매 세션을 종료한다. 확정봉 마감이나 추가 유지시간을 기다리지 않는다. 이 동작은 현재 소스와 이벤트·액션 표에 모두 명시돼 있다. 표는 미구현 상단 전략으로 인계하는 대신 기존 안전 종료 절차를 사용한다고 설명한다.
2. **종료 후 재감시 없음**: G-04의 일반 이벤트 완료는 `LOWER_TOUCH_WATCH`로 돌아가지만, 이번 G-07은 `LOGIC_TERMINATED`로 간다. 시작 명령은 01:15의 1건뿐이며 이후 재시작 기록이 없다.
3. **원인 설명이 없는 화면**: 종료 상태를 표시하면서 실행 지표를 비우는 것은 현재 코드의 의도된 동작이다. 다만 이 지표 패널에는 종료 시각, 종료 사유, 마지막 판단 수치가 없어 사용자가 장시간 계속 실행 중이었다고 오해하기 쉽다.

연속 운용이 목적이면 ‘상단 접촉 이후에도 다음 하단 이벤트를 감시할 것인가’를 매매 정책으로 정해야 한다. 이는 화면 새로고침만으로 해결되지 않는다. 종료 사유·시각·마지막 판단값을 화면에 남기는 개선은 매매 정책 변경과 별도로 필요하다.

## 장애와 구분한 근거

- 실행 로그 10개, 총 140,316개 레코드를 확인했다. sequence 누락 0, 기록된 drop 0, ERROR/CRITICAL 0이다.
- 초기 시세 연결 준비 외에는 11:27 앱 종료 전까지 `stream_unavailable`이 없다.
- 화면 연결 기록에는 최초 연결 1회, heartbeat 713건이 있고 오류·재연결 이벤트가 없다.
- 종료 이후에도 `market_input_observed` 54,082건이 기록됐다. 시세 입력 수신과 매매 전략 실행은 별개다.
- 11:26:40 heartbeat에는 `status = terminated`, `active_logic = null`, 마지막 전략 평가 03:06:20, 마지막 시장 입력 11:26:40이 함께 기록됐다.
- 수동 kill은 false이며, 주문 위험 한도 거절이나 주문 실패 기록도 없다.
- 최종 앱 종료는 exit code 0으로 확인된다. 프로세스 목록 접근은 환경 권한 때문에 불가했으므로 현재 프로세스 존재 여부를 별도로 조회했다고 주장하지 않는다.

## 주요 근거 위치

- [시작 명령](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-17_01-15-03-581508_KST_live_46388_e7b7439dadd848ae8d986b67dd91c6d5_part0001.log:46)
- [첫 하단 접촉](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-17_02-11-24-055205_KST_live_46388_e7b7439dadd848ae8d986b67dd91c6d5_part0003.log:9200)
- [Case C 최저 %B 평가](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-17_02-38-46-068373_KST_live_46388_e7b7439dadd848ae8d986b67dd91c6d5_part0004.log:4877)
- [03:00 확정봉의 Case B 평가](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-17_02-38-46-068373_KST_live_46388_e7b7439dadd848ae8d986b67dd91c6d5_part0004.log:9366)
- [새 하단 이벤트](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-17_03-00-20-052303_KST_live_46388_e7b7439dadd848ae8d986b67dd91c6d5_part0005.log:67)
- [G-07 종료 당시 입력과 판단](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-17_03-00-20-052303_KST_live_46388_e7b7439dadd848ae8d986b67dd91c6d5_part0005.log:2752)
- [running → terminated 상태 변경](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-17_03-00-20-052303_KST_live_46388_e7b7439dadd848ae8d986b67dd91c6d5_part0005.log:2753)
- [마지막 heartbeat](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-17_11-23-58-051014_KST_live_46388_e7b7439dadd848ae8d986b67dd91c6d5_part0010.log:522)
- [정상 프로세스 종료](/Users/oscar/Desktop/Binance_Auto/Log_History/runtime_health/runtime_1789575306471_46206_part0001.log:1281)
- [G-07 구현](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/domain/trading/transitions/global_transitions.py:84)
- [G-07 설계 표](/Users/oscar/Desktop/Binance_Auto/Design/Trading_Logic/Event_Action_Table/Trading_Logic_Event_Action_Table.md:180)
- [종료 시 active_logic을 비우는 코드](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/trading_logic_snapshot.py:47)
- [실시간 지표 빈 상태 문구](/Users/oscar/Desktop/Binance_Auto/UI/src/features/recent-orders/tradingIndicatorPresenter.ts:101)
- [자동매매 종료 표시](/Users/oscar/Desktop/Binance_Auto/UI/src/shared/formatting/tradingLogicState.ts:15)

분석을 위해 로그와 소스를 읽고 이 보고서만 추가했다. 앱 실행·종료, 자동매매 재시작, 주문, 전략 및 소스 변경은 수행하지 않았다. 판단 근거는 실제 실행 기록과 구현 대조이며, 이번 진단에서 테스트나 실거래 재현은 실행하지 않았다.
