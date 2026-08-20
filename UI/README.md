# Binance Auto Trader UI

Figma의 1440×1024 데스크톱 화면을 TypeScript, React, Vite와 XState 기반으로 구현한 UI 패키지입니다. 가격 차트는 Binance 공개 REST/WebSocket에서 `ETHUSDT`의 1분·30분·4시간·1일 봉을 실시간으로 표시합니다. 주문·계좌·거래 내역 명령은 아직 typed port와 deterministic fake adapter를 사용합니다.

## 실행

```bash
pnpm install
pnpm dev
```

브라우저에서 `http://127.0.0.1:5173`을 엽니다.

## 검증

```bash
pnpm typecheck
pnpm test
pnpm build
pnpm storybook
```

## 구조

- `src/app`: 앱 셸, provider, root 상태 조정
- `src/routes`: 대시보드와 거래 내역 화면 composition
- `src/features`: 기능별 Boundary 컴포넌트와 독립 상태 머신
- `src/shared`: typed contract, port, formatting, 범용 UI와 디자인 토큰
- `src/stories`: Figma 16개 프레임에 대응하는 공통 harness와 state fixture
- `apps/desktop/src-tauri`: 데스크톱 셸 설정

화면 Boundary는 API, 파일 시스템과 주문 계산을 직접 호출하지 않습니다. 공개 시장 데이터의 조회·정규화·병합·재연결은 `features/price-chart/data`와 `hooks`에 격리되어 있으며 API key를 사용하지 않습니다. 실제 주문 backend를 연결할 때는 `UiCommandPort`의 adapter를 교체합니다.

금융 차트는 [TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/)를 사용합니다. 휠·핀치 확대/축소, 드래그 이동, 동적 가격·시간축과 crosshair OHLCV를 지원하며, drawing은 봉의 실제 시각·가격을 기준으로 별도 SVG layer에 투영되어 일반/전체화면에서 같은 지점을 유지합니다. 앱 시작 시 WebSocket buffer를 먼저 열고 네 주기의 REST 과거 봉과 병합하며, 연결이 끊기면 제한된 backoff로 전체 snapshot을 다시 동기화합니다.

각 주기는 최초 1,000개 봉을 적재하고 최신 180개가 보이는 범위로 시작합니다. 사용자가 차트를 왼쪽으로 이동해 시작 지점에 가까워지면 선택 주기의 이전 1,000개를 추가로 불러오며, 같은 과정을 Binance의 첫 거래 봉까지 반복합니다. 세로 휠 축소는 과거 페이지를 추가하지 않고 현재 적재된 전체 봉을 확인하는 데 사용하며, 실제 왼쪽 drag 또는 가로 이동만 이전 페이지 조회를 시작합니다. 최대 x축 축소 간격은 현재 적재 봉 수와 실제 시간축 폭에 맞춰 다시 계산되고, sub-pixel 구간에서는 데이터 conflation으로 전체 윤곽을 유지합니다. 페이지 추가 전의 확대·이동 범위는 유지되고, 실시간 진행 봉은 전체 이력을 다시 그리지 않고 마지막 봉만 갱신합니다.
