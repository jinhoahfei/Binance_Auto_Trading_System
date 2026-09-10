# 장시간 실행 뒤 안전 종료 재시도 실패 수정

2026-09-10. 빈 분봉 수신, 시계 오차, 중지 요청 유지, REGIME 재실행과 두 자리 표시 수정은 보존했다.

## 원인

실행 로그에서 02:02 시세 장애 뒤 복구 초기화가 이어졌고, 이후 재연결이 여러 번 반복됐다. 03:00:15와 03:00:22의 STOP 요청은 포지션이 없는 상태에서도 `reconciliation_required`, 빈 action 결과로 반환됐다.

원인 기록은 첫 시세 장애를 `EXACT / MARKET_STREAM_FAILED`로 저장한 뒤, 정상적인 재연결 과정의 `market_stream_initializing` 알림까지 두 번째 독립 원인으로 계산했다. 두 번째 같은 범주 기록은 `DUPLICATE`로 고정되어 원인 정보가 사라졌다. 이전 STOP 복구 경로는 정확한 단일 시세 원인만 허용하므로, 데이터가 복구돼도 중지 명령이 진행되지 않았다. UI의 종료 대기는 반복 조회 끝에 시간 초과했다.

이전 종료 검사는 시세 장애 알림을 한 번만 전달해 실제 재연결 과정의 반복 알림을 누락했다.

## 수정

아직 원인이 정확하게 `MARKET_STREAM_FAILED`인 경우 같은 시세 범주의 반복 알림을 멱등 처리한다. 따라서 시세 장애·복구 초기화·재시도가 반복돼도 시장 문제라는 정보가 유지되고, 복구 검증 완료 뒤 사용자가 요청한 STOP을 실행한다.

계좌·주문 오류, worker 실패, 소유권 불확실성이 추가되면 기존 `CONFLICT / DUPLICATE` 차단이 유지된다. 복구만으로 매매를 자동 재개하지 않으며, 종료 대기 시간을 늘리거나 포지션이 없다는 이유만으로 강제 종료하지 않는다.

## 재현과 검증

- 기존 원인 기록 함수를 사용한 실제 runtime 조립: 시세 장애 → 초기화 → 복구 → STOP 재시도 3회 모두 `reconciliation_required`. 수량 0인데도 shutdown이 차단되는 현상 재현.
- 수정된 실제 startup·worker·파일 journal·shutdown 조립: 장애·초기화 알림 60회, 미복구 상태의 종료 차단, 복구 전 STOP 재시도 3회 또는 복구 후 STOP, shutdown 재시도와 중복 종료까지 모두 `CLOSED / accepted` 도달.
- 반복 시장 장애에 worker 실패가 추가된 경우: 안전 종료 차단 유지.
- 포지션 보유 후 반복 시장 장애: 가짜 거래소 매수 1회 → 복구 뒤 매도 1회 → 수량 0·TERMINATED. 재중지 중복 주문 없음.
- backend 전체 1,118개 실행: 1,108개 통과, 10개 건너뜀, 실패·오류 없음.
- 배포 앱 내장 코드: 문서 기준 665건과 회귀 검사 79개 통과. 네트워크 연결 차단.
- TypeScript 검사·배포 빌드·로컬 서명 검증 통과. 설치본과 빌드의 전체 파일 해시 일치.

검증은 가짜 거래소를 사용한 실제 runtime 조립으로 수행했으며 실계좌 주문을 실행하지 않았다. 새 OS 앱 창에서 장시간 실매매 후 종료한 검증을 의미하지 않는다.

## 적용

소스와 `/Applications/Binance Auto Trader.app`에 적용했다. 이미 실행 중인 이전 개발용 backend의 메모리는 수정되지 않으며, 새 실행부터 적용된다. 이전 프로세스를 강제로 종료하거나 계좌·주문 상태 확인을 우회하지 않았다.

## 증거

- [기존 실행 STOP 재시도 로그](/Users/oscar/Desktop/Binance_Auto/Log_History/repeated_recovery_exit_fix_2026-09-10/original-stop-retries.json)
- [기존 코드 재현 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/repeated_recovery_exit_fix_2026-09-10/baseline-reproduction.json)
- [반복 복구와 종료 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/repeated_recovery_exit_fix_2026-09-10/targeted.log)
- [전체 backend 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/repeated_recovery_exit_fix_2026-09-10/backend.log)
- [내장 코드 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/repeated_recovery_exit_fix_2026-09-10/bundled-verification.json)
- [설치 확인](/Users/oscar/Desktop/Binance_Auto/Log_History/repeated_recovery_exit_fix_2026-09-10/installed-app-verification.json)
