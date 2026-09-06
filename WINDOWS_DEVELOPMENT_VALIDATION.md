# Windows 개발 실행 검증 — 2026-09-07

## 판정과 범위

이 PC에서 source 개발 앱의 실행, Testnet read-only READY, Dashboard/History,
native 폴더 선택·취소, 정상 종료와 fresh restart를 확인했다.
Session 5 source에 남아 있던 Vite 감시 경로와 Tauri capability 누락을 수정했다.

실제 OS는 **Windows 10 Enterprise 22H2, build 19045, x64**다.
Windows 11 x64 native PASS로 해석하지 않는다. NSIS/PyInstaller 배포본, 설치·재설치,
macOS 재실행, 장시간 soak, 전원 차단 durability, Private Beta와 live readiness는 이번 결과에 포함하지 않는다.
2026-09-07 사용자 지시에 따라 **Windows 10 x64 개발 실행 통과를 Session 6 완료 조건으로 적용**했다.
확보한 검증 결과로 Session 6은 완료 처리한다. Windows 11 검증과 설치 배포는 별도 후속 작업이며,
Cross-platform package master, Private Beta와 live readiness는 완료로 체크하지 않는다.

시작 HEAD: `c76023e932123c23ca45f1ddca04434d139ff091`.
초기 working tree는 clean이었으며 이 작업에서 commit은 만들지 않았다.
실제 신규 주문·주문 취소·청산·live signed API 호출은 0회다.
앱의 `allow_testnet_orders=false`와 exact 개발 Origin `http://127.0.0.1:5173`을 유지했다.
시세는 기존 공개 mainnet 데이터, 인증 계좌 조회는 Spot Testnet을 사용했다.

## 이 PC의 실행 방법

저장소 루트에서 다음 명령으로 시작한다.

```powershell
powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\start_windows_development.ps1
```

이 스크립트는 checkout의 도구 경로를 현재 PowerShell에 설정하고 `UI`의
`pnpm desktop:dev`를 실행한다. 앱에서 창 닫기 → 종료를 완료한 뒤 터미널을 닫는다.
전역 PATH와 영구 PowerShell 실행 정책은 변경하지 않았다.

개발 명령을 직접 실행할 PowerShell 세션에서는 아래 환경 스크립트를 dot-source한다.
로컬 script 실행을 허용하는 현재 세션에서 사용한다.

```powershell
. .\scripts\enter_windows_development.ps1
cd UI
pnpm desktop:dev
```

설치된 구성은 다음과 같다. `.dev-tools`와 venv는 Git에 포함되지 않는 이 PC의 로컬 환경이다.
다른 checkout에서는 별도로 도구와 의존성을 준비해야 한다.

| 구성 | 확인한 버전·위치 |
|---|---|
| Python | 3.12.14 x64, `backend/.venv`; base interpreter는 이 PC의 Codex bundled Python |
| uv | 0.12.10, `.dev-tools/python-tools/bin` |
| Backend 의존성 | `uv sync --locked`; editable backend, websocket-client 1.9.0, tzdata 2026.2 |
| Node.js | 24.19.0, `.dev-tools/node-v24.19.0-win-x64`; 공식 배포 SHA-256 검증 |
| pnpm | 11.16.0, `.dev-tools/pnpm`; frozen lockfile 설치 및 lockfile 정책 검증 |
| Rust | 1.98.1, `x86_64-pc-windows-msvc`; `.dev-tools/cargo`, `.dev-tools/rustup` |
| MSVC | Visual Studio 2022 Build Tools 17.14.39, VCTools workload와 Windows SDK |
| WebView2 | 기존 설치 Runtime 사용 |

## 실제 실행에서 수정한 문제

1. **Windows Vite EBUSY:** Vite가 Rust `target`의 점유된 실행 파일까지 감시해 개발 서버가 종료됐다.
   `UI/vite.config.ts`에서 Tauri source/build 디렉터리를 Vite 감시에서 제외했다.
   Rust 변경 감시는 기존 Tauri CLI가 계속 담당한다.
2. **Windows renderer 권한 누락:** `capabilities/main-window.json`의 platform이 macOS만 허용했다.
   Backend READY 후 event listen과 descriptor IPC가 거부되어 `LIVE_BOOTSTRAP_FAILED`가 표시됐다.
   기존 main 창의 최소 권한을 Windows에도 적용하고, 설정 회귀 검사에 Windows 적용 여부와
   main 창·remote 제한을 추가했다. Origin, CSP, 명령 권한 목록은 확대하지 않았다.
3. **Windows에서 실행되지 않던 테스트:** venv redirector PID와 실제 runtime PID의 차이,
   strict JSONL fixture의 CRLF 변환, tempfile 경로, FlushFileBuffers/fsync 경계,
   CRT broken pipe errno, asyncio local socketpair와 WebSocket close-frame 경합을 수정했다.
   테스트 fixture만 자기 subprocess tree를 회수하며 production orphan 정책은 바꾸지 않았다.
4. **도구 import:** `scripts/phase13_soak.py`의 POSIX `resource` import를 플랫폼별로 제한했다.
   Windows에서 관찰하지 못한 RSS는 `None`으로 남긴다. POSIX shell·FD·macOS packaging 전용
   테스트는 이유를 명시해 해당 플랫폼에서만 실행한다. 일반 모듈 import 실패를 skip으로 숨기지 않았다.
5. **실제 picker 검증 도구:** 기존 macOS용 별도 native picker smoke를 Windows에서도 compile하도록
   확장했다. 검증 도구의 조작 대기만 180초로 설정했으며 production timeout은 바꾸지 않았다.

## 자격 증명과 Mac 이력

Credential Manager의 별도 canary set/read/delete를 실제 실행했다. 사용자가 hidden-input 도구로
Testnet 키를 등록한 뒤 실제 앱에서 읽었다. 키를 argv, 환경변수, renderer 또는 파일에 저장하지 않았다.

최초 실행은 로컬 이력이 없어 기존 app-owned 체결을 설명하지 못하는
`ORDER_RECONCILIATION_FAILED`로 차단됐다. 사용자가 제공한
`C:\Users\OSCARMIKE\Desktop\mac_Trade_History`의 두 파일을 workspace에 먼저 복사하고,
production repository parser로 **trade 16건, active pending 0건**을 검증했다.
LF/JSON과 parser 전후 SHA-256 불변을 확인한 뒤 다음 위치로 원본 그대로 복사했다.

```text
C:\Users\OSCARMIKE\AppData\Local\com.binance-auto.trader\history.jsonl
C:\Users\OSCARMIKE\AppData\Local\com.binance-auto.trader\history.jsonl.pending-orders.jsonl
```

Mac 원본은 수정하지 않았고 Mac runtime lock은 가져오지 않았다.
실제 앱 startup의 계좌 대조를 통과했으며 최종 종료 후에도 두 파일은 Mac 원본과 byte 단위로 동일했다.

## 실행 증거

아래 로그는 Git에서 제외되는 `.testnet-artifacts/windows-native-20260907/`에 보관했다.

| 검증 | 결과 | 로그 |
|---|---|---|
| Backend 전체 | 1,046 실행, **979 PASS / 67 skip**, 실패 0; 거래 opt-in 0, `PYTHONWARNINGS=error` | `backend-final.log` |
| UI 전체 | **480 PASS / 2 skip**, 46 files PASS / 1 skip; `--maxWorkers=2` | `ui-final.log` |
| UI typecheck/build | TypeScript와 Vite build PASS | `ui-build.log` |
| Rust native | lib 35 + picker 2 = **37 PASS**, `cargo fmt --all --check` PASS | `rust-final.log`, `rust-native.log` |
| Rust lint | `cargo clippy --lib --locked -- -D warnings` PASS | `rust-clippy.log` |
| 실행·설정 도구 | 38 실행, **20 PASS / 18 platform skip** | `tools-final.log` |
| Communication | **126/126 complete, gap 0** | validator 실제 실행 |
| Dashboard | READY, API·market stream·account stream 모두 online, 공개 시장 swing 대조, tooltip 닫기·재열기 PASS | `desktop-smoke-windows-capability.log` |
| History/정상 종료 | 실제 History 전체 16행, Dashboard 복귀, OS close 취소, 다시 close→확인, shutdown 202, process code 0 | 같은 로그의 `WINDOWS_*` 단계 |
| Native picker | 취소 `null`, 선택 absolute UTF-8 PASS; 선택 확정은 사용자 조작 | `picker-cancelled-v2.stdout.log`, `picker-selected.stdout.log` |
| Fresh restart/복구 | Windows PowerShell 명령으로 새 실행, 연결 재시도, 같은 backend의 renderer reload, 안전 종료 202와 process code 0 | `desktop-fresh-recovery-reload-v2.log` |
| 최종 정리 | owner `RELEASED`, runtime 부재, project process 0, Vite listener 0, history 불변 | `postflight.json` |

Backend 실제 framed subprocess 검증은 READY, early ACK 거부, CLOSED_ACK 후 RELEASED,
parent EOF·malformed control의 ORPHANED와 listener/BUY gate 보존을 포함한다.
이 결과는 테스트가 소유한 fixture process에서 얻었다. 실제 계좌 앱을 일부러 orphan으로 만드는
시나리오를 동일한 PASS로 주장하지 않는다.

History/정상 종료를 검사한 임시 TypeScript fixture는 `.dev-tools/windows-session-lifecycle.ts`에 남겼다.
실제 WebView의 버튼과 backend 응답을 사용했고 account payload를 기록하지 않았다.
검증 뒤 `desktopSmoke.ts`의 임시 import를 제거했다. 최종 fresh restart는 이 임시 fixture 없이 수행했다.

## 실패 기록과 해석의 한계

- 최초 Vite EBUSY, 이력 부재, Windows capability 누락 실행은 PASS에 포함하지 않았다.
  Capability 오류 당시 종료 IPC도 막혀 해당 검증 앱의 프로세스 tree만 회수했다.
  남은 ACTIVE 소유권은 앱의 정식 native 확인 화면을 거쳐 복구됐다.
- 첫 fresh restart는 READY smoke 없이 종료 코드 0과 RELEASED를 남겼다. 종료 코드를 성공으로
  간주하지 않았으며 원인을 확정하지 못했다. 별도 read-only 진단 READY 후 동일 실행 명령으로
  재검증해 `recovery-retry-passed`, `renderer-reload-passed`, `recovery-shutdown-accepted`를 확보했다.
  따라서 모든 시도의 안정적 시작이나 장시간 안정성을 보증하는 결과는 아니다.
- 첫 native picker 취소 시도는 조작 timeout으로 실패했다. Windows 10의 capture API가
  `SetIsBorderRequired / E_NOINTERFACE`를 반환해 스크린샷·좌표 클릭 검증을 할 수 없었다.
  이후 접근성 정보·키보드와 사용자 선택으로 실제 반환값을 검증했다. 픽셀 단위 시각 검증 PASS가 아니다.
- Sandbox의 ancestor handle, Credential Manager, loopback/network 제한으로 실패한 실행은
  실제 Windows API 사용을 허용받아 재실행했다. 최초 실패 로그를 보존했다.
- `backend/uv.lock`, `UI/pnpm-lock.yaml`, `Cargo.lock`은 Git 기준 변경하지 않았다.
  Windows checkout의 CRLF 때문에 과거 macOS 파일의 raw SHA-256과 직접 비교하지 않는다.
- 이 작업의 변경 tree를 macOS에서 다시 실행하지 않았다. 과거 macOS 결과를 현재 tree의 회귀
  통과 증거로 재사용하지 않는다. Windows 11과 배포 검증은 별도 작업으로 남는다.
