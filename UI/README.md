# Binance Auto Trader UI

Figma의 1440×1024 데스크톱 화면을 TypeScript, React, Vite와 XState 기반으로 구현한 UI 패키지입니다. 실제 투자 로직과 Binance API는 아직 연결하지 않으며, UI는 typed port와 deterministic fake adapter만 사용합니다.

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

React Boundary는 API, 파일 시스템과 주문 계산을 직접 호출하지 않습니다. 실제 backend를 연결할 때는 `UiCommandPort`의 adapter만 교체합니다.

금융 차트는 [TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/)를 사용하며, 사용자 drawing과 Figma 기준 축은 별도 SVG primitive layer로 분리합니다.
