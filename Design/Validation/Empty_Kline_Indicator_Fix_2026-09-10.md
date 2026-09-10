# 실시간 지표 중단 재발: 빈 분봉 처리 수정

2026-09-10. 기존 실시간 시계 오차, 중지·종료, 재실행, 두 자리 표시 수정은 유지한다.

## 실제 원인과 증거

사용자가 03:00에 촬영한 화면은 전략이 `reconciliation_required` 상태여서 값을 숨기는 화면이다. 실행 로그에서는 최초 중단이 02:02:00이며, 시장 수신 복구 후에도 전략 평가는 중단 상태에 남았다. 이 세션은 설치 앱 경로가 아닌 `UI/apps/desktop/src-tauri/target/debug/binance-auto-sidecar`에서 실행됐다.

공개 ETHUSDT 1분봉을 별도로 수신한 결과 03:13:02.041 거래소 이벤트에서 `f=-1, L=-1, n=0`, 모든 거래량 0인 빈 진행봉을 포착했다. 같은 시각 기존 실행 로그는 03:13:02.058에 다시 `kline_stream_invalid`를 기록했다. 사용하던 parser는 `f`, `L`, `n`을 모두 비음수 정수로 제한하여 이 정상 메시지를 거부했다.

[f/L/n의 의미는 공식 Kline 문서](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md#klinecandlestick-streams-for-utc)를 참고했다. `-1` 빈 봉 사례는 문서 예시에서 추정한 것이 아니라 실제 공개 수신 원문으로 확인했다.

이전의 두 분봉 구간 검사는 해당 빈 봉 사례를 포함하지 않아 문제를 발견하지 못했다. 02:02 최초 실패의 예외 본문은 당시 로그에 남지 않았으므로 그 개별 payload까지 확인했다고 주장하지 않는다. 03:13 재발은 시세 원문과 실행 로그의 동시 발생 및 기존 parser 재현으로 확인했다.

## 수정

- `f=L=-1` sentinel을 `n=0`이고 `v/q/V/Q`가 모두 0일 때만 허용한다.
- 음수 체결 수, 다른 음수 ID, 잘못된 자료형, 한쪽만 sentinel인 ID, 실제 거래량과 모순되는 sentinel은 계속 거부한다.
- Gateway의 오류 callback을 원래 예외가 활성화된 문맥에서 호출하여 실제 검증 실패의 stack이 진단 로그에 남도록 변경했다. 잠금 안에서 오류를 기록하고, 잠금을 해제한 뒤 복구 callback을 실행하는 순서는 유지한다.
- 실제 계좌·주문 불확실성의 차단이나 중단 세션 자동 재개 정책을 완화하지 않았다.

## 검증

| 검사 | 결과 |
|---|---|
| 기존 parser 재현 | 3시간 검사 첫 빈 분봉에서 `k.f must not be negative`로 실패 |
| 수정 parser 원문 재생 | 공개 시세 1,284건, 1분봉 마감 15회, 빈 분봉 1건 포함, 거부 오류 0 |
| 실행 중 전략 통합 검사 | 3시간 가속 재생, 빈 분봉 시작 180회, 30분봉 마감 6회. RUNNING·하단 감시 유지, 화면용 현재값과 market version 매번 갱신, 주문 없음 |
| backend 전체 | 1,116개 실행: 1,106개 통과·10개 건너뜀 |
| 실제 원문 fixture | 공개 ETHUSDT 1분봉 원문 변환 검사 통과. 전체 검사 이후 추가한 이 검사도 내장 코드 검사에 포함 |
| 앱 내장 코드 | 문서 기준 665건 및 회귀 검사 78개 모두 통과, 네트워크 연결 차단 |
| 빌드·설치 | TypeScript와 배포 빌드 통과, 로컬 서명 검증·설치 파일 해시 일치 |

실제 매수·매도는 실행하지 않았다. 3시간 검사는 결정적 시각을 사용하는 가속 통합 검사이며, 실제 시장에서 3시간 연속 자동매매를 실행했다는 뜻이 아니다. 공개 시세 재생과 실행 중 전략의 통합 검사는 별도 검증이다.

## 적용 상태

소스와 `/Applications/Binance Auto Trader.app`을 수정했다. 이전 설치본은 증거 폴더의 `previous-installed.app`에 보존했다. 키체인 승인은 사용자에게서 완료 확인을 받았다.

현재 남아 있는 개발용 backend PID 36896/36897은 이전 코드로 실행된 프로세스다. 이전 코드의 중단 상태를 강제로 RUNNING으로 바꾸거나 프로세스를 강제 종료하지 않았다. 최종 설치 앱의 정상 시작까지는 확인하지 못했으며, 개발 실행을 완전히 종료하고 새 실행본으로 재시작해야 적용된다.

## 증거 파일

- [실제 빈 1분봉 원문](/Users/oscar/Desktop/Binance_Auto/Log_History/live_indicator_freeze_2026-09-10/captured-empty-minute.json)
- [기존 실행 중단 기록](/Users/oscar/Desktop/Binance_Auto/Log_History/live_indicator_freeze_2026-09-10/original-interruptions.json)
- [기존 parser 재현](/Users/oscar/Desktop/Binance_Auto/Log_History/live_indicator_freeze_2026-09-10/baseline-reproduction.log)
- [공개 원문 재생 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/live_indicator_freeze_2026-09-10/public-replay-verification.json)
- [전체 backend 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/live_indicator_freeze_2026-09-10/backend.log)
- [내장 코드 검증](/Users/oscar/Desktop/Binance_Auto/Log_History/live_indicator_freeze_2026-09-10/bundled-verification.json)
- [설치 확인](/Users/oscar/Desktop/Binance_Auto/Log_History/live_indicator_freeze_2026-09-10/installed-app-verification.json)
