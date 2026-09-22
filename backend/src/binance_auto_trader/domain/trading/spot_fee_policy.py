"""신규 현물 거래에 적용할 BNB 미사용·편도 0.1% 수수료 정책을 정의한다."""

from decimal import Decimal


# 실제 체결 장부는 거래소 원 수수료를 보존하고 이 비율은 주문 전 정책 대조에만 쓴다.
SPOT_FEE_RATE = Decimal("0.001")
