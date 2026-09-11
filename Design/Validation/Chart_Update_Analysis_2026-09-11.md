# 차트 갱신 중단 원인 분석

2026-09-11. 사용자가 다른 실시간 지표는 갱신되지만 차트의 현재가와 마지막 봉은
01:00 진행 봉에서 멈췄고, LIVE 표시는 유지됐다고 확인했다.

## 결론과 확인 범위

차트는 backend의 시장 snapshot과 다른 WebSocket 연결을 사용한다. 현재 구현에서
차트 메시지 수신이 중단돼도 close/error 이벤트가 없으면 LIVE가 유지되고 재연결도
시작되지 않는 결함을 격리된 hook 검사로 재현했다. 사용자가 설명한 증상과 일치한다.

다만 차트 연결의 수신·종료·재연결 이력은 파일 로그에 저장되지 않는다. 이번 실행에서
차트 수신이 처음 멈춘 이유와 정확한 시각은 현재 기록만으로 확정할 수 없다.
backend의 01시 오류가 차트 정지를 직접 일으켰다고 단정할 근거도 없다.

backend에는 실제 오류가 있었지만 복구 후 갱신됐다. 따라서 backend가 01시부터
종료 시까지 계속 멈춰 있었다는 설명은 실행 기록과 맞지 않는다.

## 실행 로그

대상 run ID는 `4e36f478b5764f56a20fc26f83fee990`, PID는 `77312`이다.
2026-09-10 23:40:05에 시작해 다음 날 01:16:15에 종료된 실행의 part0001~0004를 확인했다.
아래 시각은 모두 KST다.

| 시각 | 기록과 판정 |
| --- | --- |
| 23:52:00 → 23:52:01 | 직전 1분 확정 봉과 다음 진행 봉의 동일 event time을 conflict로 처리한 뒤 복구 |
| 00:37:00 → 00:37:01 | 같은 정렬 판정으로 복구 반복 |
| 01:00:01.964 → 01:00:07.523 | 4시간 경계 source 병합 오류 후 복구 |
| 01:00:25.99 → 01:01:32.31 | 시장 입력 약 66초 공백. 01:01:26에 평가 정지 감지, 01:01:32에 복구 완료 |
| 01:13:59.965 → 01:14:01.131 | 동일 event time 정렬 판정으로 복구 |
| 01:16:13.954 | 01:00 시작 30분 진행 봉 수신, 종가 2,438.26 USDT |
| 01:16:15.307 | 애플리케이션 종료 |

네 주기 모두 종료 직전까지 입력이 기록됐다. 전체 실행에서 로그에 남은 입력 봉 수는
1분 2,733개, 30분 2,724개, 4시간 2,725개, 1일 2,723개다.
이는 WebSocket 원본 패킷 수가 아니라 `market_input_observed`에 기록된 봉 수다.

## 차트 코드의 결함 재현

`App.tsx`의 `use_realtime_chart_data()`는 Binance의 네 주기 과거 봉과 별도 combined
WebSocket을 사용한다. 다른 실시간 지표의 backend 복구가 이 연결을 재생성하지는 않는다.

| 격리 검사 | 현재 구현의 실제 결과 | 이번 증상과의 관계 |
| --- | --- | --- |
| 정상 초기화 후 메시지 없이 120초 경과 | LIVE·마지막 가격·갱신 시각 유지, 재연결 0회 | 사용자가 설명한 증상을 재현 |
| 30분 수신 없이 120초 경과 후 1분 메시지만 수신 | 30분 가격은 이전 값인데 공통 갱신 시각과 LIVE는 갱신 | 주기별 정지를 감지할 수 없음 |
| 재연결 후 네 REST 요청 중 하나만 미완료, 새 30분 메시지 수신 | 120초 후에도 이전 봉 표시, 새 메시지는 buffer에 남고 요청 취소 없음 | 별도 정지 경로. 이 경우 표시는 reconnecting이므로 이번 LIVE 증상의 직접 근거로 삼지 않음 |

검사는 원본 UI source를 `/private/tmp/binance-chart-analysis-20260911/UI`에 복사하고
가짜 WebSocket·REST·시계를 주입해 실행했다. 결함 재현 검사 3개가 모두 통과했다.
수정 후 회귀 검사를 통과했다는 의미는 아니다.

주요 코드 위치:

- `UI/src/app/App.tsx:51`: backend와 별도 차트 데이터 hook.
- `UI/src/features/price-chart/hooks/useRealtimeChartData.ts:471`: 오류·종료 등에만 연결된 재시도.
- 같은 파일 `:549`: 주기와 무관하게 공통 LIVE·updated_at 갱신. 수신 중단 감시 없음.
- 같은 파일 `:557`, `:608`: 과거 봉 전체 완료 전 실시간 봉 buffer 처리.
- `UI/src/features/price-chart/presenters/createRealtimeChartViewModel.ts:125`: 선택 주기에 공통 상태와 시각 표시.

## 실제 로그로 재현한 backend 오류

### 4시간 경계에 대기 중인 일봉이 source에 혼입

part0004의 909행에서 1일 진행 봉이 snapshot 시각 검증에 걸려 대기했다.
910행에서는 1분 확정·30분 확정·4시간 확정·새 4시간 진행 봉의 네 source를 처리한다.
그런데 `_merge_klines_into_snapshot()`은 대기하던 일봉까지 합쳐 snapshot source를
`1d, 1m, 30m, 4h, 4h` 다섯 개로 저장한다.

평가 builder는 정해진 경계 source 개수와 순서를 요구하므로
`a market version has ambiguous strategy source Klines` 오류를 발생시킨다.
실제 로그의 봉을 정규화해 원본 `MarketSnapshot`, 병합 메서드, source 검증 메서드에
전달했을 때 같은 오류가 재현됐다. 전체 거래 실행을 재생한 검사는 아니다.

관련 코드는 `market_data_controller.py:1444`와 `market_evaluation_builder.py:536`이다.
고칠 때 평가 source 검증을 단순히 느슨하게 풀지 말고, 경계의 정해진 source와
독립적인 진행 봉의 snapshot 반영 순서를 일관되게 처리해야 한다.

### 서로 다른 봉의 동일 event time을 conflict로 판정

23:52, 00:37, 01:14 전환에서는 직전 확정 1분 봉과 정확히 다음 분의 진행 봉에
같은 거래소 event time이 들어왔다. `stream_ordering.py:36`의 분기는 서로 다른 봉이라는
사실을 구분해 허용하지 못하고 conflict를 반환한다. 실제 세 쌍 모두 원본 함수로 재현했다.
동일한 봉의 모순된 변경과 정상적인 다음 봉 전환을 구분할 필요가 있다.

## 필요한 수정

- 주기별 마지막 유효 수신 시각을 기준으로 지연을 감지하고, LIVE 해제와 재연결·누락 봉 복구를 연결한다.
- WebSocket 연결 대기와 과거 봉 REST 요청에 시간 제한을 둔다.
- 차트의 연결 시작·첫 수신·수신 중단·오류·재연결 결과를 파일 로그에 기록한다.
- backend의 경계 source 혼입과 동일 event time의 다음 봉 판정을 실제 기록 기반 회귀 검사로 수정한다.

이번 작업은 원인 분석과 재현까지 수행했다. 애플리케이션 source 수정, 실행 재시작,
주문 호출은 수행하지 않았다.

## 증거

- [로그 요약](/Users/oscar/Desktop/Binance_Auto/Log_History/chart_update_analysis_2026-09-11/log_summary.json)
- [backend 실제 봉 재현 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/chart_update_analysis_2026-09-11/backend_replay.json)
- [backend 재현 코드](/Users/oscar/Desktop/Binance_Auto/Log_History/chart_update_analysis_2026-09-11/replay_backend.py)
- [UI 격리 재현 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/chart_update_analysis_2026-09-11/chartFreeze.diagnostic.test.tsx)
