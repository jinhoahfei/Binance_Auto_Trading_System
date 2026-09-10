# 중지 후 재실행·실시간 지표 표시 수정

2026-09-10. 앞선 실시간 갱신·중지·종료 수정을 유지한 후속 수정이다.

## 원인과 변경

REGIME 선택값은 중지 후에도 남지만 첫 START가 준비된 TradingSTM을 사용한 뒤 `_selected_stm = None`으로 바꾼다. 다음 START의 Guard는 이 상태를 미선택으로 해석해 `A REGIME must be selected again before a new session starts` 오류를 반환했다. 화면과 backend의 선택 수명이 달랐다.

시작 조건 검증은 지속되는 REGIME 선택을 기준으로 수행하고, 준비된 STM이 없으면 명시적 START에서 같은 REGIME의 새 TradingSTM을 생성한다. 기존 종료 세션의 상태 머신을 재사용하지 않는다. 연결, 포지션, 주문, 지원 여부, 조정 상태와 중복 명령 검사는 유지한다.

실시간 지표의 현재값과 비교 기준값은 공통 Decimal 문자열 포맷터로 소수점 둘째 자리까지 반올림한다. 예: `2485.043400943892...` → `2,485.04`, `0.019999...` → `0.02`. 매매 계산과 조건 충족 색상은 원본 정밀도의 backend 판정을 유지한다. 시간 안내의 `3분`, `6시간` 같은 단위 문구와 카운트다운 형식은 유지한다.

## 검증

- REGIME 한 번 선택 → START → STOP을 3회 반복: 각 세션 ID와 STM이 새로 생성됨. 중복 START는 기존 결과만 반환. 중지 후 타이머·주문 요청 없음.
- 중지 후 시세 미준비 상태에서 START: 기존 연결 Guard가 재실행 차단.
- 반올림 경계·음수·큰 가격·미수신·조건 충족 색상 검사 통과.
- backend 전체 1,112개 실행: 1,102개 통과, 10개 건너뜀, 실패·오류 없음.
- UI 전체 48개 파일, 497개 모두 통과.
- TypeScript 검사와 배포 앱 빌드 통과.
- 앱 내장 Python 코드에서 문서 기준 665건 및 재실행을 포함한 회귀 검사 73개 통과. 검사 중 네트워크 연결 차단.
- 설치 앱의 로컬 서명 검증 및 빌드와 전체 파일 해시 일치 확인.

실제 매수·매도는 실행하지 않았다. 재실행은 가짜 거래소에 연결된 실제 controller를 사용해 검증했다. 기존 앱을 포지션 0·매매 미실행 상태에서 정상 종료한 뒤 교체했으며, 설치 후 자동매매를 시작하지 않았다.

## 결과물

- 설치: `/Applications/Binance Auto Trader.app`
- 이전 설치본: `/Users/oscar/Desktop/Binance_Auto/Log_History/restart_indicator_fix_2026-09-10/previous-installed.app`
- [backend 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/restart_indicator_fix_2026-09-10/backend.log)
- [UI 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/restart_indicator_fix_2026-09-10/ui.log)
- [내장 코드 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/restart_indicator_fix_2026-09-10/bundled-verification.json)
- [설치 확인](/Users/oscar/Desktop/Binance_Auto/Log_History/restart_indicator_fix_2026-09-10/installed-app-verification.json)
