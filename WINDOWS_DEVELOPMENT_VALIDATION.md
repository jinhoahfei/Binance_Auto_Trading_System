# Windows 개발 실행 검증 — 2026-09-07 / 2026-09-08

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

## 2026-09-08 현재 HEAD 재검증

위 9월 7일 기록을 보존하고, 사용자 요청에 따라 Session 6을 현재 Windows 10 Enterprise
`10.0.19045` x64에서 다시 수행했다. 시작 HEAD는
`e726793b6ebe5018f1491a36fa081a97ed2c8e4f`, 초기 tree는 clean이었다. 새 commit과 production
수정은 없으며 함수·클래스·업무 책임·lockfile도 변경하지 않았다. §1, 코딩 규칙과 Communication
startup/stop, ADR-003/005를 다시 확인했다. 로그 위치는
`.testnet-artifacts/windows-native-20260908/`다.

기존 source venv와 local toolchain을 그대로 사용했다. Python `3.12.14`, Node `24.19.0`,
pnpm `11.16.0`, Rust/Cargo `1.98.1`이며 MSVC와 WebView2의 실제 compile/run을 통과했다.
새 설치·NSIS/PyInstaller packaging은 수행하지 않았다.

| 검증 | 결과 | 로그 |
|---|---|---|
| Backend 전체 | `python -m unittest discover -s tests -q`, credential/baseline/cap 환경 제거·`PYTHONWARNINGS=error`; 1,046 실행, 979 PASS/67 skip, 107.694초 | `backend.log` |
| Windows platform/framed child | `python -m unittest tests.unit.platform.test_windows_platform tests.integration.transport.test_framed_sidecar_process -v`; 16 실행, 15 PASS/1 POSIX skip, 9.578초 | `windows-framing.log` |
| UI 전체 최종 | `pnpm.cmd test --maxWorkers=1`; 46 files/480 PASS, 1 file/2 tests skip, 148.62초 | `ui-serial.log` |
| UI typecheck/build | `pnpm.cmd build`; TypeScript/Vite PASS | `ui-build.log` |
| Rust | `cargo test --locked --offline` 35 PASS; picker feature의 binary test 2 PASS; clippy `-D warnings`와 fmt PASS | `rust.log`, `rust-picker.log`, `rust-clippy.log`, `rust-fmt.log` |
| 도구/Communication | Windows development/package/trace 도구 38 실행, 20 PASS/18 platform skip; Communication 126/126, gap 0 | `tools.log`, `communication.log` |
| Credential | 기존 PowerShell 도구의 canary set/read/delete와 실제 pair check PASS; `validate_secret`의 공개 입력 경계 9/9 PASS | `credential-canary.log`, `credential-check.log`, `credential-boundaries.log` |
| 실제 앱 | Testnet read-only READY, 모든 연결 online, 공개 스윙 대조, tooltip, History 16행, picker 취소/선택, 정상 종료 취소/확정, process code 0 | `desktop-smoke.log` |
| 정상 종료 후 | owner RELEASED, runtime PID 2288 부재, Vite listener 0 | `normal-shutdown-postflight.json` |
| Fresh restart/renderer 복구 | 연결 재시도, 같은 backend의 renderer reload, 안전 종료 HTTP 202, process code 0 | `desktop-fresh-recovery.log` |
| 최종 정리 | owner RELEASED, runtime PID 4784 부재, 앱/개발 서버/검사 process 0, Vite listener 0, history/pending 불변 | `postflight.json` |

Rust picker test 명령은 `cargo test --bin native-picker-current-host-smoke --features
native-picker-smoke --locked --offline`다. Clippy는 `cargo clippy --lib --locked --offline --
-D warnings`, format은 `cargo fmt --all --check`다. 도구 명령은
`python -m unittest scripts.test_windows_development scripts.test_package_sidecar
scripts.test_check_communication_traceability -q`와
`python scripts/check_communication_traceability.py`다. 줄바꿈한 명령은 한 줄로 이어 실행한다.

### 실제 앱 검증 방법과 보존 경계

Root의 `. ./scripts/enter_windows_development.ps1`로 도구를 선택한 뒤 `UI`에서
`$env:BINANCE_DESKTOP_SMOKE='1'; pnpm.cmd desktop:dev`를 실행했다. 초기에는 WebView 내부 요소가
접근성 API에 노출되지 않아 `.dev-tools/windows-session6-revalidation.ts`를 기존 smoke entry에
일시 import했다. Helper는 실제 DOM으로 History 16행과 Dashboard 복귀를 검사하고, 실제 main 창의
`choose_csv_export_directory` production command를 호출해 native 반환값을 확인했다. 폴더 대화상자는
Computer Use 키보드로 취소·선택했다. OS `Alt+F4` 두 번에 대응한 실제 종료 dialog에서 helper가 첫
요청을 취소하고 두 번째를 확정했다. 원본 HTTP 요청·응답을 변경하지 않았으며 CSV 파일은 생성하지 않았다.

Helper는 업무 코드 밖에 격리하고 함수·블록·문장 설명을 작성했다. 이력 payload, credential,
descriptor나 token을 보관하지 않고 고정 단계만 기록했다. 검증 후 임시 import를 제거하고 해당
`UI/src/test/desktopSmoke.ts`를 HEAD의 원래 CRLF bytes로 복원했다. 최종 source diff는 없다.
첫 정상 종료의 HTTP 202 관찰 marker는 로그에 남지 않았으므로 이를 별도 PASS로 세지 않았다.
이 실행의 완료 근거는 종료 확정 marker·process code 0·RELEASED·runtime 부재다.

이어 임시 helper가 없는 상태에서 `$env:BINANCE_DESKTOP_SMOKE='recovery-reload'; pnpm.cmd
desktop:dev`로 새 runtime을 시작했다. 기존 개발 fixture가 descriptor 실패를 주입하고 실제 native
재연결, 같은 backend의 화면 재로딩, 안전 종료를 검사했다. 이 실행에는 HTTP 202를 뜻하는
`recovery-shutdown-accepted`와 process code 0이 모두 기록됐다.

두 이력 파일은 시작/종료 후 아래 raw SHA-256을 유지했다. Mac 원본이나 Credential Manager pair를
수정하지 않았다. 주문 opt-in을 켜지 않았고 native `allow_testnet_orders=false`를 유지해 실제 주문·
취소·청산·live signed/order 호출은 0회다. 공개 mainnet 시세와 기존 Spot Testnet read-only adapter만
사용했으며 Binance API 구현 변경은 없다.

| 파일 | Windows raw SHA-256 |
|---|---|
| `history.jsonl` | `ed436eecc1e0e44037248d625ee37ce0d9ff857c6ddad524a616e9e887ba16b2` |
| `history.jsonl.pending-orders.jsonl` | `d46cfc86774143f606e06b4770b27b53c8b4abb395c04c69001870a9afcffed7` |

### 이번 재검증의 실패 기록과 판정

- 첫 UI 전체 검사 `--maxWorkers=2`는 다른 native 검사/빌드와 동시에 실행했고, 477 PASS/3 실패/2
  skip이었다. `App`, `main` 복구, `realtime-indicator` axe 검사 각각이 기존 5초 timeout을 초과했다.
  `ui.log`를 보존했다. 다른 검사를 끝내고 worker 1개로 단독 재실행한 전체 suite는 통과했다.
  코드·test timeout을 바꾸지 않았으며 부하를 원인으로 확정하거나 모든 병렬 실행의 안정성을 주장하지 않는다.
- Native UI 조작 도구는 cached element 부재, 좌표 geometry 부재, UIA CacheRequest 오류를 반환했다.
  입력 실패 뒤 창을 재관찰하고 키보드로 실제 반환값을 확인했다. 픽셀 시각 검사는 수행하지 않았다.
- 첫 전체 process 집계는 CWD가 같은 Codex CUA helper 2개까지 포함해 실패했다. Executable path가
  Codex의 `runtimes/cua_node/`임을 확인해 앱 process와 분리했다. 최초 결과는
  `postflight-initial-including-cua.json`, 최종 결과는 `postflight.json`에 남겼다. 앱이나 helper를
  강제 종료하지 않았다.
- 처음 sandbox 안의 OS CIM 조회는 access denied였으며, 사전 승인 범위의 native 조회에서 실제
  OS를 확인했다. Native build의 `linker_messages` warning은 MSVC의 `.lib/.exp` 생성 안내였고
  clippy `-D warnings`와 compile/run은 성공했다.
- Credential printable 경계는 공개 입력으로 검증했다. Native `finally`의 buffer 덮어쓰기와
  secret-free IPC/log 계약은 source·기존 회귀로 확인했으며 process memory 전수 zeroization이나
  실제 credential bytes에 대한 전체 artifact scan을 수행했다고 주장하지 않는다.
- Windows fixture의 EOF/orphan·lock/flush 검증과 실제 계좌 앱의 정상 lifecycle을 구분한다.
  강제 전원 차단 durability, 장시간 soak, Windows 11, 현재 HEAD의 macOS 회귀와 package 배포는
  미실행이다. Backend의 기존 supply/soak readiness GAP도 그대로 남는다.

**판정:** Session 6 `[x]` 유지, **Session 7 구현 착수 `GO`**다. 현재 HEAD에서 Windows 개발 실행,
필수 회귀와 read-only lifecycle·종료·재시작·이력 보존을 확인했고 새 production 변경이나 Session 6
코드 blocker가 없다. Session 7 완료에는 별도 live composition root/capability와 격리 검증,
Windows package 후속 작업·macOS 회귀 및 양 OS package의 승인된 signed live read-only READY가
필요하다. Live credential/signed preflight는 Session 7의 별도 승인을 따르고 실제 live 주문은
Session 8 gate에 둔다. Cross-platform package/Private Beta/Phase 13/live는 미완료를 유지한다.
