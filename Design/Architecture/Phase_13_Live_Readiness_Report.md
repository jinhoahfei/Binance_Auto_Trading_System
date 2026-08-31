# Phase 13 live readiness 판정 보고서

| 항목 | 값 |
|---|---|
| 판정일 | 2026-08-31 KST |
| 기준 revision | `aca6f12` 위 변경 작업트리 |
| 판정 | **NO_GO** |
| 실제 live 주문 | 0건, 계속 disabled |
| 이번 Testnet 실행 | credential 읽기 0건, signed preflight 0건, 주문 0건; 명시적 승인 대기 |
| 24시간 soak | 사용자 결정으로 Phase 13에서 영구 제외; 실행·PASS 증거가 아님 |

## 1. 결론

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

다만 실제 Phase 13 Testnet public order/fill/History/UI trace는 아직 없다. Keychain credential을 읽어 고정 Testnet endpoint에만
전송하는 signed read-only preflight와 이어지는 단일 주문은 명시적 사용자 승인 없이는 실행하지
않았다. Current actual-browser visual은 SSIM `4/16` PASS,
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
| P13-04 market-event E2E | local/production-memory/harness PASS / actual Testnet GAP | Kline live promotion·generation/gap/duplicate 방어와 full-resync, builder 직접 회귀 `20/20`, 4H 12개·UTC 자정 180개 all-interval atomic boundary, public local Case 2 `9/9`, production Spot REST memory-HTTP `1/1`과 immutable evaluation/version provenance를 통과. Actual harness는 exact 3중 opt-in, BUY absolute `100 USDT` cap, same-target process lease, fresh filter·POST-start provenance, production `1L.1`→`1L.3` observer, exact order-trace grammar, known-safe failure recovery, atomic success/FAILED seal과 final zero-state 검증을 갖춤 | Current signed read-only preflight와 actual Phase 13 Testnet order/fill/History/UI trace는 아직 없음. Local/memory transport 또는 harness 준비를 실제 exchange 증거로 승격하지 않음 |
| P13-05 fault·replay | offline PASS | 7개 category, 25개 canonical scenario를 10회 replay해 digest `a5f17e96f60e825faf4ccf89e0788133f8bfcac6a3654504a4f16c5c2f2f70f2` 일치 | offline contract replay이며 실제 Testnet market→order E2E를 대신하지 않음 |
| P13-06 Communication·UI | Communication·a11y PASS / visual SSIM NO_GO / overall NO_GO | Manifest/checker `126 COMPLETE / 0 GAP`, current-host production native picker selected/cancelled `2/2`, 16-state reference manifest, 독립 axe 구조 검사와 actual-browser addon-a11y `16/16 Violations 0` 통과 | Fresh SSIM은 `4/16` PASS·`12/16` FAIL이므로 visual pixel gate 미완료 |
| P13-07 통합 실행기 | 실행 완료 / NO_GO | `check_all.sh`가 backend/script/contract/replay/no-order preflight/UI/Rust/secret/offline supply/visual/trace/readiness를 모두 실행했다. 기능·Communication gate는 PASS했고 hostile order env 제거와 OSV 외부 전송 금지를 유지 | Supply binding·offline vulnerability/license·visual·readiness가 차단돼 aggregate exit `1`. Soak는 영구 제외이며 PASS가 아님 |
| P13-08 live 판정 | **NO_GO** | default disabled, Testnet/live endpoint와 order opt-in 분리, secret scan 통과 | 위 NO_GO/GAP 전체 해소와 별도 사용자 live 승인 필요 |

## 3. 검증 결과

| 범주 | 실행 결과 |
|---|---|
| Backend 전체 | `901` tests, 외부 Testnet `8` safe skip, failure 0 (`PYTHONWARNINGS=error`) |
| UI | Vitest `41` files, `373/373` PASS; TypeScript `tsc -b` PASS; Vite production build `286` modules PASS |
| Rust/Tauri | 기본 suite `40/40` PASS, current-host production native picker selected/cancelled harness `2/2` PASS; `cargo fmt --check`와 `cargo clippy -- -D warnings` PASS |
| Release/root scripts | `169/169` PASS (`PYTHONWARNINGS=error`) |
| Private license·supply 집중 | Exact lockfile `868` components를 byte-bind했고 version-matched local metadata 선언 `494`, 미관찰 `372`는 `NOASSERTION`으로 보존. Project 자체 private/non-publish 선언은 완료했지만 final notice·current advisory/raw license output·current release binding이 없어 supply는 `NO_GO` |
| Offline OSV local-only | OS network deny와 scanner `--offline`을 함께 강제하는 wrapper만 실행했다. 외부 전송은 없었고 local DB 부재로 vulnerability·license가 각각 exit `127`이어서 fail closed `NO_GO` |
| UI contract | generated TypeScript contract drift 0 |
| P13-01 집중 | C&L core `79/79`, manual-kill transport `12/12`, risk-budget transport `9/9`, UI mapper·panel·machine `129/129` PASS |
| Deterministic replay | 25 scenarios, repeat 10, canonical digest 일치 |
| Market/REGIME reconciliation 집중 | `8/8` PASS |
| Registry coverage | Regime `13/13`, supported TYPE_0 Trading transition `109/109` exact ID coverage PASS |
| Communication checker | checker unit `17/17` PASS; matrix `126 COMPLETE / 0 GAP`, checker exit `0` |
| 16-state UI 집중 | Figma baseline manifest 무결성 `2/2`, axe WCAG A/AA 독립 scanner `16/16`, calendar/progress 포함 UI 집중 PASS. Actual Storybook addon-a11y 재실행도 layout 기반 color contrast를 포함해 `16/16 Violations 0` |
| Actual browser visual | 최종 source를 1440×1024·DPR1·explicit clip로 임시 fresh JPEG 16개에 캡처했다. SSIM 범위 `0.915904~0.981311`, `4/16` PASS·`12/16` FAIL로 visual gate `NO_GO`; 실패 상태이므로 repository current capture/manifest, threshold `0.980000`과 baseline은 변경하지 않음 |
| Secret scan | credential canary 2개가 2,670개 파일에서 모두 absent |
| 실제 Testnet read-only | 과거 retained verified-closed 증거는 `6/6` PASS이나 current-session 증거로 승격하지 않음. 이번 실행은 Keychain credential 사용 승인이 없어 credential 읽기·signed request·주문 모두 0건 |
| Phase 13 actual 안전성 집중 | Actual harness helper `15/15` + actual target `1` safe skip, trace contract `25/25`, Controller/REST/bootstrap/architecture 핵심 묶음 `139` PASS + actual `1` safe skip. Credential·signed request·주문 0건 |
| `check_all.sh` 현재 집계 | Credential·cap·세 opt-in을 제거한 최종 aggregate는 Backend `901`(8 skip), scripts `169/169`, UI `373/373`, Rust `40/40`, public Case 2 `10/10`, Communication `126/126`, secret scan PASS. Supply binding·offline vulnerability/license·visual·readiness가 BLOCKED여서 예상대로 exit `1` |

### 3.1 재현 명령과 주요 파일

기준 revision은 `aca6f1262e934fddc94c93b4f42e41ef8f4dd604`이며, 아래 명령은 그 revision 위
현재 변경 작업트리에서 실행했다. 아래 local 명령에는 credential과 주문 opt-in을 전달하지 않았다.
과거 retained Testnet read-only `6/6`은 보존하지만 이번 session의 current signed preflight로
재사용하지 않는다.

```text
cd backend && PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=. backend/.venv/bin/python -m unittest discover -s scripts -p 'test_*.py'
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

### 7.2 이번 session에서 실행하지 않은 mutation

Keychain service `com.binance-auto.trader.testnet`의 API key/secret을 읽어 signed read-only
preflight에 사용하는 단계는 명시적 사용자 승인 필요 판정을 받아 실행되지 않았다. 따라서 이번
session에서 credential 값 읽기·stdout 출력·signed Testnet request·주문은 모두 `0`건이다. 과거의
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
