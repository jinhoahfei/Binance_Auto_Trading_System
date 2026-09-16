# 백그라운드 연결 유지와 장애 진단

2026-09-17 구현. 실거래 프로그램의 중단·재시작 없이 별도 앱에서 검증한다.

## 동작

- 메인 WebView는 `background_throttling(Disabled)`로 생성한다. macOS 14 이상에서는 `WKPreferences.inactiveSchedulingPolicy`를 실제로 읽어 `verified`/`mismatch`를 기록하며, 구버전은 `unsupported`로 구분한다. 최소 OS 11은 유지한다.
- macOS에서는 앱 실행 중 `UserInitiatedAllowingIdleSystemSleep` 활동을 보유하고 정상 종료 시 반환한다. 컴퓨터 잠자기를 금지하지 않는다.
- 화면·백엔드 통신 장애는 `UI_CONNECTION_DISCONNECTED`로 분리한다. 매매 lifecycle을 중지 상태로 바꾸거나 중지 명령을 보내지 않는다. 상단에 화면 복구 상태와 마지막 정상 수신 시각을 표시한다.
- 화면 연결이 끊어졌을 때 거래소 상태는 확인 불가다. 거래소 상태 조회가 성공하면 백엔드의 확인 시각을 함께 표시한다.
- 전체 snapshot과 이후 정상 이벤트를 모두 확인할 때까지 새 명령을 차단한다. 기존 명령 식별자·결과 조회·안전 종료 절차는 유지한다. 백엔드 75초, 차트 15초 제한도 유지한다.
- 복귀 시 오래된 이벤트를 먼저 적용하지 않고 한 번만 재동기화한다. 정상 실행의 제한 시간은 단조 시계로 검사하므로 벽시계 변경만으로 재연결하지 않는다. OS 복귀 알림에서는 잠자기 동안의 벽시계 공백도 검사한다.

## 진단 경계

| 위치 | 기록과 독립성 |
| --- | --- |
| 화면 | 5초 생존 신호, 타이머 지연, 마지막 이벤트 수신·적용 시각/순번, 차트 수신, 표시/네트워크 상태. IPC 요청은 1개만 대기한다. |
| 네이티브 | 독립 OS thread에서 5초 관측. 메인 스레드 응답, 화면 신호 공백, 백엔드 응답, 전원/열/가림 상태, 자원과 저장 실패를 기록한다. |
| 백엔드 | 인증된 `GET /v1/diagnostics/liveness`. 별도 작은 진단 상태만 읽으며 application lock·계좌 조회·거래소 호출을 하지 않는다. |
| 파일 | 정상 요약 30초, 최근 5분의 5초 측정값은 메모리에 보관. 사건 발생 시 이전 구간과 복구 후 60초를 저장한다. 별도 writer와 256개 queue, 기존 5MiB 분할을 사용한다. |

네이티브 HTTP 관측은 전체 2초 제한, 동시 요청 1개다. 매매 엔진의 정상 여부를 판단하는 제어 입력으로 사용하지 않는다.

`schema_version: 2`는 실제 시각과 단조 시각, renderer/native/backend 실행 ID와 session을 기록한다. 차트와 화면은 같은 renderer ID를 사용한다. WebSocket AUTH의 선택적 UUID `client_connection_id`로 서버 연결과 대응시키며 기존 AUTH도 허용한다. 원문 인증 값·계좌 내용은 신규 진단에 보관하지 않는다.

네이티브 자원 수치는 해당 앱 프로세스의 누적 CPU/최대 RSS다. 별도 WebContent/GPU 프로세스까지 포함한 전체 메모리 수치로 해석하지 않는다.

## 원인 판정 원칙

- 화면 신호가 없다는 사실만으로 화면 실행 정지를 확정하지 않는다. 복구한 화면의 실제 타이머 지연 증거도 확인한다. 이 증거가 없으면 IPC 지연 가능성을 남긴다.
- 메인 스레드·화면·백엔드 처리 지연, 백엔드 probe 실패, 실제 프로세스 종료, 거래소 stream 장애를 구분한다. HTTP 응답 실패만으로 백엔드 종료를 단정하지 않는다.
- OS 잠자기/복귀는 공식 알림으로 기록한다. 가림·최소화·설정값만으로 OS 실행 제한을 확정하지 않는다.
- OS 기록은 해당 실행의 PID에 귀속되는 명시적 완료 상태만 엄격하게 정규화한다. 다른 PID, 부정/예방/요청 문구, 다른 사건의 시각은 직접 증거로 인정하지 않는다.
- OS 수집은 별도 작업으로 최근 5분, 10초, 2MiB 이하에서 제한한다. 접근 거부·미지원·기록 없음·해석 실패·시간 초과를 구분한다. 원문은 저장하지 않는다.
- WebContent 자식 프로세스와 공개되지 않은 OS 내부 결정을 항상 귀속할 수는 없다. 확실성·누락 증거를 결과에 남기며 기록 부재를 정상이나 OS 제한 확정으로 해석하지 않는다.

## 분석

저장 경로는 개발 실행의 `Log_History/runtime_health`, `backend_connection`, `chart`다. 패키징 실행에서는 앱 로그 디렉터리를 사용한다. 기존 v1 파일도 읽는다.

```sh
python3 scripts/analyze_runtime_incidents.py --log-root Log_History --output /private/tmp/new-connection-report
```

새 출력 디렉터리에 JSON과 한국어 요약을 생성한다. 화면 수신 공백, 감지 지연, 재연결 시간, 관측된 서버 socket close→open 공백, 엔진 처리의 관측 공백을 구분한다. 폴링으로 본 엔진 공백은 하한값이며, 서버 close 관측 시각을 숨은 네트워크 장애의 정확한 시작 시각으로 단정하지 않는다. 파일·행 근거와 시간 기준을 함께 제공한다. 재시작한 실행의 단조 시계를 이어 붙이지 않는다.

회귀 자료 `backend/tests/fixtures/liveness/incident_99971.jsonl`은 2026-09-16의 실제 화면 연결 로그 두 행을 선별한 것이다. 화면 공백 **99.971초**, 감지 지연 **97.982초**, 복구 **1.989초**가 계산되며 바이낸스 단절 시간은 알 수 없음으로 유지된다.

## 주문 없는 실제 WebView 검증

```sh
node UI/scripts/buildBackgroundLivenessSoak.mjs --native
python3 scripts/run_background_liveness_soak.py --output /private/tmp/new-renderer-freeze --seconds 180 --phase hidden --freeze-seconds 100
python3 scripts/run_background_liveness_soak.py --output /private/tmp/new-minimized-20m --seconds 1200 --phase minimized
python3 scripts/run_background_liveness_soak.py --output /private/tmp/new-background-24h --seconds 86400 --phase hidden
```

별도 앱 identifier와 가짜 계좌 runtime만 사용하며 모든 변경 HTTP 명령을 거부한다. API 키·원래 실거래 프로세스·실제 주문을 사용하지 않는다. 공개 차트는 실제로 수신한다. 정상 종료 후 `<출력경로>.status.json`과 `validation.json`에 판정을 남긴다.

- 전체화면 가림은 `--phase fullscreen-cover`, 다른 데스크톱 전환은 `--phase other-desktop`으로 각각 1,200초 실행한다. 실제 해당 화면 조작을 수행해야 하며, phase 이름만으로 완료를 인정하면 안 된다.
- 가림/복귀 반복에는 `--cycle-seconds 1200`을 추가할 수 있다. 이 옵션은 검증 창을 주기적으로 표시하므로 사용자 작업을 방해할 수 있는 실행에서는 생략한다.
- 상태만 확인하려면 `python3 scripts/validate_background_soak.py <출력경로>`를 실행한다. 짧은 시험을 20분/24시간 통과로 표시하지 않는다.
- 24시간 지속 수신, 24시간 가림/복귀 반복, 전체화면 가림, 최소화, 데스크톱 전환은 각각의 범위로 보고한다. 다른 범위의 결과를 대신 사용하지 않는다.

## 완료된 검증과 남은 적용 조건

- 백엔드 1,216개 실행: 성공, 기존 실제 거래소 opt-in 테스트 10개 건너뜀.
- 화면 642개 성공, TypeScript 검사 성공. 100초 정지·밀린 이벤트 폐기·재조회 중복 방지·시계 변경·매매 상태 유지·팝업 부재·확인 불가 표시 포함.
- 네이티브 59개 성공. OS 대상/부정 문구 검증, queue 유실 기록, 파일 분할·종료 보호 포함.
- 실제 macOS 27 / WebView `22625.1.29.11.27`에서 `Disabled` 적용 읽기 확인.
- 실제 WebView 150초 시험 중 화면만 100초 정지: 화면 수신 공백 102.367초, 재연결 2.176초, 재연결 1회, 오류/진단 유실 0. 독립 백엔드 관측 지속, 공개 차트 복구 성공. “화면 실행 지연 확인 / OS 직접 증거 없음”으로 판정.
- 실제 최소화 35초 시험: 재연결 0회, 진단 유실 0, 최대 관측 생존 신호 나이 4.351초, 공개 차트 수신 확인.
- 위 짧은 시험은 정식 20분 각 조건 시험이나 24시간 시험의 완료 증거가 아니다. 장시간 상태 파일과 실제 조작 증거를 확인한 뒤 해당 적용 기준을 완료 처리해야 한다.
- 다음 실행용 앱은 기존 앱 경로와 별도 빌드 디렉터리에 생성한다. 이번 작업에서 실거래 앱을 종료·재시작하거나 새 앱으로 교체하지 않는다.

## 현재 진행 중인 장시간 시험

2026-09-17 00:29 KST에 주문 없는 독립 앱 두 개를 시작했다.

- 최소화 20분: `artifacts/background-liveness-20260917/minimized-20m.status.json`
- 숨김 상태 지속 수신 24시간: `artifacts/background-liveness-20260917/background-24h.status.json`

완료 시 상태 파일과 각 출력 폴더의 `validation.json`을 자동 갱신한다. 24시간 시험은 지속 수신 범위이며, 창 표시를 강제로 반복하지 않는다. 24시간 가림/복귀 반복, 전체화면 가림 20분, 다른 데스크톱 20분은 아직 완료하지 않았다. 테스트 완료 전에 장시간 적용 기준을 통과했다고 보고하지 않는다.

다음 실행용 개발 빌드: `UI/apps/desktop/src-tauri/target/background-connection-20260917/debug/bundle/macos/Binance Auto Trader.app`. 기존 설치본을 교체하거나 실행하지 않았다. 장시간 검증 결과는 위 상태 파일에서 별도로 확인해야 한다.
