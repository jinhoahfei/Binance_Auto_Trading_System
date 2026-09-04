# Phase 13 live readiness 판정 보고서

| 항목 | 값 |
|---|---|
| 판정일 | 2026-09-04 KST |
| 기준 revision | `e402673` 위 local-only 변경 작업트리; external evidence는 historical |
| 판정 | **NO_GO** |
| 실제 live 주문 | 0건, 계속 disabled |
| 보존 누적 Testnet 실행 | 고정 Keychain 조회 12건(2 item × secure child 6회), signed Testnet target 6회; 이전 actual `NO_SIGNAL` 1회와 최신 actual `FAILED` 1회. 이 Phase 13 secure runner에 귀속되는 submission attempt·order POST delegate·durable Trade는 누적 0건 |
| 최신 외부 실행(실행 시점 source) | Composite filter를 반영한 signed read-only `4/4` PASS. 조건부 actual은 durable BUY 전 reconciliation-required로 `FAILED`; app-attributable submission attempt·order POST delegate·BUY·STOP SELL·durable Trade 0건. Runtime Position/pending도 0이지만 fresh verification이 `INCOMPLETE`이므로 P13-04는 GAP. 이후 30초/trace와 first-cause/failure-evidence v2를 보강한 current tree는 external 미결속 |
| 24시간 soak | 사용자 결정으로 Phase 13에서 영구 제외; 실행·PASS 증거가 아님 |

## 1. 결론

이 보고서의 최신 외부 실행 판정과 재개 계약은 §10이 권위 기준이다. §8~9의 결과는
zero-mutation 이력으로 보존하지만 최신 preflight·actual PASS로 재사용하지 않는다.

Phase 13의 fail-closed 기반과 장애 복구 범위는 크게 보강됐지만, live readiness는 아직
승인할 수 없다. Configured-unbounded 세 상한·`REALIZED_ONLY`·`CANCEL_AND_LIQUIDATE`,
30분 EMA9/OLS production builder와 all-interval 원자 경계를 구현·검증했다. 이번 작업에서는
Testnet 신규 BUY의 `decision_price × final_submitted_quantity`를 configured cap과 absolute
`100 USDT`에 동시에 묶고, 세 exact opt-in, fresh `exchangeInfo`, 제출 직전 filter snapshot과
Binance server-time 기준 POST 시작 시각을 production adapter와 secret-free trace에 결속했다.
Builder 직접 회귀 `20/20`, 4H 경계 12개·UTC 자정 180개 유효 arrival permutation,
public `observe_kline` 기반 local Case 2 `9/9`과 production `BinanceSpotRESTClient`
memory-HTTP E2E `1/1`, 합계 `10/10`을 통과했다. Actual 전용 harness는 private Action 호출이나
threshold patch 없이 public start→market event→자연 CASE_C BUY→public stop recovery SELL만
허용하며, NO_SIGNAL도 sealed trace를 남긴 뒤 non-zero로 끝낸다. Communication checker는
`126 COMPLETE / 0 GAP`이며,
current-host Tauri picker의 selected→absolute UTF-8와 cancelled→`null` 계약도 `2/2` 통과했다.

후속 local-only 작업에서는 `TradingController`의 reconciliation 진입 origin을 여섯 개의
secret-free category와 `MISSING|EXACT|DUPLICATE|CONFLICT` latch 상태로 고정했다. Failure
finalizer의 새 writer는 schema v2 `first_cause`와 stable fresh verification stage를 기록하며,
baseline history·pending·manual-kill control은 source/copy directory descriptor를 함께 고정한 owner-only
임시 snapshot으로 격리해 fresh runtime이 보존 source를 변경하지 못하게 한다. Known-safe recovery
판정 뒤 submission permit은 모든 failure에서 닫고 atomic
seal이 terminal check와 active/reconciliation blocker를 같은 lock에서 결속해 terminal STOP recovery를
새 reconciliation blocker로 되열지 않는다. Active seal의 Context publication 예외도 stable secondary code로
기록하고 close·fresh·artifact seal은 계속한다. V2 validator는 active runtime status, 마지막
attempt보다 이른 fresh timestamp, status/blocker 모순, attempt 수보다 큰 runtime/verified durable
count, no-attempt nonzero Position/matching, serial owner pending 2건 이상, producer가 만들 수 없는
error 순서와 completed recovery에 붙은 publication error를 거부한다. Source/copy leaf의 same-inode
content 복원은 ctime/mtime으로, absent sidecar 생성·삭제는 pinned parent ctime으로 감지한다.
Actual Testnet factory는 explicit evidence clock을 REST `clock`·`result_clock`, permission proxy와
runtime/event stream에 동일 identity로 전달한다. 이 clock은 wall UTC anchor를 run 시작 시 한 번만
읽고 `monotonic_ns` 경과를 더해 OS wall 역행을 격리한다. REST 직후 top-level preflight 다섯
`observed_at`, permission/runtime/account/UI/final과 startup account source timestamp는 local run-scoped
축에서 `[started_at, completed_at]` 및 causal order를 지킨다. 반면 `submit_time_filter_evidence`,
Binance Kline, server-aligned order attempted/result와 fill/Trade는 별도 server/exchange 축이다. 두 축을
서로 대소 비교하지 않고 exchange 축 내부 fetch order·30초 freshness·attempt → fill → result/Trade
identity만 강제한다. `SUCCESS`와 `NO_SIGNAL`의 exchange 축 ±60초 coherent-shift 회귀가 통과했다.
[Binance Spot REST Timing security](https://developers.binance.com/en/docs/products/spot/rest-api#timing-security)의
signed `timestamp`·`serverTime`·`recvWindow` 계약과 같은 분리다. Wall-clock backward-regression 회귀는
trace의 local timestamp 범위를 보존하고 failure artifact seal을 검증한다.
V3는 run UUID·startup Account version에 결속한 `ACCOUNT_SNAPSHOT_APPLIED`를 preflight 전에
보존한다. 후속 account row는 source time을 단조 증가시키고 version을 엄격히 증가시키며
`ACCOUNT_UPDATED` event ID·publication time·aggregate version과 exact다. Producer는 startup maximum 이하
buffered/replay를 건너뛴고, validator는 transport publication의 전역 단조성·aggregate version 비감소·
order publication의 post-preflight 순서를 검증한다. SUCCESS에서 STOP SELL의 모든 safety fetch는
BUY terminal result 이후다. 공식 `transactTime`/`updateTime`/`time`이 없는 order success
payload는 local clock fallback 대신 `UNKNOWN` reconciliation으로 닫힌다.
이 구현은 Keychain, Binance network와 주문을
사용하지 않았고 최신 historical FAILED artifact의 원인을 사후 추정하거나 external 결과를
성공으로 바꾸지 않는다.

사용자에게 exact 세 범위를 승인받은 뒤 고정 Keychain credential을 memory-only로 읽어 signed
read-only preflight를 실행했다. 빈 baseline의 첫 startup은 기존 recent order 6건을 설명하지 못해
fail closed했고, Phase 9 verified-closed history 6줄을 결속한 두 번째 preflight는 `4/4` PASS했다.
같은 baseline의 actual target은 자연 Case C signal이 없어 180초 뒤 sealed `NO_SIGNAL`로 끝났으며,
order attempt/result/durable trade와 실제 Testnet 주문은 모두 `0`건이다. 따라서 actual
order/fill/History/UI success trace는 아직 없다. 실행 후 공식 `/myFilters` 예시의 non-empty
exchange/symbol filter를 evaluator 없이 무시하지 않도록 parser를 추가 fail-close했으므로 preserved
external 결과는 current-source release binding이 아니며 새 승인 뒤 read-only 재검증이 필요하다.
Current actual-browser visual은 SSIM `4/16` PASS,
`12/16` FAIL로 재검증돼 `NO_GO`이며, 별도 actual-browser axe는 color contrast를 포함해
`16/16 Violations 0`이다. Supply evidence는 868 components 중 version-matched
local metadata 선언 494개와 `NOASSERTION` 372개를 기록했지만 final notice·current advisory와
release binding이 끝나지 않아 `NO_GO`다. 따라서 Roadmap의 Phase 13 master와 live release는
계속 `[ ]`로 유지한다.

사용자가 영구 제외한 24시간 Testnet soak는 이 판정에서 실행하지 않았다.
Readiness schema v2에서는 `EXCLUDED / USER_SCOPE_EXCLUSION`으로 보존하며,
이 상태는 PASS가 아니고 다른 미완료 항목의 `NO_GO`를 바꾸지 않는다.

## 2. P13 작업별 판정

| 작업 | 상태 | 확인된 결과 | 남은 조건 |
|---|---|---|---|
| P13-01 위험 한도·kill switch | local implementation PASS / overall NO_GO | Versioned configured-unbounded `RiskPolicy` 세 상한을 strict domain·wire·UI 상태로 보존하고 `UNAVAILABLE`/version mismatch와 구분한다. Decimal budget wire는 current/reserved/projected exposure와 KST PnL을 계속 게시한다. Manual kill은 receipt fsync 뒤 same-ID query/cancel/terminal requery를 수행하고 reconnect에서 같은 ID를 재취소한다. Partial을 먼저 History/Position에 reconcile한 뒤 residual만 canonical STOP/recovery SELL한다. RECON activation, reconnect same-ID re-cancel, release fresh TOCTOU, cleanup-incomplete shutdown 차단, HTTP 202 pending/200 complete와 restart/provenance focused test 통과 | Public market event로 시작하는 실제 Phase 13 Testnet C&L trace 필요. Local suite 밖의 추가 actual timeout/5xx/persistence 주문은 자동 다음 작업이 아니라 별도 사용자 승인 대상. 전략 손절은 configured-unbounded 운영 risk cap을 대체하지 않음 |
| P13-02 intent·crash reconciliation | local PASS | 일반 runtime은 append-only/fsync lifecycle과 intent당 총 5회 budget을 유지한다. Phase 13 actual target은 별도로 intent당 1회, exact BUY 1회→STOP SELL 1회 permit, permit 비복구, cancel 금지와 주문 POST 재전송 금지를 강제한다. Policy-version replay, timeout/5xx/decode UNKNOWN same-ID query, confirmed rejection만 release, REMOVE/history 실패 fail-close 및 restart 회귀 통과. v3 `PREPARED`는 SUBMITTED fsync-before-POST provenance와 4회 exact absence 뒤 정리·gate 복구하고 legacy v1/v2는 fail closed하며 attempt audit을 보존 | 실제 production market signal과 결합된 Testnet trace 필요 |
| P13-03 watchdog·orphan | local PASS | launcher와 Python runtime identity 분리, parent가 독점 소유한 FD5의 kernel EOF liveness와 같은 iteration의 `ORPHANED`, 0600 no-follow single-link artifact, ownership ambiguity의 신규 BUY/relaunch 차단, 자동 kill/cancel/reorder 금지. Native startup이 stale `ACTIVE`/`ORPHANED`의 lock·exact identity·PID 부재를 검증해 state/PID/start UUID를 표시하고, 명시적 확인 뒤 같은 device/inode와 PID 부재를 재검증한 경우에만 `RELEASED` fsync 후 재시작 | 실제 live 승인과 무관하며 자동 process kill, order cancel·청산은 계속 금지. 시간 기반 heartbeat 대신 parent process 수명에 결합된 FD EOF를 liveness 신호로 사용 |
| P13-04 market-event E2E | local/production-memory/harness/cause instrumentation PASS / preserved `NO_SIGNAL` / latest read-only PASS·actual FAILED | 공식 15개 symbol type·4개 exchange count·MAX_ASSET strict composite, account-wide open order/list empty와 trace v3를 구현했다. Controller는 최초 reconciliation origin을 stable category와 monotonic latch로 보존하고 failure writer v2는 `first_cause`, stable fresh stage, account-wide empty truth와 source/copy descriptor-bound isolated durability snapshot을 exact schema로 봉인한다. V2 error의 producer 순서, no-attempt zero truth, serial pending cap과 leaf/parent ABA도 executable negative contract로 검증한다. Preserved v1 FAILED와 v2 NO_SIGNAL은 각 원래 계약으로 계속 검증한다. 최신 signed read-only는 `4/4` PASS했지만 조건부 actual은 durable BUY 전 reconciliation-required로 종료했으며 새 instrumentation과 external로 결속되지 않았다. Submission attempt·BUY·STOP SELL·run Trade는 0이고 Position/pending도 runtime에서 0이다 | 새 external 실행은 세 범위의 새 명시 승인 뒤에만 가능하며, natural public Case C BUY→STOP SELL·History/Performance/UI publication·fresh zero exposure가 모두 필요. Historical FAILED의 first cause나 fresh stage는 v2로 소급 추정하지 않음 |
| P13-05 fault·replay | offline PASS | 7개 category, 25개 canonical scenario를 10회 replay해 digest `a5f17e96f60e825faf4ccf89e0788133f8bfcac6a3654504a4f16c5c2f2f70f2` 일치 | offline contract replay이며 실제 Testnet market→order E2E를 대신하지 않음 |
| P13-06 Communication·UI | Communication·a11y PASS / visual SSIM NO_GO / overall NO_GO | Manifest/checker `126 COMPLETE / 0 GAP`, current-host production native picker selected/cancelled `2/2`, 16-state reference manifest, 독립 axe 구조 검사와 actual-browser addon-a11y `16/16 Violations 0` 통과 | Fresh SSIM은 `4/16` PASS·`12/16` FAIL이므로 visual pixel gate 미완료 |
| P13-07 통합 실행기 | 실행 완료 / NO_GO | `check_all.sh`가 backend/script/contract/replay/no-order preflight/UI/Rust/secret/offline supply/visual/trace/readiness를 모두 실행했다. 기능·Communication gate는 PASS했고 hostile order env 제거와 OSV 외부 전송 금지를 유지 | Supply binding·offline vulnerability/license·visual·readiness가 차단돼 aggregate exit `1`. Soak는 영구 제외이며 PASS가 아님 |
| P13-08 live 판정 | **NO_GO** | default disabled, Testnet/live endpoint와 order opt-in 분리, secret scan 통과 | 위 NO_GO/GAP 전체 해소와 별도 사용자 live 승인 필요 |

## 3. 검증 결과

| 범주 | 실행 결과 |
|---|---|
| Backend 전체 | current tree `Ran 962 tests`, `OK (skipped=8)` (`PYTHONWARNINGS=error`; credential/order env 제거, local loopback only). 외부 Testnet 8개는 safe skip했고 Binance/Keychain/order target은 0회다 |
| UI | Vitest `41` files, `373/373` PASS; TypeScript `tsc -b` PASS; Vite production build `286` modules PASS |
| Rust/Tauri | 기본 suite `40/40` PASS, current-host production native picker selected/cancelled harness `2/2` PASS; `cargo fmt --check`와 `cargo clippy -- -D warnings` PASS |
| Release/root scripts | 최신 tree `183/183` PASS (`PYTHONWARNINGS=error`) |
| Private license·supply 집중 | Exact lockfile `868` components를 byte-bind했고 version-matched local metadata 선언 `494`, 미관찰 `372`는 `NOASSERTION`으로 보존. Project 자체 private/non-publish 선언은 완료했지만 final notice·current advisory/raw license output·current release binding이 없어 supply는 `NO_GO` |
| Offline OSV local-only | OS network deny와 scanner `--offline`을 함께 강제하는 wrapper만 실행했다. 외부 전송은 없었고 local DB 부재로 vulnerability·license가 각각 exit `127`이어서 fail closed `NO_GO` |
| UI contract | generated TypeScript contract drift 0 |
| P13-01 집중 | C&L core `79/79`, manual-kill transport `12/12`, risk-budget transport `9/9`, UI mapper·panel·machine `129/129` PASS |
| Deterministic replay | 25 scenarios, repeat 10, canonical digest 일치 |
| Market/REGIME reconciliation 집중 | `8/8` PASS |
| Reconciliation cause·failure evidence v2 | 관련 integration `50/50`, Case 2 module `Ran 30 tests`, `OK (skipped=1)`로 여섯 cause category와 monotonic `MISSING\|EXACT\|DUPLICATE\|CONFLICT` latch, v2 raw logical ID 비기록, exception-bound frozen/current snapshot race, v1/v2 schema dispatch, stage/reason·timestamp·truth 결속, REST 최종 재조회와 descriptor-bound isolated durability snapshot을 검증했다. External actual 1개는 safe skip했고 Testnet은 실행하지 않음 |
| Registry coverage | Regime `13/13`, supported TYPE_0 Trading transition `109/109` exact ID coverage PASS |
| Communication checker | checker unit `17/17` PASS; matrix `126 COMPLETE / 0 GAP`, checker exit `0` |
| 16-state UI 집중 | Figma baseline manifest 무결성 `2/2`, axe WCAG A/AA 독립 scanner `16/16`, calendar/progress 포함 UI 집중 PASS. Actual Storybook addon-a11y 재실행도 layout 기반 color contrast를 포함해 `16/16 Violations 0` |
| Actual browser visual | 최종 source를 1440×1024·DPR1·explicit clip로 임시 fresh JPEG 16개에 캡처했다. SSIM 범위 `0.915904~0.981311`, `4/16` PASS·`12/16` FAIL로 visual gate `NO_GO`; 실패 상태이므로 repository current capture/manifest, threshold `0.980000`과 baseline은 변경하지 않음 |
| Secret scan | credential canary 2개가 2,670개 파일에서 모두 absent |
| 실제 Testnet read-only | 외부 실행 시점 source를 pinned verified baseline으로 1회 실행해 `4/4` PASS, all-client open 0·recent 6 exact baseline 일치, startup reconciliation과 signed/public stream READY를 확인했다. Credential는 고정 Keychain 2 item에서 memory-only로 전달됐고 order opt-in/cap은 없었다. 이후 보강한 current tree의 external binding으로 재사용하지 않는다 |
| Phase 13 actual 안전성 집중 | Current tree Backend `Ran 962 tests`, `OK (skipped=8)`, scripts `183/183`, secure runner `14/14`, cause 관련 integration `50/50`, Case 2 helper `29/29`·external actual `1` safe skip, convention `2/2`, Communication `126/126` PASS다. 직전 source의 exact actual 1회는 durable BUY 전 reconciliation-required로 `FAILED`; app-attributable submission attempt·order POST delegate·BUY·SELL·durable Trade 0, preserved v1 failure evidence canonical PASS, fresh verification `INCOMPLETE`이다 |
| `check_all.sh` 집계 | 마지막 전체 aggregate는 보강 전 tree의 Backend `901`, scripts `169`, UI `373`, Rust `40`, public Case 2 `10`, Communication `126`과 secret scan을 통과했지만 supply·visual·readiness 때문에 exit `1`이었다. 최신 actual failure 뒤에는 final aggregate를 재실행하지 않았으며, current local 회귀는 별도로 Backend `Ran 962 tests`, `OK (skipped=8)`, scripts `183/183` PASS다 |

### 3.1 재현 명령과 주요 파일

기준 revision은 `d9a27afc23f5524c4699f41abaed68030e79672a`이며, 아래 명령은 그 revision 위
현재 변경 작업트리에서 실행했다. 최신 backend/scripts 명령은 Testnet credential, cap, 세 실행
opt-in과 baseline path/FD/SHA를 명시적으로 제거하고 `PYTHONWARNINGS=error`를 강제한다. Backend
transport test에는 local loopback만 허용했으며 Binance external network는 opt-in 부재로 열리지
않았다. 과거 retained Testnet read-only 결과는 보존하지만 current-tree signed preflight로
재사용하지 않는다.

```text
(cd backend && env \
  -u BINANCE_TESTNET_API_KEY -u BINANCE_TESTNET_API_SECRET \
  -u BINANCE_RUN_TESTNET -u BINANCE_RUN_TESTNET_ORDERS \
  -u BINANCE_RUN_PHASE13_PUBLIC_CASE2 -u BINANCE_TESTNET_MAX_NOTIONAL \
  -u BINANCE_TESTNET_BASELINE_HISTORY_PATH -u BINANCE_TESTNET_BASELINE_HISTORY_FD \
  -u BINANCE_TESTNET_BASELINE_HISTORY_SHA256 -u BINANCE_TESTNET_BASELINE_PENDING_FD \
  -u BINANCE_TESTNET_BASELINE_PENDING_SHA256 \
  PYTHONWARNINGS=error PYTHONPATH=src \
  .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q)
env \
  -u BINANCE_TESTNET_API_KEY -u BINANCE_TESTNET_API_SECRET \
  -u BINANCE_RUN_TESTNET -u BINANCE_RUN_TESTNET_ORDERS \
  -u BINANCE_RUN_PHASE13_PUBLIC_CASE2 -u BINANCE_TESTNET_MAX_NOTIONAL \
  -u BINANCE_TESTNET_BASELINE_HISTORY_PATH -u BINANCE_TESTNET_BASELINE_HISTORY_FD \
  -u BINANCE_TESTNET_BASELINE_HISTORY_SHA256 -u BINANCE_TESTNET_BASELINE_PENDING_FD \
  -u BINANCE_TESTNET_BASELINE_PENDING_SHA256 \
  PYTHONWARNINGS=error PYTHONPATH=. \
  backend/.venv/bin/python -m unittest discover -s scripts -p 'test_*.py' -q
cd UI && ./node_modules/.bin/vitest run --maxWorkers=1
cd UI && ./node_modules/.bin/tsc -b --pretty false
cd UI && ./node_modules/.bin/vite build
cd UI/apps/desktop/src-tauri && cargo test --locked --offline
cd UI/apps/desktop/src-tauri && cargo fmt --check
cd UI/apps/desktop/src-tauri && cargo clippy --locked --offline --all-targets -- -D warnings
cd UI/apps/desktop/src-tauri && cargo run --locked --offline --features native-picker-smoke --bin native-picker-current-host-smoke -- selected
cd UI/apps/desktop/src-tauri && cargo run --locked --offline --features native-picker-smoke --bin native-picker-current-host-smoke -- cancelled
PYTHONPATH=backend/src:. backend/.venv/bin/python scripts/phase13_deterministic_replay.py backend/tests/fixtures/phase13/canonical_fault_trace.json --repeat 10 --quiet
PYTHONPATH=. backend/.venv/bin/python scripts/check_communication_traceability.py
PYTHONPATH=. backend/.venv/bin/python scripts/check_phase13_visual_regression.py
/bin/sh scripts/check_all.sh
```

주요 구현·증거 파일은 `domain/trading/risk.py`, `domain/market/ema_slope.py`,
`application/trading_controller.py`, `application/market_data_controller.py`,
`application/market_evaluation_builder.py`, `adapters/binance/spot_rest_client.py`,
`adapters/persistence/trade_history_repository.py`, `transport/app.py`, Tauri `sidecar.rs`,
`dialog.rs`와 `bin/native_picker_current_host_smoke.rs`, UI `TraderPanel.tsx`,
`tests/integration/test_market_all_interval_boundary_flow.py`,
`tests/integration/test_public_market_case2_flow.py`,
`tests/integration/test_public_market_spot_rest_case2_flow.py`,
`tests/testnet/test_phase13_public_market_case2.py`, `tests/testnet/_phase13_trace.py`,
`scripts/check_all.sh`,
`scripts/phase13_deterministic_replay.py`, `communication_traceability.json`,
`phase13_supply_chain_evidence.json`, UI `visual-regression/baseline_manifest.json`과
`comparison_policy.json`, `current_capture_manifest.json`, `visual-regression/current/`의 실제 JPEG,
`FigmaFrameHarness.a11y.test.tsx`이다.

빈 history로 첫 read-only startup을 시도했을 때 Testnet account의 과거 `bat-` execution을 설명할
수 없어 `RECONCILIATION_REQUIRED`로 차단됐다. Phase 9의 pending 0·Position 0 canonical closed
history를 검증·복제한 뒤에만 READY가 됐다. unknown app order guard는 완화하지 않았다.

## 4. Communication 완료

- Case 1 `1L.3`: builder 직접 회귀 `20/20`과 4H 12개·UTC 자정 180개
  all-interval atomic boundary로 same-version provenance를 검증했다.
- Case 2 `1`~`14`의 24개 메시지: public `observe_kline` 기반 immediate/partial/
  UNKNOWN/failure/SELL/STOP 여섯 흐름과 production Spot REST memory-HTTP E2E를
  private Action seam 없이 검증했다.
- Case 4 `2.1.2.1`: current-host Tauri harness가 production picker의
  selected→absolute UTF-8와 cancelled→`null` terminal 계약을 직접 검증했다.
- `communication_traceability.json`과 checker 결과는 `126 COMPLETE / 0 GAP`, exit `0`이다.

이 완료는 actual Phase 13 Testnet 주문 또는 visual SSIM readiness 완료를 의미하지 않는다.

## 5. Supply-chain 판정

2026-08-25 OSV 2.5.1 retained evidence의 세 lockfile SHA-256과 package 수는 2026-08-31
local inventory와 일치한다. 그러나 retained scan은 이후 추가한 현재 project manifest bytes를
결합하지 않으므로 전체 current state와 같다고 판정하지 않는다. `current_vs_scanned_match=false`이며,
아래 finding 수치는 current scan 결과가 아니라 해소되지 않은 historical NO_GO 입력이다.

- 868 packages, advisory record 18개, affected package 17개.
- Retained triage 당시 `aarch64-apple-darwin` graph에서 UNIC 0.9 계열 unmaintained advisory
  5개가 reachable했다.
- Retained triage 당시 published `tauri-utils 2.9.3`은 `urlpattern 0.3.0`을 통해 해당 UNIC graph를
  사용했으며 안전한 lockfile-only update가 없었다. 이 결론의 현재성은 새 raw scan 전까지 주장하지
  않지만 해소 증거도 없으므로 release gate는 계속 닫는다.
- Retained OSV summary의 과거 local package `UNKNOWN` 2개는 current project manifest 판정으로
  사용하지 않는다. 현재 inventory는 backend/UI/Rust `3/3`의 proprietary/`UNLICENSED` 선언과
  registry 배포 차단을 byte-bind하고 project 자체 `UNKNOWN` group을 0으로 만든다.
- PyInstaller와 `pyinstaller-hooks-contrib`의 `non-standard` group, 그리고
  `pyinstaller-hooks-contrib`의 `GPL-2.0` group 등 third-party license/notice 검토는 끝나지 않았다.
- Supply evidence schema v4는 현재 세 lockfile의 `10 + 412 + 446 = 868` package coordinate,
  CycloneDX 1.6 SBOM, coordinate-complete license inventory와 notice review를 raw byte hash로 결합한다.
  Version-matched local installed metadata에서 third-party 494개의 license 선언과 source hash를
  관찰했지만 법적 승인은 아니며, 나머지 372개는 `NOASSERTION`이다. Notice는
  `NOT RELEASE-READY`이다. Phase 12 local-fixed app content-tree와 DMG
  digest도 historical record로 보존했지만 current Phase 13 artifact/source provenance는 아니다.
  Current raw offline OSV/license output, 완전한 third-party metadata/final notice와 current release
  binding이 없어 `artifact_binding.complete=false`다.

Dependency·lockfile metadata를 외부 OSV 서비스로 전송하는 것은 영구 불허이므로 remote OSV
refresh를 수행하지 않는다. `scripts/run_phase13_offline_osv.py`만 실행 경계로 사용해 OS network
deny와 scanner offline cached DB를 함께 강제한다. 최신 advisory 확인 불가 또는 local cache 부재는
fail closed한다. 실제 local-only 실행도 vulnerability와 license가 local DB 부재로 각각 exit
`127`을 반환했다. 새 scan으로 finding이 해소됐다고 간주하지 않고 historical evidence와 현재
manifest mismatch를 함께 `NO_GO`로 유지한다.

프로젝트 자체의 license 범위는 비공개·개인용으로 확정했으며 배포 license를
부여하지 않는다. 이 결정은 제3자 dependency license·notice 의무를 없애지 않는다.
Python은 `LicenseRef-Proprietary`·`Private :: Do Not Upload`, npm은
`private: true`·`UNLICENSED`, Cargo는 `publish = false`·repository `LICENSE`를 선언했고,
root/backend proprietary notice와 README를 추가했다. Local inventory와 supply checker는
세 project manifest와 repository notice의 현재 byte를 결합한다. 따라서 project 정책
구현과 exact SBOM/scope binding은 완료됐지만 앞의 historical 두 third-party group,
third-party 494개 선언의 법적/텍스트 검토, 372개 `NOASSERTION`, current cached raw
advisory/license output, final notice와 current Phase 13
release artifact 공백 때문에 supply-chain은 계속 `NO_GO`다. 외부 OSV 전송 금지는
최신성·license 공백을 PASS로 바꾸지 않는다.

## 6. 확정된 사용자 결정과 잔여 검증

다음 항목은 확정됐다.

- `max_order_notional=None`, `max_position_notional=None`, `max_daily_loss=None`.
  이들은 configured-unbounded이며 `RISK_POLICY_UNAVAILABLE`과 다르다.
- `daily_loss_scope=REALIZED_ONLY`, `manual_kill_behavior=CANCEL_AND_LIQUIDATE`.
- 30분 EMA9, 첫 9개 확정봉 SMA seed, alpha 0.2, 최근 EMA9 6개 OLS,
  Decimal 정밀도 34와 최종 8자리 `ROUND_HALF_EVEN`. Raw slope는
  `raw_ols_slope / candidate_price * 100`, `%/30분봉`이며 actual/realtime/TP/1분 trailing은
  각각 실제 계산 입력 가격을 분모로 사용한다.
- Local gate 통과 후 Phase 13 Spot Testnet `ETHUSDT`, 각 신규 BUY decision notional
  `100 USDT` 이하, authoritative 보유 수량의 recovery SELL.
- 비공개·개인용 프로젝트를 세 ecosystem metadata·배포 차단·repository notice로 강제한다.
  제3자 의무는 별도이며, 24시간 soak는 영구 제외한다. Soak를 제외했다는 사실은 PASS가 아님.
- 외부 OSV 서비스 전송은 영구 불허다. OS network deny와 scanner offline cached DB만 허용하고,
  최신 advisory 확인 불가 또는 local cache 부재는 fail closed한다.

확정된 risk·manual-kill, 30분 builder, all-interval atomic boundary, public local Case 2,
production Spot REST memory-HTTP, Communication `126/126`과 actual native picker는 검증
증거를 갖춘다. 다만 실제 Phase 13 Testnet public path, 12개 current visual SSIM mismatch와
third-party supply evidence가 남아 있으므로
Phase 13은 `NO_GO`다. Live mode는
별도 release commit, signed checklist과 사용자 live 승인 전에 configuration/backend/UI
세 경계에서 계속 disabled다.

## 7. 2026-08-31 실행 blocker와 재개 계약

이 절은 사용자 승인 전 기록이다. 같은 날짜의 보존된 첫 실행은 §8, 최신 외부 실행과 현재 재개
계약은 §9가 우선한다.

### 7.1 이번 작업에서 닫은 안전 공백

- `BINANCE_RUN_TESTNET=1`, `BINANCE_RUN_TESTNET_ORDERS=1`,
  `BINANCE_RUN_PHASE13_PUBLIC_CASE2=1`의 세 exact opt-in이 동시에 있어야 actual target이 열린다.
  기본값, 누락과 알 수 없는 값은 모두 disabled로 수렴하며 `scripts/check_all.sh`는 세 번째 flag와
  credential/cap까지 child 환경에서 제거한다.
- 신규 BUY configured cap은 finite `0 < cap <= 100`만 허용한다. Production preparation이 fresh
  symbol filter로 만든 최종 수량에 immutable decision price를 곱해 configured cap과 absolute
  `100 USDT`를 다시 검사하고, 초과·filter/provenance drift는 journal과 REST POST 전에 차단한다.
  같은 run의 authoritative Position을 정리하는 recovery SELL에는 BUY cap을 잘못 적용하지 않는다.
- Actual trace는 preparation-time과 submit-time filter evidence를 구분하고
  `source event → fresh filter → server-aligned POST start → incremental fills → terminal result →
  durable Trade/Position/Performance/UI publication`의 동일 identity와 시간 인과관계를 검증한다.
  Actual artifact는 API key와 secret canary가 하나라도 검출되면 file 생성 전에 거부한다.
- Actual harness는 public command와 market/account event만 사용한다. `_execute_action`, threshold
  patch, Position/Trade·성공 상태 직접 주입은 금지하며, natural signal이 없으면 `NO_SIGNAL` sealed
  trace를 남기고 주문 0건을 확인한 뒤 실패한다.
- Production observer는 실제 `MarketDataController -> TradingController`의 `1L.1` Kline,
  `1L.2` evaluation과 effect 직전 `1L.3 SubmitOrder`를 같은 immutable evaluation/version으로
  기록한다. Bounded retention은 evaluation 단위로만 제거하며 chain이 없으면 주문 전에 실패한다.
- Actual target의 Controller intent budget은 `1`, permission proxy는 서로 다른 ID의 exact
  `ETHUSDT CASE_C BUY 1회 -> STOP SELL 1회`만 허용한다. Permit은 delegate 전에 소비하고
  예외/UNKNOWN에서도 되돌리지 않으며 두 번째 permit 뒤 제출을 영구 차단한다. Cancel은 모두
  delegate 전에 거부하고 REST 주문 timestamp 오류에도 POST를 다시 보내지 않는다.
- 성공 order trace는 BUY/SELL별 exact prefix, 최대 네 same-ID query branch, fill 적용과 durable
  suffix를 요구한다. Exchange order ID는 관찰 뒤 `None`으로 후퇴할 수 없고 terminal fill·durable
  suffix는 terminal result의 ID와 같아야 한다. 각 entry의 `context_version_before`는 직전 entry의
  `context_version_after`보다 작을 수 없어 겹치는 version 후퇴도 거부한다.
- Durable BUY 뒤 예외는 BUY 1건, SELL 0건, pending/UNKNOWN/reconciliation 0건, Position==BUY fill과
  effective free ETH가 모두 확인된 known-safe 상태에서만 block 전에 public STOP을 한 번 완결한다.
  모호하거나 상태가 바뀌면 새 SELL 0건으로 보존하고, 모든 경로에서 block·scheduler 실패·runtime
  close·fresh verification 뒤 stable reason의 atomic `FAILED` evidence를 seal하며 원 예외를 보존한다.
  Guard BUY/SELL client ID는 각각 durable Trade ID와 일치해야 하며, `SUCCESS/NOT_REQUIRED`는 first
  runtime과 모든 field가 concrete한 fresh snapshot의 zero exposure에 교차 결속한다. Fresh 확인이
  불완전하면 각각 `FAILED/SKIPPED`로 낮춘다.
- Runtime/client 전에 owner-only same-target process lease를 획득하고 symlink·parent/leaf inode race를
  거부한다. 이 advisory lease는 다른 도구나 account activity까지 잠그지 못하므로 actual run 동안
  legacy suite, 다른 Testnet 자동화와 동일 account 수동 주문은 모두 중지해야 한다. Preexisting
  hardlink는 `fchmod` 전에 type/owner/link count로 거부해 unrelated target의 bytes와 mode를 보존한다.
- REST 3xx는 redirect를 따르지 않아 API key/header/body를 다른 origin에 보내지 않는다. Actual
  artifact는 0600 same-directory temp, file fsync, hard-link no-clobber와 directory fsync 뒤 inode·bytes를
  재검증한다. Visual/supply readers도 parent dirfd `O_NOFOLLOW`, bounded regular-file read와
  post-read identity 검증을 사용하며 app digest는 64 MiB/file, 512 MiB total, 10,000 entries를 넘으면
  fail closed한다.

### 7.2 §8 승인 전 보존 snapshot — 당시 실행하지 않은 mutation

이 snapshot 기록 시점에는 Keychain service `com.binance-auto.trader.testnet`의 API key/secret을
읽어 signed read-only preflight에 사용하는 단계가 명시적 사용자 승인 필요 판정을 받아 실행되지
않았다. 따라서 당시 session의 credential 값 읽기·stdout 출력·signed Testnet request·주문은 모두
`0`건이다. 과거의
retained read-only `6/6`을 current preflight로 가장하지 않으며 actual Testnet 증거도 생성하지 않았다.

다음 세 범위를 사용자가 명시적으로 승인한 뒤에만 7.3을 재개한다.

1. macOS Keychain의 위 service에서 Testnet API key와 secret을 한 backend child memory로 읽기
2. 그 credential을 live가 아닌 코드에 고정된 Binance Spot Testnet HTTPS/WebSocket endpoint에만
   signed read-only preflight 용도로 전송하기
3. 모든 preflight가 통과한 같은 실행에서 `ETHUSDT` 신규 BUY decision notional 최대 `100 USDT`
   한 번과, 그 run이 만든 정확한 authoritative Position의 recovery SELL 한 번만 허용하기

Binance 계약은 추측하지 않고 공식 [Spot Test Network REST API](https://developers.binance.com/en/docs/products/spot/testnet/rest-api),
[Spot trading endpoints](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade),
[symbol filters](https://developers.binance.com/en/docs/products/spot/filters),
[User Data Stream](https://developers.binance.com/en/docs/products/spot/user-data-stream)과
[Testnet WebSocket API](https://developers.binance.com/en/docs/products/spot/testnet/web-socket-api),
[Testnet WebSocket streams](https://developers.binance.com/en/docs/products/spot/testnet/web-socket-streams)를
2026-08-31에 다시 확인했다.

### 7.3 승인 후 정확한 재개 순서

1. 세 주문 flag 중 order/public-case 두 개는 `0`으로 둔 채 current signed read-only suite만 실행한다.
   Account `canTrade is True`, commission, fresh `ETHUSDT` filters, balances, open/recent orders,
   signed account stream과 public Kline stream READY를 모두 확인한다.
2. Verified closed baseline, fresh Position/pending/UNKNOWN/run-owned open order `0`과 설명되지 않은
   balance/order 부재를 확인한다. 하나라도 불명확하면 `RECONCILIATION_REQUIRED`로 중단한다.
3. Phase 9 lifecycle/cold-restart, 다른 Testnet 자동화와 동일 account의 수동 주문이 모두 중지됐음을
   확인한다. 같은 source와 fresh runtime에서만 세 opt-in과 cap `100`을 켜
   `backend.tests.testnet.test_phase13_public_market_case2`의 단일 actual test만 직렬 실행한다.
4. 자연 signal이 나온 경우에만 BUY 한 번과 정확한 recovery SELL 한 번을 허용한다. NO_SIGNAL,
   UNKNOWN, persistence 불명확, filter drift 또는 final zero-state 불일치는 추가 주문 없이 중단한다.
5. Sealed trace, exchange order/fill, History/Performance/UI transport와 final Position/pending/open order
   `0`이 모두 일치할 때만 actual P13-04를 PASS로 바꾼다. 추가 fault 주문이나 재시도는 별도 승인 대상이다.

Visual은 actual-browser addon-a11y `16/16 Violations 0`이나 SSIM `4/16`이므로 계속 `NO_GO`다.
Supply도 local cached database 부재로 vulnerability/license wrapper가 각각 exit `127`이고,
third-party final notice·current artifact binding이 미완료이므로 계속 `NO_GO`다. 이 두 blocker와
actual Testnet 증거가 모두 닫히기 전에는 Phase 13 master 또는 live release를 완료 처리하지 않는다.

최종 주문 없는 `/bin/sh scripts/check_all.sh`는 기능, UI, Rust, Communication과 secret scan을
통과했고 supply evidence binding, offline vulnerability/license, visual `12/16` 실패와 generic
readiness GAP을 모두 숨기지 않고 실행한 뒤 exit `1`을 반환했다. 이 결과는 runner 실패가 아니라
현재 `NO_GO`를 올바르게 집계한 것이며, blocker를 제거하거나 PASS로 바꾸지 않는다.

실제 timeout/5xx/persistence fault를 exchange 주문으로 추가 재현하는 작업은 현재 자동 재개 범위가
아니다. Local deterministic fault suite와 fail-closed 구현은 보존하되, 추가 actual mutation이
필요하면 예상 주문 수와 최대 노출을 제시하고 별도 사용자 승인을 받아야 한다.

## 8. 2026-08-31 승인 후 실행 결과 — 보존된 이전 판정

사용자는 §7.2의 세 범위를 모두 명시 승인했다. 실행은 코드에 고정된 Spot Testnet과 macOS
Keychain service `com.binance-auto.trader.testnet`의 `api-key`/`api-secret`만 사용하는
`scripts/run_testnet_from_keychain.py`로 제한했다. Credential 값·길이·부분문자열은 argv, stdout,
artifact와 예외에 기록하지 않았고 live endpoint·live 주문·24시간 soak·외부 OSV 전송은 실행하지
않았다. Secure child는 세 번 시작됐으므로 고정 Keychain item lookup은 `2 × 3 = 6`건이다. 개별
REST/WebSocket wire call 수는 credential-safe runner가 payload logging을 하지 않으므로 별도 수치로
주장하지 않으며, signed external target 실행 횟수와 mutation 수만 아래와 같이 exact 기록한다.

1. Baseline 없이 signed read-only target을 1회 실행했다. `open_order_count=0`,
   `recent_order_count=6`을 확인했지만 빈 local history가 기존 app-owned execution 6건을 설명하지
   못해 startup reconciliation이 fail closed했다. `Ran 4`, error 1이며 주문 mutation은 0건이다.
2. Regular file인 Phase 9 closed history
   `backend/.testnet-artifacts/phase9-cold-restart-20260824T094110991260Z-edea774ca29f43ffbddb37fc88205b38/history.jsonl`
   6줄, SHA-256 `7553c789cea3b536f573176661c50d9129e1584416d17f550c51cc452b072b34`를
   baseline으로 결속했다. 두 번째 signed read-only target은 `4/4` PASS했고 다시
   `open_order_count=0`, `recent_order_count=6`을 확인했다.
3. 동일 baseline, exact 세 opt-in과 `BINANCE_TESTNET_MAX_NOTIONAL=100`으로 actual target을 정확히
   1회 실행했다. 180초 동안 자연 Lower-BB Case C signal이 없어서 helper 16개 통과 뒤 actual 1건은
   의도된 non-zero `NO_SIGNAL`로 종료했다. Private seam, threshold patch, 재시도와 주문 제출은
   없었다.
4. Preserved artifact는
   `backend/.testnet-artifacts/phase13-public-case2-20260831T065832120542Z-4cb81b390000444fa09861215588317e/phase13-public-case2-trace.json`이다.
   Schema v2 canonical body digest는
   `621c99c8e5cd71d5c86b64f4bc00efa4d77cadb6d779962645c1f1749bb60203`, digest field까지
   포함한 file SHA-256은 `31880863a3244f2189af9b0446d3bbfd5df39c231836dc0baeeaf8dbf668dcba`다.
   Trace validator가 digest와 exact schema를 재검증했다.
5. Trace의 order attempt/result/durable trade/submit-time filter evidence는 각각 0건이다. Fresh final
   state는 actual order, duplicate order/trade, pending, unknown, matching open order와 Position이
   모두 0이며 recovery는 `NOT_REQUIRED`다. 기존 history 6줄은 baseline으로만 남았다.
6. 실행 뒤 공식 `/myFilters` 응답 예시를 재감사해 non-empty `exchangeFilters`와 `symbolFilters`도
   account에 relevant한 제약임을 확인했다. 해당 filter type의 exact evaluator가 없는 현재 source는
   이 두 collection이 비어 있지 않으면 fail closed하도록 보강했고 local regression만 다시 실행했다.
   따라서 앞의 external 결과는 안전한 zero-mutation 기록으로 보존하지만 current-source release
   evidence로 승격하지 않는다.

P13-04는 actual Testnet `SUCCESS`가 아니라 `NO_SIGNAL`이므로 계속 GAP이고 Phase 13/live 판정은
`NO_GO`다. §7에서 받은 one-shot mutation 승인은 이 1회 실행으로 소진했으며 자동 재시도하지 않는다.
다른 시각에 다시 실행하려면 Keychain memory-only 조회, 고정 Testnet signed read-only preflight와
최대 `100 USDT` BUY 1회 및 same-run STOP SELL 1회의 예상 범위를 다시 제시하고 새 명시 승인을
받아야 한다. 승인 뒤에도 read-only부터 반복하며 `NO_SIGNAL/BLOCKED/UNKNOWN/FAILED`이면 즉시
중단한다.

§16.14 stop condition에 따라 이번 `NO_SIGNAL` 뒤 visual 수정, supply/license 작업과
`scripts/check_all.sh` 최종 집계는 시작하지 않았다. Visual은 fresh `4/16` PASS·`12/16` FAIL,
addon-a11y `16/16 Violations 0`, supply는 local DB·완전한 third-party license/final notice와 current
artifact binding 부재 상태를 그대로 보존한다. Live는 configuration/backend/UI 모두 disabled다.

## 9. 2026-08-31 실행 시점 signed preflight와 post-run hardening — 최신 판정

이 절은 §8 이후 진전된 실행 시점 source로 Roadmap §16.15.6을 집행한 결과이며, 이 보고서의 최신
외부 상태와 재개 계약이다. 실행 뒤 all-client recent identity 결속과 failure output redaction을
local-only로 더 보강했으므로 아래 external 결과를 현재 작업트리의 release evidence로 승격하지
않는다. 사용자는 ① 고정 Keychain Testnet credential 두 item의 memory-only
조회, ② 코드에 고정된 Binance Spot Testnet signed read-only preflight, ③ preflight 통과 시
`ETHUSDT` 최대 `100 USDT` 신규 BUY 1회와 same-run exact Position STOP SELL 1회를 승인했다.

### 9.1 외부 실행 결과와 mutation 수

Verified Phase 9 baseline을 descriptor와 digest로 pin한 뒤 secure runner `read-only` mode를 정확히
한 번 실행했다. 고정 Keychain item lookup은 `2`건, signed external target은 `1`회다. Credential
값·길이·부분문자열은 argv, stdout, artifact와 예외에 기록하지 않았다. Read-only child의
order-related opt-in 두 개는 `0`이고 cap은 unset이었다.

§8의 보존 실행까지 합친 누적은 secure child `4`회, 고정 Keychain lookup `8`건과 signed external
target `4`회다. 이전 actual을 포함한 누적 주문 mutation도 `0`건이다.

첫 test `test_account_kline_and_order_queries_use_normalized_contracts`가 signed
`GET /api/v3/myFilters` 응답의 non-empty `symbolFilters`를 현재 strict evaluator로 증명할 수 없어
`BinancePayloadError: symbolFilters contains unsupported relevant filters`로 fail closed했다. 결과는
`Ran 1 test`, error `1`, 약 `0.258s`, exit `1`이다. Failfast 때문에 all-client
`openOrders`/`allOrders`, startup reconciliation과 signed/public stream READY에는 도달하지 않았다.
따라서 최신 target의 open/recent count와 stream READY를 주장하지 않는다.

Preflight가 통과하지 않았으므로 조건부 actual child `0`회, 신규 BUY `0`건, STOP SELL `0`건,
order attempt/result/durable Trade `0`건, 새 actual artifact `0`개다. 같은 승인으로 external target을
재시도하지 않았다. Live endpoint·live credential·live 주문·24시간 soak·외부 OSV 전송도 0건이다.

### 9.2 post-run current-tree 안전 경계와 local 증거

- Baseline history는
  `backend/.testnet-artifacts/phase9-cold-restart-20260824T094110991260Z-edea774ca29f43ffbddb37fc88205b38/history.jsonl`,
  6줄, SHA-256 `7553c789cea3b536f573176661c50d9129e1584416d17f550c51cc452b072b34`,
  mode `0600`이다.
- Pending journal은 같은 directory의 `history.jsonl.pending-orders.jsonl`, 4줄, SHA-256
  `61a3545a829525eadbaccdd7c6ba56afc186e8b3a3db0c636f9f79bbb6c4e265`, mode `0600`이다.
  Semantic replay는 active pending `0`과 `ETHUSDT` Position `0`을 확인했다.
- Runner는 두 file을 Keychain 조회 전에 owner/type/link/path/inode/pre-post stat/SHA로 검증하고
  `O_NOFOLLOW` FD로 pin한다. Child에는 path가 아니라 inherited FD와 digest만 전달한다.
- Helper suite가 actual suite보다 먼저 실행되며 unittest failfast가 helper/preflight 실패 뒤 actual을
  막는다. Account isolation은 client ID prefix에 제한하지 않고 모든 client의 signed symbol-scoped
  open orders와 recent orders `limit=1000`을 확인하도록 구현·unit test했다. Open은 empty여야 하고
  recent `(exchange order ID, client order ID)` exact set은 verified closed Trade set과 같아야 하며
  fresh baseline이면 recent도 empty여야 한다. Missing/duplicate/client mismatch/manual extra는
  mutation 전에 고정 문장으로 막고 failure output에 ID·fill·가격·domain repr를 반사하지 않는다.
  Production canary는 regime/split/start와 direct submit/sell-all/cancel 여섯 surface가 모두 `0`회임을
  확인한다.
- `scripts/check_all.sh`는 `PYTHONWARNINGS=error`를 강제하고 credential, cap, 세 opt-in과 baseline
  path/FD/SHA를 모두 제거한다.
- 최신 local 검증은 backend `923/923` OK(외부 Testnet 8 safe skip), scripts `183/183`, secure runner
  `14/14`, 변경 범위 집중 회귀 `168` OK(외부 4 safe skip), shell syntax와 `git diff --check` PASS다. 변경
  Python의 class/function docstring, tab, 한국어 블록·문장 주석 검사도 PASS다.

공식 Binance filter 계약상 `MAX_ASSET`은 account-wide exposure가 아니라 단일 주문의 transaction
limit이다. Base asset에는 order quantity, quote asset에는 reference price를 적용한 order notional을
제한한다. `MAX_POSITION`은 별도로 base balance와 open BUY quantity를 포함한다. 따라서 이번
`symbolFilters`를 account-wide 값이라고 추측하지 않으며, 실제 filter type의 공식 schema·단위·side와
필요 state를 type별로 구현할 때까지 차단한다. 기준은 공식
[Spot Test Network REST API](https://developers.binance.com/en/docs/products/spot/testnet/rest-api),
[Spot REST API](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md),
[Spot filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md)와
[User Data Stream](https://developers.binance.com/en/docs/products/spot/user-data-stream)이다.

기존 schema v2 `NO_SIGNAL` trace는 canonical body digest
`621c99c8e5cd71d5c86b64f4bc00efa4d77cadb6d779962645c1f1749bb60203`, file SHA-256
`31880863a3244f2189af9b0446d3bbfd5df39c231836dc0baeeaf8dbf668dcba`, mode `0600`으로 재검증했다.
Attempt/result/run trade/Position/pending/unknown/open은 모두 `0`이다. 이는 이전 source의 보존된
zero-mutation 이력이며 최신 실패의 actual artifact나 current-tree SUCCESS가 아니다.

### 9.3 최신 readiness와 재개 조건

P13-04는 최신 외부 signed read-only 단계에서 `BLOCKED`이고 post-run current tree는 외부 미검증이며
actual SUCCESS가 없으므로 GAP이다.
Visual은 diagnostic `4/16` PASS·`12/16` FAIL이고 supply는 local advisory DB, 완전한 third-party
license/final notice와 current app·DMG/source binding이 없어 `NO_GO`다. 이번 stop condition 뒤
UI·supply를 수정하거나 final `scripts/check_all.sh` aggregate를 실행하지 않았다. Phase 13과 live는
계속 `NO_GO`/disabled다.

다음 session은 외부 동작 없이 공식 문서와 strict fixture를 사용해 relevant `symbolFilters`를
type별 DTO/evaluator로 먼저 구현한다. Count/position filter에 추가 account state가 필요하면 공식
signed read-only endpoint의 완전한 state를 요구하고, 알 수 없는 type·field·단위·side는 계속 fail
closed한다. 반환 type 확인이 필요하면 credential과 raw filter value를 남기지 않는 schema-only
diagnostic을 local에서 설계한다. Signed diagnostic 자체는 새 승인 전 실행하지 않는다.

Local strict/negative/TOCTOU/convention 회귀를 모두 통과한 뒤에만 사용자에게 Keychain memory-only,
고정 Testnet signed read-only, 그리고 preflight 통과 시 최대 `100 USDT` BUY 1회와 same-run STOP
SELL 1회의 세 범위를 다시 명시 승인받는다. 새 read-only는 account/filter/reference,
Position/pending/unknown 0, all-client open 0, all-client recent identity set의 verified Trade baseline
일치, reconciliation과 두 stream READY를 모두 통과해야 한다. 그 뒤에만 actual을 한 번 실행하며
`NO_SIGNAL/BLOCKED/UNKNOWN/FAILED`는
자동 재시도하지 않는다. Actual SUCCESS 뒤에는 backend/source provenance를 먼저 freeze한 경우에만
visual, local-only supply와 final no-order aggregate를 진행한다. 별도 live 승인 전 live는 disabled다.

## 10. 2026-08-31 composite-filter 구현 후 재실행 결과 — 최신 판정

이 절은 Roadmap §16.16.5를 새 명시 승인으로 실행한 결과이며 본 문서의 유일한
최신 외부 상태다. 외부 실행 시점 source는 공식 `/myFilters` union을 strict composite DTO로 정규화하고,
account-wide open order/list empty를 preflight와 각 prepare에서 signed read로 다시 증명하며,
trace v3에 세 scope·두 관찰 시각을 보존한다. 기존 v2 `NO_SIGNAL` artifact는 변경하지
않고 backward validator로 재검증했다.

외부 실행 직후 current tree에는 prepared composite evidence의 고정 `30초` expiry, clock regression
차단과 fingerprint 소비 뒤 transport의 order POST 직전 재검증이 local-only로 추가됐다. Trace v3의
`public_relevant_filters` overlap 및 같은 30초 인과 검증도 결정론 회귀로 완료됐다. 따라서 이
절의 외부 PASS/FAILED는 직전 source의 역사 evidence이며 current tree의 external binding이 아니다.

§9의 quote `MAX_ASSET` reference-notional 설명은 공식 근거가 부족했던 과거 판단이다. 공식 문서는
quantity-based MARKET 주문의 quote conversion price를 정의하지 않으므로 current source는 base
`MAX_ASSET`만 candidate quantity로 평가하고 quote presence는 fail closed한다. Reference price는
MARKET `MIN_NOTIONAL`/`NOTIONAL`에만 사용하며, 이 §10의 정정이 §9보다 우선한다.

공개 [Spot Filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md)의 평가 가능한
symbol filter는 `15`종이다. [Testnet schema lifecycle](https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/sbe/schemas/sbe_schema_lifecycle_testnet.json)의
최신 SBE `3:5`와 [spot_3_5.xml](https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/sbe/schemas/spot_3_5.xml)에만
enum으로 나타나고 공개 평가식이 없는 `T_PLUS_SELL`은 presence로 fail closed한다. Exchange-scope와
symbol-scope open-order count를 구분하며 plain MARKET은 두 `MAX_NUM_ORDERS` 계열에 `+1`,
algo·iceberg·order-list count에 `0`을 적용한다. `MAX_NUM_ORDER_AMENDS`는 account count가 아니라
개별 order amend 횟수이고 신규 MARKET의 amend delta는 `0`이다.

### 10.1 외부 실행과 exact mutation 결과

1. 고정 baseline history/pending를 owner/type/link/inode/digest로 pin한 secure runner
   `read-only`를 정확히 1회 실행했다. Keychain item lookup은 2건이고 signed target은
   1회이며 credential 값·길이·부분문자열을 argv/stdout/file/exception에 남기지 않았다.
   Order opt-in은 두 개 모두 `0`, cap key는 unset이었다.
2. Read-only는 `Ran 4 tests in 3.223s`, `OK`다. All-client `ETHUSDT` open order 0건,
   recent order 6건이 verified closed Trade 6건의 exact identity set과 일치했다. Account/commission,
   full relevant filter/reference, startup reconciliation, signed account stream과 public Kline stream READY를
   모두 통과했다.
3. 같은 source·baseline에서 secure runner `phase13-public-case2`를 정확히 1회 실행했다.
   추가 Keychain lookup은 2건이고 exact 세 opt-in과 cap `100`만 child에 주입했다. Helper 18개는
   먼저 통과했으며 actual 1개는 durable BUY 전 `reconciliation_required=true`가 되어
   `Ran 19 tests in 5.204s`, failure 1로 종료했다. 자동 재시도하지 않았다.
4. Sealed failure evidence의 submission attempt는 0개다. 따라서 이 application harness에 귀속되는
   REST order POST permit 소비·delegate, 신규 BUY·STOP SELL과 run durable Trade는 0건이다. Runtime
   snapshot은 local `ETHUSDT` Position 0, pending 0, durable Trade 0과 submissions blocked를 확인했다.
   Recovery는 `FAILURE_RECOVERY_STATE_AMBIGUOUS` 때문에 `SKIPPED`됐고 실제 매도 시도는 없다. 이는
   app-attributable mutation path가 시작되지 않았음만 증명하며 독립 외부 mutation 부재 증거는 아니다.
5. Failure finalizer의 별도 read-only fresh verification은 `FRESH_VERIFICATION_FAILED`로
   `INCOMPLETE`이다. 종료 후 local `ETHUSDT` Position·pending·unknown, account balance, all-symbol
   `openOrders`·`openOrderList`와 symbol-scoped `ETHUSDT allOrders` recent identity를 각각 fresh
   snapshot으로 재확인했다고 주장하지 않는다. 이는 P13-04를 `FAILED/GAP`으로 유지하는 최신 blocker다.

이번 session으로 보존 누적은 secure child 6회, 고정 Keychain item lookup 12건,
signed external target 6회이며 이 Phase 13 secure runner에 귀속되는 submission attempt/order POST
delegate/durable Trade는 누적 0건이다. Live endpoint/credential/order, 24시간 soak와 외부 OSV
전송은 실행하지 않았다.

### 10.2 보존 artifact와 local 검증

- Latest failure artifact:
  `backend/.testnet-artifacts/phase13-public-case2-20260831T095958280425Z-b2c3cd9008584a539acf71703050c743/phase13-public-case2-failed.json`
- Failure body SHA-256:
  `3a3fd9475099dfab5f5ef75397470d9d3fb2acb8591af2bbf96679db999ba029`
- Digest field를 포함한 file SHA-256:
  `c177ddd6f16d4a53285c42d962b97e47416de37cc39e436e0b61cd06519b6e90`
- File은 mode `0600`, owner UID `501`, link count `1`, 1,102 bytes이고 canonical bytes·body digest를
  offline validator가 재확인했다. Credential, raw filter/order payload과 ID·fill·가격은 없다.
- 같은 directory의 staged `history.jsonl`은 6줄, mode `0600`, SHA-256
  `7553c789cea3b536f573176661c50d9129e1584416d17f550c51cc452b072b34`로 verified
  baseline과 byte-for-byte 같고 새 pending sidecar는 생성되지 않았다. Process lease는
  nonblocking exclusive reacquire가 성공해 현재 해제 상태다.
- 다음 수치는 2026-09-04 추가 보강 전 확정한 historical local gate이며 최종 전체 회귀 수가 아니다.
  Credential/order env를 제거하고 `PYTHONWARNINGS=error`로 당시 tree의 Backend
  `Ran 962 tests`, `OK (skipped=8)`, scripts `183/183`, secure runner `14/14`, cause 관련
  integration `50/50`, Case 2 module `Ran 30 tests`, `OK (skipped=1)`, convention `2/2`,
  baseline/trace 계약 `33/33`, Communication checker unit `17/17`과 matrix
  `126 COMPLETE / 0 GAP`을 통과했다. Backend 전체는 local loopback만 허용하는 실행
  경계에서 검증했으며 Binance external network와 credential/order opt-in은 제거했다.
- 추가 보강 뒤 current Backend는 `Ran 967 tests in 32.817s`, `OK (skipped=8)`이고 trace/Case 2
  `58`(skip `1`), startup·reconciliation `74`, lifecycle `10`, cause integration `51`, runner `14`,
  baseline/trace `33`, public local Case 2 `9`, Communication `126/126`이 통과했다. Root scripts는
  ignored historical Phase 12 app만 남고 결속 DMG가 누락된 local supply 상태를 fail closed해
  `Ran 183 tests in 34.640s`, `FAILED (failures=1, errors=2)`다. Validator를 완화하거나 retained app을
  숨겨 PASS를 합성하지 않았으므로 supply와 최종 readiness는 계속 `NO_GO`다.
- Current writer는 failure evidence schema v2만 새로 쓴다. `first_cause`는 같은
  Controller session lock에서 읽은 `reconciliation_required`, latch `status`, `EXACT`일 때만 공개하는
  `category`를 담는다. Missing, 동일 원인의 중복과 서로 다른 원인의 경합은
  category를 숨기고 secondary error로 fail closed한다. Current bool은 해소될 수 있지만
  latch는 보존되므로 일반 account disconnect·app-prefix unknown과 market/prepare/order origin에는
  `reconciliation_required=false`, `status=EXACT`, non-null category 조합도 유효하다. Prefixless
  external execution은 worker wake와 direct reconnect를 금지하고 fresh process account-wide 검증만
  허용한다. `ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION` category 자체는 일반 account disconnect와
  app-prefix unknown도 함께 표현하므로 항상 영구적이지 않다. Event-runtime failure,
  process-ownership ambiguity와 별도의 prefixless-external flag만 process-lifetime blocker이며,
  이 세 flag는 concrete `false`를 허용하지 않는다. Startup reconciliation 입구, startup final commit
  직전 재검사와 direct start 모두 fresh process 전까지 pre-I/O 차단한다.
- Fresh verification은 `RUNTIME_CREATION`부터 `CLEANUP`까지 허용된 15개 stable
  stage 중 최초 실패를 고정한다. Source history·pending·
  manual-kill control은 filesystem root부터 final parent까지 component-wise no-follow로 연 `0700`
  source/copy ancestor FD chain과 `0600` isolated copy로 fresh runtime에 전달한다. Leaf ctime/mtime을
  포함한 before/after fingerprint나 chain identity/ctime 중 하나라도 변하면 `DURABILITY`로
  강등한다. Final leaf는 source → copy → source 순서로 다시 캡처하고, 그 뒤 chain을 검증해 leaf와
  ancestor rename/restore ABA를 모두 막는다. Destination/source ancestor close는 한쪽 실패 뒤에도
  전체 close를 계속하고 최초 cleanup error를 보존한다. Artifact publication도 file close·temporary
  unlink·directory close를 모두 best effort로 시도하며 최초 cleanup error를 유지한다. `VERIFIED`
  직전에 account-wide open order/list, symbol open result, recent baseline과 stream/
  reconciliation을 다시 읽어 REST 사이 TOCTOU를 닫는다. Ancestor ctime pin은 sibling 생성·삭제도
  fail closed하므로 actual execution 동안 source/destination evidence directory를 격리하고 sibling
  mutation을 금지한다. 변경 시 자동 재시도 없이 `DURABILITY/INCOMPLETE`로 종료한다.
- Actual Testnet factory는 explicit evidence clock을 REST `clock`·`result_clock`, permission proxy와
  runtime/event stream에 동일 identity로 전달한다. Run 시작의 단일 wall UTC anchor와 `monotonic_ns`
  경과가 REST 직후 다섯 preflight `observed_at`, permission/runtime/account/UI/final과 startup account
  source를 local `[started_at, completed_at]` 및 causal order에 결속한다. Wall-clock backward-regression
  회귀는 trace의 local timestamp 범위를 보존하고 failure artifact seal을 검증한다.
- `submit_time_filter_evidence`, Binance Kline, server-aligned order attempted/result와 fill/Trade는 local
  축과 cross-axis 비교하지 않는다. Server/exchange 축 내부 fetch order·30초 freshness·attempt → fill →
  result/Trade identity를 검증하며, `SUCCESS`와 `NO_SIGNAL`의 전체 축 ±60초 coherent shift도 통과한다.
  이는 [Binance Spot REST Timing security](https://developers.binance.com/en/docs/products/spot/rest-api#timing-security)의
  signed `timestamp`·`serverTime`·`recvWindow` 축과 일치한다.
- Account와 Market controller는 version·immutable state·source provenance를 한 번에 읽는 state
  pointer/snapshot을 제공한다. Startup account evidence는 application lock 안에서 state를 한 번 읽은
  뒤 evidence clock을 읽는 state → clock atomic capture다. Market observer도 같은 lock에서 state와
  evaluation context를 한 번만 freeze한다. `NO_SIGNAL` collector는 synthetic snapshot을 만들지 않고
  production `1L.1 / KLINE_OBSERVED` frozen event만 수집하며 captured market version으로만 cursor를
  전진시킨다.
- Current full trace schema v3는 exact `SUCCESS`와 `NO_SIGNAL`만 허용한다. Public Action 뒤 blocker,
  `BLOCKED`, `UNKNOWN` 또는 `FAILED`는 불완전한 full trace가 아니라 failure evidence schema v2로
  봉인한다. Preserved trace v2와 failure v1의 historical branch는 그대로 둔다.
- V3 startup Account version은 `1` 이상이고 source 시각은 다섯 preflight observation 중 첫 시각보다
  늦을 수 없다. Preflight Account version과 같은 public row가 있어야 하며 `SUCCESS`는 recovery
  effective free quantity 이상을 fingerprint version 또는 그 이후 Account row의 free quantity로
  증명한다. Startup 뒤 전진한 모든 `ACCOUNT_UPDATED`와 public account row는 exact subsequence로
  양방향 일치한다. V3 `NO_SIGNAL` market row는 실제 production `1L.1`, `TYPE_0`이고
  action/evaluation/side/strategy가 모두 null이어야 한다. `SUCCESS` evaluation ID
  `market:{market_version}:{source_event_id}`와 parsed source identity/time을 exact 결속한다. Action
  단일 source는 30m open/closed 또는 closed 1m, atomic source는 동일 UTC boundary의 exact 2/4/6개
  1m·30m 및 필요한 4h·1d closed/open batch다. `NO_SIGNAL` single 4h/1d도 허용하며 market version
  strict increase, context version nondecrease를 강제한다. 모든 V3 `NO_SIGNAL` `source_event_id`는
  trace 전체에서 유일해야 하며 version만 높인 duplicate source도 위조로 거부한다.
- V3 `SUCCESS`는 policy version `13`, 모든 regime `TYPE_0`, BUY/SELL 동일 configured cap과
  `bat-{sha256(session_id + NUL + intent_id)[:24]}-0` client order ID를 요구한다. SELL intent와 STOP
  evaluation, trace message 1의 `v→v`, message 2의 동일 before `v→v+1`, message 14의 exact client
  outcome을 producer identity에 결속한다. BUY message 2의 `context_version_after`는 immutable
  fingerprint와 `1L.3` context version에 exact 결속한다. 첫 비동기 message `8` 또는 `9` 전 모든
  non-final command ID는 evaluation ID이고, 그 경계부터 intent ID이며 final `14`는 outcome identity다.
  Message `9`는 독립·반복되거나 stream partial 뒤 scheduled query terminal로 이어질 수 있다. Same-ID
  query budget은 최대 4회지만 stream event 수에 임의의 4회 제한은 없고 전체 구조 상한은 `2048`이다.
  이는 공식
  [Binance User Data Stream](https://github.com/binance/binance-spot-api-docs/blob/master/user-data-stream.md)과
  [Binance Market Orders FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_orders_faq.md)의
  partial/terminal 계약에 맞춘다. Messages `1..6.1`의 exchange order ID는 null이고 concrete ID는
  `7` 또는 `9`에서만 처음 나타나며 이후 바뀌거나 null로 돌아갈 수 없다. Result와 durable Trade는
  extra `UNKNOWN` 없이 attempt와 zip된 BUY → SELL exact order이고 Trade ID는
  `trade-{exchange_order_id}`다. 각 result fill은
  `(event_time, canonical integer tradeId)` strict ascending이고 Binance `tradeId`는 non-negative여야 하며, 이는 공식
  [Binance SOR FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/sor_faq.md)의
  allocation/trade 구분과 일치한다. Fee asset은 `ETH` 또는 `USDT`만 허용하고 quote fee는 USDT fee
  자체 또는 Decimal precision 34의 `ETH fee * price`다. Fill fee 합, BUY cost basis, SELL net
  proceeds와 exact quantity allocation으로 domain realized PnL을 다시 계산해 Trade와 Performance
  run/total에 결속한다. Empty baseline의 realized PnL과 fee는 모두 zero다. 양수 ETH base-fee SELL은
  production Position 회계가 표현하지 못하므로 산술을 맞춰도 SUCCESS로 봉인하지 않는다.
  Preflight MARKET BUY received-asset commission rate가 zero이고 discount asset이 없으면 BUY fill과
  durable Trade의 `fee_amount`·`fee_quote_amount`도 exact zero다. 이 received-asset commission과
  discount 의미는 공식
  [Binance Commission FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/commission_faq.md)를
  따른다.
- V3 transport sequence는 `1..N` 연속이다. Account event interleave는 허용하되
  `ACCOUNT_UPDATED.related_id`는 null이어야 한다. Durable Trade order와 같은 순서의 adjacent
  `ORDER_EXECUTED` → `PERFORMANCE_UPDATED` pair 뒤에는 다음 order 전에 trace context version 이상인
  `TRADING_SESSION_UPDATED`가 있어야 하고, 두 cycle의 session `related_id`는 같은 canonical UUIDv4다.
- 공식 FULL/order-query payload는
  [Binance Spot New Order](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-trade)의
  `transactTime` 또는
  [All Orders](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#all-orders-user_data)의
  `updateTime`/`time` 중 하나를 제공해야 한다. 모두 없거나 잘못된 값을 local clock으로 보완하지
  않고 `UNKNOWN` reconciliation으로 fail closed한다.
- Latest historical FAILED file은 schema v1 exact bytes이며 v2 field를 소급 추가하지
  않는다. Artifact-specific canonical body/file digest 검증은 그 v1 contract로 계속하고,
  preserved NO_SIGNAL trace v2도 자신의 기존 trace contract로 계속 검증한다.
- 위 외부 PASS/FAILED는 고정 30초 prepared-evidence 보강 직전 source의 보존 기록이다. Current
  tree의 expiry/clock-regression/transport POST 직전 guard와 trace overlap/30초 회귀는 모두
  완료했지만 새 source digest와 signed target을 결속하지 않았으므로 current-tree external PASS로
  사용하지 않는다.
- 위 immutable snapshot, exact V3 chronology/accounting/outcome-routing과 ancestor-chain durability는
  local-only 보강이다. 아래 수치는 이번 추가 보강 전 historical local gate다. 당시 Backend
  `Ran 962 tests in 32.223s`,
  `OK (skipped=8)`, scripts `Ran 183 tests in 33.050s`, secure runner `14/14`, cause integration `50/50`,
  Case 2 helper `29/29`·external actual `1` safe skip, convention `2/2`, Communication `126/126`을
  통과했다. 이번 보강의 Keychain lookup,
  Binance signed/public target, Testnet diagnostic/actual, submission attempt, order POST,
  BUY·STOP SELL과 durable Trade는 모두 `0`건이고 current external binding은 미완료다.

### 10.3 최신 `NO_GO`와 재개 계약

이번 one-shot 승인은 read-only 1회와 actual target 1회로 소진됐다. 새 승인 없이
external diagnostic/read-only/actual을 반복하지 않는다. Local-only first-cause/fresh-stage
구분은 다음 계약으로 구현했다.

1. Controller는 account stream unknown/external execution, prepare filter/cap reject,
   event worker/runtime failure, market stream failure, order/persistence ambiguity, process
   ownership ambiguity를 여섯 stable category로 구분한다. 최초만 `EXACT`로 보존하고
   동일 원인의 두 번째 기록은 `DUPLICATE`, 다른 원인은 `CONFLICT`로 영구 잠그며
   둘 다 category를 노출하지 않는다. Raw ID·filter value·balance·price·exception
   repr는 증거에 넣지 않는다. App-prefix unknown만 same-process account recovery를 허용하고,
   prefixless external은 process-lifetime flag로 worker wake와 direct reconnect를 pre-I/O 차단해 fresh
   process account-wide 검증만 허용한다. 이 category는 일반 disconnect도 포함하므로 category 자체가
   permanent blocker는 아니다. Event-runtime failure, process-ownership ambiguity와 prefixless-external
   flag는 startup 입구·final commit·direct start를 모두 차단한다.
2. Failure 감지 시 frozen cause snapshot과 finalizer 시작 직전 current snapshot이 다르면
   `CONFLICT`로 fail closed한다. Schema v2 writer는 `first_cause`, 최초 stable fresh
   failure stage, account-wide open order/list empty truth를 exact field로 보존한다.
   Known-safe recovery 종료 뒤 logical submission을 모든 outcome에서 차단하고 atomic seal이 terminal
   check와 active/reconciliation blocker를 같은 lock에서 결속해 terminal recovery와 historical cause
   latch를 보존한다. Status commit 뒤 Context
   publication 예외는 runtime reconciliation에 결속한 stable secondary code로 남기고 최종 증거
   봉인을 계속한다. Active runtime status, 마지막 attempt 전 fresh timestamp, attempt 수를 넘는
   runtime/verified durable count, reconciliation status/blocker 모순, no-attempt nonzero
   Position/matching, serial pending 2건 이상, producer 순서를 거스른 error와 completed recovery의
   publication error는 seal하지 않는다.
   Testnet factory는 explicit evidence clock을 REST `clock`·`result_clock`, permission proxy와
   runtime/event stream에 동일 identity로 주입한다. REST 직후 다섯 preflight `observed_at`,
   permission/runtime/account/UI/final과 startup account source는 local run-scoped 범위·인과를 지키고,
   wall-clock backward-regression 회귀는 trace local 범위와 failure artifact seal을 검증한다.
   `submit_time_filter_evidence`, Binance Kline, server-aligned attempted/result와 fill/Trade는 별도
   server/exchange 축 내부 fetch order·30초 freshness·attempt → fill → result/Trade identity만 비교한다.
   Local 축과 cross-axis 대소를 비교하지 않으며 `SUCCESS`/`NO_SIGNAL`의 ±60초 coherent shift를 허용한다.
   이는 Binance 공식 signed `timestamp`·`serverTime`·`recvWindow` timing 계약과 일치한다.
   V3 startup account row는 run/version-bound snapshot으로 preflight 전에 고정하고, 후속 row는
   source time·Account version 전진과 `ACCOUNT_UPDATED` envelope exact provenance를 요구한다.
   Startup version은 1 이상이고 첫 preflight observation 이전이어야 하며 preflight version row와
   SUCCESS recovery free-balance evidence가 있어야 한다. Account startup producer와 Market observer는
   각각 lock 안에서 immutable state pointer를 한 번 읽어 state→clock/context를 atomic freeze한다.
   NO_SIGNAL collector는 synthetic snapshot 없이 production `1L.1`만 수집하고 captured version으로
   cursor를 전진한다.
   Transport publication time은 전역 단조, aggregate version은 비감소이며 order/performance는
   preflight 후에만 나올 수 있다. V3 sequence는 1..N 연속이고 ACCOUNT related ID는 null, 같은
   session UUID의 Trade pair→session cycle은 BUY→SELL durable order와 일치해야 한다. Policy 13,
   TYPE_0, cap·client ID·Kline provenance·result/trade/fill/fee/domain PnL과 empty baseline zero를 strict
   producer shape로 재계산한다. STOP SELL safety composite는 BUY terminal 후에 시작하고,
   order response의 공식 처리 시각이 없으면 `UNKNOWN` reconciliation으로 fail closed한다.
   Full trace v3는 exact SUCCESS와 NO_SIGNAL만 허용한다. Public Action 뒤 blocker와
   BLOCKED/UNKNOWN/FAILED는 failure evidence v2로 봉인한다.
   Historical schema v1 FAILED bytes와 preserved v2 NO_SIGNAL trace는 변경하지 않는다.
3. Fresh runtime은 source durability file을 직접 열지 않고 filesystem root부터 final parent까지
   source/copy ancestor descriptor chain을 함께 pin한 owner-only isolated snapshot을 사용한다. Finalizer는
   ctime/mtime을 포함한 source/copy fingerprint와 chain identity/ctime, account-wide open truth, recent exact baseline,
   stream와 reconciliation을 다시 읽고 변경·누락·cleanup 실패를 stable stage/reason으로
   강등한다. Final leaf는 source → copy → source로 캡처하고 뒤이어 chain을 검증한다. Source/destination
   ancestor close와 artifact file close·unlink·directory close는 전체 cleanup을 best effort로 수행하고
   최초 error를 보존한다. Ancestor ctime은 sibling churn도 fail closed하므로 actual execution 동안
   evidence directory를 격리하며, 변경 시 자동 재시도 없이 `DURABILITY/INCOMPLETE`로 종료한다. 이
   보강은 주문·Keychain·외부 network 없이 구현했으며 historical
   external failure의 원인을 사후 결론내지 않는다.
4. 다음 수치는 2026-09-04 추가 보강 전 historical local gate다. 당시 Backend/scripts/runner/
   Communication/convention 회귀와 기존
   baseline·preserved v2 `NO_SIGNAL`·latest v1 FAILED artifact digest를 모두 재검증했다. Immutable
   snapshot, exact V3와 ancestor-chain 보강을 포함해 Backend `962`, scripts `183`, runner `14`, cause
   integration `50`, Case 2 local `29`, convention `2`, Communication `126`이 통과했다. 이 local PASS를
   external binding으로 해석하지 않는다.
5. External 재실행이 필요하면 사용자에게 ① 고정 Keychain item 2개의 memory-only 조회,
   ② 고정 Spot Testnet signed read-only 1회, ③ 그 preflight 통과 시만 `ETHUSDT`
   BUY 최대 `100 USDT` 1회와 same-run exact Position STOP SELL 1회를 새로 명시
   승인받는다. 이전 one-shot 승인이나 일반 파일·명령 권한 허용을 재사용하지 않고 답을 받기 전에는
   Keychain/Binance/order target을 모두 0으로 유지한다. Read-only는 local `ETHUSDT` Position/pending/unknown 0, account balance,
   all-symbol `openOrders`/`openOrderList` 0, symbol-scoped `ETHUSDT allOrders` recent exact baseline,
   full filter/reference, reconciliation과 두 stream READY를 모두 통과해야 한다. 그 경우에만 actual을
   1회 실행하고 어떤 실패도 자동 재시도하지 않는다.
6. Actual `SUCCESS`가 exact BUY/STOP SELL/Trade·History·Performance·UI publication과 fresh zero
   exposure까지 완결되면 order-critical backend subtree exact digest를 freeze한다. 이후 해당 byte와
   dependency lockfile을 바꾸지 않고 UI visual·supply evidence·docs만 명시적 allowlist diff로 결속해
   visual `16/16`, local-only supply/license/final notice/current app·DMG binding과 최종 no-order
   `check_all.sh`를 순서대로 재개한다. Order-critical byte가 바뀌면 actual을 역사 evidence로 강등하고
   새 승인·재실행한다.

Visual `4/16`, historical actual-browser axe `16/16 Violations 0`, supply `NO_GO`는 이번 actual
failure 후 변경하지 않았다. P13-04, Phase 13 master와 live는 계속 `GAP`/`NO_GO`/
disabled다.
