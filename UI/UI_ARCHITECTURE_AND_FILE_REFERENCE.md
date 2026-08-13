# Binance Auto Trader UI 아키텍처 및 파일 참조서

| 항목 | 내용 |
|---|---|
| 문서 상태 | As-built — 현재 구현 기준 |
| 작성일 | 2026-08-12 |
| 대상 경로 | `/Users/oscar/Desktop/Binance_Auto/UI` |
| 대상 구현 | React + TypeScript + XState + Vite + Tauri 2 UI |
| 제외 범위 | Binance 실거래, 투자 판단 알고리즘, 실제 CSV 파일 시스템 adapter, 실제 backend 연결 |

## 1. 문서 목적

이 문서는 `UI_Implementation_Architecture_Plan.md`의 구현 전 제안과 달리, 현재 `UI` 폴더에 실제로 구현된 구조를 설명한다. 다음 내용을 한곳에서 확인할 수 있도록 작성했다.

- 유지보수 대상 디렉터리의 전체 트리
- 각 계층과 기능 모듈의 책임
- 사람이 관리하는 파일별 역할
- React Boundary, presenter, facade, XState actor, adapter 사이의 데이터 흐름
- UI Event-Action Table이 코드에 분산 구현된 위치
- Figma 16개 프레임과 Storybook/기준 이미지의 대응 관계
- 테스트, 실행, 확장 시 지켜야 할 규칙
- 현재 데모 구현과 앞으로 연결해야 할 실제 backend의 경계

`node_modules`, `dist`, `storybook-static`처럼 명령 실행으로 다시 생성할 수 있는 디렉터리는 파일을 하나씩 나열하지 않고 생성물로 따로 설명한다.

## 2. 현재 구현 요약

현재 UI는 다음 특성을 가진 데스크톱 SPA이다.

- React가 화면 Boundary와 페이지 composition을 담당한다.
- XState actor가 Event-Action Table의 상태와 전이를 기능별로 나누어 관리한다.
- `UiApplicationFacade`가 React intent를 적절한 actor event로 변환하고 전역 modal을 조정한다.
- `UiApplicationStore`가 facade를 React의 `useSyncExternalStore` 계약으로 감싼다.
- presenter가 actor의 `AppViewModel`을 각 페이지와 기능 컴포넌트의 props로 변환한다.
- backend 명령은 `UiCommandPort` 뒤로 격리되어 있다.
- 현재 실행 환경은 `FakeUiCommandAdapter`를 사용하므로 실거래와 실제 파일 쓰기를 수행하지 않는다.
- Tauri는 1440×1024 데스크톱 창과 안전한 종료 lifecycle을 제공하는 최소 shell이다.
- Storybook의 공통 harness가 Figma 16개 프레임을 상태 fixture로 재현한다.
- 스타일은 semantic CSS token, CSS Modules, Radix primitive를 중심으로 구성한다.

초기 데모 상태는 API 연결 표시가 online이고 자동매매는 정지 상태이며, 실제 적용 REGIME은 `null`이다. 따라서 처음에는 어떤 REGIME 버튼도 선택되지 않고, REGIME 미선택 상태에서 자동매매 실행을 누르면 경고 modal과 REGIME 패널 강조 흐름이 시작된다.

## 3. 전체 아키텍처

```mermaid
flowchart LR
    User["사용자"] --> Boundary["React Boundary\n컴포넌트와 페이지"]
    Boundary --> Presenter["Presenter\nViewModel과 UI props 변환"]
    Presenter --> Controller["UiApplicationController\nintent 전달"]
    Controller --> Store["UiApplicationStore\nReact 외부 store"]
    Store --> Facade["UiApplicationFacade\nactor 조정과 modal 단일화"]
    Facade --> Actors["기능별 XState actor"]
    Actors --> Port["UiCommandPort"]
    Port --> Fake["현재: FakeUiCommandAdapter"]
    Port -. "향후 교체" .-> Backend["실제 backend와 desktop adapter"]
    Actors --> Snapshot["UiApplicationSnapshot"]
    Snapshot --> ViewModel["AppViewModel"]
    ViewModel --> Presenter
```

### 3.1 계층별 책임

| 계층 | 대표 위치 | 책임 |
|---|---|---|
| Entry/Provider | `src/main.tsx`, `src/app/providers` | React root와 전역 provider를 생성한다. |
| App composition | `src/app/App.tsx` | 헤더, 현재 route, 전역 modal host를 조합한다. |
| Route Boundary | `src/routes` | 기능 컴포넌트를 화면 레이아웃으로 배치한다. 업무 상태를 직접 판단하지 않는다. |
| Feature Boundary | `src/features/*/components` | ViewModel을 렌더링하고 typed intent만 상위로 보낸다. |
| Presenter | `src/app/presenters` | 공통 `AppViewModel`을 route/feature props로 변환한다. |
| Control facade | `src/app/control` | UI intent를 actor event로 라우팅하고 actor snapshot을 단일화한다. |
| State actor | `src/app/machines`, `src/features/*/machines` | Event-Action 상태, guard, action, 비동기 command lifecycle을 관리한다. |
| Runtime store | `src/app/runtime`, `src/app/hooks` | facade와 React 구독 수명주기, Tauri 창 종료를 연결한다. |
| Contract/Port | `src/shared/contracts`, `src/shared/ports` | 기능 간 데이터 형식과 backend 명령 경계를 정의한다. |
| Adapter/Fixture | `src/shared/testing`, `src/app/bootstrap` | backend 없이 결정적인 데모와 테스트 결과를 제공한다. |
| Design system | `src/shared/styles`, `src/shared/ui` | 공통 token, 전역 규칙, 재사용 가능한 primitive를 제공한다. |
| Desktop shell | `apps/desktop/src-tauri` | Tauri 창, 권한, CSP, 최종 창 제거를 담당한다. |

### 3.2 사용자 입력에서 다시 렌더링되기까지

1. 사용자가 Boundary 컴포넌트의 버튼, 탭, slider, 달력 등을 조작한다.
2. 컴포넌트는 업무 event가 아니라 `PriceChartIntent`, `RegimePanelIntent` 같은 화면 intent를 방출한다.
3. presenter가 화면 intent를 `UiApplicationIntent`로 변환해 `UiApplicationController.dispatch()`에 전달한다.
4. `UiApplicationStore`가 intent를 `UiApplicationFacade`에 전달한다.
5. facade가 해당 기능의 XState actor event로 변환한다.
6. 필요하면 actor가 `UiCommandPort`의 비동기 명령을 invoke한다.
7. actor snapshot이 바뀌면 facade가 전역 modal slot을 다시 계산하고 전체 snapshot을 발행한다.
8. store가 새 `AppViewModel`을 캐시하고 React listener에 알린다.
9. presenter가 새 페이지 props를 만들고 React가 화면을 다시 그린다.

## 4. 최상위 디렉터리 트리

```text
UI/
├── .storybook/                       # Storybook 설정
│   ├── main.ts
│   └── preview.ts
├── apps/
│   └── desktop/
│       └── src-tauri/                # Tauri 2 데스크톱 shell
├── src/
│   ├── app/                          # 앱 조립, 제어, runtime
│   ├── assets/                       # Figma SVG asset
│   ├── features/                     # 기능별 Boundary와 actor
│   ├── routes/                       # 대시보드와 거래 내역 화면
│   ├── shared/                       # 공통 계약, port, primitive, token
│   ├── stories/                      # Figma 16개 상태 Story
│   ├── test/                         # 공통 테스트 설정
│   ├── main.tsx
│   └── vite-env.d.ts
├── visual-regression/
│   ├── figma/                        # Figma 1440×1024 기준 PNG 16개
│   └── README.md
├── .gitignore
├── index.html
├── package.json
├── pnpm-lock.yaml
├── pnpm-workspace.yaml
├── README.md
├── UI_Implementation_Architecture_Plan.md
├── UI_ARCHITECTURE_AND_FILE_REFERENCE.md
├── tsconfig.app.json
├── tsconfig.json
├── tsconfig.node.json
├── vite.config.ts
└── vitest.config.ts
```

### 4.1 최상위 파일 설명

| 파일 | 역할 |
|---|---|
| `.gitignore` | `node_modules`, 빌드 결과, Storybook 결과 등 UI 로컬 생성물을 Git 추적에서 제외한다. |
| `README.md` | 패키지 목적, 기본 실행·검증 명령, 상위 폴더 책임을 짧게 안내한다. |
| `UI_Implementation_Architecture_Plan.md` | 구현 전에 작성된 목표 기술·계층·모듈 구조 제안서다. 현재 소스의 사실 확인에는 이 문서를 사용한다. |
| `UI_ARCHITECTURE_AND_FILE_REFERENCE.md` | 현재 구현의 디렉터리, 파일, runtime 흐름을 기록하는 본 문서다. |
| `index.html` | Vite가 읽는 HTML entry이며 React가 연결될 `#root`를 제공한다. |
| `package.json` | React, XState, Radix, Lightweight Charts, Tauri 등 의존성과 실행 script를 정의한다. |
| `pnpm-lock.yaml` | 설치되는 JavaScript 패키지의 정확한 버전과 해시를 고정한다. 수동 편집하지 않는다. |
| `pnpm-workspace.yaml` | pnpm의 hoisted node linker 설정을 정의한다. |
| `tsconfig.json` | app과 Node 설정을 project reference로 묶는 TypeScript 최상위 설정이다. |
| `tsconfig.app.json` | DOM/React 소스의 strict typecheck, JSX, exact optional property 설정을 정의한다. |
| `tsconfig.node.json` | Vite와 Vitest 설정 파일을 검사하는 Node 측 TypeScript 설정이다. |
| `vite.config.ts` | React plugin과 고정 개발 서버 port 정책을 정의한다. |
| `vitest.config.ts` | jsdom, 공통 setup, CSS 처리를 사용하는 component/machine 테스트 환경을 정의한다. |

### 4.2 자동 생성 디렉터리

| 디렉터리 | 생성 명령/원천 | 취급 방법 |
|---|---|---|
| `node_modules/` | `pnpm install` | 외부 의존성이다. 직접 수정하지 않는다. |
| `dist/` | `pnpm build` | production web bundle이다. 소스가 아니므로 직접 수정하지 않는다. |
| `storybook-static/` | `pnpm build-storybook` | 정적 Storybook 결과다. 직접 수정하지 않는다. |
| `.pnpm-store/` 또는 pnpm cache | pnpm | 패키지 cache이며 구현 문서와 코드 검토 대상이 아니다. |

## 5. Storybook과 데스크톱 shell

### 5.1 `.storybook` 트리와 파일

```text
.storybook/
├── main.ts
└── preview.ts
```

| 파일 | 역할 |
|---|---|
| `.storybook/main.ts` | `src/stories/**/*.stories.tsx`를 story 원본으로 등록하고 React-Vite framework와 접근성 addon을 설정한다. |
| `.storybook/preview.ts` | 전역 CSS를 불러오고 fullscreen layout, dark canvas 배경, 접근성 오류 정책을 적용한다. |

### 5.2 `apps/desktop/src-tauri` 트리와 파일

```text
apps/desktop/src-tauri/
├── capabilities/
│   └── main-window.json
├── src/
│   ├── lib.rs
│   └── main.rs
├── build.rs
├── Cargo.toml
└── tauri.conf.json
```

| 파일 | 역할 |
|---|---|
| `Cargo.toml` | Tauri shell crate, Rust edition, `serde`, `tauri` 의존성을 정의한다. |
| `build.rs` | Tauri build-time code generation을 실행한다. |
| `src/main.rs` | native executable entry이며 library의 `run()`을 호출한다. |
| `src/lib.rs` | 최소 권한 `tauri::Builder`를 만들고 애플리케이션을 실행한다. |
| `tauri.conf.json` | 1440×1024 기본 창, 1180×760 최소 크기, Vite dev URL, frontend build 경로, CSP와 bundle 설정을 정의한다. |
| `capabilities/main-window.json` | 메인 창의 기본 API와 상태머신 종료 완료 후 `destroy` 권한만 허용한다. |

현재 Tauri Rust 코드는 거래 기능을 구현하지 않는다. 창과 보안 경계를 제공하며, OS close event의 처리 순서는 TypeScript의 `DesktopWindowLifecycle`과 `appExitMachine`이 담당한다.

## 6. `src/app` 구조

```text
src/app/
├── bootstrap/
│   ├── createDemoUiApplication.ts
│   ├── demoFixtures.ts
│   └── index.ts
├── components/
│   ├── modals/
│   │   ├── ExitConfirmationDialog.module.css
│   │   ├── ExitConfirmationDialog.tsx
│   │   ├── OperationStatusDialog.module.css
│   │   └── OperationStatusDialog.tsx
│   ├── AppModalHost.tsx
│   └── index.ts
├── control/
│   ├── UiApplicationFacade.test.ts
│   ├── UiApplicationFacade.ts
│   └── index.ts
├── hooks/
│   ├── index.ts
│   ├── useCsvCalendarNavigation.ts
│   ├── useDesktopWindowLifecycle.ts
│   └── useUiApplication.ts
├── machines/
│   ├── index.ts
│   └── uiShellMachine.ts
├── presenters/
│   ├── csvExportPresenter.ts
│   ├── dashboardPresenter.ts
│   ├── index.ts
│   ├── presenters.test.ts
│   └── tradeHistoryPresenter.ts
├── providers/
│   └── AppProviders.tsx
├── runtime/
│   ├── DesktopWindowLifecycle.ts
│   ├── UiApplicationStore.test.ts
│   ├── UiApplicationStore.ts
│   └── index.ts
├── App.module.css
├── App.test.tsx
└── App.tsx
```

### 6.1 App와 bootstrap

| 파일 | 역할 |
|---|---|
| `App.tsx` | `AppHeader`, 현재 route, `AppModalHost`를 조합하는 최상위 Boundary다. 종료 final 상태에서는 안전 종료 안내만 표시한다. |
| `App.module.css` | 앱 canvas, 대시보드 여백, 종료 final 화면의 layout을 정의한다. |
| `App.test.tsx` | REGIME 선택, 자동매매 시작, route 이동, CSV, 중지까지 실제 runtime wiring을 통합 검증한다. |
| `bootstrap/createDemoUiApplication.ts` | `FakeUiCommandAdapter`와 모든 actor를 포함한 `UiApplicationFacade`를 Figma 기준 초기값으로 생성하고 demo online event를 주입한다. |
| `bootstrap/demoFixtures.ts` | Figma 거래 행과 지표를 공통 `TradeRecord`, `RegimeMetric` 계약으로 정규화한다. |
| `bootstrap/index.ts` | bootstrap factory와 fixture의 공개 import 경계를 제공한다. |

### 6.2 전역 modal 구성

| 파일 | 역할 |
|---|---|
| `components/AppModalHost.tsx` | shell의 단일 modal 종류를 거래 확인, REGIME 확인, CSV, 종료, 진행·완료·오류 Dialog로 연결한다. CSV 달력 navigation hook도 여기에서 presenter에 전달한다. |
| `components/index.ts` | app-level modal 컴포넌트의 공개 barrel이다. |
| `components/modals/ExitConfirmationDialog.tsx` | 포지션이 없을 때의 일반 종료와 포지션이 있을 때의 강제 매도 후 종료 확인 문구를 표시한다. |
| `components/modals/ExitConfirmationDialog.module.css` | 종료 확인 dialog의 안내 박스와 버튼 행 스타일을 정의한다. |
| `components/modals/OperationStatusDialog.tsx` | CSV 처리 결과와 앱 종료 처리 중 상태를 공통 modal 표면으로 표시한다. |
| `components/modals/OperationStatusDialog.module.css` | 진행 표시, 성공·실패 tone, 결과 경로와 action 영역을 스타일링한다. |

### 6.3 Control facade

| 파일 | 역할 |
|---|---|
| `control/UiApplicationFacade.ts` | 모든 feature actor를 생성·시작·종료하고, `UiApplicationIntent`를 actor event로 변환하며, 전체 snapshot과 `AppViewModel`을 만든다. exit → trading → REGIME → CSV 안전 우선순위로 modal 하나만 활성화한다. |
| `control/UiApplicationFacade.test.ts` | route 상태 보존, REGIME 미선택 경고, chart 선 interaction, account/history summary 투영을 검증한다. |
| `control/index.ts` | facade class, selector, intent/snapshot/ViewModel type을 공개한다. |

`UiApplicationFacade.ts`가 크지만 기능별 상태 자체를 소유하는 거대 상태머신은 아니다. 이 파일은 actor registry와 event router 역할을 하며, 실제 전이는 `features/*/machines`에 분리되어 있다.

### 6.4 Hooks, presenter, provider, runtime

| 파일 | 역할 |
|---|---|
| `hooks/useUiApplication.ts` | `UiApplicationStore`를 한 번 생성하고 `useSyncExternalStore`로 controller와 최신 ViewModel을 React에 제공한다. |
| `hooks/useCsvCalendarNavigation.ts` | 시작일·종료일 달력의 표시 연월만 React local state로 관리하고 이전/다음 월과 연·월 선택 함수를 제공한다. |
| `hooks/useDesktopWindowLifecycle.ts` | Tauri close event의 기본 종료를 막아 `APP_EXIT_CLICKED`를 보내고, actor가 final 상태가 되면 창을 실제 제거한다. |
| `hooks/index.ts` | app hook의 공개 barrel이다. |
| `presenters/dashboardPresenter.ts` | `AppViewModel`과 고정 chart fixture를 결합해 Dashboard props를 만들고 chart, trader, REGIME, split-order intent를 facade intent로 변환한다. |
| `presenters/tradeHistoryPresenter.ts` | actor의 거래 record와 filter enum을 표 행·필터 props로 변환하고 route/CSV action을 연결한다. |
| `presenters/csvExportPresenter.ts` | CSV actor의 draft, validation, calendar target을 `CSVExportDialogProps`로 변환한다. |
| `presenters/presenters.test.ts` | account/history 값 투영과 chart 선 hover/context/delete intent 변환을 검증한다. |
| `presenters/index.ts` | 세 presenter 함수의 공개 barrel이다. |
| `providers/AppProviders.tsx` | 향후 server-owned snapshot 조회를 위한 TanStack Query client를 앱 수명주기에 맞춰 제공한다. 현재 데모 데이터 흐름은 actor/fake adapter가 담당한다. |
| `runtime/UiApplicationStore.ts` | facade를 React 외부 store로 감싸며 최초 구독에서 actor를 시작하고 마지막 구독에서 정리한다. React StrictMode의 즉시 재구독에도 snapshot을 유지한다. |
| `runtime/UiApplicationStore.test.ts` | 최초 구독, dispatch 알림, StrictMode식 구독 교체 수명주기를 검증한다. |
| `runtime/DesktopWindowLifecycle.ts` | Tauri window API를 `on_close_requested()`와 `destroy()`만 가진 좁은 port로 감싸고 브라우저 실행에서는 `null`을 반환한다. |
| `runtime/index.ts` | runtime store와 desktop lifecycle 계약의 공개 barrel이다. |

### 6.5 App shell machine

| 파일 | 역할 |
|---|---|
| `machines/uiShellMachine.ts` | `dashboard`와 `trade_history` route 이동, dashboard deep-history 의미, 전역 modal 단일 slot을 병렬 region으로 관리한다. URL router 대신 이 actor가 현재 route를 소유한다. |
| `machines/index.ts` | shell machine과 route/modal type의 공개 barrel이다. |

## 7. `src/assets` 구조

```text
src/assets/figma/
├── chart-maximize.svg
├── chart-pen.svg
├── csv-export-icon-bg.svg
├── header-start.svg
├── header-stop-icon.svg
├── logo-frame.svg
├── logo-glyph.svg
├── modal-status-negative-bg.svg
├── modal-status-negative-dot.svg
├── modal-status-negative-ring.svg
├── modal-status-positive-bg.svg
├── modal-status-positive-dot.svg
└── modal-status-positive-ring.svg
```

| 파일 | 역할 |
|---|---|
| `chart-maximize.svg` | 차트 전체화면 진입·종료 버튼의 Figma 아이콘이다. |
| `chart-pen.svg` | 차트 선 그리기 mode 버튼 아이콘이다. |
| `csv-export-icon-bg.svg` | CSV dialog 제목 왼쪽의 배경 아이콘이다. |
| `header-start.svg` | 자동매매 실행 버튼 아이콘이다. |
| `header-stop-icon.svg` | 매매 중지 버튼 아이콘이다. |
| `logo-frame.svg` | 상단 브랜드 로고의 외곽 frame이다. |
| `logo-glyph.svg` | 상단 브랜드 로고의 내부 glyph다. |
| `modal-status-negative-bg.svg` | 경고·위험 modal 상태 아이콘의 바깥 배경층이다. |
| `modal-status-negative-ring.svg` | 경고·위험 modal 상태 아이콘의 중간 ring이다. |
| `modal-status-negative-dot.svg` | 경고·위험 modal 상태 아이콘의 중심 dot이다. |
| `modal-status-positive-bg.svg` | 확인·성공 modal 상태 아이콘의 바깥 배경층이다. |
| `modal-status-positive-ring.svg` | 확인·성공 modal 상태 아이콘의 중간 ring이다. |
| `modal-status-positive-dot.svg` | 확인·성공 modal 상태 아이콘의 중심 dot이다. |

## 8. `src/features` 구조

각 feature는 가능한 경우 `components`, `machines`, `types.ts`, `index.ts`로 나눈다. 컴포넌트는 표시와 intent 방출, machine은 상태 전이, `types.ts`는 Boundary 계약, `index.ts`는 외부에 허용할 공개 API를 맡는다.

```text
src/features/
├── account-summary/
├── app-exit/
├── connection-status/
├── csv-export/
├── price-chart/
├── recent-orders/
├── regime-selection/
├── split-order/
├── trade-history/
└── trading-control/
```

### 8.1 `account-summary`

```text
account-summary/
├── components/
│   ├── AccountSection.module.css
│   ├── AccountSection.tsx
│   ├── AssetCard.module.css
│   ├── AssetCard.tsx
│   ├── StrategyCard.module.css
│   └── StrategyCard.tsx
├── machines/
│   ├── accountSummaryMachine.test.ts
│   └── accountSummaryMachine.ts
├── index.ts
└── types.ts
```

| 파일 | 역할 |
|---|---|
| `components/AccountSection.tsx` | 전략 카드, 자산 카드, 분할 주문 control을 “거래 / 계좌” 영역에 배치한다. |
| `components/AccountSection.module.css` | 계좌 영역 제목, 3열 카드 layout과 구분선을 정의한다. |
| `components/StrategyCard.tsx` | 자동매매 작동 상태, 적용 전략, 수익률과 수익 금액을 표시한다. |
| `components/StrategyCard.module.css` | 전략 상태 dot, tone, 설명 목록을 스타일링한다. |
| `components/AssetCard.tsx` | 총자산, KRW, ETH, 평가 손익을 표시한다. |
| `components/AssetCard.module.css` | 자산 값의 숫자 글꼴, 행 layout, 손익 tone을 정의한다. |
| `machines/accountSummaryMachine.ts` | 투자 로직 상태와 자산 snapshot을 `trading_logic_status`, `asset_summary` 독립 region으로 투영한다. |
| `machines/accountSummaryMachine.test.ts` | DI1/DI2 갱신이 서로 영향을 주지 않는지 검증한다. |
| `types.ts` | `StrategySummaryViewModel`, `AssetSummaryViewModel`, card tone과 section props를 정의한다. |
| `index.ts` | account-summary의 컴포넌트, machine, type 공개 API다. |

### 8.2 `app-exit`

```text
app-exit/
├── machines/
│   └── appExitMachine.ts
└── index.ts
```

| 파일 | 역할 |
|---|---|
| `machines/appExitMachine.ts` | OS 종료 요청을 포지션 유무에 따라 일반 종료 확인 또는 강제 매도 확인으로 나누고, force-sell, shutdown, UI final 상태까지 조정한다. |
| `index.ts` | 종료 machine과 context/event type을 공개한다. |

### 8.3 `connection-status`

```text
connection-status/
├── machines/
│   └── connectionMachine.ts
└── index.ts
```

| 파일 | 역할 |
|---|---|
| `machines/connectionMachine.ts` | `api_offline`, `connecting`, `api_online`, `reconnecting` 표시 상태와 reconnect 횟수·오류를 관리한다. 실제 WebSocket protocol은 구현하지 않는다. |
| `index.ts` | 연결 machine과 context/event type을 공개한다. |

### 8.4 `csv-export`

```text
csv-export/
├── components/
│   ├── CSVExportDialog.module.css
│   ├── CSVExportDialog.test.tsx
│   ├── CSVExportDialog.tsx
│   ├── CalendarPopover.module.css
│   ├── CalendarPopover.tsx
│   ├── index.ts
│   └── types.ts
├── machines/
│   ├── csvExportMachine.test.ts
│   └── csvExportMachine.ts
└── index.ts
```

| 파일 | 역할 |
|---|---|
| `components/CSVExportDialog.tsx` | 저장 위치, 기간 preset, 시작·종료일, 파일명과 validation 메시지를 표시하는 controlled Radix dialog다. 달력이 열려 있을 때 외부 입력은 달력만 닫는다. |
| `components/CSVExportDialog.module.css` | Figma 560px dialog, 입력 section, 날짜 field, 오류 상태와 action 행을 정의한다. |
| `components/CSVExportDialog.test.tsx` | 입력 intent, 달력 대상·월 이동, 외부 click 예외, validation, focus 복원을 검증한다. |
| `components/CalendarPopover.tsx` | 월별 날짜 cell 생성, 연·월 선택, 이전·다음 월, Escape와 방향키/Home/End navigation을 제공한다. |
| `components/CalendarPopover.module.css` | 244px 달력 popover, 요일, 날짜 grid, 선택일과 비활성 날짜를 스타일링한다. |
| `components/types.ts` | CSV draft, validation error, calendar target과 표시 월 ViewModel을 정의한다. |
| `components/index.ts` | CSV Boundary 컴포넌트와 표시 type의 공개 barrel이다. |
| `machines/csvExportMachine.ts` | dialog, directory picker, 기간/달력, 파일명 편집을 병렬 region으로 관리하고 validation 및 export invoke를 수행한다. |
| `machines/csvExportMachine.test.ts` | 경로 오류, 달력 외부 click, 반대 날짜 field 전환, picker 상태 보존, export 완료를 검증한다. |
| `index.ts` | CSV 컴포넌트와 machine API를 feature 외부에 공개한다. |

### 8.5 `price-chart`

```text
price-chart/
├── components/
│   ├── ChartCanvas.module.css
│   ├── ChartCanvas.tsx
│   ├── ChartToolbar.module.css
│   ├── ChartToolbar.tsx
│   ├── IndicatorSettingsPopover.module.css
│   ├── IndicatorSettingsPopover.tsx
│   ├── LightweightChartSurface.module.css
│   ├── LightweightChartSurface.tsx
│   ├── PriceChartPanel.module.css
│   ├── PriceChartPanel.test.tsx
│   └── PriceChartPanel.tsx
├── machines/
│   ├── chartMachine.test.ts
│   └── chartMachine.ts
├── index.ts
└── types.ts
```

| 파일 | 역할 |
|---|---|
| `components/PriceChartPanel.tsx` | 차트 제목, timestamp, toolbar, chart surface, 지표 popover를 조합한다. fullscreen 상태를 panel layout에 반영한다. |
| `components/PriceChartPanel.module.css` | 472px 기본 panel, header, popover 위치와 fullscreen overlay layout을 정의한다. |
| `components/PriceChartPanel.test.tsx` | interval, 지표, drawing, fullscreen, 선 hover/context/delete intent와 렌더링을 검증한다. |
| `components/ChartToolbar.tsx` | 1분·30분·4시간·1일 interval, active trading state와 지표 설정 버튼을 표시한다. |
| `components/ChartToolbar.module.css` | interval segmented control, active state badge, toolbar 버튼을 스타일링한다. |
| `components/IndicatorSettingsPopover.tsx` | EMA9, 볼린저밴드, 거래량 표시 여부를 controlled toggle로 노출한다. |
| `components/IndicatorSettingsPopover.module.css` | Figma 지표 설정 popover, 색상 swatch, switch와 행 layout을 정의한다. |
| `components/LightweightChartSurface.tsx` | Lightweight Charts instance와 candle, EMA, Bollinger, volume series를 생성·갱신하고 resize를 처리한다. |
| `components/LightweightChartSurface.module.css` | 금융 차트 canvas를 SVG interaction layer 아래에 배치한다. |
| `components/ChartCanvas.tsx` | chart 축과 Figma fallback SVG, drawing 생성, 저장 선 hit area, hover, context menu, 삭제, 도구 버튼을 담당한다. Lightweight Charts가 준비되면 중복 market SVG layer를 숨긴다. |
| `components/ChartCanvas.module.css` | chart/축/도구/drawing overlay/context menu와 fullscreen 크기를 정의한다. |
| `machines/chartMachine.ts` | interval, 지표 popover와 3개 indicator, active state, viewport, drawing, line selection을 병렬 region으로 관리한다. |
| `machines/chartMachine.test.ts` | interval별 drawing 보존, fullscreen, indicator 독립 toggle을 검증한다. |
| `types.ts` | chart ViewModel, candle/line 자료형과 모든 `PriceChartIntent`를 정의한다. |
| `index.ts` | chart Boundary, actor와 type의 공개 API다. `LightweightChartSurface`는 feature 내부 구현으로 유지한다. |

### 8.6 `recent-orders`

```text
recent-orders/
├── components/
│   ├── RealtimeIndicators.module.css
│   ├── RealtimeIndicators.tsx
│   ├── RecentOrdersList.module.css
│   ├── RecentOrdersList.tsx
│   ├── TraderPanel.module.css
│   ├── TraderPanel.test.tsx
│   └── TraderPanel.tsx
├── machines/
│   └── recentOrdersMachine.ts
├── index.ts
└── types.ts
```

| 파일 | 역할 |
|---|---|
| `components/TraderPanel.tsx` | “체결 내역”과 “실시간 지표” controlled tab을 표시하고 전체 거래 내역 이동 intent를 방출한다. |
| `components/TraderPanel.module.css` | 우측 412px panel, tab, panel body와 scroll 영역을 정의한다. |
| `components/TraderPanel.test.tsx` | tab 변경과 전체 보기 intent를 검증한다. |
| `components/RecentOrdersList.tsx` | 매수·매도 체결을 시간, 가격, 수량/수익률과 함께 목록으로 표시한다. |
| `components/RecentOrdersList.module.css` | 체결 side tone, 행 구분, 숫자 정렬을 정의한다. |
| `components/RealtimeIndicators.tsx` | 전략별 실시간 지표 그룹과 체류 시간을 표시한다. |
| `components/RealtimeIndicators.module.css` | 지표 grid, positive/negative dot와 숫자 tone을 정의한다. |
| `machines/recentOrdersMachine.ts` | 두 tab 상태, 최근 체결 prepend, 실시간 지표 갱신을 관리한다. |
| `types.ts` | 체결 행, 지표 그룹, tab, `TraderPanelIntent` 계약을 정의한다. |
| `index.ts` | recent-orders의 공개 Boundary와 actor API다. |

### 8.7 `regime-selection`

```text
regime-selection/
├── components/
│   ├── RegimeChangeDialog.module.css
│   ├── RegimeChangeDialog.tsx
│   ├── RegimeMetric.module.css
│   ├── RegimeMetric.tsx
│   ├── RegimePanel.module.css
│   ├── RegimePanel.test.tsx
│   ├── RegimePanel.tsx
│   ├── RegimeTypeButton.module.css
│   └── RegimeTypeButton.tsx
├── machines/
│   ├── regimeMachine.test.ts
│   └── regimeMachine.ts
├── index.ts
└── types.ts
```

| 파일 | 역할 |
|---|---|
| `components/RegimePanel.tsx` | 추천 타입, 실제 적용 타입, 5개 수동 선택 버튼, 4개 판단 지표와 강조 상태를 표시한다. 적용값이 `null`이면 아무 버튼도 선택하지 않는다. |
| `components/RegimePanel.module.css` | Figma 948×116 panel, 내부 3영역, 선택 button과 5회 glow animation/reduced-motion 대체를 정의한다. |
| `components/RegimePanel.test.tsx` | 초기 미선택과 적용 타입 표시/intent를 검증한다. |
| `components/RegimeTypeButton.tsx` | 하나의 REGIME type을 상승·하락 tone과 candidate/applied 상태로 표시한다. |
| `components/RegimeTypeButton.module.css` | type별 color, selected, candidate outline, disabled 상태를 정의한다. |
| `components/RegimeMetric.tsx` | REGIME 판단 지표 이름과 값을 tone과 함께 표시한다. |
| `components/RegimeMetric.module.css` | 지표 label/value와 상태 dot를 스타일링한다. |
| `components/RegimeChangeDialog.tsx` | 후보 REGIME을 실제 적용하기 전 확인 modal을 표시한다. |
| `components/RegimeChangeDialog.module.css` | 확인 modal의 type 안내 박스와 action 행을 Figma 좌표에 맞춘다. |
| `machines/regimeMachine.ts` | 추천값, 적용값, 확인 전 candidate, 4초/5회 panel 강조, 비동기 apply command를 관리한다. |
| `machines/regimeMachine.test.ts` | click만으로 적용되지 않는지, 취소 시 기존값 보존, 확인 후 adapter 적용을 검증한다. |
| `types.ts` | REGIME type, metric, panel props와 `RegimePanelIntent`를 정의한다. |
| `index.ts` | regime-selection의 공개 Boundary와 actor API다. |

### 8.8 `split-order`

```text
split-order/
├── components/
│   ├── PercentSlider.module.css
│   ├── PercentSlider.tsx
│   ├── SplitOrderControls.module.css
│   ├── SplitOrderControls.test.tsx
│   └── SplitOrderControls.tsx
├── machines/
│   ├── splitOrderMachine.test.ts
│   └── splitOrderMachine.ts
├── index.ts
└── types.ts
```

| 파일 | 역할 |
|---|---|
| `components/PercentSlider.tsx` | 0~100 범위를 10 단위로 제한하는 native range input과 ±10 버튼을 제공한다. drag 중에도 연속 `onInput` 값을 전달한다. |
| `components/PercentSlider.module.css` | 매수/매도 tone, track progress, thumb, ± 버튼과 percentage badge를 정의한다. |
| `components/SplitOrderControls.tsx` | 분할 매수와 분할 매도 slider 두 개를 하나의 카드로 묶고 side별 intent를 방출한다. |
| `components/SplitOrderControls.module.css` | 분할 주문 카드와 두 slider의 세로 layout을 정의한다. |
| `components/SplitOrderControls.test.tsx` | 버튼 증가와 slider 연속 이동 intent를 검증한다. |
| `machines/splitOrderMachine.ts` | 비율 정규화, optimistic 표시, 비동기 저장, 저장 중 최신값 queue, 실패·재시도를 관리한다. |
| `machines/splitOrderMachine.test.ts` | 저장 중 연속 입력 시 최신 비율이 보존되고 순차 저장되는지 검증한다. |
| `types.ts` | side, slider props, control props와 `SplitOrderIntent`를 정의한다. |
| `index.ts` | split-order의 공개 Boundary와 actor API다. |

### 8.9 `trade-history`

```text
trade-history/
├── components/
│   ├── HistoryFilters.module.css
│   ├── HistoryFilters.tsx
│   ├── SummaryCards.module.css
│   ├── SummaryCards.tsx
│   ├── TradeHistoryComponents.test.tsx
│   ├── TradeTable.module.css
│   ├── TradeTable.tsx
│   ├── index.ts
│   └── types.ts
├── machines/
│   ├── tradeHistoryMachine.test.ts
│   ├── tradeHistoryMachine.ts
│   ├── tradeHistorySummaryMachine.test.ts
│   └── tradeHistorySummaryMachine.ts
├── fixtures.ts
└── index.ts
```

| 파일 | 역할 |
|---|---|
| `components/SummaryCards.tsx` | 당일 수익률, 매도 성과, 보유 ETH, 수수료 요약 카드 4개를 표시한다. |
| `components/SummaryCards.module.css` | 4열 요약 card, 수치 tone과 세부 통계를 정의한다. |
| `components/HistoryFilters.tsx` | 기간 4종과 거래 side 3종을 독립 group으로 제공하고 CSV action을 노출한다. |
| `components/HistoryFilters.module.css` | 64px toolbar, segmented filter와 CSV 버튼을 스타일링한다. |
| `components/TradeTable.tsx` | 11개 열의 거래 행, scroll 영역, empty state와 자동매매 이동 action을 표시한다. |
| `components/TradeTable.module.css` | 고정 header, 행/열 폭, side badge, 숫자 tone, empty layout과 scroll을 정의한다. |
| `components/TradeHistoryComponents.test.tsx` | 요약/행 구조, 두 filter intent, empty action을 검증한다. |
| `components/types.ts` | history 표시 filter, summary, 표 행, empty state ViewModel을 정의한다. |
| `components/index.ts` | 거래 내역 Boundary 컴포넌트의 공개 barrel이다. |
| `machines/tradeHistoryMachine.ts` | 기간·side filter를 조합해 `UiCommandPort.load_trade_history()`를 호출하고 idle/loading/ready/empty/failed를 관리한다. |
| `machines/tradeHistoryMachine.test.ts` | filter 조합, empty 결과, adapter 실패를 검증한다. |
| `machines/tradeHistorySummaryMachine.ts` | 수익률, 매도 성과, ETH 보유량, 수수료를 네 독립 표시 region으로 관리한다. |
| `machines/tradeHistorySummaryMachine.test.ts` | D1~D4 summary 갱신의 독립성을 검증한다. |
| `fixtures.ts` | Figma 요약 수치, empty 요약과 9개 상세 거래 행 fixture를 제공한다. |
| `index.ts` | component, fixture, query actor와 summary actor의 공개 API다. |

### 8.10 `trading-control`

```text
trading-control/
├── components/
│   ├── AppHeader.module.css
│   ├── AppHeader.tsx
│   ├── TradingConfirmationDialog.module.css
│   └── TradingConfirmationDialog.tsx
├── machines/
│   ├── tradingCommandMachine.test.ts
│   └── tradingCommandMachine.ts
└── index.ts
```

| 파일 | 역할 |
|---|---|
| `components/AppHeader.tsx` | 브랜드, LIVE/OFFLINE 상태, 자동매매 실행과 매매 중지 버튼을 표시한다. 실행 상태와 pending 상태에 따라 버튼을 제어한다. |
| `components/AppHeader.module.css` | 72px header, logo, 연결 badge, 실행·중지 action을 Figma 규격으로 배치한다. |
| `components/TradingConfirmationDialog.tsx` | 시작, 시작 중, 일반 중지, 강제 매도 중지, REGIME 미선택, API 미연결 variant를 하나의 공통 확인 Boundary로 표시한다. |
| `components/TradingConfirmationDialog.module.css` | 64px 안내 박스, 진행 bar, tone과 버튼 행을 공통 modal 고정 좌표에 맞춘다. |
| `machines/tradingCommandMachine.ts` | 시작 guard, 시작 확인, 실행, 일반 중지, 강제 매도 중지, API 단절 중지를 adapter invoke와 함께 관리한다. 실제 position snapshot은 성공한 강제 매도에서만 비운다. |
| `machines/tradingCommandMachine.test.ts` | REGIME 미선택, 중복 없는 시작, 강제 매도 실패·취소 시 position 보존, 성공 시 position clear를 검증한다. |
| `index.ts` | header, 거래 dialog와 command machine의 공개 API다. |

## 9. UI Event-Action Table 구현 위치

Event-Action Table은 한 파일의 switch문으로 구현하지 않았다. 서로 독립적으로 변하는 상태를 기능별 XState actor로 나누고, 각 transition의 `meta.spec_ids`에 원본 명세 ID를 기록한다. `UiApplicationFacade`는 이 actor들을 조정할 뿐 전이 원본은 각 machine 파일이다.

| Event-Action 영역 | 구현 파일 | 핵심 상태/책임 |
|---|---|---|
| 화면 이동과 modal slot | `src/app/machines/uiShellMachine.ts` | dashboard/history 이동, H* 의미, modal 단일화 |
| API 연결 표시 | `features/connection-status/machines/connectionMachine.ts` | offline, connecting, online, reconnecting |
| 자동매매 시작·중지 | `features/trading-control/machines/tradingCommandMachine.ts` | REGIME/API guard, 확인, pending, running, force-sell |
| REGIME 선택·강조 | `features/regime-selection/machines/regimeMachine.ts` | recommended, candidate, applied, highlight, apply invoke |
| 차트 | `features/price-chart/machines/chartMachine.ts` | interval, indicator, fullscreen, drawing, line menu |
| 우측 트레이딩 panel | `features/recent-orders/machines/recentOrdersMachine.ts` | 최근 체결/실시간 지표 tab과 실시간 update |
| 계좌 요약 | `features/account-summary/machines/accountSummaryMachine.ts` | DI1/DI2 표시 snapshot |
| 분할 주문 | `features/split-order/machines/splitOrderMachine.ts` | SI/SO 비율 저장과 연속 변경 |
| 거래 내역 조회 | `features/trade-history/machines/tradeHistoryMachine.ts` | 기간/side filter와 조회 lifecycle |
| 거래 내역 summary | `features/trade-history/machines/tradeHistorySummaryMachine.ts` | D1~D4 독립 summary update |
| CSV | `features/csv-export/machines/csvExportMachine.ts` | CR1/CR2/CR3 draft와 TD4 export lifecycle |
| 프로그램 종료 | `features/app-exit/machines/appExitMachine.ts` | ES3 확인, force-sell, shutdown, final |

### 9.1 actor 구성 원칙

- actor가 다른 actor의 내부 state node를 직접 수정하지 않는다.
- actor 간 조정이 필요하면 facade가 read-only snapshot을 읽고 명시적 event를 보낸다.
- 비동기 작업은 XState `invoke`와 `onDone`/`onError`로 표현한다.
- 화면 컴포넌트는 actor를 직접 import하지 않고 presenter/controller를 통해 intent를 보낸다.
- 전역 modal은 shell에 단일 slot만 존재한다.
- Event-Action 추적이 필요한 transition은 `meta.spec_ids`를 유지한다.

## 10. `src/routes` 구조

```text
src/routes/
├── dashboard/
│   ├── DashboardPage.module.css
│   ├── DashboardPage.test.tsx
│   ├── DashboardPage.tsx
│   ├── dashboardFixture.ts
│   └── index.ts
└── trade-history/
    ├── TradeHistoryPage.module.css
    ├── TradeHistoryPage.test.tsx
    ├── TradeHistoryPage.tsx
    └── index.ts
```

| 파일 | 역할 |
|---|---|
| `dashboard/DashboardPage.tsx` | REGIME, 차트, 계좌/분할주문, 우측 트레이더 feature를 2열 dashboard로 배치한다. |
| `dashboard/DashboardPage.module.css` | 좌측 948px와 우측 412px grid, feature 사이 간격과 최소 폭 대응을 정의한다. |
| `dashboard/DashboardPage.test.tsx` | 모든 feature Boundary가 header 없는 dashboard에 배치되는지 검증한다. |
| `dashboard/dashboardFixture.ts` | Figma 기본 dashboard의 chart, 지표, 체결, 계좌, split-order 표시 데이터를 제공한다. |
| `dashboard/index.ts` | dashboard page, props와 fixture의 공개 barrel이다. |
| `trade-history/TradeHistoryPage.tsx` | 돌아가기, 제목, 요약, filter, table과 선택적 CSV dialog를 조합한다. |
| `trade-history/TradeHistoryPage.module.css` | 상세 route의 1440px 기준 여백, heading, summary, toolbar, table 위치를 정의한다. |
| `trade-history/TradeHistoryPage.test.tsx` | page composition과 돌아가기 intent를 검증한다. |
| `trade-history/index.ts` | trade-history page와 props의 공개 barrel이다. |

이 프로젝트의 route는 URL 기반 React Router가 아니라 `uiShellMachine`의 state다. 데스크톱 단일 창에서 dashboard state를 보존한 채 상세 화면으로 이동하기 위한 선택이다.

## 11. `src/shared` 구조

```text
src/shared/
├── contracts/
│   ├── index.ts
│   └── uiContracts.ts
├── hooks/
│   ├── index.ts
│   └── useDialogFocusReturn.ts
├── ports/
│   ├── UiCommandPort.ts
│   └── index.ts
├── styles/
│   ├── global.css
│   └── tokens.css
├── testing/
│   ├── FakeUiCommandAdapter.ts
│   ├── fixtures.ts
│   └── index.ts
└── ui/
    ├── Button/
    │   ├── Button.module.css
    │   └── Button.tsx
    ├── ModalSurface/
    │   ├── ModalSurface.module.css
    │   └── ModalSurface.tsx
    ├── StatusIndicatorIcon/
    │   ├── StatusIndicatorIcon.module.css
    │   └── StatusIndicatorIcon.tsx
    ├── Surface/
    │   ├── Surface.module.css
    │   └── Surface.tsx
    └── index.ts
```

### 11.1 Contract, port, testing

| 파일 | 역할 |
|---|---|
| `contracts/uiContracts.ts` | `RegimeType`, 기간, 거래 방향, `TradeRecord`, chart drawing, CSV option/receipt와 공통 오류 형식을 정의한다. cross-feature 계약만 둔다. |
| `contracts/index.ts` | 공통 contract type의 공개 barrel이다. |
| `ports/UiCommandPort.ts` | 자동매매 시작/중지, REGIME 적용, split ratio, history 조회, 폴더 선택, CSV export, shutdown 명령 경계를 정의한다. |
| `ports/index.ts` | `UiCommandPort`를 공개한다. |
| `testing/FakeUiCommandAdapter.ts` | 모든 port 명령을 기록하고 결정적 성공/실패를 반환한다. 테스트와 현재 데모 runtime이 함께 사용한다. |
| `testing/fixtures.ts` | 공통 기준일, REGIME 지표, 거래 record와 chart drawing fixture를 제공한다. |
| `testing/index.ts` | fake adapter와 fixture의 공개 barrel이다. |

### 11.2 Style과 공통 UI

| 파일 | 역할 |
|---|---|
| `styles/tokens.css` | 배경, surface, border, text, 상태 color, shadow, radius, spacing, header 높이와 font family의 semantic token을 정의한다. |
| `styles/global.css` | Inter/IBM Plex Sans KR font, box sizing, root 최소 폭, focus, selection, screen-reader helper와 reduced-motion 정책을 적용한다. |
| `hooks/useDialogFocusReturn.ts` | 조건부 modal이 unmount될 때 다른 dialog가 없다면 원래 trigger로 focus를 복원한다. |
| `hooks/index.ts` | shared hook의 공개 barrel이다. |
| `ui/Button/Button.tsx` | neutral/positive/negative/info tone과 compact option을 가진 공통 버튼 primitive다. |
| `ui/Button/Button.module.css` | 버튼 높이, tone, hover, pressed, disabled와 compact 스타일을 정의한다. |
| `ui/ModalSurface/ModalSurface.tsx` | Radix Dialog의 focus trap, overlay, 외부 click/Escape 정책, 제목·설명·leading visual을 공통화한다. |
| `ui/ModalSurface/ModalSurface.module.css` | 420×232 compact와 560px wide modal, overlay, heading, 고정 내부 좌표를 정의한다. |
| `ui/StatusIndicatorIcon/StatusIndicatorIcon.tsx` | 긍정/부정 Figma SVG 3개를 겹쳐 modal 상태 아이콘을 만든다. |
| `ui/StatusIndicatorIcon/StatusIndicatorIcon.module.css` | 아이콘 3개 layer의 정확한 크기와 위치를 정의한다. |
| `ui/Surface/Surface.tsx` | section/article/div 중 하나로 렌더링할 수 있는 범용 panel 표면이다. |
| `ui/Surface/Surface.module.css` | 공통 surface 배경, border, radius를 정의한다. |
| `ui/index.ts` | shared UI primitive의 유일한 공개 import 경계다. |

## 12. Entry, Story, 테스트 설정

### 12.1 `src` entry 파일

| 파일 | 역할 |
|---|---|
| `src/main.tsx` | `#root`를 검사하고 React StrictMode, `AppProviders`, `App`을 mount하며 global CSS를 불러온다. |
| `src/vite-env.d.ts` | Vite의 `import.meta`와 asset module type을 TypeScript에 제공한다. |
| `src/test/setup.ts` | 모든 Vitest 파일에 `@testing-library/jest-dom` matcher를 등록한다. |

### 12.2 Storybook 구조

```text
src/stories/
├── FigmaFrames.stories.tsx
└── figma-frames/
    ├── FigmaFrameHarness.module.css
    ├── FigmaFrameHarness.tsx
    └── figmaFrameFixtures.ts
```

| 파일 | 역할 |
|---|---|
| `stories/FigmaFrames.stories.tsx` | 공통 harness에 frame key만 주입해 Figma 16개 story export를 선언한다. |
| `stories/figma-frames/FigmaFrameHarness.tsx` | 공통 header와 route Boundary를 조합하고 frame별 modal/tab/empty/highlight 상태를 주입한다. Story 상호작용은 정적 비교를 위해 무시한다. |
| `stories/figma-frames/FigmaFrameHarness.module.css` | 1440×1024 고정 story viewport와 route 위치를 정의한다. |
| `stories/figma-frames/figmaFrameFixtures.ts` | 16개 frame key와 각 frame의 header, dashboard, history, CSV, modal 상태 fixture를 정의한다. |

### 12.3 테스트 파일 요약

현재 테스트는 작성 시점 기준 20개 파일, 53개 test로 구성되며 다음 계층을 확인한다.

| 범위 | 테스트 파일 |
|---|---|
| App runtime 통합 | `app/App.test.tsx` |
| Facade 조정 | `app/control/UiApplicationFacade.test.ts` |
| Presenter 변환 | `app/presenters/presenters.test.ts` |
| Store 수명주기 | `app/runtime/UiApplicationStore.test.ts` |
| Route composition | `routes/dashboard/DashboardPage.test.tsx`, `routes/trade-history/TradeHistoryPage.test.tsx` |
| Account actor | `features/account-summary/machines/accountSummaryMachine.test.ts` |
| Trading actor | `features/trading-control/machines/tradingCommandMachine.test.ts` |
| REGIME | `features/regime-selection/components/RegimePanel.test.tsx`, `machines/regimeMachine.test.ts` |
| Chart | `features/price-chart/components/PriceChartPanel.test.tsx`, `machines/chartMachine.test.ts` |
| Split order | `features/split-order/components/SplitOrderControls.test.tsx`, `machines/splitOrderMachine.test.ts` |
| Trader panel | `features/recent-orders/components/TraderPanel.test.tsx` |
| History | `features/trade-history/components/TradeHistoryComponents.test.tsx`, 두 machine test |
| CSV | `features/csv-export/components/CSVExportDialog.test.tsx`, `machines/csvExportMachine.test.ts` |

## 13. Visual regression 기준 자료

```text
visual-regression/
├── README.md
└── figma/
    ├── 01-realtime-indicator.png
    ├── 02-recent-orders.png
    ├── 03-indicator-settings.png
    ├── 04-trade-history.png
    ├── 05-start-confirm.png
    ├── 06-stop-with-position.png
    ├── 07-stop-no-position.png
    ├── 08-csv-end-calendar.png
    ├── 09-csv-start-calendar.png
    ├── 10-csv-no-calendar.png
    ├── 11-regime-confirm.png
    ├── 12-regime-required.png
    ├── 13-regime-highlight.png
    ├── 14-history-empty.png
    ├── 15-start-loading.png
    └── 16-csv-error.png
```

| 파일/그룹 | 역할 |
|---|---|
| `visual-regression/README.md` | Figma file key, viewport, Story 순서와 비교 규칙을 설명한다. |
| `01-realtime-indicator.png` | 우측 실시간 지표 tab 기준이다. |
| `02-recent-orders.png` | 우측 최근 체결 tab 기준이다. |
| `03-indicator-settings.png` | 차트 지표 설정 popover 기준이다. |
| `04-trade-history.png` | 거래 내역 상세 기준이다. |
| `05-start-confirm.png` | 자동매매 시작 확인 modal 기준이다. |
| `06-stop-with-position.png` | 포지션 보유 중지 modal 기준이다. |
| `07-stop-no-position.png` | 포지션 미보유 중지 modal 기준이다. |
| `08-csv-end-calendar.png` | CSV 종료일 달력 기준이다. |
| `09-csv-start-calendar.png` | CSV 시작일 달력 기준이다. |
| `10-csv-no-calendar.png` | CSV dialog 기본 기준이다. |
| `11-regime-confirm.png` | REGIME 적용 확인 modal 기준이다. |
| `12-regime-required.png` | REGIME 미선택 경고 modal 기준이다. |
| `13-regime-highlight.png` | REGIME panel glow 기준이다. |
| `14-history-empty.png` | 거래 내역 empty state 기준이다. |
| `15-start-loading.png` | 자동매매 시작 pending modal 기준이다. |
| `16-csv-error.png` | CSV validation error 기준이다. |

PNG는 모두 1440×1024, `deviceScaleFactor=1` 기준이다. 구현 variant를 복제 컴포넌트로 만들지 않고 같은 Boundary에 다른 fixture를 주입해 비교한다.

## 14. 상태 소유권

| 상태 종류 | 현재 owner | 예시 |
|---|---|---|
| 전역 route/modal | `uiShellMachine` | dashboard/history, 활성 modal 하나 |
| 기능 UI 상태 | 기능별 XState actor | tab, interval, filter, candidate, pending |
| backend 표시 snapshot | read-only actor context | account, 거래 summary, position 여부 |
| UI 순간 상태 | React component/hook | 달력 표시 월, drawing 중 pointer, chart instance, focus return |
| 화면용 파생값 | `AppViewModel`과 presenter | status label, table row, component props |
| 외부 명령 결과 | `UiCommandPort` 구현체 | 시작/중지, 조회, CSV, shutdown |
| 현재 데모 원본 | fixture와 `FakeUiCommandAdapter` | 고정 거래, 지표, 경로, 성공 결과 |

같은 값을 React state, actor context와 fixture에 동시에 authoritative하게 두지 않는다. React local state는 사용 중인 임시 표현만 소유하며 확정 상태는 actor 또는 향후 backend가 소유한다.

## 15. Modal 정책

- 확인 modal은 backdrop click과 Escape만으로 닫히지 않는다.
- CSV dialog는 외부 click 정책을 actor event로 전달한다.
- CSV 달력이 열려 있으면 외부 click, 다른 입력 영역 click, 반대 날짜 field click이 먼저 달력 상태를 정리한다.
- `AppModalHost`와 `uiShellMachine`은 동시에 전역 modal 하나만 보이게 한다.
- modal이 닫히면 `use_dialog_focus_return()`이 원래 trigger로 focus를 복원한다.
- compact 확인 modal은 420×232이며 내부 안내 박스와 버튼 위치가 설명문의 줄 수에 따라 변하지 않도록 고정한다.
- 종료, 자동매매, REGIME, CSV가 동시에 상태 변화를 요청할 때는 안전과 데이터 손실 위험이 큰 흐름을 우선한다.

## 16. 디자인 시스템과 접근성

### 16.1 디자인 규칙

- Figma 기준 viewport는 1440×1024다.
- web root 최소 폭은 1180px이고 Tauri 최소 창은 1180×760이다.
- 한국어 UI는 IBM Plex Sans KR, 숫자·영문 수치는 Inter를 우선 사용한다.
- color를 component 안에 반복하지 않고 `tokens.css`의 semantic token을 우선 사용한다.
- component style은 같은 폴더의 `*.module.css`에 둬 selector 충돌을 막는다.
- 공통 역할까지 같은 요소만 `shared/ui`에 둔다. 업무 의미가 있는 버튼은 feature 내부에 둔다.

### 16.2 접근성 구현

- Radix Dialog로 focus trap과 background inert 처리를 제공한다.
- tab, slider, dialog, menu, table에 적절한 ARIA role과 accessible name을 사용한다.
- 상승/하락, 매수/매도를 색상만으로 표현하지 않고 text와 부호를 함께 표시한다.
- chart의 가는 선은 별도 HTML hit area로 keyboard focus와 context-menu key를 지원한다.
- 달력은 Escape, 방향키, Home, End를 지원한다.
- `:focus-visible`을 전역 token으로 표시한다.
- `prefers-reduced-motion`에서는 animation 시간을 제거하고 REGIME 강조 outline은 유지한다.

## 17. 현재 구현 범위와 실제 backend 연결 지점

### 17.1 현재 가능한 것

- Figma 기준 dashboard와 거래 내역 화면 렌더링
- REGIME 선택 확인과 미선택 경고/강조
- 자동매매 시작·중지 UI state와 확인 modal
- 분할 매수·매도 slider interaction
- 차트 interval, indicator, fullscreen, drawing과 선 삭제
- 최근 체결/실시간 지표 tab
- history filter, empty state, CSV 입력·달력·validation
- 앱 종료 확인과 Tauri 창 close interception
- Storybook 16개 Figma 상태 확인

### 17.2 아직 demo 또는 경계만 있는 것

| 항목 | 현재 상태 | 실제 구현 시 교체/확장 위치 |
|---|---|---|
| Binance 연결 | demo에서 `API_CONNECTED` event를 주입 | connection service와 facade backend event bridge |
| 자동매매 | fake command를 기록하고 성공 처리 | `UiCommandPort.start_trading/stop_trading/force_sell_and_stop` adapter |
| REGIME 계산 | 추천값과 지표 fixture | backend snapshot event → facade intent |
| 계좌/포지션 | Figma fixture와 actor read-only update | backend WebSocket/account query adapter |
| 거래 내역 | fake adapter의 메모리 배열 filter | `UiCommandPort.load_trade_history` 실제 repository adapter |
| CSV 폴더 선택 | 결정적 fake 경로 반환 | Tauri dialog 또는 backend filesystem adapter |
| CSV 파일 쓰기 | fake receipt 반환 | `UiCommandPort.export_csv` 실제 writer/atomic save |
| shutdown | fake command 완료 | 거래 engine flush, stream close, sidecar 종료 adapter |
| TanStack Query | provider만 준비 | 실제 query/cache/snapshot 동기화 도입 |
| E2E | component/machine 테스트만 존재 | Playwright/Tauri E2E suite 추가 |

실제 adapter를 추가할 때 React component가 Binance SDK, Tauri file API 또는 거래 계산을 직접 import하면 안 된다. 기존 `UiCommandPort`를 구현하거나 필요한 port를 별도 모듈로 추가한다.

## 18. 실행과 검증

### 18.1 pnpm 명령이 있는 경우

```bash
cd /Users/oscar/Desktop/Binance_Auto/UI
pnpm install
pnpm dev
```

브라우저에서 `http://127.0.0.1:5173`을 연다. 개발 서버 종료는 실행한 terminal에서 `Control+C`를 누른다.

### 18.2 `pnpm: command not found`인 경우

Node.js에 포함된 Corepack을 사용할 수 있다.

```bash
cd /Users/oscar/Desktop/Binance_Auto/UI
corepack pnpm install
corepack pnpm dev
```

### 18.3 주요 script

| 명령 | 역할 |
|---|---|
| `pnpm dev` | Vite 개발 서버를 `127.0.0.1:5173`에 실행한다. |
| `pnpm typecheck` | strict TypeScript project build 검사를 수행한다. |
| `pnpm test` | Vitest 전체 suite를 한 번 실행한다. |
| `pnpm test:watch` | 파일 변경을 감시하며 관련 테스트를 다시 실행한다. |
| `pnpm build` | typecheck 후 production bundle을 `dist`에 만든다. |
| `pnpm storybook` | Storybook 개발 서버를 6006 port에 실행한다. |
| `pnpm build-storybook` | 정적 Storybook을 `storybook-static`에 만든다. |
| `pnpm desktop:dev` | Rust/Cargo 환경에서 Tauri desktop 개발 앱을 실행한다. |
| `pnpm desktop:build` | Tauri desktop bundle을 생성한다. |

## 19. 새 기능을 추가할 때의 순서

1. 화면 내부에서만 쓰는 type은 해당 feature의 `types.ts`에 정의한다.
2. 여러 feature나 adapter가 공유하는 type만 `shared/contracts`에 둔다.
3. 외부 작업이 필요하면 component에서 직접 호출하지 말고 `UiCommandPort` 또는 별도 port를 정의한다.
4. 표시 컴포넌트와 `*.module.css`를 feature `components`에 둔다.
5. 상태 전이가 필요하면 feature `machines`에 actor와 test를 추가한다.
6. Event-Action Table과 관련된 전이는 `meta.spec_ids`에 원본 ID를 기록한다.
7. feature 외부에 필요한 symbol만 `index.ts`에서 공개한다.
8. presenter에서 `AppViewModel`과 component props/intent를 변환한다.
9. facade에 새 actor 또는 intent routing을 연결한다.
10. 전역 modal이면 `UiModalKind`, modal derivation 우선순위, `AppModalHost`를 함께 갱신한다.
11. route는 feature를 배치하고 업무 guard를 직접 작성하지 않는다.
12. component/machine test와 필요한 Figma Story state를 추가한다.

## 20. 유지보수 시 주의사항

- `UI_Implementation_Architecture_Plan.md`는 제안서이고 본 문서는 현재 구현 설명서다. 코드 확인 시 두 문서의 상태를 혼동하지 않는다.
- `index.ts` 공개 경계를 우회해 다른 feature의 내부 파일을 직접 import하지 않는다.
- `UiApplicationFacade`에 화면 markup이나 금융 계산을 넣지 않는다.
- presenter에 상태 전이 규칙을 넣지 않는다. presenter는 변환과 intent 연결만 담당한다.
- machine action에서 DOM을 직접 조작하지 않는다.
- finance 값은 contract에서 문자열로 유지하고 UI에서 표시 목적 이외의 부동소수 계산을 하지 않는다.
- fixture는 실제 backend 원본이 아니다. live 연결 후에는 snapshot으로 대체한다.
- Figma frame마다 component를 복제하지 말고 기존 component의 state/props variant로 표현한다.
- `dist`, `storybook-static`, `node_modules`, lockfile 내부를 문제 해결 목적으로 직접 고치지 않는다.
- 함수와 class를 추가할 때 프로젝트의 구조화된 한국어 설명 주석과 코딩 convention을 유지한다.

## 21. 관련 문서

- `../CODING_CONVENTIONS.md` — 코딩 convention과 주석 형식
- `../Design/Architecture/Communication_Diagram_Message_Flow_Specification.md` — Boundary/Control/backend 메시지 흐름
- `../Design/UI/UI_Behavior.md` — 사용자 관점 동작
- `../Design/UI/UI_Rule.md` — UI 상태와 표시 규칙
- `../Design/UI/UI_Event_Action_Table.md` — event, guard, action, 다음 상태 명세
- `UI_Implementation_Architecture_Plan.md` — 구현 전 목표 아키텍처와 기술 선택 근거
- `visual-regression/README.md` — Figma 기준 이미지와 Story 대응

