# 승인된 ETH 잔여 장부 정책

2026-09-08 사용자는 설명된 “수수료로 생긴 주문 단위 미만 ETH를 별도 장부에 보존하고 전략 종료 시
잔여량을 명시”하는 정책의 구현을 승인했다. 이 승인은 실제 주문·출금·자동 자산 전환 승인이 아니다.

## 수량과 원가

- 기존 ETH 수수료 차감 회계를 사용한다. 매수 체결량에서 ETH 수수료를 뺀 실제 취득량이 기준이다.
- 현재 열린 lot에 ETH 매수 수수료가 있고, 마지막 SELL의 요청량이 매도 직전 전략 Position 전량과
  같으며, 매도 후 양수 잔여가 공식 LOT_SIZE stepSize보다 작을 때만 분리한다.
- 일반 분할 매도, 수수료 없는 lot, stepSize 이상의 잔여, MIN_NOTIONAL 미달만으로는 분리하지 않는다.
- 잔여량과 해당 미실현 원가 전부를 장부에 저장한다. 새 매도 Trade나 실현손익은 만들지 않는다.
- 전략 Position은 닫혀도 잔여 ETH는 실제 계좌 자산이다. 수량과 원가는 다음 lot에 섞이지 않는다.
- 잔여의 평가금액도 기존 max_position_notional 10 USDT 계산에 포함한다. 장부 분리는 위험 한도를
  확대하지 않는다. 여러 lot의 잔여가 누적되어 거래 가능한 크기가 되어도 자동 매도·전환하지 않는다.

## 파일과 책임

| 책임 | 파일 |
|---|---|
| 불변 수량·원가·이력 prefix 결속 | `domain/trading/residual.py` |
| 저장·재생·전량 매도 및 수수료 출처 확인 | `application/residual_settlement.py` |
| bounded strict JSON, 중복 key/record·symlink/hardlink 거부, atomic replace/fsync | `adapters/persistence/residual_repository.py` |
| 기존 terminal/STOP/startup/reconnect 경계 연결 | `application/trading_controller.py` |
| 저장 완료 값만 전략 Position에서 분리 | `domain/trading/position.py` |
| live 전용 정책 조립 | `bootstrap/live.py` |
| 잔여 Decimal transport와 화면 표시 | `transport/contracts.py`, `ResidualNotice.tsx`, 기존 mapper/machine/facade |

Backend 경로는 `backend/src/binance_auto_trader/` 기준이다. 실제 장부는 live app-data의
`residual-ledger.json`이며 기존 Testnet 저장소와 분리된다. 거래 ID·시각·매수량·fee·매도량은 원래
Trade JSONL에 남기고, 잔여 record의 `history_count`와 SHA-256으로 해당 prefix 전체에 결속한다.
장부 schema v1은 정확한 Decimal 문자열 quantity/cost_basis/step_size를 저장한다.

## 저장 실패와 복구

1. SELL의 실제 체결과 기존 Trade history를 먼저 저장한다.
2. 잔여 장부를 파일·directory fsync까지 완료한 뒤 전략 Position을 닫는다.
3. 그 뒤 pending journal 해제와 전략 종료 outcome을 진행한다.
4. 저장이 불명확하면 새 주문을 차단한다. History가 확정된 체결을 취소하거나 다시 제출하지 않는다.
5. 재시작은 Trade와 잔여 이관을 원래 순서로 재생한다. Rename 뒤 memory 반영 전 중단도 같은
   이관 하나로 복구한다. 이력 변경·잘못된 수량/원가·현재 공식 단위보다 큰 저장 단위는 거부한다.
6. 잔여가 있는 전용 계좌는 startup/reconnect에서 전략 ETH+잔여 ETH와 실제 계좌 ETH의 일치를
   확인한다. 차이를 임의 보정하거나 잔여 장부를 자동 삭제하지 않는다.

## 종료 표시와 zero exposure

화면은 전략 종료 후에도 `전략 종료 · 잔여 ETH 있음`, 정확한 ETH 수량과 미실현 원가를 표시한다.
잔여가 남으면 가격 변동 위험이 남으며 **zero exposure가 아니다**. 현재 열린 전략 Position이
있거나 실행 중이면 종료로 표시하지 않는다. 재연결 snapshot에서도 장부 값이 유지된다.

Session 8의 승인된 종료 조건은 전략 Position/pending/unknown/open order/list 0과 잔여 장부의
내구성·실계좌 일치다. 잔여가 없을 때만 별도로 완전한 zero exposure를 주장할 수 있다.
`scripts/live_runtime_readiness.py`는 Session 7의 빈 계좌 상태 검증 도구로, residual_assets_zero도
별도 검사한다. 잔여가 허용된 Session 8 종료 증거 전체를 이 도구의 PASS 하나로 대체하지 않는다.

## 검증과 현재 한계

Backend 전체 1,065 실행 / 1,055 PASS / 10 외부 safe skip. 신규 domain/storage 6개와 실제
Controller BUY→STOP→ledger→fresh replay 통합 1개가 포함된다. 최초 통합 fixture의 BUY/SELL
거래소 ID 중복 실패는 실제로 서로 다른 두 주문 ID를 쓰도록 fixture를 수정한 뒤 통과했다.
재연결 잔여 보존 테스트를 포함한 UI 최종 전체 487 PASS를 확인했다.
TypeScript, generated contract, Communication 126/126, docstring field 검사, diff whitespace PASS.

실제 live 주문은 0회다. 실제 source read-only READY와 새 native 실계좌·주문 비활성 연결·정상 종료는
통과했지만, 실제 체결로 잔여를 생성하는 실험은 하지 않았다. 잔여 생성 자체는 외부 fixture로 검증했다.
BNB fee 환산은 이번 승인 범위에 포함하지 않았다. 최신 signed preflight는 ETH 잔여 정책과 MARKET
최소 금액 잔액 조건을 통과했으나 `supported_fee_asset=false`로 실제 주문은 계속 NO_GO다.

공식 근거:
[Binance Commission FAQ](https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/faqs/commission_faq.md),
[Binance filters](https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/filters.md).
