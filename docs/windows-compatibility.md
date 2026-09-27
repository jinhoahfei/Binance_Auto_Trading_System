# Windows 호환성 — 2026-09-27

Windows x64에서 소스 개발 실행과 Python sidecar를 포함한 NSIS 설치 프로그램 빌드를 지원한다.
macOS에서 추가된 실행 프로필, 실거래 데이터 분리, 잔여 수량 복구, 연결 진단과 안전 종료 경로를
Windows에도 연결했다. 전략과 거래 수수료 계산은 양 OS에서 같은 backend/UI 소스를 사용한다.

이번 변경은 macOS에서 수정하고 검증했다. **현재 버전의 Windows 실제 앱 실행과 설치 프로그램
설치·제거는 아직 실행하지 않았다.** 과거 Windows 10 개발 앱의 검증 결과를 이번 버전이나
Windows 11 설치본의 성공 증거로 사용하지 않는다. Windows CI는 아래 회귀 검사와 설치 프로그램
빌드를 수행하도록 추가했으며 이 작업에서 원격 CI를 실행하거나 설치 프로그램을 배포하지 않았다.

## 준비와 설치

- Windows 10/11 x64, Python 3.11 이상 x64, uv, Node.js 24, pnpm 11.16.0, Git.
- Rust의 `x86_64-pc-windows-msvc` toolchain과 Clippy.
- Visual Studio Build Tools의 `Desktop development with C++` 및 Windows SDK.
- Microsoft Edge WebView2 Runtime. 설치 프로그램은 없을 때 공식 bootstrapper를 다운로드하도록 설정했다.

Native 개발 요구 사항은 [Tauri 공식 사전 준비 문서](https://v2.tauri.app/start/prerequisites/)를
참조한다. [Windows 설치 프로그램 방식](https://v2.tauri.app/distribute/windows-installer/)은
NSIS를 사용한다. Windows executable은 Windows에서 빌드해야 하며 macOS에서 교차 빌드하지 않는다.

저장소 루트의 PowerShell에서 실행한다.

```powershell
.\scripts\setup_windows_development.ps1
.\scripts\check_windows.ps1
.\scripts\start_windows_development.ps1
```

Setup은 `uv sync --locked --extra desktop`과 `pnpm install --frozen-lockfile`을 실행한다.
도구는 시스템 PATH 또는 기존 `.dev-tools`에서 찾으며 특정 PC의 Node 설치 경로를 요구하지 않는다.
환경 설정은 현재 PowerShell에만 적용한다. 이미 생성된 `backend/.venv`가 다른 OS용이라면 해당
OS에서 새 가상 환경을 준비해야 한다. macOS의 `.venv`와 `node_modules`를 Windows로 복사하지 않는다.

개발 앱은 `backend/.venv/Scripts/python.exe`로 소스를 실행한다. Python 변경 후에는 앱의 일반
종료를 완료하고 다시 시작한다. 앱이 종료되기 전에 터미널을 강제 종료하지 않는다.

## 실행 설정과 자격 증명

기본 프로필은 `TESTNET`이며 주문은 비활성이다. Windows Credential Manager의 현재 사용자
저장소를 사용한다. 값은 숨김 입력으로만 받고 명령 인자·환경 변수·설정 파일에 넣지 않는다.

```powershell
# 기존 Testnet 설정 도구
.\scripts\configure_testnet_credentials.ps1 -Action set

# Live 자격 증명을 저장하고 읽기 전용 프로필로 선택
.\scripts\configure_live_credentials.ps1 -Action set

# 값 출력 없이 자격 증명 존재·형식 확인
.\scripts\configure_live_credentials.ps1 -Action check

# Live 읽기 전용으로 전환 / Testnet으로 되돌리기
.\scripts\configure_live_credentials.ps1 -Action read-only
.\scripts\configure_live_credentials.ps1 -Action disable
```

프로필은 앱 시작 때 고정되므로 변경 전에는 앱을 정상 종료하고 변경 후 다시 실행한다.
`set`은 먼저 `TESTNET`으로 전환한 뒤 두 live credential을 저장·검증하고 `LIVE_READ_ONLY`를
선택한다. `orders` 동작은 별도의 `LIVE ORDERS 10 USDT` 입력을 요구하며 기존 macOS의
`LIVE_ORDERS_V1` 정책과 동일한 10 USDT 매수 진입 한도를 선택한다. 이번 호환성 작업에서는
자격 증명 저장소를 읽거나 변경하지 않았고 실제 주문도 실행하지 않았다.

| 구분 | Credential Manager target / 저장 위치 |
| --- | --- |
| 실행 프로필 | `com.binance-auto.trader.desktop-profile/execution-profile` |
| Testnet 키·secret | `com.binance-auto.trader.testnet/api-key`, `.../api-secret` |
| Live 키·secret | `com.binance-auto.trader.live/api-key`, `.../api-secret` |
| Testnet 데이터 | `%LOCALAPPDATA%\com.binance-auto.trader` |
| Live 데이터 | `%LOCALAPPDATA%\com.binance-auto.trader\com.binance-auto.trader.live` |

실제 데이터 경로는 환경 변수 문자열 대신 Windows Known Folder API로 결정한다.
개발 모드의 연결·실행 진단 로그는 checkout의 `Log_History`에 남고 설치본은 Tauri의 사용자별
앱 로그 경로를 사용한다. 설치된 backend의 로그는 Known Folder 기반
`%LOCALAPPDATA%\com.binance-auto.trader\logs\backend`에 저장하므로 정리된 sidecar 환경에
`USERPROFILE`이 없어도 시작할 수 있다. 기존 macOS 데이터나 Testnet 데이터를 live 경로로 자동 복사하지 않는다.

## 설치 프로그램 빌드

```powershell
. .\scripts\enter_windows_development.ps1
Set-Location UI
pnpm desktop:build
```

생성 위치는 `UI\apps\desktop\src-tauri\target\release\bundle\nsis\*-setup.exe`다.
설치본에는 `binance-auto-sidecar.exe`가 포함되므로 대상 사용자 PC에는 Python/Node/Rust가
필요하지 않다. 개발 모드는 별도 설정으로 배포 sidecar가 없어도 실행할 수 있다.

Windows packager는 lockfile의 PyInstaller 6.22.2를 사용하고 TLS와 `tzdata` 자료를 포함한다.
stdin/stdout으로 native와 통신해야 하므로 sidecar는 console 형식으로 만들고 native launcher가
콘솔 창을 숨긴다. 빌드 실패 시 기존 sidecar executable을 보존한다. NSIS는 현재 사용자에게
설치하며 Windows 코드 서명 인증서는 구성하지 않았다.

## 회귀 검사와 실제 확인 범위

`check_windows.ps1`은 credential과 주문 opt-in을 제거한 자식 환경에서 다음을 검사한다.

- 전체 backend 단위·통합 검사, Windows 프레임 IPC와 종료·소유권 복구.
- Windows 실행/빌드·credential·진단 스크립트 회귀 검사.
- 생성된 UI 계약과 결정적 이벤트 replay.
- 전체 UI 테스트, TypeScript 검사, Vite production 빌드.
- Rust native 테스트와 Clippy.

검사 단계만 보려면 `check_windows.ps1 --list`를 사용한다. macOS에서는 같은 개발 도구가
설치된 상태에서 `backend/.venv/bin/python scripts/check_platform.py`로 공통 검사를 실행할 수 있다.
기존 macOS 서명·공증·DMG 검증은 `check_all.sh`에 남겨 두었다. 이 회귀 검사는 별도의
release readiness, 실제 계좌 검증 또는 Windows UI 동작 확인을 대신하지 않는다.

`.github/workflows/windows-compatibility.yml`은 PR 또는 수동 실행에서 Windows의 위 검사와
NSIS 빌드를 수행한다. 실제 Windows PC에서는 개발 앱과 설치 앱 각각의 초기 표시, 한글 사용자
경로, 새로고침·재연결, 창 닫기 후 재실행, 최소화 후 복원, CSV 내보내기와 설치·제거를 추가로
확인해야 한다. 실제 계좌 연결이 필요한 확인은 별도 사용자의 자격 증명 설정 후 진행한다.

### 이번 작업의 로컬 검증 결과

| 검사 | 결과 |
| --- | --- |
| Backend 전체 | 1,428개 실행, 실패 0, 플랫폼/외부 연결 관련 10개 skip |
| 설치 로그 경로 추가 수정 | 관련 진단·통합 검사 27개 통과 |
| UI 전체 | 694개 통과; 로컬 socket 권한으로 실패한 2개는 권한 허용 후 재검증 통과 |
| Rust | 61개 통과, Clippy 경고 0 |
| UI 타입·production 빌드 / native soak 앱 빌드 | 통과 |
| Windows 도구 회귀 검사 | 19개 실행, 실패 0, Windows PowerShell 전용 5개 skip |
| UI 계약·결정적 replay | 통과 |
| 전체 scripts 회귀 검사 | 215개 실행, 기존 실패·오류 10개, Windows 전용 5개 skip |

전체 scripts 검사의 10건은 변경 전 HEAD를 별도 임시 경로에 추출해서도 동일하게 재현했다.
Communication traceability의 오래된 테스트 참조 1건과 기존 supply-chain inventory/SBOM/license
증거 불일치 9건이다. 이번 Windows 변경의 성공이나 release readiness로 덮어쓰지 않았다.
Windows CI의 실행 결과와 실제 설치·화면 조작 결과는 아직 없다.
