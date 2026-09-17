//! macOS native Keychain profile을 launch 동안 고정해 credential과 owner namespace를 함께 선택한다.

use super::*;
use security_framework::passwords::{generic_password, PasswordOptions};
use std::sync::OnceLock;

pub(super) const LIVE_SERVICE: &str = "com.binance-auto.trader.live";
const PROFILE_SERVICE: &str = "com.binance-auto.trader.desktop-profile";
const PROFILE_ACCOUNT: &str = "execution-profile";
static PROFILE: OnceLock<Result<MacosExecutionProfile, SidecarFailure>> = OnceLock::new();

/// 클래스 이름: MacosExecutionProfile
/// 기능: native 설정 도구가 승인한 credential·storage·order 선택을 launch 동안 보존한다.
/// 작성 날짜: 2026/09/08
#[derive(Clone, Copy, PartialEq, Debug)]
pub(super) enum MacosExecutionProfile {
    Testnet,
    LiveReadOnly,
    LiveOrders,
}


/// 함수 이름: parse_profile()
/// 기능: 없는 live profile은 비활성으로 유지하고 알려진 exact native 값만 허용한다.
/// 인자: value -> Keychain profile bytes 또는 item 부재
/// 반환값: immutable mode 또는 startup failure
/// 작성 날짜: 2026/09/08
fn parse_profile(value: Option<&[u8]>) -> Result<MacosExecutionProfile, SidecarFailure> {
    // 공백·unknown version은 Testnet으로 fallback하지 않는다.
    match value {
        None | Some(b"TESTNET") => Ok(MacosExecutionProfile::Testnet),
        Some(b"LIVE_READ_ONLY") => Ok(MacosExecutionProfile::LiveReadOnly),
        Some(b"LIVE_ORDERS_V1") => Ok(MacosExecutionProfile::LiveOrders),
        _ => Err(SidecarFailure::startup()),
    }
}


/// 함수 이름: selected_profile()
/// 기능: 첫 native 조회 결과를 고정해 실행 중 profile 변경으로 owner 경로가 바뀌지 않게 한다.
/// 인자: 없음
/// 반환값: launch 전용 profile 또는 동일 failure
/// 작성 날짜: 2026/09/08
pub(super) fn selected_profile() -> Result<MacosExecutionProfile, SidecarFailure> {
    // Item 부재만 disabled 기본값으로 인정하고 Keychain 접근 오류는 숨기지 않는다.
    PROFILE
        .get_or_init(|| {
            match generic_password(PasswordOptions::new_generic_password(
                PROFILE_SERVICE,
                PROFILE_ACCOUNT,
            )) {
                Ok(value) => parse_profile(Some(&value)),
                Err(error) if error.code() == -25300 => parse_profile(None),
                Err(_) => Err(SidecarFailure::credentials_unavailable()),
            }
        })
        .clone()
}


/// 함수 이름: profile_directory()
/// 기능: live의 history·pending·runtime owner를 기존 Testnet directory와 분리한다.
/// 인자: original -> 기존 Tauri app-data, profile -> frozen native profile
/// 반환값: 선택된 namespace directory
/// 작성 날짜: 2026/09/08
pub(super) fn profile_directory(original: PathBuf, profile: MacosExecutionProfile) -> PathBuf {
    if profile == MacosExecutionProfile::Testnet {
        original
    } else {
        original.join(LIVE_SERVICE) // 기존 이력을 live directory로 복사하지 않는다.
    }
}

/// 클래스 이름: LiveBootstrapWire
/// 기능: native-only live namespace와 opt-in을 legacy credential wire에 결합한다.
/// 작성 날짜: 2026/09/08
#[derive(Serialize)]
struct LiveBootstrapWire<'a> {
    #[serde(flatten)]
    base: SidecarBootstrapConfiguration<'a>,
    execution_mode: &'static str,
    credential_namespace: &'static str,
    allow_live_orders: bool,
    live_confirmation: &'static str,
    policy_version: u32,
}


/// 함수 이름: serialize_profile_configuration()
/// 기능: frozen native profile을 bounded secret wire로 직렬화한다.
/// 인자: configuration -> native credential references, profile -> launch의 고정 profile
/// 반환값: pipe 전송 뒤 지울 Zeroizing bytes 또는 startup failure
/// 작성 날짜: 2026/09/08
pub(super) fn serialize_profile_configuration(
    mut configuration: SidecarBootstrapConfiguration<'_>,
    profile: MacosExecutionProfile,
) -> Result<Zeroizing<Vec<u8>>, SidecarFailure> {
    if profile == MacosExecutionProfile::Testnet {
        return serialize_bootstrap_configuration(&configuration);
    }
    // Live order 권한은 별도 native profile 하나에서만 cap과 동시에 활성화한다.
    let allow_live_orders = profile == MacosExecutionProfile::LiveOrders;
    configuration.max_notional = if allow_live_orders { Some("10") } else { None };
    let payload = Zeroizing::new(
        serde_json::to_vec(&LiveBootstrapWire {
            base: configuration,
            execution_mode: "live",
            credential_namespace: LIVE_SERVICE,
            allow_live_orders,
            live_confirmation: "LIVE",
            policy_version: 1,
        })
        .map_err(|_| SidecarFailure::startup())?,
    );
    if payload.is_empty() || payload.len() > MAXIMUM_CONFIG_BYTES {
        return Err(SidecarFailure::startup());
    }
    Ok(payload) // Secret-bearing serde Value나 일반 log 사본을 만들지 않는다.
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 함수 이름: profile_is_exact_and_namespace_isolated()
    /// 기능: disabled 기본값·native opt-in·unknown 거부와 경로 격리를 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/08
    #[test]
    fn profile_is_exact_and_namespace_isolated() {
        // Profile 누락과 unknown은 서로 다른 결과여야 한다.
        assert_eq!(parse_profile(None).unwrap(), MacosExecutionProfile::Testnet);
        assert_eq!(
            parse_profile(Some(b"LIVE_READ_ONLY")).unwrap(),
            MacosExecutionProfile::LiveReadOnly
        );
        assert_eq!(
            parse_profile(Some(b"LIVE_ORDERS_V1")).unwrap(),
            MacosExecutionProfile::LiveOrders
        );
        for invalid in [
            b"LIVE".as_slice(),
            b"live",
            b" LIVE_READ_ONLY",
            b"LIVE_ORDERS_V2",
        ] {
            assert!(parse_profile(Some(invalid)).is_err());
        }
        let original = PathBuf::from("/private/example");
        assert_ne!(
            profile_directory(original.clone(), MacosExecutionProfile::LiveReadOnly),
            original
        );
    }

    /// 함수 이름: native_live_wire_binds_cap_and_readonly_namespace()
    /// 기능: live profile별 exact wire와 cap 결속을 canary만으로 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/08
    #[test]
    fn native_live_wire_binds_cap_and_readonly_namespace() {
        // 두 live profile은 같은 live namespace를 사용하되 mutation flag와 cap만 함께 바뀐다.
        for profile in [
            MacosExecutionProfile::LiveReadOnly,
            MacosExecutionProfile::LiveOrders,
        ] {
            let payload = serialize_profile_configuration(
                SidecarBootstrapConfiguration {
                    schema_version: BACKEND_SCHEMA_VERSION,
                    allowed_origin: PRODUCTION_UI_ORIGIN,
                    history_path: "/private/tmp/com.binance-auto.trader.live/trade-history.jsonl",
                    api_key: "live-canary-key",
                    api_secret: "live-canary-secret",
                    allow_testnet_orders: false,
                    max_notional: None,
                },
                profile,
            )
            .unwrap();
            let decoded: serde_json::Value = serde_json::from_slice(&payload).unwrap();
            assert_eq!(decoded.as_object().unwrap().len(), 12);
            assert_eq!(decoded["execution_mode"], "live");
            assert_eq!(decoded["credential_namespace"], LIVE_SERVICE);
            assert_eq!(decoded["allow_testnet_orders"], false);
            assert_eq!(
                decoded["allow_live_orders"],
                profile == MacosExecutionProfile::LiveOrders
            );
            if profile == MacosExecutionProfile::LiveOrders {
                assert_eq!(decoded["max_notional"], "10");
            } else {
                assert!(decoded["max_notional"].is_null());
            }
        }
    }
}
