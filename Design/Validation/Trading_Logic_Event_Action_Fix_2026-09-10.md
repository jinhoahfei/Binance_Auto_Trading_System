# Event–Action Table 불일치 수정 및 검증 결과

2026-09-10. 기준 소스 commit: `94be568` 이후 이번 작업의 변경분.

기준 문서는 [Trading_Logic_Event_Action_Table.md](/Users/oscar/Desktop/Binance_Auto/Design/Trading_Logic/Event_Action_Table/Trading_Logic_Event_Action_Table.md)이다. 표를 바꾸어 구현에 맞추지 않고, 앞선 감사에서 발견한 구현·표시·설치본 문제를 수정했다.

## 수정 내용

| 발견 문제 | 수정 후 동작 |
|---|---|
| Trend Hold가 표에 없는 비상손절·확정봉 손절·6시간 청산을 추가 적용 | PB-15/16/17대로 EMA slope ≤ 0.04를 5초 유지하거나 %B < 0.60을 5초 유지할 때만 Trend Hold 청산. 보유 상태에서 늦게 도착한 손절·시간청산 이벤트도 Trend Hold 주문을 만들지 않음 |
| Trend Hold 화면이 평가하지 않아야 할 손절·시간 지표를 표시 | 해당 상태의 두 약화 지표만 표시. 기존 공통 상단 안전 종료 조건은 표대로 유지 |
| 평가 중단 상태가 ‘정상 작동’으로 보임 | backend lifecycle을 UI까지 전달. `reconciliation_required`는 ‘주문 상태 확인 필요’, `stopping`은 ‘중지 처리 중’으로 표시 |
| 평가가 중단돼도 이전 값·색상·타이머가 실시간처럼 보임 | 중단 안내를 표시하고 값은 `—`, 판정은 회색, 타이머는 확인 대기로 표시 |
| 재연결 알림을 닫으면 복원된 실행 상태가 중지로 바뀜 | 재동기화로 수신한 lifecycle에 따라 실행/확인 필요 상태로 복귀 |
| 소스와 실제 앱의 조건이 다름 | 최신 소스로 앱을 재빌드하고 `/Applications/Binance Auto Trader.app`에 설치. 이전 설치본 백업과 전체 파일 해시 대조 완료 |

Case B 일반 보유 상태의 −1% 비상손절, 확정봉 손절, 6시간 청산은 표대로 유지한다. 이번 변경은 **Trend Hold에서 표에 없이 추가 적용하던 분기**를 제거한 것이다.

실시간 현재가 ≤ 같은 시점의 30분봉 BB 하단이면 봉 확정을 기다리지 않고 하단 이벤트를 시작한다. Case B 활성화는 하단 접촉 순간 저장한 같은 30분봉의 BBW < 0.02로 판단한다. 접촉 이후의 BBW나 봉 저가를 재검사하지 않는 기존 수정도 새 앱에 포함했다.

## 검증 결과

| 검사 | 최종 결과 |
|---|---|
| backend 전체 테스트 | 1,108개 실행: 1,098개 통과, 10개 건너뜀, 실패·오류 0 |
| UI 전체 테스트 | 48개 파일, 490개 테스트 모두 통과 |
| TypeScript 검사 | 통과 |
| 문서 기준 독립 경계·우선순위 검사 | 665건 모두 통과. 감사에서 불일치했던 Trend Hold 3건 포함 |
| 기존 테스트와 독립 검사의 전이 관측 | 표의 109개 전이를 모두 최소 1회 관측 |
| 실제 backend 오류 snapshot → UI 변환 | 중단 안내·확인 필요 상태·회색 지표 재현 통과 |
| 새 앱에 내장된 코드의 독립 검사 | 665건 모두 통과 |
| 새 앱에 내장된 코드의 접촉·BBW·지표·상태 흐름 검사 | 17개 모두 통과 |
| 앱 빌드 및 macOS 로컬 서명 검사 | 통과 |
| 설치본과 검증 빌드의 파일 내용 비교 | 전체 일반 파일 SHA-256 일치 |
| 설치 앱 시작 확인 | 정상 연결, ‘매매 시작 전 / 매매 중지’. 실제 실행 앱과 sidecar 경로 모두 `/Applications` 설치본 확인 |

앱 내장 코드 검사는 PyInstaller archive의 프로젝트 모듈을 로드하여 수행했다. 검사 중 거래소 네트워크 연결을 차단했으며, 소스 모듈로 대체하지 않았다. 소스 테스트만 통과하고 구버전 실행 파일을 남기는 문제를 별도로 확인했다.

전체 검사 최초 실행에서는 샌드박스의 로컬 포트 차단으로 프로세스 통신 테스트가 실패했다. 실제 거래 자격증명을 제거하고 로컬 테스트 서버 사용 권한으로 재실행하여 위 최종 통과 결과를 확인했다. 빌드에는 프로젝트에 지정된 로컬 pnpm 11.16.0을 사용했다.

## 증거와 설치 위치

- [검증 요약](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_fix_2026-09-10/verification-summary.json)
- [backend 전체 로그](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_fix_2026-09-10/suite.log), [UI 전체 로그](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_fix_2026-09-10/ui.log)
- [665개 입력·결과](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_fix_2026-09-10/independent.json), [내장 코드 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_fix_2026-09-10/bundled-verification.json)
- [backend 중단 상태의 UI 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_fix_2026-09-10/ui-probe.json)
- [설치본 검증](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_fix_2026-09-10/installed-app-verification.json), [검증 소스 해시](/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_fix_2026-09-10/audited-source-sha256.json)
- 실행 앱: `/Applications/Binance Auto Trader.app`
- 이전 설치본: `/Users/oscar/Desktop/Binance_Auto/Log_History/event_action_fix_2026-09-10/previous-installed.app`

앞선 감사 보고서는 당시 결과를 보존한다. 현재 수정·설치 결과는 이 문서를 기준으로 확인한다.

## 검증 범위

실제 주문은 전송하지 않았다. 실시간 접촉 이후 추적과 매수·매도 흐름은 통제된 시장 입력과 가짜 체결로 검증했다. 109개 전이 관측은 모든 입력 조합이나 장시간 실거래의 무오류 보증을 의미하지 않는다.

과거 스크린샷에서 지표가 멈춘 직접 원인은 당시 로그가 없어 확정되지 않았다. 이번에는 확인된 ‘평가 중단을 정상 작동으로 숨기는 경로’를 수정하고 재발 방지 검사를 추가했다.
