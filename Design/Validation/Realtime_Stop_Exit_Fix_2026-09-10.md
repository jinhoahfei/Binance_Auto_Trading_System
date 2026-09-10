# 실시간 지표·매매 중지·안전 종료 수정 결과

기준: `989d737` 이후 수정. 2026-09-10.

## 원인과 수정

### 1. 분봉 마감 시 정상 시세를 거부

실제 설치 앱 로그에서 01:15:59, 01:16:59, 01:17:59에 동일한 오류를 확인했다. 거래소는 분봉 마감을 전송했지만 PC 시각이 뒤처져 `closed Kline must end by the snapshot time` 검증에 걸렸다. 이때 시장 연결은 사용 불가로 전환되고 전체 데이터를 다시 받았다. 매매 중 동일 오류가 발생하면 조정 필요 상태에 들어가 전략 평가가 중단되는 경로다.

설치 검증 중 01:37–01:40 구간에는 거래소 event와 로컬 수신 시각 차이가 약 2.04초로 확인됐다. 초기 1초 보정은 이 구간에서 부족했으므로 최종 허용 범위를 5초로 변경하고 같은 조건을 회귀 검사에 추가했다.

수정: snapshot에 실제 포함되는 source임을 검증한 뒤 거래소 event 시각의 최대 5초 시계 오차를 허용한다. 직전 검증 시각도 유지하여 다른 주기의 늦은 tick이 마감 시각을 되돌리지 못하게 했다. 큰 미래 봉, source 불일치, 비연속 이력, 잘못된 마감 이벤트의 검증은 유지한다.

재현 검사: 실제 로그의 20ms·약 2.045초 및 허용 한계 5초 오차의 분봉 마감 → 늦은 다른 주기 tick → 다음 분봉 → 후속 tick까지 version이 연속 갱신된다. 큰 미래 봉은 거부되고 기존 snapshot은 유지된다.

### 2. 실시간 갱신이 중지 명령을 끊음

UI는 거래 snapshot을 받을 때 매번 거래 상태를 재진입했다. 같은 RUNNING 상태의 반복 갱신도 중지 확인창을 닫거나 진행 중인 중지 요청의 Promise 처리를 취소할 수 있었다. 조정 필요 상태에서는 중지 버튼이 비활성화되고 해당 상태의 중지 이벤트도 무시됐다.

수정: lifecycle이 같은 실시간 snapshot은 시작·중지 확인창과 진행 중 명령을 유지하면서 데이터만 갱신한다. lifecycle 자체가 변경되면 서버 상태를 반영한다. 조정 필요 상태에서도 명시적인 중지 확인과 요청을 허용한다.

재현 검사: RUNNING 및 조정 필요 상태에서 중지 확인 직후와 요청 처리 중 snapshot을 반복 전달해도 확인창·명령이 유지되고 중지 요청은 한 번만 실행된다.

### 3. 안전 종료가 조정 필요 상태를 기다리기만 함

backend는 시세 복구 후에도 자동매매를 임의로 재개하지 않도록 중단 상태를 유지한다. 그러나 그 상태의 STOP 요청도 작업 없이 반환했다. 종료 adapter 역시 RUNNING에만 STOP을 보내고 조정 필요 상태에는 snapshot 조회만 반복해, 종료를 진행할 요청 없이 제한 시간이 끝날 수 있었다.

수정: 조정 필요 상태의 프로그램 종료도 먼저 명시적 STOP을 전달한다. backend는 원인이 시세 연결에 한정된 세션의 STOP 요청을 기억하고, 검증된 시세 복구가 완료되면 표의 일반 STOP 경로로 처리한다. 포지션이 없으면 종료하고, 포지션이 있으면 기존 청산 절차를 따른다. 요청 없이 매매를 자동 재개하지 않는다.

worker 실패, 계좌·주문 불확실성, 미완료 startup 조정, 미결 주문·청산 의도가 남은 상태는 이 복구 경로로 우회하지 않는다. 프로그램 종료는 여전히 포지션·주문·조정 상태와 프로세스 정상 종료를 확인한다.

## 검증 결과

| 검사 | 결과 |
|---|---|
| backend 전체 | 1,111개 실행, 1,101개 통과·10개 건너뜀, 실패·오류 없음 |
| UI 전체 | 48개 파일, 493개 모두 통과 |
| TypeScript | 통과 |
| 새 앱 내장 코드 | 문서 기준 665건과 시세·상태·중지 회귀 검사 37개 통과 |
| 시세 복구 전·후 중지 | startup 조정과 파일 journal을 갖춘 fake 거래소 통합 검사 통과 |
| 포지션 보유 중 시세 중단 후 중지 | fake 매수 1회 → 복구 후 매도 1회 → 수량 0·TERMINATED. 재중지 시 중복 주문 없음 |
| 다른 오류가 함께 있는 경우 | 조정 필요 상태 유지, 안전 종료 차단 유지 |
| 종료 adapter | 조정 필요 → STOP → terminal snapshot → shutdown → native 정상 종료 확인 순서 통과 |
| 빌드·설치 | 로컬 서명 검증 통과. 설치본과 검증 빌드 전체 파일 해시 일치 |
| 최종 설치본 실제 시세 | 01:44:27–01:46:27 KST 관측. 거래소 시각 01:45·01:46 분봉 마감 통과, version 2 → 225 연속 갱신, 처리 오류·비정상 연결 중단 0건 |
| 최종 설치본 화면 | 01:44:31 가격 2,497.96 → 01:45:39 가격 2,498.07 → 01:46:29 가격 2,498.15 확인 |
| 최종 설치본 일반 종료 | 포지션 0·매매 시작 전 상태에서 UI 종료 요청. 01:46:41 CLOSED 확인, 앱·sidecar 잔류 프로세스 없음 |

실제 주문은 실행하지 않았다. 포지션 청산과 중지는 가짜 거래소를 사용하는 실제 controller·주문 pipeline으로 검증했다.

## 설치와 증거

- 설치 앱: `/Applications/Binance Auto Trader.app`
- 이전 설치본 백업: `/Users/oscar/Desktop/Binance_Auto/Log_History/runtime_stop_exit_fix_2026-09-10/previous-installed.app`
- [추가 2초 시각 오차 로그](/Users/oscar/Desktop/Binance_Auto/Log_History/runtime_stop_exit_fix_2026-09-10/extended-clock-errors.json)
- [실제 오류 로그 발췌](/Users/oscar/Desktop/Binance_Auto/Log_History/runtime_stop_exit_fix_2026-09-10/original-clock-errors.json)
- [backend 전체 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/runtime_stop_exit_fix_2026-09-10/backend.log), [UI 전체 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/runtime_stop_exit_fix_2026-09-10/ui.log)
- [내장 코드 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/runtime_stop_exit_fix_2026-09-10/bundled-verification.json), [설치본 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/runtime_stop_exit_fix_2026-09-10/installed-app-verification.json)
- [수정 소스 해시](/Users/oscar/Desktop/Binance_Auto/Log_History/runtime_stop_exit_fix_2026-09-10/source-sha256.json)

키체인 승인 후 최종 설치 앱에서 실제 시세와 일반 종료 검증을 완료했다. 실행 중이던 앱은 정상 종료된 상태다. 실제 매매 중 중지·보유 포지션 청산 시나리오는 위의 fake 거래소 통합 검사로 검증했으며, 실계좌 주문은 실행하지 않았다. 두 분봉 마감 구간 관측 결과이며 장시간 연속 운전 검증을 의미하지 않는다.

- [실제 시세 연속 수신 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/runtime_stop_exit_fix_2026-09-10/live-verification.json)
- [최종 설치본 종료 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/runtime_stop_exit_fix_2026-09-10/exit-verification.json)
