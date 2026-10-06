# 장시간 실행 후 화면 종료: 원인 분석 및 해결 계획

작성일: 2026-09-27 KST. 이번 작업은 분석과 계획 수립이며, 애플리케이션 구현은 변경하지 않았다. 첨부 캡처는 오류 증거로만 사용했다.

후속 구현과 검증 상태는 [2026-10-04 구현 기록](renderer-oom-implementation-20261004.md)에 정리한다. 아래 내용은 분석 당시의 증거와 계획이다.

**현재 UI는 변경하지 않는다.** 레이아웃, 색상, 글꼴, 아이콘, 문구, 버튼, 모달, 차트 표시와 조작 방식을 유지한다. 연결 문제를 이유로 새 화면이나 팝업을 추가하지 않는다. 기존 정상·연결 복구 상태의 표시 계약을 보존한다.

**모든 후속 신규·수정 코드는 [CODING_CONVENTIONS.md](../CODING_CONVENTIONS.md)를 반드시 준수한다.** 의미가 명확한 이름, 클래스 `PascalCase`, 함수·변수 `snake_case`, 기존 파일과 일치하는 4칸 들여쓰기, 연산자 공백과 작업 단위 빈 줄을 유지한다. 함수 문서에는 이름·기능·인자·반환값·작성 날짜를, 클래스 문서에는 이름·기능·작성 날짜를 기록한다. TypeScript와 Rust는 기존 JSDoc·문서 주석 형식을 따르고 React·외부 API·직렬화 계약의 필수 이름을 보존한다. 자동 검사 대상 밖의 파일도 개별 검토한다.

이번 계획의 범위는 장시간 실행 시 화면이 종료되어 연결이 유실된 것처럼 보이는 문제다. 매매 전략, 주문 판단, 수량, 수수료와 거래 상태 전이 변경은 범위에 포함하지 않는다.

캡처와 충돌 덤프를 대조한 결론은 **WebView2의 화면 렌더러가 메모리 부족으로 종료되고, 그 결과 UI와 백엔드 사이 연결도 끊어진다**는 것이다. 세 차례의 실제 덤프가 같은 결과를 보여 준다. 다만 메모리를 보유한 정확한 JavaScript 객체나 네이티브 할당 경로까지 확정한 것은 아니다. 해당 부분을 특정 라이브러리나 차트의 확정된 메모리 누수로 표현하면 증거를 넘어선다.

**확인한 실행 증거**

앱 전용 `LocalAppData/com.binance-auto.trader/EBWebView/Crashpad/reports`에서 덤프 3개를 읽었다. 모두 `prod=Edge WebView2`, `ptype=renderer`, 예외 코드 `0xe0000008`이며, 실패한 할당 요청은 2,097,152바이트였다. Chromium은 이 OOM 종료 경로에서 실패한 할당 크기를 예외 인자로 기록하고 프로세스를 종료한다. [Chromium OOM 구현](https://raw.githubusercontent.com/chromium/chromium/main/base/allocator/partition_allocator/src/partition_alloc/oom.cc), [OOM 예외 코드 정의 이력](https://codereview.chromium.org/2130293003/).

| 덤프 기록 시각(KST) | 백엔드의 UI 연결 종료 시각 | 해당 연결 유지 시간 | 덤프의 커밋된 전용 메모리 |
| --- | --- | --- | --- |
| 20:12:48.983 | 20:12:49.092 | 약 78분 41초 | 12.263 GiB |
| 21:39:01.462 | 21:39:01.563 | 약 81분 55초 | 12.194 GiB |
| 22:55:32.966 | 22:55:33.062 | 약 73분 47초 | 12.251 GiB |

메모리는 덤프 `MINIDUMP_MEMORY_INFO_LIST`의 `MEM_COMMIT`이면서 `MEM_PRIVATE`인 영역 크기를 합산했다. 이는 렌더러의 커밋된 전용 메모리이며, 물리 RAM 사용량이나 JavaScript heap 크기와 동일하지 않다. 세 종료 시점의 값만으로 분당 증가율을 실측했다고 판단하지 않는다. OOM 직후 수집되는 시스템 잔여 메모리 값 역시 실제 할당 실패 순간과 다를 수 있다.

UI 연결은 이후 20:17:06, 21:41:46, 23:28:56에 새로 인증됐다. 그동안 백엔드 PID `8892`와 `run_id=9804ab59c3a84864abfe42fffa9b47e1`은 유지됐고, 분석 기준 시각인 23:42:29까지 시세 입력 로그가 이어졌다. 해당 실행에서 `stream_unavailable`과 `stream_reconciled`는 시작 직후 초기화 때 각각 1회 기록됐다. 기록상 반복된 것은 UI 연결 종료이며, 이 세 사건을 Binance 시세 연결 장애로 볼 증거는 없다.

주요 로그 근거:

- `Log_History/2026-09-27_19-47-50-025375_KST_live_8892_9804ab59c3a84864abfe42fffa9b47e1_part0003.log:9067`
- `Log_History/2026-09-27_21-11-11-955302_KST_live_8892_9804ab59c3a84864abfe42fffa9b47e1_part0006.log:9917`
- `Log_History/2026-09-27_22-30-15-896190_KST_live_8892_9804ab59c3a84864abfe42fffa9b47e1_part0009.log:9712`
- `Log_History/2026-09-27_23-23-19-835595_KST_live_8892_9804ab59c3a84864abfe42fffa9b47e1_part0011.log:2404` — 마지막 새 연결.

캡처 파일 수정 시각은 23:28:53이다. 화면 캡처 시각을 실제 충돌 시각으로 사용하지 않았다. 덤프와 서버 연결 종료가 일치하는 22:55:33이 마지막 확인된 충돌이다.

세 덤프 모두 `http://127.0.0.1:5173` 개발 화면, `devtools_present=true`, WebView2 `153.0.4234.48`이었다. 개발자 도구가 연결돼 있었다는 사실은 확인됐지만, 도구 자체가 원인이라는 뜻은 아니다. React 개발 빌드에는 기본적으로 성능 추적이 포함되므로 개발·배포 빌드와 개발자 도구 연결 여부를 분리해서 비교해야 한다. [React 공식 설명](https://react.dev/reference/dev-tools/react-performance-tracks).

원본 덤프를 복사하거나 외부로 전송하지 않았다. 필요한 필드, 덤프 해시, 로그 위치, 확인한 수치는 [분석 증거 JSON](../artifacts/renderer-oom-analysis-20260927/evidence.json)에 보존했다.

**코드에서 확인한 문제와 아직 남은 가설**

| 항목 | 근거 | 판정과 의미 |
| --- | --- | --- |
| 화면 프로세스 OOM | 실제 덤프 3개와 UI socket 종료 시각 일치 | 직접 원인 확인. 단순 재연결 타이머 변경으로 메모리 부족을 해결할 수 없다. |
| Windows 진단 로그 저장 실패 | `UI/apps/desktop/src-tauri/src/chart_diagnostics.rs:130`의 append 전용 파일에 `set_len` 호출 | 별도 결함 재현. 현 실행의 chart·backend_connection·runtime_health 로그 모두 0바이트여서 원인 추적을 방해한다. 이것이 12 GiB의 직접 원인이라는 증거는 없다. |
| Windows 메모리 관측 누락 | `UI/apps/desktop/src-tauri/src/runtime_diagnostics.rs:523`의 자원 측정은 Unix에서만 수행 | Windows는 `unavailable`. 기존 native PID 수치만으로 WebView2 렌더러 메모리를 판단할 수 없다. |
| 차트 데이터에 최종 보관 상한이 없음 | `useRealtimeChartData.ts:190`, `:210`, `:464`, `:727` | 새 봉·과거 페이지·재동기화 데이터를 계속 보존한다. 최초 조회 `limit=1000`은 전체 보관 상한이 아니다. 장기 증가 경로는 확실하나, 새 봉만 약 80분 쌓인 양으로 12 GiB를 설명할 수는 없다. |
| 매번 전체 차트 표시 데이터 재계산 | `UI/src/app/App.tsx:55`, `:62`, `dashboardPresenter.ts:260`, `createRealtimeChartViewModel.ts:105` | 선택 주기의 봉이 바뀌지 않은 UI 갱신에서도 지표·배열을 다시 만들 수 있다. 할당·CPU 부담 후보이며, 할당된 객체가 실제로 남는지는 추가 측정이 필요하다. |
| 렌더러 종료 후 복구 경계 | 소스에서 WebView2 `ProcessFailed` 처리 없음 | 화면 JavaScript 자체가 종료되면 기존 JS 재연결 코드는 실행될 수 없다. native에서 종료 사실을 관측하는 경계가 필요하다. |

Windows 파일 결함은 원래 코드와 같은 `OpenOptions::new().create_new(true).append(true)`로 임시 파일을 연 뒤 `set_len(0)`을 호출해 확인했다. 결과는 `PermissionDenied / OS error 5`였고, 같은 임시 파일을 write 권한으로 열면 `set_len(0)`이 성공했다. 실제 운영 로그는 수정하지 않았다. 해결 구현에서는 `.append(true).write(true)`만 추가하면 해결된다고 가정하지 말고 Windows 접근 권한과 seek·절단·기록 순서를 검증해야 한다.

무제한 누수로 확인되지 않은 경로도 있다. `UIStateController`의 이벤트 큐는 `drain()`에서 제거되고, backend event ID는 10,000개, 진단 대기 큐는 256개로 제한된다. 차트의 구독 해제, 타이머 해제, `chart.remove()` 처리도 존재한다. 따라서 단순히 `push`, `setInterval` 또는 WebSocket이 있다는 이유로 원인으로 지목하지 않는다.

실제 `UIStateController`와 mapper에 가짜 명령 adapter를 연결해 새 전체 snapshot을 5,001회 전달하는 격리 진단을 수행했다. GC 후 heap은 체크포인트에서 27.43 → 28.05 → 27.99 → 27.96 → 26.51 → 25.85 MiB였고 지연 이벤트는 계속 0개였다. 이 경로에서는 지속 증가를 재현하지 못했다. Node 측정으로 React DOM, 차트, WebView2 또는 native IPC 안정성을 증명한 것은 아니다.

**해결 순서와 완료 기준**

1. **원인 관측을 먼저 복구한다.** Windows 진단 writer를 플랫폼에 맞게 수정하고 실제 저장 성공 여부를 확인한다. 기존 5 MiB 분할, 부분 기록 복구, 단일 writer와 256개 대기 상한을 유지한다. 디스크 실패 시 진단 자체의 재시도·콘솔 출력·미완료 IPC가 추가로 누적되지 않도록 제한과 누락 수 집계를 검증한다. `ProcessFailed`에서 렌더러 PID, 오류 종류·원인, 시각, WebView2 버전만 수집하고 원본 인증정보·payload는 기록하지 않는다.

2. **동일 입력으로 증가 경로를 분리한다.** 주문 없는 로컬 fixture에서 개발 빌드/배포 빌드와 개발자 도구 연결/미연결의 4개 조합을 비교한다. 기존 충돌이 약 74~82분이었으므로 우선 각 조합을 최소 2시간 확인한다. renderer·browser·GPU·backend의 전용 메모리와 작업 집합을 구분하고, JS heap, DOM·listener, 차트 봉 수, 성능 추적 entry 수, IPC 미완료 수, 진단 queue, socket·timer 수를 함께 측정한다. heap snapshot은 초기·30분·60분 등 제한된 시점에 수집하고, 측정 도구가 연결되지 않은 비교군도 남긴다. heap이 평탄한데 프로세스 메모리만 늘면 네이티브 할당·그래픽·추적 버퍼 쪽으로 조사를 전환한다. [Microsoft 메모리 진단](https://learn.microsoft.com/en-us/microsoft-edge/devtools/memory-problems/).

3. **측정에서 확인된 경로만 수정한다.** 차트 경로가 원인이면 선택 주기 데이터의 참조·revision을 기준으로 표시 모델을 재사용하고, 실제 변경 구간만 계산·발행한다. 보관량이 원인이면 측정에 근거한 주기별 메모리 예산과 과거 페이지 퇴거·재조회 정책을 둔다. 현재 보이는 봉, pan/zoom 위치, 과거 조회 기능과 drawing을 보존하고 EMA의 이전 계산값·볼린저 warmup을 검증한다. `slice(-1000)`로 기존 과거 조회를 잘라 UI 동작을 바꾸지 않는다. 개발 도구나 런타임 경로가 원인이면 재현 가능한 최소 사례와 버전 비교를 확보한 뒤 실행 설정 또는 검증된 의존성 수정으로 범위를 좁힌다. 근거 없이 라이브러리를 일괄 교체하지 않는다.

4. **렌더러 종료 뒤 복구를 native 경계에서 연결한다.** 메모리 증가 원인 수정 후에도 비정상 renderer 종료에 대비한다. 종료 이벤트는 중복 처리하지 않고, 기존 backend session이 살아 있는 경우 현재 descriptor → 전체 snapshot 검증 → 단일 UI stream 순서로 기존 bootstrap을 다시 사용한다. renderer 복구가 매매 시작·중지·청산 명령이나 backend 중복 실행을 발생시키지 않도록 한다. 거래 명령을 자동 재전송하지 않고 서버 상태로 대조한다. 종료 진행·snapshot 불일치·반복 충돌에서는 재시도 상한과 기존 복구 경로를 사용한다. 차트 선택·drawing·필터 등 사용자 화면 상태의 복구 범위를 별도로 검증하고 인증 token은 저장하지 않는다. 정기적인 강제 새로고침을 근본 해결로 사용하지 않는다. WebView2는 renderer 종료 시 오류 페이지로 바뀌며, 호스트에서 `Reload` 또는 WebView 재생성을 처리하도록 설명한다. [Microsoft 프로세스 복구 문서](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/process-related-events).

5. **장시간·장애·화면 보존 검증으로 완료를 판정한다.** 원인 수정 전후에 동일한 입력과 도구 조건으로 먼저 증가율을 비교한다. 이후 실제 Windows WebView2에서 24시간 연속 수신, 최소화·복원·절전 복귀, 과거 봉 탐색, 네트워크 차단·복구와 반복 renderer 재연결을 검증한다. 합격 조건은 OOM 0회, 정상 warmup 이후 retained heap·전용 메모리의 지속 증가 부재, 정상 종료 후 socket·timer·IPC 잔존 부재, 기존 수신 주기에 맞는 화면 최신성, 같은 backend session과 거래 상태 유지다. 메모리 예산은 정상 비교군으로 정하고 12 GiB에 도달하지 않았다는 이유만으로 합격시키지 않는다.

6. **UI 및 컨벤션 조건을 독립적으로 확인한다.** 같은 fixture·시각·viewport에서 기존 화면과 수정 후 화면을 비교한다. 대시보드, 연결 복구 표시, 차트 네 주기와 확대·축소·drawing·전체화면, 거래 이력, 기존 모달을 포함한다. 같은 입력에 대한 표시값·문구·사용자 조작은 같아야 한다. 관련 UI 회귀, `pnpm typecheck`, `pnpm build`, Windows 진단 writer·native 복구 테스트를 실행하고 transport 계약을 수정한 경우 관련 backend 회귀도 수행한다. 변경 파일마다 컨벤션을 검토하고 `git diff --check`를 통과시킨다.

이번 분석에서 운영 앱을 재시작하거나 매매 명령을 실행하지 않았다. 생산 코드 수정, 24시간 시험, UI 변경 전후 비교는 아직 수행하지 않았으며 후속 구현의 완료 조건이다. 즉시 확정된 것은 반복되는 renderer OOM과 Windows 진단 저장 결함이고, 대량 메모리를 붙잡는 정확한 경로는 위 비교 측정으로 최종 확정해야 한다.
