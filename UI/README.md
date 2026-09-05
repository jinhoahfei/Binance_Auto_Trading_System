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
