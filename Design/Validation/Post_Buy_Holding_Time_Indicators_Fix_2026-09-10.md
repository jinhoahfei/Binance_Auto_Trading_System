# 매수 직후 보유시간 오류와 실시간 지표 중단 수정

2026-09-10. 이전 Case C 회복 기준 수정은 유지하고, 체결 후 보유시간 계산과 실제 지표 게시 경로를 검증했다.

## 원인

21:32 시작한 live 실행은 21:35:18 Case C BUY를 체결하고 이력에 반영했다. 거래소 체결 시각은 21:35:18.511 KST인데, 로컬 후속 처리와 예외 기록은 약 21:35:18.482 KST였다. 거래소 시각이 로컬 시각보다 약 29ms 앞섰다.

`Position.entered_at`은 거래소 체결 시각을 보존한다. `_prepare_market_event_context()`는 후속 이벤트의 로컬 발생 시각으로 보유시간을 계산했고, `_calculate_non_negative_elapsed()`가 이 차이를 음수로 판단해 `ValueError`를 발생시켰다.

그 결과 worker가 `EVENT_RUNTIME_FAILED`로 세션을 잠갔다. C-12 이후 `CASE_C_POSITION_OPENED` 처리와 보유 단계 진입이 완료되지 않았고, 지표 저장소·transport 게시도 더 진행하지 못했다. 시세 입력 로그가 계속 생겨도 전략 평가와 실시간 지표는 멈출 수 있다. 이번 실행에서 보유시간 오류와 지표 정지는 같은 실패 경로로 연결된다.

거래소 시각이 정상이어도, 체결 전에 queue에 적재된 market을 체결 후 처리하면 같은 음수 차이가 생길 수 있다. 이 backlog 경로도 재현했다.

## 수정

보유시간에 한해 `max(0, evaluation_time - Position.entered_at)`을 적용한다. 아직 체결 시각에 도달하지 않은 평가의 보유시간은 0이며, 평가 시각이 체결 시각을 지난 뒤부터 원래 체결 시각 기준으로 증가한다.

- 거래소 체결 시각과 영구 이력은 변경하지 않는다.
- queue의 원래 시장 발생 시각과 지표 값도 보존한다.
- Case C 60분·Case B 6시간 시간 청산 경계는 실제 체결 시각 기준을 유지한다.
- 같은 로컬 clock에서 생성한 신호·Case C 회복 타이머가 잘못 미래를 가리키는 경우의 검증은 유지한다.
- worker 예외 차단이나 재조정 gate를 해제하는 방식으로 오류를 숨기지 않는다.

이제 정상적인 시각 오차 때문에 worker가 중단되지 않으므로, 매수 후 보유 상태 전이와 지표 게시가 계속된다. UI의 표시값을 임의로 바꾸는 수정은 없으며, 실제 transport event를 구독한 화면이 자동 갱신되는지 검사했다.

## 재현과 검증

| 검사 | 결과 |
| --- | --- |
| 수정 전 실제 worker와 queue 재현 | 4개 중 3개 실패: worker 재조정 상태 전환 및 음수 보유시간 예외 확인 |
| 수정 후 전용 회귀 검사 | 4개 통과 |
| backend 전체 | 1,144개 실행, 1,134개 통과, opt-in Testnet 10개 건너뜀 |
| UI 전체 | 49개 파일, 503개 통과 |
| 수정 앱 내장 코드 | 이전 Case C 복구 9개 + 이번 보유시간·지표 4개, 총 13개 통과 |

전용 검사는 다음 동작을 확인한다.

1. 실제 worker에서 거래소 체결 시각이 로컬보다 29ms 앞서도 `CASE_C_HOLDING`까지 진입하고, 다음 시장 입력마다 새로운 sequence의 지표 event를 게시한다.
2. 로컬 시각이 체결 시각에 도달하기 전에도 %B·EMA slope 지표는 바뀌고, 보유시간만 0으로 유지한다. 체결 시각을 지난 뒤 타이머가 감소한다.
3. 체결 전 적재된 market을 체결 후 처리해도 원본 지표·시장 시각을 유지하며 worker가 중단되지 않는다.
4. 실제 체결 시각과 이력 시각을 보존하고, 60분·6시간 경계 직전·정확한 경계·직후의 시간 청산 판정이 유지된다.
5. 동일 clock의 잘못된 미래 signal·회복 타이머는 여전히 거부한다.

UI 검사는 Case B와 C 각각에서 NEW → FILLED 응답에 29ms 시각 오차를 주입한다. 실제 Python Controller/TradingSTM이 만든 event를 WebSocket adapter와 App 화면에 전달해 보유 단계, 같은 보유 단계의 연속 %B 변경, 타이머 감소, 이후 단계 전환·종료를 검사했다. 수동 rerender나 추가 snapshot 재조회 없이 갱신됐다.

검사는 가짜 거래소와 임시 이력을 사용했다. 관련 재현과 내장 코드 검사는 외부 연결을 차단했다. 실계좌 주문을 호출하지 않았다.

## 패키징과 적용 상태

빌드는 `/private/tmp`의 별도 소스 복사본에서 수행했다. 이전에 검증한 앱의 UI·native shell 파일 해시를 확인한 뒤 수정 backend만 결합하고 로컬 서명을 다시 검증했다. UI 제품 코드와 native shell 코드는 이번 변경에 포함되지 않는다.

검증된 수정 앱은 `Log_History/post_buy_indicators_fix_2026-09-10/Binance Auto Trader.app`에 보관했다. 기존 개발용 앱 실행 파일, 개발용 sidecar, 공유 패키징 sidecar의 SHA-256이 작업 전후 동일함을 확인했다. `/Applications` 설치본을 교체하거나 앱을 실행하지 않았다.

프로세스 조회에서는 21:32에 시작한 기존 backend PID 43044/43052가 여전히 남아 있었다. 이미 오류로 잠긴 기존 세션을 자동 해제하거나 강제 종료하지 않았다. 소스와 수정 앱에 수정이 완료된 것이며, 현재 구버전 프로세스의 메모리에 적용된 것은 아니다.

## 증거

- [체결 후 오류 로그](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-10_21-32-03-918765_KST_live_43052_c49ae28fea6a490ab25c457b48c7e6f9_part0001.log:1486)
- [수정 전 재현](/Users/oscar/Desktop/Binance_Auto/Log_History/post_buy_indicators_fix_2026-09-10/baseline.log)
- [전체 backend 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/post_buy_indicators_fix_2026-09-10/backend.log)
- [전체 UI 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/post_buy_indicators_fix_2026-09-10/ui.log)
- [수정 앱 내장 코드 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/post_buy_indicators_fix_2026-09-10/bundled-verification.json)
- [기존 실행 파일 보존과 수정 앱 해시](/Users/oscar/Desktop/Binance_Auto/Log_History/post_buy_indicators_fix_2026-09-10/artifact-verification.json)
- [전용 회귀 검사](/Users/oscar/Desktop/Binance_Auto/backend/tests/integration/test_post_buy_indicators.py)
