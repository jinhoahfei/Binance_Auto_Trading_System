"""잔여 ETH의 자동 예치 근거만 읽으며 예치·상환·자산 이동을 제공하지 않는다."""

from datetime import datetime, timezone
from decimal import Decimal, localcontext

from binance_auto_trader.domain.trading.residual import EarnResidualEvidence

EARN_READ_ENDPOINTS = frozenset({
    "/sapi/v1/simple-earn/flexible/position",
    "/sapi/v1/simple-earn/flexible/history/subscriptionRecord",
    "/sapi/v1/simple-earn/flexible/history/redemptionRecord",
    "/sapi/v1/simple-earn/flexible/history/rewardsRecord",
})
_HISTORY_WINDOW_MS = 30 * 24 * 60 * 60 * 1000


def read_earn_residual_evidence(request, since: datetime, now_ms: int) -> EarnResidualEvidence | None:
    """
    함수 이름: read_earn_residual_evidence()
    기능: 30일 구간·pagination 전체에서 AUTO/SPOT 예치·상환·보상과 현재 보유량을 검증한다.
    인자: request -> fixed signed GET callback, since -> 최초 잔여 발생 시각, now_ms -> 조회 상한
    반환값: 검증된 ETH 예치 근거 또는 ETH 예치 부재 None
    작성 날짜: 2026/09/15
    """
    if not isinstance(since, datetime) or since.tzinfo is None or since.utcoffset() is None:
        raise ValueError("Earn history requires an aware timestamp")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = since - epoch
    since_ms = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
    if type(now_ms) is not int or not 0 < since_ms <= now_ms or now_ms - since_ms > 120 * _HISTORY_WINDOW_MS:
        raise ValueError("Earn history range is invalid or exceeds verification budget")

    def read_rows(endpoint, parameters):
        """
        함수 이름: read_rows()
        기능: total과 페이지 수를 검증해 잘린 조회를 완전한 근거로 사용하지 않는다.
        인자: endpoint -> 고정 조회 경로, parameters -> ETH와 시간 조건
        반환값: 전체 row tuple
        작성 날짜: 2026/09/15
        """
        rows = []
        expected_total = None
        for page in range(1, 11):
            payload = request(endpoint, {**parameters, "asset": "ETH", "current": page, "size": 100})
            if not isinstance(payload, dict) or type(payload.get("total")) is not int or not isinstance(payload.get("rows"), list):
                raise ValueError("invalid Earn page")
            total, batch = payload["total"], payload["rows"]
            if not 0 <= total <= 1000 or len(batch) > 100 or any(not isinstance(row, dict) or row.get("asset") != "ETH" for row in batch):
                raise ValueError("invalid Earn rows")
            if expected_total is not None and total != expected_total:
                raise ValueError("Earn pages changed during read")
            expected_total = total
            rows.extend(batch)
            if len(rows) == total:
                return tuple(rows)
            if not batch or len(rows) > total:
                raise ValueError("incomplete Earn pages")
        raise ValueError("Earn history page budget exceeded")

    positions = read_rows("/sapi/v1/simple-earn/flexible/position", {})
    if len(positions) > 1:
        raise ValueError("multiple Earn ETH products require reconciliation")
    position = positions[0] if positions else None
    product_id = position.get("productId") if position else None
    if position is not None and (not isinstance(product_id, str) or not product_id or position.get("canRedeem") is not True or _amount(position.get("collateralAmount")) != 0):
        raise ValueError("Earn ETH is unavailable or collateralized")
    subscriptions = []
    redemptions = []
    reward_rows = []
    start = since_ms
    while start <= now_ms:
        end = min(start + _HISTORY_WINDOW_MS - 1, now_ms)
        parameters = {"startTime": start, "endTime": end}
        redemptions.extend(read_rows("/sapi/v1/simple-earn/flexible/history/redemptionRecord", parameters))
        for row in read_rows("/sapi/v1/simple-earn/flexible/history/subscriptionRecord", parameters):
            amount = _amount(row.get("amount"))
            timestamp = row.get("time")
            identity = row.get("purchaseId")
            if product_id is None:
                product_id = row.get("productId")
            if (row.get("type") != "AUTO" or row.get("status") != "SUCCESS" or row.get("sourceAccount") != "SPOT"
                    or not isinstance(product_id, str) or not product_id or row.get("productId") != product_id or type(timestamp) is not int or not start <= timestamp <= end
                    or type(identity) not in (int, str) or amount <= 0):
                raise ValueError("Earn subscription does not prove an automatic Spot transfer")
            if ("amtFromSpot" in row and _amount(row["amtFromSpot"]) != amount) or ("amtFromFunding" in row and _amount(row["amtFromFunding"]) != 0):
                raise ValueError("Earn subscription mixes funding sources")
            subscriptions.append((str(identity), timestamp, amount))
        start = end + 1
    if not subscriptions:
        return None
    returned = []
    for row in redemptions:
        timestamp, identity = row.get("time"), row.get("redeemId")
        if (row.get("productId", row.get("projectId")) != product_id or row.get("destAccount") != "SPOT" or row.get("status") != "PAID"
                or type(timestamp) is not int or not since_ms <= timestamp <= now_ms or type(identity) not in (int, str)):
            raise ValueError("Earn redemption is not a completed Spot return")
        if timestamp <= min(entry[1] for entry in subscriptions):
            raise ValueError("Earn redemption predates verified subscriptions")
        returned.append((str(identity), timestamp, _amount(row.get("amount"))))
    if returned:
        # 상환액의 초과분을 임의의 허용 오차로 간주하지 않고 공식 REALTIME 보상으로 증명한다.
        start = since_ms
        while start <= now_ms:
            end = min(start + _HISTORY_WINDOW_MS - 1, now_ms)
            reward_rows.extend(read_rows("/sapi/v1/simple-earn/flexible/history/rewardsRecord", {"type": "REALTIME", "startTime": start, "endTime": end}))
            start = end + 1
        identities = set()
        reward_amounts = []
        for row in reward_rows:
            timestamp = row.get("time")
            if (row.get("productId", row.get("projectId")) != product_id or row.get("type") != "REALTIME"
                    or type(timestamp) is not int or not since_ms <= timestamp <= now_ms or timestamp in identities):
                raise ValueError("invalid or duplicate Earn reward")
            identities.add(timestamp)
            reward_amounts.append(_amount(row.get("rewards")))
        with localcontext() as context:
            context.prec = 34
            rewards = sum(reward_amounts, Decimal("0"))
    else:
        rewards = _amount(position.get("cumulativeRealTimeRewards")) if position else Decimal("0")
    return EarnResidualEvidence(tuple(sorted(subscriptions, key=lambda row: (row[1], row[0]))),
                                _amount(position.get("totalAmount")) if position else Decimal("0"), rewards,
                                tuple(sorted(returned, key=lambda row: (row[1], row[0]))))


def _amount(value: object) -> Decimal:
    """
    함수 이름: _amount()
    기능: 부동소수 반올림 없이 공식 수량 문자열만 읽는다.
    인자: value -> JSON 수량
    반환값: 유한한 0 이상 Decimal
    작성 날짜: 2026/09/15
    """
    if not isinstance(value, str):
        raise ValueError("Earn quantity must be a string")
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid Earn quantity")
    return amount
