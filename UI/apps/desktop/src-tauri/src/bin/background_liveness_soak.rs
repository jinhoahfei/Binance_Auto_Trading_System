// 주문 없는 장시간 연결 유지 검증 앱의 진입점을 호출한다.


/// 함수 이름: main()
/// 기능: 장시간 연결 검증용 네이티브 진입점을 실행한다.
/// 인자: 없음
/// 반환값: 검증 앱 종료 후 반환
/// 작성 날짜: 2026/09/17
fn main() {
    binance_auto_trader_lib::run_background_liveness_soak();
}
