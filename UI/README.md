# Binance Auto Trader UI

Figma의 1440×1024 데스크톱 화면을 TypeScript, React, Vite와 XState 기반으로 구현한 UI 패키지입니다. production entry는 Tauri에서 받은 loopback descriptor로 backend 전체 snapshot을 먼저 읽은 뒤에만 화면 actor를 시작합니다. 새로고침한 메인 화면도 현재 backend session의 descriptor를 다시 받아 연결합니다. 계좌와 REGIME 추천은 backend의 ETHUSDT/USDT 값을 표시하며, snapshot 준비·schema·session 검증이 실패하면 demo로 fallback하지 않습니다.

데스크톱의 차트와 REGIME 계산은 Binance 실제 시장의 공개 REST 봉과 WebSocket 시세를 사용합니다. 계좌 조회와 계좌 stream은 Testnet을 유지하고 주문 전송은 비활성입니다. 상단에 `시세·REGIME 실제 시장`, `계좌 Testnet · 주문 비활성`을 항상 표시합니다. 숫자로 표시하던 Swing Low/High는 백엔드의 확정 HL/LL·HH/LH 판정으로 표시하며 0.30% 기준 미달은 `-`입니다.

좌상단 연결됨/연결 끊김에 마우스를 올리거나 키보드로 포커스하면 Binance 연결 상태 툴팁이 열립니다. 백엔드의 인증 API 조회 결과와 시세·계좌 WebSocket 연결 여부를 각각 표시하며, `/v1/binance/connection-status`를 통해 응답 완료 후 5초마다 갱신합니다. 커서가 표시 영역을 벗어나거나 포커스를 잃거나 Escape를 누르면 즉시 닫히고 UI 조회와 타이머가 정리됩니다. 진단용 API 응답은 연결 확인에만 사용하며 계좌 상태에는 적용하지 않습니다.

시장 snapshot은 실시간 시세마다 갱신되고 REGIME 지표는 마지막 4시간봉 평가 시점의 version과 가격을 보존합니다. 시작 및 재연결 검증은 이 정상적인 version 차이를 허용하며, 지표가 미래의 시장 version을 참조하거나 동일 version의 가격이 다르면 응답을 거부합니다.

부트스트랩 복구 화면의 안전 종료는 전체 대시보드 snapshot을 재조회하지 않습니다. `/v1/shutdown/state`에서 현재 backend session·거래 version·lifecycle만 확인한 뒤 기존 `/v1/shutdown`을 호출합니다. 백엔드는 열린 포지션·미체결 주문·재조정 상태를 다시 검사하며, HTTP 202와 native 정상 종료 코드 0을 확인한 뒤 창을 닫습니다. 응답이 유실되면 같은 요청을 재시도하고, 202 이후에는 프로세스 종료 확인만 재시도합니다.

연결 정보를 받지 못한 복구 화면에서도 `연결 다시 확인`과 `안전 종료`는 native 연결을 다시 요청합니다. HMR로 entry가 재평가되면 React root를 중복 생성하지 않고 전체 화면을 다시 불러옵니다. token은 browser 저장소에 넣지 않으며, native 원본도 backend 종료 시 제거합니다.

## 실행

데스크톱 앱은 `UI` 디렉터리에서 `pnpm desktop:dev`로 실행합니다. 종료할 때는 앱의 창 닫기 → 일반 종료를 먼저 완료합니다. 백엔드 정상 종료 후 소유권 기록이 `RELEASED`로 바뀌어 다음 실행이 가능합니다. 터미널에서 먼저 실행을 끊어 백엔드만 남으면 `ORPHANED` 소유권 보호로 새 실행이 차단됩니다.

만료 소유권의 `확인 후 해제·계속`은 동일 앱 프로세스에서 시작을 계속하므로 개발 서버가 함께 종료되지 않습니다. 개발 서버가 꺼져 있으면 backend와 흰 창을 먼저 만들지 않고 native `다시 시도`·`종료` 안내를 표시합니다.

### Windows 11 x64 개발 실행

2026-09-07 현재 이 PC에는 개발 환경이 준비되어 있습니다. 실제 검증 OS는 **Windows 10
Enterprise 22H2 x64**이며 Windows 11에서 실행한 결과는 아닙니다.
저장소 루트에서 다음 명령으로 로컬 도구 경로와 venv를 적용해 시작합니다.

```powershell
powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\start_windows_development.ps1
```

이 스크립트는 이 checkout의 `.dev-tools`와 `backend/.venv`를 사용합니다.
일반 개발 PowerShell에는 `scripts/enter_windows_development.ps1`을 dot-source할 수 있습니다.
현재 shell에만 경로를 설정하며 전역 PATH와 영구 실행 정책은 바꾸지 않습니다.
설치 구성·실제 READY/종료 결과·실패 기록은 [Windows 개발 검증 보고서](../WINDOWS_DEVELOPMENT_VALIDATION.md)에 있습니다.

Windows에서도 `UI`에서 **`pnpm desktop:dev`**를 사용합니다. Tauri debug 앱과 Vite를 실행하고
`backend/.venv/Scripts/python.exe`가 현재 Python 소스를 직접 실행합니다. Python 변경 후에는 앱을
정상 종료하고 다시 실행합니다. PyInstaller, sidecar 배포 `.exe`, NSIS 설치 프로그램은 필요하지 않습니다.

먼저 Visual Studio Build Tools의 **Desktop development with C++**와 Windows SDK,
Rust stable `x86_64-pc-windows-msvc`, WebView2 Runtime, Node.js 및 이 프로젝트의
`pnpm@11.16.0`, Python **x64 3.11 이상**, `uv`를 준비합니다. Rust toolchain의 host가
`x86_64-pc-windows-msvc`인지 `rustc -vV`로 확인합니다. Windows ARM64와 32-bit Python은 지원하지 않습니다.
Tauri의 설치 항목은 [공식 prerequisites](https://v2.tauri.app/start/prerequisites/)를 따릅니다.

저장소 루트의 PowerShell에서 최초 한 번 실행합니다.

```powershell
cd backend
uv sync --locked
cd ..
powershell -NoProfile -File scripts/configure_testnet_credentials.ps1 -Action canary
powershell -NoProfile -File scripts/configure_testnet_credentials.ps1 -Action set
cd UI
pnpm install --frozen-lockfile
pnpm desktop:dev
```

`set`은 키와 secret을 숨김 입력으로 받아 현재 Windows 사용자의 Credential Manager에 저장합니다.
PowerShell 실행 정책이 로컬 script를 차단하면 조직 정책을 확인한 뒤 허용된 방식으로 실행합니다.
`check`는 저장된 두 항목의 유효 여부만 확인하고 `delete`는 이 앱의 Testnet 항목만 삭제합니다.
고정 generic target은 `com.binance-auto.trader.testnet/api-key`,
`com.binance-auto.trader.testnet/api-secret`이며 canary는 별도 `session5-canary` 항목을 사용하고 지웁니다.
키를 명령행·환경변수·`.env`·renderer에 넣지 않습니다. 저장 도중 오류가 나면 두 값을 다시 설정한 뒤 시작합니다.

개발 화면 Origin은 정확히 `http://127.0.0.1:5173`입니다. 이미 5173 포트를 사용 중이면 해당 개발 서버를
정상 종료한 뒤 재시도합니다. History와 runtime 소유권은 Windows의 현재 사용자 LocalAppData 아래
`com.binance-auto.trader`에 보존됩니다. 창 닫기 → 일반 종료를 완료한 뒤 개발 터미널을 닫습니다.
소유권 복구가 표시되면 살아 있는 backend와 계좌 상태를 먼저 확인하고 native 복구 안내를 따릅니다.

Session 5는 macOS에서 source와 공통 계약을 검증한 단계입니다. 2026-09-07에는 위 Windows 10 PC에서
native compile, Credential Manager canary, 실제 READY·picker·정상 종료·fresh restart를 검증했습니다.
사용자 지시에 따라 이 Windows 10 x64 개발 실행 통과로 Session 6을 완료했습니다.
Windows 11 native 검증과 설치 배포는 별도 후속 작업이며 Windows release build는 계속 명시적으로 차단됩니다.

```bash
pnpm install
pnpm dev
```

브라우저에서 `http://127.0.0.1:5173`을 엽니다.

일반 browser dev entry에는 native descriptor가 없으므로 live bootstrap failure 화면이
정상입니다. 화면 개발과 Storybook/test fixture는
`create_demo_ui_application()`을 명시적으로 주입합니다. 실제 데스크톱 실행은 native에서
Python sidecar를 시작하고 현재 session의 연결 정보를 허용된 메인 창에 제공합니다.

## 검증

```bash
pnpm typecheck
pnpm test
pnpm build
pnpm storybook
```

`pnpm test:active-strategy`는 개발 서버와 Binance 연결 없이 ACTIVE STATE 및 거래 / 계좌 → 전략 상태 → 현재 상태의 일치를 검증합니다. 기존 backend 테스트 환경(`backend/.venv/bin/python`, 없으면 `python3`)에서 실제 TradingSTM에 Case B·C의 시장 조건과 가짜 주문·체결 응답을 입력한 뒤, 발행된 JSON event를 실제 UI adapter와 App에 전달합니다. 두 표시 영역과 단계별 단일 지표 목록의 자동 갱신, 5초 유지 판정의 색상 변화, C 트레일링의 이전 EMA 기준 보존, 청산 후 회복·B 인계, 중지·종료, 연결 단절 시 회색 표시와 event 유실 후 전체 snapshot 복원을 검사합니다. 지표 수치 비교는 backend의 순수 조건 평가를 전략 전이와 공유하고, UI는 true/false/null 판정만 초록/빨강/회색으로 표시합니다. Python 외부 socket 연결은 차단하고 UI HTTP·WebSocket은 메모리 대역을 사용하므로 5173 포트를 점유하지 않습니다.

실시간 지표의 구현 경계는 `backend/src/binance_auto_trader/domain/trading/conditions.py`의 순수 평가, `backend/src/binance_auto_trader/application/trading_indicator_snapshot.py`의 단계·평가 보존, 기존 transport 계약, `features/recent-orders/tradingIndicatorPresenter.ts`의 문구·색상 투영으로 분리되어 있습니다. 시작 전·종료 후와 구버전 payload에서는 예시 지표를 표시하지 않습니다. 트레이딩 패널은 기존 순서를 유지하는 세로 스크롤 영역이며 단일 열에서는 높이 640px를 사용합니다.

시간 조건에는 남은 시간과 `시작 대기`·`진행 중`·`정지 · 조건 미충족`·`유지 완료` 상태가 표시됩니다. B의 3시간 신호 유효시간, 5초 연속 유지와 6시간 시간청산, C의 3분 회복·손절 유지와 60분 시간청산이 대상입니다. C 회복은 정확히 180초까지 유효하고 초과하거나 저점이 갱신되면 새 회차의 `03:00`과 리셋 사유를 함께 표시합니다. 진입 일시정지는 B 신호의 경과 시간을 멈추지 않습니다. 화면의 숫자는 서버 측 측정·전송 시각과 로컬 monotonic 경과로 매초 갱신하며 `00:00`만으로 전략 판정을 바꾸지 않습니다. 서버 판정이 오기 전에는 `판정 대기`, 연결 단절이나 구버전 타이머 누락에는 `— · 확인 대기`를 표시합니다. 숨겨진 탭은 interval만 중지하고 다시 열 때 보정하며, 재전송은 최초 수신 시각을 보존합니다.

타이머의 불변 도메인 모델은 `domain/trading/timers.py`, 실제 시장 유지시간 연결은 `application/market_condition_timers.py`, runtime 회차·리셋은 `application/trading_indicator_timers.py`가 담당합니다. `pnpm test:active-strategy`에는 가짜 시계의 카운트다운·정지·탭 복원 및 실제 C-10/C-11 리셋 event 유실 후 전체 snapshot 복원이 포함됩니다. 구현 변경을 실행 중인 앱에 반영하려면 Python sidecar도 재시작해야 합니다.

`BINANCE_DESKTOP_SMOKE=1 pnpm desktop:dev`는 실제 Tauri·Binance 백엔드를 실행하고 시세·계좌·주문 환경 표시, 공개 4시간봉과 backend·화면 스윙 판정의 일치, 연결 툴팁, 초기 renderer 예외를 확인합니다. 터미널의 `desktop-smoke` 결과에는 고정 단계·연결 상태·스윙 분류만 기록하며, 마지막 `passed` 이후 앱은 열린 상태로 유지됩니다. 이 검사는 자동매매·주문·청산을 실행하지 않습니다. 검증용 script와 결과 endpoint는 이 환경 변수를 켠 Vite 개발 실행에만 주입되며 배포 빌드에는 포함되지 않습니다.

`BINANCE_DESKTOP_SMOKE=recovery-shutdown pnpm desktop:dev`는 실제 backend snapshot의 UI 응답 복사본에 지표 오류를 주입하고 `MALFORMED_BACKEND_PAYLOAD` 화면에서 안전 종료를 누릅니다. 종료 상태 조회와 HTTP 202는 실제 backend를 사용하며, native 앱 종료 코드 0과 소유권 파일의 `RELEASED`까지 확인해야 성공입니다. 이 검사는 거래 상태를 바꾸거나 청산을 제출하지 않으며 기존 backend의 종료 안전 검사를 그대로 거칩니다.

`BINANCE_DESKTOP_SMOKE=recovery-reload pnpm desktop:dev`는 최초 연결 정보 수신만 실패시켜 `연결 다시 확인`으로 복구한 뒤, 전체 화면을 새로고침해 동일 backend에 다시 연결합니다. 마지막으로 연결 정보가 없는 화면의 `안전 종료`를 누릅니다. 로그의 `recovery-retry-passed`, `renderer-reload-passed`, `recovery-shutdown-accepted`와 실제 process code 0·소유권 `RELEASED`를 함께 확인합니다. browser 저장소에는 시험 단계만 넣으며 token과 계좌 값은 보관하지 않습니다.

## 구조

- `src/app`: 앱 셸, provider, root 상태 조정
- `src/routes`: 대시보드와 거래 내역 화면 composition
- `src/features`: 기능별 Boundary 컴포넌트와 독립 상태 머신
- `src/shared/api`: `BackendUiAdapter`, strict runtime mapper와 reconnect lifecycle
- `src/shared/contracts`: Python schema에서 생성한 wire 계약과 공통 UI 계약
- `src/shared`: typed port, 금융 문자열 표시, 범용 UI와 디자인 토큰
- `src/stories`: Figma 16개 프레임에 대응하는 공통 harness와 state fixture
- `apps/desktop/src-tauri`: 데스크톱 셸과 현재 backend session의 memory-only descriptor 복구 command

화면 Boundary는 API, 파일 시스템과 주문 계산을 직접 호출하지 않습니다.
`BackendUiAdapter`는 HTTP/WebSocket wire 변환, token, timeout, sequence와
snapshot-first full resync만 담당하고 업무 guard를 만들지 않습니다. REGIME·start/stop/split
명령과 Phase 10 상세 이력 query/event는 실제 backend에 연결되어 있습니다. CSV filesystem과
shutdown command는 해당 owner Phase 전까지 backend의 typed unavailable 결과로 닫혀 있습니다.
demo와 Storybook은 계속 deterministic `FakeUiCommandAdapter`를 사용합니다.

공개 시장 데이터의 조회·정규화·병합·재연결은 `features/price-chart/data`와 `hooks`에 격리되어 있으며 API key를 사용하지 않습니다. backend market event와 결과 parity가 확보되기 전까지 이 표시용 차트를 유지하며 trading 판단에는 사용하지 않습니다.

금융 차트는 [TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/)를 사용합니다. 휠·핀치 확대/축소, 드래그 이동, 동적 가격·시간축과 crosshair OHLCV를 지원하며, drawing은 봉의 실제 시각·가격을 기준으로 별도 SVG layer에 투영되어 일반/전체화면에서 같은 지점을 유지합니다. 앱 시작 시 WebSocket buffer를 먼저 열고 네 주기의 REST 과거 봉과 병합하며, 연결이 끊기면 제한된 backoff로 전체 snapshot을 다시 동기화합니다.

각 주기는 최초 1,000개 봉을 적재하고 최신 180개가 보이는 범위로 시작합니다. 사용자가 차트를 왼쪽으로 이동해 시작 지점에 가까워지면 선택 주기의 이전 1,000개를 추가로 불러오며, 같은 과정을 Binance의 첫 거래 봉까지 반복합니다. 세로 휠 축소는 과거 페이지를 추가하지 않고 현재 적재된 전체 봉을 확인하는 데 사용하며, 실제 왼쪽 drag 또는 가로 이동만 이전 페이지 조회를 시작합니다. 최대 x축 축소 간격은 현재 적재 봉 수와 실제 시간축 폭에 맞춰 다시 계산되고, sub-pixel 구간에서는 데이터 conflation으로 전체 윤곽을 유지합니다. 페이지 추가 전의 확대·이동 범위는 유지되고, 실시간 진행 봉은 전체 이력을 다시 그리지 않고 마지막 봉만 갱신합니다.
