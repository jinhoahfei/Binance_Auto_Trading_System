# Binance Auto Trading System

Binance Spot `ETHUSDT` long-only 자동 거래 시스템입니다. Phase 9의 실제 Spot Testnet
REST/WebSocket adapter, authenticated read-only, 10 USDT BUY 진입 cap lifecycle과
cold-restart 복구 검증을 완료했습니다. 기본 실행은 fail-closed이며 데스크톱의 기본
프로필은 주문이 비활성화된 Testnet입니다. Live 조회와 주문은 아래의 OS별 설정 도구로
명시적으로 선택합니다.

신규 현물 거래는 **BNB 납부 없이 매수·매도 각각 0.1%**를 사용합니다. 주문 전에는
할인 자산 납부 미적용과 양방향 비할인 수수료율 `0.001`을 확인하며 BNB 잔액·가격을
거래 전제조건으로 삼지 않습니다. 실제 체결 수수료와 과거 BNB 거래 이력은 원래 값으로
보존합니다. 현재 기준은 [현물 수수료 정책](SPOT_FEE_POLICY.md)입니다.

## 안전 원칙

- 평상시 검증은 `disabled` 또는 `fake` mode를 사용합니다.
- Testnet은 고정된 Binance Spot Testnet endpoint만 사용하며 명시적 opt-in 없이는
  network test가 실행되지 않습니다.
- read-only Testnet과 주문 Testnet은 별도 opt-in입니다. read-only flag만으로는
  prepare, submit, cancel 같은 주문 mutation을 호출할 수 없습니다.
- `BINANCE_TESTNET_MAX_NOTIONAL`은 decision price와 준비 수량으로 계산하는 BUY 진입
  사전 상한입니다. MARKET 체결 금액의 하드 상한은 아니며, STOP/recovery SELL은 이
  quote 상한의 예외입니다. 일반 STOP은 앱 소유 Position과 free ETH 중 작은 수량을 넘지
  않고, recovery는 `free ETH >= Position`일 때만 정확한 Position 전량 한 주문을 허용합니다.
  일반 SELL에는 기존 local quote 방어가 유지됩니다.
  실제 체결 금액과 거래소의 평균/reference 가격 기반 filter 판정이 가격 변동과
  slippage를 포함해 최종 권위입니다.
- 데스크톱의 Live 실행은 OS 자격 증명 저장소의 실행 프로필로만 선택합니다.
  환경 변수로 Live endpoint나 주문 권한을 바꾸지 않습니다.
- 데스크톱 API key와 secret은 macOS Keychain 또는 Windows Credential Manager에 저장합니다.
  아래 Testnet 검증 스크립트의 환경 변수 주입 방식은 데스크톱 키 등록과 별개입니다.
  값을 source, fixture, URL, UI/renderer, `localStorage`, 일반 log 또는 shell 명령
  예시에 넣지 마십시오.

## 사용 권한과 배포 정책

이 저장소와 자체 제작 artifact는 비공개·개인용 proprietary software입니다. 다른 사람에게
사용·복제·수정·재배포 권한을 부여하지 않으며, Python의 `Private :: Do Not Upload`, npm의
`private: true`, Cargo의 `publish = false`로 각 공개 registry의 우발적 배포를 차단합니다.
이 정책은 포함된 제3자 dependency의 각 라이선스와 notice 의무를 변경하거나 제거하지 않습니다.
자세한 범위는 `LICENSE`를 따릅니다.

## macOS·Windows 개발 모드 실행

두 OS 모두 **`UI` 폴더에서 `pnpm desktop:dev`**로 데스크톱 앱을 실행합니다.
명령에는 `desktop:dev`의 콜론(`:`)이 들어갑니다. 처음에는 **개발 도구·의존성 설치 →
아래 API 키 등록 → 앱 실행** 순서로 진행합니다. 이후에는 실행 명령만 사용하면 됩니다.
개발 모드도 선택한 프로필의 실제 계좌에 연결하므로, 개발 실행이라는 이유로 가상 거래가
되는 것은 아닙니다.

공통 준비 도구는 Python 3.11 이상, `uv`, Node.js 24, `pnpm@11.16.0`, Rust/Cargo, Git입니다.
macOS에는 Xcode Command Line Tools, Windows x64에는 Visual Studio Build Tools의
`Desktop development with C++` 및 Windows SDK, Rust `x86_64-pc-windows-msvc`, WebView2가
필요합니다. OS별 상세 설치 항목은 [Tauri 공식 준비 안내](https://v2.tauri.app/start/prerequisites/)를
참조하십시오. `.venv`와 `node_modules`는 각 OS에서 설치하고 다른 OS에서 복사하지 않습니다.

### macOS

터미널에서 저장소 루트(`README.md`, `backend`, `UI`, `scripts`가 있는 폴더)를 연 뒤,
최초 설치 또는 의존성이 바뀐 경우 다음을 실행합니다.

```bash
cd backend
uv sync --locked --extra desktop
cd ../UI
pnpm install --frozen-lockfile
cd ..
```

아래 **API 키 적용** 절차를 마친 뒤 저장소 루트에서 실행합니다.

```bash
cd UI
pnpm desktop:dev
```

macOS 개발 실행은 Python backend를 sidecar 실행 파일로 패키징한 뒤 개발 화면과 앱을
시작하므로 `--extra desktop`에 포함된 PyInstaller가 필요합니다. Python 변경을 반영하려면
앱을 정상 종료하고 `pnpm desktop:dev`를 다시 실행합니다.

### Windows x64

PowerShell에서 저장소 루트를 연 뒤 최초 설치 또는 의존성이 바뀐 경우 실행합니다.

```powershell
.\scripts\setup_windows_development.ps1
```

이 스크립트는 `backend/.venv`와 UI 의존성을 lockfile 기준으로 설치합니다. 기존 `.dev-tools`와
시스템 PATH의 도구를 지원하며 전역 PATH를 변경하지 않습니다. 아래 **API 키 적용** 절차를
마친 뒤, 저장소 루트에서 다음 중 한 가지 방법으로 실행합니다.

```powershell
# 환경 설정과 앱 실행을 한 번에 수행
.\scripts\start_windows_development.ps1
```

또는 macOS처럼 UI 폴더에서 직접 실행할 수 있습니다.

```powershell
# 첫 줄 맨 앞의 점과 공백도 함께 입력합니다.
. .\scripts\enter_windows_development.ps1
cd UI
pnpm.cmd desktop:dev
```

`pnpm.cmd desktop:dev`는 Windows에서 같은 pnpm 스크립트를 실행하는 명령입니다.
Windows 개발 앱은 `backend/.venv/Scripts/python.exe`로 backend 소스를 실행하므로
설치용 `.exe`를 먼저 만들 필요는 없습니다. Python 변경 후에는 앱을 정상 종료하고 다시
실행합니다. 개발 환경 검증은 저장소 루트에서 `.\scripts\check_windows.ps1`로 수행할 수 있습니다.

종료는 두 OS 모두 **앱의 창 닫기 → 일반 종료 완료 → 터미널 종료** 순서로 합니다.
터미널을 먼저 강제 종료하면 남은 backend의 소유권 보호로 다음 실행이 차단될 수 있습니다.
Windows의 세부 지원 범위와 실제 검증 상태는 [Windows 호환성 안내](docs/windows-compatibility.md)를
참조하십시오.

## 발급받은 API 키 적용

Binance에서 발급받은 **API Key와 Secret Key 한 쌍**을 준비합니다. 실제 계좌용 Live 키와
Spot Testnet 키는 서로 다른 키이므로 아래에서 발급받은 환경에 맞는 절차를 선택합니다.
현재 앱은 API key/secret 기반 서명을 사용합니다. 공개키·개인키 파일을 등록하는 방식은
아래 도구의 입력 대상이 아닙니다.

키 등록·교체·실행 프로필 변경은 **앱을 정상 종료한 상태에서**, 앱을 실행할 것과 같은 OS
사용자 계정으로 진행합니다. 모든 명령은 **저장소 루트 기준**입니다. API Key나 Secret을
명령어 뒤에 붙이거나 `.env`·소스 코드에 넣지 말고, 도구가 요청하는 숨김 입력란에 입력합니다.
저장된 키는 다음 앱 시작 때 자동으로 읽으므로 UI나 backend에 별도로 붙여넣지 않습니다.

### 실제 Binance 계좌용 Live 키 등록

Live 키는 출금 권한을 비활성화한 상태로 사용합니다. 두 OS 모두 `set`으로 등록하면
실행 프로필이 **`LIVE_READ_ONLY`(실계좌 조회, 주문 비활성)**로 설정됩니다.

**macOS — Keychain에 등록**

```bash
sh scripts/configure_live_keychain.sh set
sh scripts/configure_live_keychain.sh check
```

`set`에서 확인 문구로 대문자 `LIVE`를 입력합니다. 이어 API Key를 입력하고, 재입력을
요청하면 **같은 API Key**를 다시 입력합니다. 다음 Secret 입력·재입력에는 **같은 Secret Key**를
입력합니다. 숨김 입력은 화면에 보이지 않는 것이 정상입니다. Keychain 접근 허용 창이
나타나면 실행한 앱·도구에 대한 요청인지 확인합니다. `check`는 저장 항목의 존재를 확인하며
키 값을 출력하지 않습니다.

**Windows — 자격 증명 관리자에 등록**

```powershell
.\scripts\configure_live_credentials.ps1 -Action set
.\scripts\configure_live_credentials.ps1 -Action check
```

`set`에서 대문자 `LIVE`를 입력한 다음, `LIVE API key`와 `LIVE API secret` 숨김 입력란에
각 값을 입력합니다. `check`는 저장된 두 값의 유효한 형식을 확인하고 값은 출력하지 않습니다.
저장 위치는 현재 사용자의 Windows 자격 증명 관리자(Credential Manager)입니다.

등록 후 위 개발 모드 명령으로 앱을 실행하고 화면의 실행 환경이 **Live · 주문 비활성**에
해당하는 상태인지 확인합니다. 키 등록 도구의 `check` 성공은 거래소 인증 성공을 뜻하지
않으므로 앱에서 계좌와 연결 상태까지 확인합니다.

### Live 조회 전용·주문 허용 프로필 전환

키를 다시 입력하지 않고 저장된 Live 키의 실행 프로필만 바꿀 수 있습니다. 아래 명령은
변경 전에 앱을 종료하고 실행하며, 변경 후에는 앱을 다시 시작해야 적용됩니다.

| 프로필 | 앱 동작 |
| --- | --- |
| `TESTNET` | Testnet 키 사용, 데스크톱 주문 비활성. 프로필 미설정 시 기본값 |
| `LIVE_READ_ONLY` | Live 키 사용, 실계좌 조회만 허용 |
| `LIVE_ORDERS_V1` | Live 키 사용, 기존 주문 정책과 10 USDT 매수 진입 한도 적용 |

조회 전용으로 선택하거나 주문 허용을 해제하려면 다음을 실행합니다.

```bash
# macOS
sh scripts/configure_live_keychain.sh read-only
```

```powershell
# Windows
.\scripts\configure_live_credentials.ps1 -Action read-only
```

**실제 주문 기능을 사용할 때만** 다음 `orders` 동작을 선택합니다. `LIVE` 확인에 더해
`LIVE ORDERS 10 USDT`를 정확히 입력해야 합니다. 이는 실제 계좌 주문을 허용하는 설정이며,
10 USDT는 매수 진입 사전 한도입니다. 최종 체결 금액의 보장이나 손실 한도를 뜻하지 않습니다.

```bash
# macOS — 실제 주문 허용을 선택하는 경우에만 실행
sh scripts/configure_live_keychain.sh orders
```

```powershell
# Windows — 실제 주문 허용을 선택하는 경우에만 실행
.\scripts\configure_live_credentials.ps1 -Action orders
```

### Spot Testnet 키 등록과 Testnet으로 복귀

실제 계좌 키 대신 **Spot Testnet에서 발급받은 키 한 쌍**을 사용합니다.

```bash
# macOS
sh scripts/configure_testnet_keychain.sh
```

```powershell
# Windows
.\scripts\configure_testnet_credentials.ps1 -Action set
.\scripts\configure_testnet_credentials.ps1 -Action check
```

각 도구가 요청하는 API Key와 Secret 숨김 입력을 완료하면 됩니다. macOS에서 재입력을
요청하면 같은 값을 다시 입력합니다. 처음 설정하는 PC는 프로필이 없으면 Testnet으로
실행됩니다. **이전에 Live 프로필을 선택한 PC는 Testnet 키 등록만으로 프로필이 바뀌지
않으므로**, 앱을 종료한 뒤 아래 명령도 실행합니다.

```bash
# macOS — Live 실행을 끄고 Testnet 프로필로 복귀
sh scripts/configure_live_keychain.sh disable
```

```powershell
# Windows — Live 실행을 끄고 Testnet 프로필로 복귀
.\scripts\configure_live_credentials.ps1 -Action disable
```

`disable`에서도 확인 문구 `LIVE`를 입력합니다. Live 키를 삭제하지 않고 실행 프로필만
Testnet으로 바꾸며, 다음 시작에는 별도로 저장한 Testnet 키가 필요합니다.

키를 재발급했다면 해당 환경의 등록 명령을 다시 실행해 Key와 Secret **둘 다** 교체합니다.
Live 키를 다시 `set`하면 조회 전용으로 돌아갑니다. 키가 다른 OS로 자동 동기화되거나 이
저장소에 포함되는 것은 아니므로 macOS와 Windows에서 각각 등록해야 합니다.

## Backend 설치와 기본 검증

Windows x64의 설치·검증·실행은 [Windows 호환성 안내](docs/windows-compatibility.md)를
따릅니다. 저장소 루트에서 `scripts/setup_windows_development.ps1`로 의존성을 설치하고,
`scripts/check_windows.ps1`로 주문 없는 회귀 검증을 실행한 뒤
`scripts/start_windows_development.ps1`로 앱을 시작합니다. `UI`의 `pnpm desktop:build`는
Windows에서 Python sidecar를 포함한 사용자별 NSIS 설치 프로그램을 만듭니다.
macOS와 Windows는 같은 실행 프로필과 거래·복구·종료 로직을 사용하며 OS별 자격 증명 저장소를
사용합니다. 현재 변경의 Windows 실제 실행·설치 검증 상태는 안내 문서에서 별도로 구분합니다.

Python 3.11 이상이 필요합니다.

```bash
cd backend
python3 -m pip install -e .
```

Testnet flag를 명시적으로 끈 상태에서 전체 fake/unit/integration suite를 먼저
실행합니다. 이 명령은 Testnet 주문을 보내지 않습니다.

```bash
cd backend
BINANCE_RUN_TESTNET=0 BINANCE_RUN_TESTNET_ORDERS=0 \
  BINANCE_RUN_PHASE13_PUBLIC_CASE2=0 \
  PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v
```

## Binance Spot Testnet 설정

이 절은 **backend Testnet 검증 스크립트**를 위한 환경 변수 설정입니다. 위 데스크톱
개발 실행의 키 등록 절차와는 별개이며, `pnpm desktop:dev`의 키는 OS 저장소에서 읽습니다.
검증 스크립트가 사용하는 환경 변수 이름은 다음과 같습니다. 이 저장소는 credential 값이나
`.env` 파일을 제공하지 않습니다.

| 환경 변수 | 용도 |
|---|---|
| `BINANCE_RUN_TESTNET` | `1`일 때만 credential 기반 Testnet suite를 허용 |
| `BINANCE_TESTNET_API_KEY` | Binance Spot Testnet 전용 API key |
| `BINANCE_TESTNET_API_SECRET` | Binance Spot Testnet 전용 API secret |
| `BINANCE_RUN_TESTNET_ORDERS` | `1`일 때만 Testnet 주문 mutation을 추가 허용 |
| `BINANCE_RUN_PHASE13_PUBLIC_CASE2` | `1`일 때만 Phase 13 public market Case 2 전용 target을 추가 허용 |
| `BINANCE_TESTNET_MAX_NOTIONAL` | 주문 opt-in 시 필수인 decision-price 기준 `0 < cap <= 100 USDT` BUY quote 진입 상한 |
| `BINANCE_TESTNET_BASELINE_HISTORY_PATH` | 같은 Testnet account의 이전 완료 주문이 있을 때만 사용하는 absolute verified-closed canonical history 경로 |

### Read-only 검증

`BINANCE_TESTNET_API_KEY`와 `BINANCE_TESTNET_API_SECRET`을 process environment에
미리 안전하게 주입한 뒤 실행합니다. 이 단계는 account, 네 Kline interval,
open order, recent execution, signed account commission과 signed account stream의
normalized contract를 실제 Testnet에서 확인합니다. 주문 mutation은 수행하지 않습니다.

```bash
cd backend
BINANCE_RUN_TESTNET=1 BINANCE_RUN_TESTNET_ORDERS=0 \
  BINANCE_RUN_PHASE13_PUBLIC_CASE2=0 \
  PYTHONPATH=src python3 -m unittest -v \
  tests.testnet.test_binance_testnet_read_only
```

### 로컬 fault injection

이 suite는 Binance network에 접속하지 않고 in-memory transport로 accepted-response
timeout, partial cumulative fill과 disconnect를 결정적으로 주입합니다. 아래 값은 skip
gate를 여는 테스트 전용 literal이며 실제 credential이 아닙니다. 현재 2개 테스트가
2/2 통과했고 외부 network와 주문 mutation은 모두 0회입니다.

```bash
cd backend
BINANCE_RUN_TESTNET=1 BINANCE_RUN_TESTNET_ORDERS=0 \
  BINANCE_RUN_PHASE13_PUBLIC_CASE2=0 \
  BINANCE_TESTNET_API_KEY=local-fixture-key \
  BINANCE_TESTNET_API_SECRET=local-fixture-secret \
  PYTHONPATH=src python3 -m unittest -v \
  tests.testnet.test_binance_testnet_fault_injection
```

### 소액 주문 검증

기본 suite와 read-only 검증이 모두 성공한 뒤에만 실행합니다. Testnet 계좌 잔액을
확인하고 `BINANCE_TESTNET_MAX_NOTIONAL`을 process environment에 안전하게 설정해야
합니다. 아래 명령은 별도의 주문 opt-in을 켜므로 실제 Spot Testnet BUY/SELL 주문을
발생시킬 수 있습니다. 이 상한은 BUY decision-price 사전 검사이므로 주문 직전 잔액과
Testnet 시세를 다시 확인해야 합니다. 가격 상승 뒤 STOP/recovery SELL의 평가액은 이
상한보다 클 수 있지만 authoritative Position 전량을 넘겨 매도할 수는 없습니다.

```bash
cd backend
BINANCE_RUN_TESTNET=1 BINANCE_RUN_TESTNET_ORDERS=1 \
  BINANCE_RUN_PHASE13_PUBLIC_CASE2=0 \
  BINANCE_TESTNET_MAX_NOTIONAL="$PHASE9_APPROVED_MAX_NOTIONAL" \
  PYTHONPATH=src python3 -m unittest -v \
  tests.testnet.test_binance_testnet_order_lifecycle
```

이 lifecycle harness는 production Testnet runtime, Controller, Gateway, pending journal,
history persistence와 public stop/force-sell 경로를 사용합니다. 다만 BUY trigger만 테스트
전용 private action seam으로 주입하므로 market event → strategy signal까지의 E2E
검증으로 해석하면 안 됩니다.

실제 주문 run의 `history.jsonl`과 pending sidecar는
`backend/.testnet-artifacts/phase9-order-lifecycle-*`에 보존됩니다. 성공 여부와 관계없이
실제 restart 검증 전에는 자동 삭제하지 않으며, 실패 예외에는 credential이 아닌 artifact
경로와 관찰한 client order ID가 붙습니다. 상태가 불명확하면 이 파일을 먼저 보존하고
같은 ID reconciliation 없이 lifecycle test를 새로 실행하지 마십시오.

같은 Testnet account에서 다음 actual suite를 실행할 때 이전 `bat-` 주문이 recent order에
남아 있으면 새 빈 artifact는 production unknown-order guard에 의해 거부됩니다. 이 경우에만
직전의 정상 종료 `history.jsonl` absolute path를
`BINANCE_TESTNET_BASELINE_HISTORY_PATH`로 전달하십시오. helper는 source pending 0,
domain replay Position 0, non-empty canonical history, 새 destination을 POST 전에 검증해
durable copy합니다. 열린 Position, 상대 경로, 기존 destination은 거부하며 production
startup reconciliation을 우회하지 않습니다.

lifecycle 성공과 artifact/pending 상태를 확인한 뒤에만 같은 승인 cap으로 cold-restart
suite를 별도로 실행합니다. 두 suite를 병렬 실행하지 마십시오.

```bash
cd backend
BINANCE_RUN_TESTNET=1 BINANCE_RUN_TESTNET_ORDERS=1 \
  BINANCE_RUN_PHASE13_PUBLIC_CASE2=0 \
  BINANCE_TESTNET_MAX_NOTIONAL="$PHASE9_APPROVED_MAX_NOTIONAL" \
  PYTHONPATH=src python3 -m unittest -v \
  tests.testnet.test_binance_testnet_cold_restart
```

`PHASE9_APPROVED_MAX_NOTIONAL`은 임의 기본값이 아니라 사용자가 숫자로 승인한 양수 USDT
BUY 진입 cap을 현재 shell session에 보존하는 예시 변수입니다. Production Testnet bootstrap은
Phase 9 historical target에도 절대 ceiling을 적용하므로 이 값은 `100`을 넘을 수 없습니다.

### Phase 13 public market Case 2

Phase 13 actual evidence는 위 Phase 9 private action seam을 재사용하지 않습니다. 주문 없는
전체 backend와 read-only Testnet 검증이 통과하고 fresh runtime의 Position, pending/UNKNOWN,
app-owned open order가 모두 `0`인 경우에만 아래 **단일 target 하나**를 직렬 실행합니다.
Test discovery 전체나 다른 lifecycle/cold-restart target과 함께 실행하지 마십시오.

```bash
cd backend
BINANCE_RUN_TESTNET=1 BINANCE_RUN_TESTNET_ORDERS=1 \
  BINANCE_RUN_PHASE13_PUBLIC_CASE2=1 \
  BINANCE_TESTNET_MAX_NOTIONAL=100 \
  PYTHONPATH=src python3 -m unittest -v \
  tests.testnet.test_phase13_public_market_case2
```

같은 Testnet account의 이전 `bat-` 주문이 recent order에 남아 있을 때만, 위 명령에
`BINANCE_TESTNET_BASELINE_HISTORY_PATH="$VERIFIED_CLOSED_HISTORY_PATH"`를 추가합니다.
변수는 non-empty absolute verified-closed history를 가리켜야 하며, clean account에서는
환경 변수 자체를 생략해야 합니다.

이 target은 public Kline부터 production strategy/order path를 관찰하며 signal threshold를
완화하거나 private Action을 호출하지 않습니다. 자연 Case 2 signal이 없으면 신규 주문 `0`을
재확인하고 secret-free `NO_SIGNAL` trace를 보존한 뒤 non-zero로 종료합니다. BUY가 체결된 경우에만
그 run이 만든 authoritative Position의 exact STOP/recovery SELL을 허용하고, 마지막 fresh read-only
runtime에서 Position, pending/UNKNOWN과 matching open order가 모두 `0`인지 다시 검증합니다.
Trace에는 normalized order/fill/Trade/UI identity만 기록하며 credential, signature, header, raw
request와 local session token은 기록하지 않습니다.

전용 flag가 `1`이면 legacy lifecycle/cold-restart 주문 suite는 상호 배타적으로 비활성화됩니다.
Production Controller는 intent당 제출 예산 `1`, thread-safe permission proxy는 순서가 고정된
`ETHUSDT CASE_C BUY 1회 → STOP SELL 1회`, REST adapter는 주문별 wire POST 1회만 허용합니다.
동일 artifact root의 owner-only process lease를 runtime/client 생성 전에 획득하므로 같은 target의
병렬 process는 network 전에 차단됩니다. 다만 이 lease는 다른 Testnet 도구나 외부 account client를
통제하지 않으므로 실행 중에는 Phase 9 target, 다른 자동화와 같은 account의 수동 주문을 모두
중지해야 합니다. Preexisting hardlink나 symlink leaf는 mode 변경이나 network 전에 거부합니다.

실패가 durable BUY 뒤 발생하면 제출 차단 전에 BUY 1건, pending/UNKNOWN 0건, authoritative
Position과 effective free ETH가 모두 정확히 증명된 경우에만 public STOP recovery를 한 번
완결합니다. 상태가 모호하거나 SELL permit이 이미 소비됐으면 새 SELL을 만들지 않습니다. 그 뒤
모든 경로에서 후속 제출과 scheduler를 닫고, fresh read-only 검증에서 관찰하지 못한 값은
`null`/`INCOMPLETE`로 보존한 sealed `FAILED` evidence를 남깁니다. 모든 actual artifact는 owner-only
임시 파일을 같은 directory에 fsync한 뒤 기존 destination을 덮지 않는 방식으로 원자 게시합니다.
주문 POST는 HTTP timestamp 오류에서도 재전송하지 않으며, 3xx 응답은 redirect를 따라가지 않고
credential/header/body를 다른 origin에 전달하지 않습니다.

복구 `SUCCESS/NOT_REQUIRED`는 guard와 durable Trade의 BUY/SELL client ID, first runtime과 모든
필수 값이 실제로 관찰된 fresh runtime의 zero exposure가 모두 일치할 때만 기록합니다. Fresh 확인이
불완전하면 각각 `FAILED/SKIPPED`로 낮춰 성공을 합성하지 않습니다.

2026-08-31 작업에서는 위 target 구현과 주문 없는 회귀 검증만 수행했습니다. Keychain credential
읽기, signed Testnet preflight와 Phase 13 Testnet 주문은 각각 `0`건이며, 세 동작은 사용자의 명시적
승인을 받은 뒤에만 실행합니다.

## Testnet reset과 복구 주의사항

Binance Spot Testnet은 대략 월 1회 사전 통지 없이 reset될 수 있습니다. Binance는
reset 때 Testnet API key는 보존된다고 안내하지만 주문과 잔액 데이터는 초기화될 수
있으므로, reset 뒤에는 read-only parity와 startup reconciliation을 다시 확인해야
합니다. 자세한 endpoint와 reset 정책은 [Binance Spot Testnet General Info](https://developers.binance.com/en/docs/products/spot/testnet/general-info)를
참조하십시오.

공식 WebSocket API connection은 최대 24시간 유효하므로 정상 운영 중에도 disconnect를
예상해야 합니다. disconnect 뒤 REST full reconciliation과 새 signed subscription이
완료되지 않으면 주문 command lock을 우회하지 마십시오.

account event는 WebSocket receive loop에서 application callback을 직접 실행하지 않고
bounded 단일 FIFO worker로 넘깁니다. event enqueue부터 callback 완료와 queue drain까지
`account_ready=false`이며 command/start/startup/reconnect와 주문 POST 직전 gate가 모두
닫힙니다. queue overflow나 consumer/worker 실패도 socket을 닫고 reconciliation을
요구하므로, 단순히 연결 flag만 보고 주문을 허용하지 마십시오.

startup 시 durable `PREPARED` pending record가 있으면 같은 client order ID를 먼저
조회합니다. 여러 번의 no-such-order 응답만으로 record를 삭제하거나 새 주문을 제출하지
않으며, open/recent result 또는 exact history execution으로 identity를 확정할 때만
정리합니다. reconnect는 첫 REST snapshot → 새 signed stream → 두 번째 REST snapshot으로
사이 gap을 닫고, 설명되지 않은 fill·잔액 감소·reset provenance는 fail closed 처리합니다.

Testnet reset 뒤 숫자 `orderId`가 재사용될 수 있으므로 숫자 ID만으로 durable 주문을
확정하지 않습니다. `(clientOrderId, orderId)` pair가 같고 terminal status, 누적 수량·금액,
평균가, 수수료·수수료 자산과 마지막 체결 시각까지 history와 정확히 일치해야 합니다.
다른 client ID가 같은 숫자 ID를 사용하거나 누적 execution이 달라지면 Position 적용과
pending 삭제 전에 reconciliation-required로 차단합니다.

pending sidecar v2는 `PREPARED` UPSERT와 `SUBMISSION_REJECTED_CONFIRMED` TRANSITION을
각각 file과 directory에 fsync합니다. typed `-1013`/재동기화 뒤의 `-1021`/`-1022`
거부는 후속 query보다 먼저
이 전이를 기록하며, 재시작은 durable 거부 전이와 네 번의 exact
`-2013`/`ORDER_NOT_VISIBLE`가 모두 확인된 경우에만 journal을 지우고 READY로 돌아갑니다.
일반 `PREPARED`와 legacy v1 UPSERT에는 이 정리 예외를 적용하지 않습니다.
history commit과 sidecar REMOVE는 별도 durable marker로 추적합니다. history 저장 뒤
REMOVE fsync가 실패하면 reconnect가 실제 sidecar를 다시 읽어 같은 ID를 확인하고 제거를
완료할 때까지 public command gate를 열지 않습니다.

signed WebSocket subscription의 비정상 ACK는 신뢰하지 않는 응답입니다. 공개 오류에는
검증된 정수 Binance code만 포함하며, 문자열·객체 값이나 요청 payload는 그대로 반사하지
않아 API secret 등 민감정보가 예외와 로그로 유출되지 않게 합니다.

현재 repository에서는 production adapter와 로컬 검증을 완료했고 2026-08-24 사용자
Testnet credential로 authenticated read-only와 10 USDT cap actual BUY/force-sell,
process 종료 뒤 recovered-position 전량 SELL 및 fresh Position 0 replay를 통과했습니다.
따라서 Phase 9는 완료입니다. 현재 가장 앞선 미완료 범위는 Phase 13이며, actual public
Testnet Case 2, visual SSIM, current local-only supply-chain evidence와 release provenance가
모두 닫히기 전까지 전체 판정은 `NO_GO`입니다.

구현 범위와 실제 검증 상태는
[통합 시스템 구현 로드맵](./INTEGRATED_SYSTEM_IMPLEMENTATION_ROADMAP.md)의 Phase 9와 §16에
기록되어 있습니다.
