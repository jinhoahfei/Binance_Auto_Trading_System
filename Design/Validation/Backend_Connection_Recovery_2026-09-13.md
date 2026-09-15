# 장시간 실행 후 UI–백엔드 연결 중단과 종료 실패 수정

## 확인한 원인

2026-09-11 실행 `e9786301cb434dbea2aac00cbd35e6b0`에서는 UI 종료 오류 화면이 촬영된 14:54:21 뒤에도 백엔드의 전략 처리가 14:54:32까지 이어졌다. 이후 부모 앱 종료와 소유권 상실이 기록됐다. 따라서 부모 프로세스 종료를 최초 통신 장애의 원인으로 해석하지 않는다.

기존 `BackendUiAdapter.full_resynchronize()`는 전체 상태 재조회가 한 번 실패하면 재시도 가능 여부와 관계없이 `fail_closed()`를 호출했다. 이때 인증 정보와 이벤트 구독을 폐기했다. 동일 adapter로 종료를 요청하면 HTTP 요청 이전에 `ADAPTER_STOPPED`가 발생했다. 재조회에 일시적 오류를 주입한 테스트로 사진의 오류 경로를 재현했다.

당시 UI의 최초 실패 종류는 기존 기록으로 소급 확정할 수 없다. 아래 진단은 다음 실행부터 통신 실패의 단계와 원인을 남기기 위한 변경이다.

## 구현한 동작

1. 일시적 연결·조회 실패는 인증 정보와 미확정 명령의 멱등 식별자를 보존한다. 재시도는 하나의 작업만 수행하며 대기 시간은 1·2·5·10·30초, 이후 30초다. 재시도 횟수만으로 영구 중단하지 않는다.
2. 소켓 연결은 10초, HTTP 조회는 5초, 정상 이벤트 수신 중단은 75초에 감지한다. 종료 이벤트 없이 발생한 소켓 오류도 복구한다. 정상 idle 상태에서도 백엔드 worker는 로그 활성화 여부와 무관하게 60초 주기로 상태를 게시한다.
3. 재연결한 뒤 전체 snapshot, 동일 backend session, 유효한 새 이벤트 수신까지 확인해야 UI를 다시 online으로 표시한다. 이전 연결의 늦은 callback·취소된 조회 응답은 현재 상태를 변경하지 못한다.
4. UI 연결만 끊겼을 때 거래소·계좌 연결이 정상인 백엔드의 자동매매는 유지한다. UI의 신규 명령은 복구 동안 차단하며, start·stop·주문을 자동으로 재전송하지 않는다. 기존 거래소·계좌 장애와 주문 보호 장치는 그대로 적용된다.
5. 종료 클릭을 하나의 작업으로 합치고 재연결과 직렬화한다. 연결 장애 중에는 최신 거래 상태를 조회한 뒤 기존 중지·정상 종료 절차를 따른다. 종료 결과가 불명확하면 동일한 요청 본문·멱등 키로만 재확인한다. 백엔드의 정상 종료를 확인한 뒤 창을 닫는다.
6. 잘못된 데이터·스키마·세션 또는 명시적 소켓 거부 등 영구 실패는 기존 복구 화면으로 연결한다. 폐기된 adapter 대신 native가 보관한 동일 세션 연결 정보를 다시 얻는다. 전체 지표가 잘못돼도 좁은 종료 상태 API로 안전 종료를 시도할 수 있으며, 열린 포지션·미체결 주문·조정 필요 상태를 backend가 거부하면 창을 유지한다.
7. 부모 앱의 소유권이 사라지면 `recovery.phase=blocked`, `block_reason=PROCESS_OWNERSHIP_AMBIGUOUS`를 게시하고 최초 소유권 상실 이유를 한 번 기록한다.

## 최초 오류 진단

UI 로그는 백엔드 HTTP를 통하지 않고 Tauri native 명령 `record_backend_connection_diagnostics`로 저장한다. 백엔드가 응답하지 않아도 기록할 수 있다.

| 항목 | 기록 내용 |
| --- | --- |
| 시간·실행 상관관계 | UI·native 시간, renderer·adapter·backend session ID, native/backend PID와 backend 시작 식별자 |
| 최초 실패 | `first_failure`, incident ID, 고정 오류 코드, 오류 타입, 실패 단계 |
| 발생 위치 | 연결·인증 전송·수신·JSON 해석·데이터 검증·UI 반영·재조회·종료 단계, 확인 가능한 내부 소스 위치와 검증 필드 |
| HTTP | request ID, 작업 종류, 응답 상태, 소요 시간, 시작·성공·실패 |
| WebSocket | 연결 세대, 열림·인증 전송·닫힘·오류, close code와 clean 여부, 마지막 수신 시각·sequence |
| 복구·환경 | 재시도 횟수·대기 시간, 정상 이벤트 수신 재개, online/offline·화면 표시 상태 변화 |
| 저장 장애 | 고정 저장 실패 코드, 유실 건수 `dropped_before`, 재시도 후 동일 기록 식별자 |

최초 오류를 cleanup 전에 queue에 넣는다. 같은 복구 사건의 후속 오류는 동일 incident ID로 연결하며 최초 항목을 덮어쓰지 않는다. 정상 이벤트 수신 이후의 새 장애는 새 사건으로 분리한다. 명시적인 버전 충돌·열린 exposure에 따른 종료 거부는 통신 장애의 최초 원인과 구분해 HTTP 실패로 기록한다.

메모리 대기는 최대 256건, 송신 batch는 최대 32건이다. 저장 실패 중에는 최초 오류보다 일반 기록을 먼저 버리고 유실 수를 누적한다. native 응답을 5초 이상 기다리면 같은 기록 식별자로 재시도한다. 수신 확인만 유실된 경우 중복 저장될 수 있으므로 분석 시 `(renderer_id, sequence)`로 중복을 제거한다.

파일은 5MiB마다 분할하고 이미 저장된 파일을 보존한다. native는 batch를 기록하고 디스크 동기화를 마친 뒤 완료를 알린다. 정상·복구 종료 모두 마지막 UI 로그 저장을 기다린다. 인증 값·원본 응답·자유 형식 오류 메시지·헤더·계좌 데이터는 UI 진단 schema에 없으며 native에서도 알 수 없는 필드를 거부한다.

브라우저가 제공하지 않는 소켓 내부 오류는 추측하지 않는다. `socket_error`와 발생 단계, 관련 HTTP·backend 로그로 범위를 좁힌다. 릴리스 번들에서는 원본 소스 행이 없을 수 있으므로 고정 단계·검증 필드·빌드 버전을 함께 사용한다. 저장 공간 부족이 계속되거나 OS가 앱을 즉시 종료하면 모든 기록의 보존을 보장할 수 없으며, queue 상한과 유실 건수를 통해 누락 여부를 확인한다.

백엔드에는 `ui_http_request_*`, `ui_http_response_failed`, `ui_stream_opened/authenticated/heartbeat/timeout/protocol_failed/connection_lost/closed`, `process_ownership_lost`를 추가했다. WebSocket 송신 timeout은 인증 timeout과 구분하고, 예외 메시지 대신 타입과 내부 위치를 남긴다.

## 로그 위치와 분석 순서

- 개발 실행: `Log_History/backend_connection/connection_*_part*.log`
- 설치 앱: Tauri `app_log_dir()/backend_connection/` (macOS는 `~/Library/Logs/com.binance-auto.trader/backend_connection/`)
- 백엔드 실행 기록: 기존 runtime 진단 로그

1. UI의 `first_failure`를 찾고 시간·session·incident를 확인한다. 종료 시의 `ADAPTER_STOPPED` 같은 후속 오류부터 원인을 판단하지 않는다.
2. 동일 incident의 request ID, HTTP 상태, 실패 단계와 소켓 close code를 확인한다.
3. backend transport session/request ID 및 PID·시각으로 백엔드 로그와 대조한다. backend의 `ui_stream_*` connection ID는 backend 연결 이력을 묶는 별도 식별자다.
4. `connection_ready`로 이벤트 수신 재개를 확인하고, 종료 사건은 HTTP 수락뿐 아니라 native 정상 종료까지 확인한다.
5. `dropped_before`와 저장 실패 기록을 먼저 확인해 로그 부재를 정상 동작으로 해석하지 않는다.

## 검증

- 백엔드 전체: 1,159개 실행, 10개 건너뜀, 실패 없음. Testnet과 실주문 검증은 비활성화했다.
- UI 전체: 51개 파일, 531개 통과. 타입 검사와 릴리스 화면 빌드 통과.
- Native: 51개 통과. 실제 파일 저장, backend 부재 시 저장, 자유 형식 필드 거부, 기존 파일 분할·부분 쓰기 복구 포함.
- 신규 회귀: 사진의 종료 실패, 100회 재연결, 단일 타이머와 소켓 정리, 단계별 timeout, close 없는 error, 인증/프로토콜 거부, 이전 응답 폐기, 중복 종료 요청, terminal 복구 화면의 정상·차단 종료, 저장 실패와 최초 오류 보존.
- 실제 loopback 사전 점검: 주문 기능 없는 runtime, production Python HTTP/WebSocket·DTO·heartbeat worker, production UI adapter·진단 queue를 사용한다. 80초 idle 구간과 통신·조회·저장 장애를 포함했다. Tauri IPC 대신 Node 파일 sink를 쓰므로 실제 WebView/native 전체 실행의 24시간 증거로 해석하지 않는다.
- 결과 원문: `Log_History/backend_connection_verification_20260913/` 및 `Log_History/backend_connection_soak_smoke_20260913_*/`.

24시간 점검 명령은 다음과 같다. 출력 폴더는 반드시 새 경로를 사용한다.

```sh
cd UI
node scripts/backendConnectionSoak.mjs --hours 24 --output Log_History/backend_connection_soak_24h_NEW_RUN
```

15분마다 실제 소켓 연결을 끊고 최초 두 snapshot 조회에 HTTP 503을 주입한다. 다음 장애는 이전 연결이 복구된 뒤 주입한다. 시간당 80초는 값 변화가 없는 상태에서 60초 heartbeat를 확인한다. 최초 장애에서는 진단 저장도 잠시 실패시킨다. 5초마다 시간·이벤트·복구 횟수·메모리·소켓·backend thread 수를 기록한다. 주문 API는 fixture가 거부하며 자격 증명 파일을 읽지 않는다.

`status.json`의 `completed_needs_review`는 설정한 시간의 실행 종료이며 장시간 안정성 합격을 자동 선언하지 않는다. 복구 완료 횟수·최종 수신 지연·리소스 추이를 확인해야 한다. 24시간 경과 전에는 24시간 검증 완료로 보고하지 않는다.

배포용 앱은 `UI/apps/desktop/src-tauri/target/release/bundle/macos/Binance Auto Trader.app`에 생성한다. 이번 변경은 새 빌드를 실행할 때 적용된다.

## 이번 24시간 실행

최종 코드 점검은 `Log_History/backend_connection_soak_24h_20260913_final/`에서 실행한다. 시작 식별자와 소스 SHA-256은 `Log_History/backend_connection_verification_20260913/soak-24h-final-launch.json`에 보존한다. 앞선 예비 실행은 최종 오류 분류 보완 후 정상 중단하고 최종 코드로 새 실행을 시작했다.

사전 점검의 마지막 50.5초 실행은 장애 2회·복구 2회, 이벤트 21건, 최대 소켓 1개였다. 잠시 저장할 수 없던 최초 오류 2건도 저장됐고 유실 수는 0이었다. 별도의 126초 점검에서는 80초 idle 구간을 통과했다. 최종 24시간 점검은 2026-09-14 01:23:57 KST에 완료됐다. 장애 95회·복구 95회, 중앙 복구 시간 4.074초, 진단 유실 0건으로 검증한 통신 범위 내 통과다. 메모리 누수 부재를 확정하는 검사는 아니며 자세한 분석은 `Backend_Connection_Soak_Result_2026-09-14.md`에 기록했다.
