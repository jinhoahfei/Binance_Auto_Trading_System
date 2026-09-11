# 차트 갱신 복구와 진단 로그 저장 수정

2026-09-11. [원인 분석](Chart_Update_Analysis_2026-09-11.md)에서 재현한 차트 정지와
backend 봉 전환 오류를 수정하고, 차트 전송·화면 반영을 파일로 추적하는 경로를 추가했다.

## 차트 갱신

- 네 주기를 각각 감시한다. 마지막 유효 봉 수신 후 15초가 지나면 LIVE를 해제하고
  재연결한다. 한 주기의 수신이 다른 주기의 정지를 가리지 않는다.
- WebSocket open 대기와 REST 응답 body 수신까지 각각 15초 제한을 적용한다.
  네 주기의 실제 수신과 과거 봉 동기화가 확인돼야 LIVE로 전환한다.
- 재시도는 1·2·5·10·30초로 제한하고, 정상 수신이 확인된 뒤 재시도 횟수를 초기화한다.
- 재연결을 결정하는 즉시 이전 세대 callback과 요청을 폐기한다. 늦은 응답이 이전 가격이나
  LIVE 상태를 다시 게시할 수 없다.
- 다시 조회한 REST 봉이 보관 중이던 오래된 봉을 갱신하고, 그동안 수신한 WebSocket 봉이
  최종 값을 결정한다. 보정된 과거 봉도 차트에 다시 반영하며 기존 확대 범위를 보존한다.
- 선택 주기의 실제 수신 시각을 차트 시각으로 표시한다. 가시성·네트워크 변경 시에도
  상태를 검사한다. 화면 배치·CSS는 변경하지 않았다.

## backend 경계 처리

4시간 경계에서 대기하던 일봉이 canonical strategy source에 섞이던 문제를 수정했다.
1분 확정·30분 확정·4시간 확정·새 4시간 진행 봉을 먼저 같은 version으로 평가하고,
독립적인 일봉은 이후 별도 version에 반영한다. 기존 source 개수·순서 검증을 유지한다.

같은 거래소 event time을 가진 직전 확정 봉과 정확히 다음 진행 봉은 정상 전환으로
허용한다. 같은 봉의 모순된 변경과 중간 봉을 건너뛴 전환은 계속 재동기화 대상으로 판정한다.

backend의 `market_input_observed`에는 요청한 봉뿐 아니라 builder가 실제 읽는
`snapshot_source_klines`도 기록한다. 이후 source 혼입 문제는 같은 version의 두 값을
로그에서 직접 대조할 수 있다.

## 로그 저장 구조

차트 진단은 Tauri의 `record_chart_diagnostics` command가 직접 JSONL 파일에 저장한다.
Python backend 연결 장애 중에도 renderer와 native shell이 동작하면 기록할 수 있다.

| 항목 | 내용 |
| --- | --- |
| 개발 실행 경로 | `Log_History/chart/chart_<native 시작 기록 시각 ms>_<native PID>_part0001.log` |
| 설치 앱 경로 | 운영체제의 앱 로그 디렉터리 아래 `chart/` |
| 파일 분할 | 파일당 5MiB, 과거 파일 보존 |
| 파일 쓰기 | batch마다 append·sync, 실패한 부분 기록은 마지막 확정 위치에서 복구 |
| 실행 연결 | native PID, backend PID·process start ID·session ID, renderer UUID, 연결 세대 |
| 순서·시각 | renderer sequence, renderer `at_ms`, native `native_at_ms` — 모두 Unix millisecond |
| 저장 장애 | renderer queue 최대 256건, 5초 후 재시도, `dropped_before`로 누락 건수 기록 |

일반 브라우저 preview는 native 파일 저장 경로를 사용하지 않는다. 이번 파일 저장 대상은
`pnpm desktop:dev`와 설치된 데스크톱 앱이다. 앱 종료나 renderer 자체 종료 전에 아직
native로 전달되지 못한 메모리 queue까지 영구 보존한다고 보장하지는 않는다.

### 기록하는 사건

| 구간 | 사건과 진단 값 |
| --- | --- |
| 연결 | `connection_started`, `socket_opened`, `connect_timeout`, 연결 세대 |
| 과거 봉 | `rest_started`, `rest_completed`, `rest_failed`, `rest_timeout`, 주기·소요 시간·HTTP 상태 |
| 실제 수신 | 주기별 `first_kline`, 30초 간격 `heartbeat`, 마지막 수신·거래소 event·봉 시작 시각·종가·수신 수 |
| 지연·복구 | `stream_stale`, `socket_error`, `socket_closed`, close code·정상 종료 여부, `reconnect_scheduled`, `connection_ready` |
| 화면 | 첫 반영·주기 변경·30초 간격 `render_applied`, 실패 시 `render_failed`, 반영한 마지막 봉·가격·연결 세대 |
| 실행 환경 | 가시성 변경, 네트워크 연결 상태 변경, `stopped` |

오류는 HTTP·시간 제한·JSON·payload·TypeError·RangeError 등 고정 분류로 남긴다.
원본 응답 body, 임의 예외 원문, URL, 인증 정보는 저장하지 않는다. native command도 고정
event·필드·최대 batch 크기를 검증하고 main window만 허용한다.

### 다음 장애 확인 순서

1. `backend_pid`와 시각으로 해당 Python 실행 로그를 연결한다. renderer UUID와 연결 세대로
   새로고침·재연결 전후를 구분한다.
2. `heartbeat`의 주기별 수신 시각·count를 확인한다. REST가 끝나지 않았다면 해당 주기의
   `rest_timeout`·`rest_failed`와 HTTP 상태를 확인한다.
3. 수신은 전진하는데 화면이 멈췄다면 `render_applied`의 시각·가격과 `render_failed`를 확인한다.
   이 사건은 차트 라이브러리 데이터 반영 완료를 뜻하며 화면 픽셀 캡처를 뜻하지 않는다.
4. `stream_stale` 이후 재연결과 `connection_ready`가 이어졌는지 확인한다. 파일 저장 장애는
   renderer의 고정 오류 코드 `CHART_DIAGNOSTIC_WRITE_FAILED`와 이후 `dropped_before`로 식별한다.

## 검증

| 검사 | 결과 |
| --- | --- |
| backend 전체 | 1,156개 실행, 1,146개 통과, 외부 거래 opt-in 검사 10개 건너뜀 |
| UI 전체 | 49개 파일, 513개 통과 |
| 데스크톱 native 전체 | 49개 통과 |
| TypeScript 검사·UI production build | 통과 |
| 경계 도착 순서 | 경계 네 봉과 일봉의 120가지 순서에서 strict source 검증·일봉 보존 확인 |
| 당시 실제 로그 재생 | 01시 canonical 네 source와 이후 일봉 분리 성공, 동일 E 전환 세 쌍 모두 accept |
| 저장 경로 검사 | native 파일 저장·분할·부분 append 복구, schema 거부, renderer 실패 재시도·256건 제한·누락 수 검증 |

실제 주문은 호출하지 않았다. UI build와 native 검사 target은 `/private/tmp`의 별도 경로를
사용했다. 변경된 native command와 backend는 데스크톱 앱을 다음에 재실행할 때 적용된다.

## 증거

- [backend 전체 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/chart_update_fix_2026-09-11/backend-tests.log)
- [UI 전체 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/chart_update_fix_2026-09-11/ui-tests.log)
- [native 전체 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/chart_update_fix_2026-09-11/native-tests.log)
- [UI build](/Users/oscar/Desktop/Binance_Auto/Log_History/chart_update_fix_2026-09-11/ui-build.log)
- [실제 봉 재생 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/chart_update_fix_2026-09-11/actual_kline_replay.json)
