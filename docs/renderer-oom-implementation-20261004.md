# 장시간 실행 시 렌더러 메모리 부족 수정과 검증

작성일: 2026-10-04 KST. [기존 분석과 계획](renderer-oom-analysis-plan-20260927.md)의 구현 기록이다. 주문 없는 fixture와 테스트를 사용했으며 운영 매매를 실행하지 않았다.

사용자의 최종 요청에 따라 **24시간 검사는 작업 범위에서 제외한다.** 추가 운영 동작 검증은 사용자가 수행하며, 장시간 시험이나 후속 자동 점검을 예약하지 않는다.

**현재 UI는 변경하지 않는다.** 기존 레이아웃·스타일·아이콘·문구·차트 데이터 및 조작을 유지한다. **모든 신규·수정 코드는 [CODING_CONVENTIONS.md](../CODING_CONVENTIONS.md)를 반드시 준수한다.** 의미 있는 이름, 함수·변수의 snake_case, 기존 JSDoc·Rust 문서 주석과 들여쓰기를 적용하고 외부 API의 필수 이름은 보존한다. 매매 전략과 주문 판단은 변경 범위에 포함하지 않는다.

## 재현한 메모리 보관 경로

설치된 React 19.2.8 개발 빌드의 component 성능 기록이 매번 새로 전달된 차트 배열의 변경 내용을 문자열로 확장하여 User Timing 저장소에 보관한다. 1,000개 봉과 세 지표 배열을 전달하는 두 단계 React component에서 화면 요소가 늘지 않아도 메모리가 계속 증가했다. 실제 차트 엔진 없이도 재현되므로 차트 봉을 임의로 삭제하거나 라이브러리를 교체하지 않았다.

| 동일 600회 갱신 시험 | 초기 GC 후 heap | 마지막 GC 후 heap | 마지막 measure 수 |
| --- | ---: | ---: | ---: |
| React 개발 빌드, 수정 전 | 46.61 MiB | 930.00 MiB | 1,800 |
| React 개발 빌드, 선택적 정리 적용 | 46.60 MiB | 40.70 MiB | 1 |
| React 배포 빌드, 정리 없이 대조 | 45.75 MiB | 47.34 MiB | 1 |

선택적 정리 시험의 100~400회 중간 표본은 49.46~49.65 MiB였다. 남은 measure 하나와 별도의 mark는 외부 기록을 보존하는 시험용 값이다. 원래 구현에서 모든 measure를 지운 대조 시험은 49.85 MiB로 감소했다. 실제 수정은 React component·scheduler 소유가 확인된 이름만 정리하며 외부 measure나 mark를 일괄 삭제하지 않는다.

이 수치는 Node/jsdom에서 강제 GC 후 측정한 JavaScript heap이다. 과거 충돌 덤프의 약 12 GiB 렌더러 전용 commit과 동일한 지표가 아니며, 과거 덤프의 모든 메모리가 이 경로 때문이었다고 단정하지 않는다. 이 재현으로 수정할 수 있는 지속 보관 경로를 확인했고, 실제 WebView2 검증을 별도로 수행한다.

재현 코드와 결과: [probe](../artifacts/renderer-oom-implementation-20261004/react-performance-probe.mjs), [수정 전](../artifacts/renderer-oom-implementation-20261004/react-performance-retained.json), [선택적 정리](../artifacts/renderer-oom-implementation-20261004/react-performance-selective.json), [배포 빌드](../artifacts/renderer-oom-implementation-20261004/react-performance-production.json).

## 적용한 수정

1. `reactPerformanceRetention.ts`의 observer를 개발 빌드에서 첫 React render 전에 설치한다. React의 Components/Scheduler track을 확인하고 같은 이름에 외부 측정이 있으면 보존한다. pagehide와 HMR에서 구독을 해제한다. 배포 빌드에서는 설치하지 않는다.
2. Windows 진단 writer는 쓰기 권한과 명시적 seek를 사용한다. 마지막 확정 위치 뒤의 부분 기록을 복구한 후 5 MiB 단위로 분할한다. 실제 Windows 파일 기록·절단·분할과 누락 수 보존을 시험한다.
3. backend 진단 저장은 native ACK가 끝나기 전 새 IPC를 만들지 않는다. 5초 대기 제한과 실제 IPC 수명을 구분하고 늦은 성공을 중복 전송하지 않는다. 진단 큐는 기존 256개 상한을 유지하고 연속 저장 실패는 한 번만 콘솔에 출력한다. 차트 진단도 연속 오류 출력을 제한한다.
4. native·backend·WebView2 browser/renderer/GPU별 작업 집합과 전용 commit을 byte 단위로 기록한다. 환경에서 확인한 최대 64개 프로세스만 조회하며 명령행·인증 값·원본 payload를 수집하지 않는다. 종료된 PID는 정확히 알 수 없으므로 ProcessFailed 로그의 PID는 표본 시각을 가진 후보로 표시한다.
5. 최초 snapshot 적용 후 renderer 식별자가 확인된 main renderer 종료에만 기존 창을 재로드한다. native 실행당 최대 3회이고 중복 사건을 합친다. 새 renderer의 같은 backend session 및 snapshot 적용 후 heartbeat로만 복구를 확인한다. 백엔드 프로세스 생성이나 거래 명령 재전송 없이 기존 descriptor와 검증된 전체 snapshot으로 초기화한다.
6. 안전 종료 HTTP 요청 전에 native 복구를 차단한다. 종료 수락·응답 유실·미완료 준비에서는 차단을 유지하고 확정 거절 뒤에만 해제한다. native gate 응답도 최대 5초 기다리며 응답 없는 같은 IPC를 중복 생성하지 않는다.
7. 같은 backend session의 차트 주기·지표·완성된 drawing·전체화면·이력 필터·현재 페이지를 sessionStorage에 제한적으로 보관한다. 인증 token, 계좌·주문·거래 상태와 진행 중 확인 모달은 저장하지 않는다. 잘못된 값, 다른 session, 저장소 접근 실패는 bootstrap을 막지 않는다. 최대 262,144문자·총 1,000개 drawing을 넘으면 복원값을 저장하지 않는다. 이 한도는 실제 차트 데이터나 사용 중 drawing을 삭제하지 않는다.

## 검증 범위와 남은 확인

진단 IPC가 한 시간 응답하지 않는 상황, 5,000개 진단 입력, 늦은 성공·실패, 연속 오류와 누락 수를 가짜 시간으로 시험했다. 종료 gate가 응답하지 않으면 HTTP 명령이 나가지 않고 중복 native 호출도 생기지 않는 것을 확인했다. 화면 설정 복원은 최신 backend 거래 상태를 유지하고 token을 저장하지 않는 통합 시험을 포함한다.

확인한 자동 검사:

- UI 전체: 715개 통과, 기존 2개 건너뜀. `--maxWorkers=2 --testTimeout=15000`으로 실행했다. 기본 5초 제한에서는 긴 App 통합 시나리오가 동시 빌드 중 시간 초과되어 실행 제한만 늘렸으며 검증 assertion은 유지했다. [전체 결과](../artifacts/renderer-oom-implementation-20261004/ui-full-results.json).
- TypeScript `tsc -b --pretty false`와 Vite 배포 빌드 통과. 기존 500 kB chunk 경고는 남아 있다.
- Windows Rust library 테스트 57개 통과. 실제 파일 기록·5 MiB 분할·부분 tail 복구·누락 집계·프로세스 메모리 및 복구 정책을 포함한다.
- 장시간 실행 runner의 메모리 상한·표본 집계 Python 테스트 2개 통과.
- 동일 fixture·날짜로 기존 HEAD와 변경 후 App의 14개 상태 DOM 비교가 모두 일치했다. 차트 네 주기·전체화면·drawing·지표 설정·연결 복구·이력·모달 등을 비교했다. 이는 DOM과 소스 보존 검증이며 canvas 픽셀 스크린샷 비교를 대신하지 않는다. [비교 결과](../artifacts/renderer-oom-implementation-20261004/renderer-parity-comparison.json).

Windows 회귀 검증 과정에서 테스트의 경로 구분자를 정규화하고 Python fixture 출력의 UTF-8 인코딩을 명시했다. 이 두 수정은 운영 거래 코드에 영향을 주지 않는다.

실제 Windows WebView2에서는 별도 프로필·주문 없는 Python fixture로 다음 4개 장애 시험을 통과했다. 제한 환경에서는 WebView 생성이 멈춰 승인된 로컬 실행으로 검증했고, 먼저 생성한 시험 프로세스는 정리한 뒤 재실행했다. [실제 native 결과](../artifacts/renderer-oom-implementation-20261004/native-recovery-verified/validation.json).

| 조건 | 실제 ProcessFailed 횟수 | 화면 재로드 횟수 | 확인 결과 |
| --- | ---: | ---: | --- |
| 일반 renderer 종료 | 1 | 1 | 새 renderer가 같은 backend session과 실제 Python PID·생성 식별자로 복귀 |
| 반복 renderer 종료 | 4 | 3 | 네 번째 종료에서 재로드 상한 적용 |
| 안전 종료 gate 설정 | 1 | 0 | 화면 자동 복구 차단, 기존 backend 유지 |
| backend 종료 뒤 renderer 종료 | 1 | 0 | backend 재실행과 화면 재로드 모두 없음 |

네이티브 복구 시험은 기존 bootstrap·snapshot 검증·stream을 사용하며 매매 명령을 호출하지 않는다. 최초 반복 종료 시험은 네 번째 fault의 전달 대기 10초가 부족해 미완료로 기록했고, 시험 도구의 한정된 대기를 25초로 늘린 재실행에서 실제 네 번째 이벤트까지 확인했다.

실제 차트 1,000개 봉을 250ms마다 갱신한 WebView2 개발 빌드의 수정 전 대조군에서도 DOM 수는 76개로 유지됐지만 60초 뒤 성능 measure가 1,149개, JS used heap이 545.67 MiB, renderer 전용 commit이 1,065.35 MiB까지 증가했다. 메모리 안전 한도 감시로 중단했으며 OOM까지 강제하지 않았다. 당시 자원 표본 간격이 30초여서 768 MiB 중단 기준을 넘어선 뒤 관찰됐고, 검증 전용 빌드만 이후 5초 표본으로 보완했다. 운영 표본 간격은 유지한다.

배포 빌드 대조군 두 실행은 각각 약 65초 동안 measure 0개, 오류 0개로 종료했다. 개발자 도구 열기를 요청했지만 개발 대조군의 종료 시 실제 상태는 닫힘이었고, 이전 배포 대조군은 실제 열림 상태를 기록하지 않았다. 따라서 DevTools 연결·미연결의 비교를 완료했다고 해석하지 않는다. 수정 후 개발 빌드의 native 비교 실행과 픽셀 비교 쌍은 사용자 종료 요청 전에 완료되지 않았으며, 수정 효과의 수치는 앞의 Node/jsdom 대조 시험 결과다. [짧은 native 시험 요약](../artifacts/renderer-oom-implementation-20261004/memory-runs/short-matrix-summary.json).

복구 시 차트의 내부 pan/zoom 위치와 이미 가져온 과거 봉 버퍼는 다시 초기화된다. 정상 사용 중 차트 조작과 과거 조회 기능은 그대로 유지한다. 사용자 요청에 따라 24시간 검사와 추가 동작 검증을 이어서 실행하지 않는다. 절전 복귀 및 모든 화면의 픽셀 비교를 완료했다고 주장하지 않는다.
