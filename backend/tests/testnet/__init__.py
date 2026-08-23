"""명시적 환경 opt-in에서만 실행되는 Binance Spot Testnet suite를 표시한다."""

# Package import는 환경 조회나 network 연결 없이 test module discovery만 허용한다.
__all__: tuple[str, ...] = ()  # testnet helper는 각 module이 명시적으로 import한다.
