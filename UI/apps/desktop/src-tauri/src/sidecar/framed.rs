//! Windows stdio의 bounded big-endian frame을 OS I/O와 분리한다.

use super::{serialize_bootstrap_configuration, SidecarBootstrapConfiguration, SidecarFailure};
use serde::Serialize;
use std::io::{self, Write};
use zeroize::Zeroizing;

pub(super) const MAXIMUM_FRAME_BYTES: usize = 1024 * 1024;
pub(super) const MAXIMUM_BOOTSTRAP_FRAME_BYTES: usize = 16 * 1024;
pub(super) const CLOSED_ACK_PAYLOAD: &[u8] = br#"{"type":"CLOSED_ACK"}"#;

/// 클래스 이름: BootstrapFrame
/// 기능: 최초 parent frame의 exact type/token/configuration 구조만 직렬화한다.
/// 작성 날짜: 2026/09/06
#[derive(Serialize)]
struct BootstrapFrame<'a> {
    #[serde(rename = "type")]
    message_type: &'static str,
    token: &'a str,
    configuration: &'a SidecarBootstrapConfiguration<'a>,
}

/// 함수 이름: serialize_bootstrap_frame()
/// 기능: 기존 strict configuration과 native token을 bounded 최초 JSON frame으로 묶는다.
/// 인자: token -> per-launch token, configuration -> 기존 read-only bootstrap contract
/// 반환값: zeroizing frame payload 또는 secret-free startup failure
/// 작성 날짜: 2026/09/06
pub(super) fn serialize_bootstrap_frame(
    token: &str,
    configuration: &SidecarBootstrapConfiguration<'_>,
) -> Result<Zeroizing<Vec<u8>>, SidecarFailure> {
    // 내부 configuration의 기존 8 KiB 제한을 유지한 뒤 전체 bootstrap은 16 KiB로 제한한다.
    let _validated_configuration = serialize_bootstrap_configuration(configuration)?;
    let frame = BootstrapFrame {
        message_type: "BOOTSTRAP",
        token,
        configuration,
    };
    let payload =
        Zeroizing::new(serde_json::to_vec(&frame).map_err(|_| SidecarFailure::startup())?);
    if payload.len() > MAXIMUM_BOOTSTRAP_FRAME_BYTES {
        return Err(SidecarFailure::startup());
    }
    Ok(payload) // 직렬화한 credential 사본은 frame publication 직후 Drop으로 지운다.
}

/// 함수 이름: write_frame()
/// 기능: 크기를 먼저 검증한 뒤 4-byte big-endian 길이와 payload를 같은 writer에 전달한다.
/// 인자: writer -> parent stdio pipe, payload -> UTF-8 JSON, limit -> 메시지별 상한
/// 반환값: write 결과 또는 크기 validation 오류
/// 작성 날짜: 2026/09/06
pub(super) fn write_frame(writer: &mut impl Write, payload: &[u8], limit: usize) -> io::Result<()> {
    // 길이 검증 전에 prefix를 쓰지 않아 oversized bootstrap이 부분 publication되지 않게 한다.
    if payload.is_empty() || payload.len() > limit || limit > MAXIMUM_FRAME_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Invalid IPC frame size",
        ));
    }
    writer.write_all(&(payload.len() as u32).to_be_bytes())?;
    writer.write_all(payload)?;
    writer.flush() // 하나의 완성된 frame만 child가 해석하도록 pipe writer를 flush한다.
}

/// 함수 이름: completed_frame_payload()
/// 기능: split prefix와 body를 보존하면서 완성된 한 frame의 payload slice만 제공한다.
/// 인자: bytes -> 누적 framing buffer, limit -> 해당 메시지의 최대 크기
/// 반환값: 아직 미완료면 None, 완료면 payload, invalid/trailing bytes면 오류
/// 작성 날짜: 2026/09/06
pub(super) fn completed_frame_payload(bytes: &[u8], limit: usize) -> io::Result<Option<&[u8]>> {
    if bytes.len() < 4 {
        return Ok(None);
    }

    // Prefix의 길이를 allocation 전에 확인하고 두 frame을 READY 한 건으로 합치지 않는다.
    let size = u32::from_be_bytes(bytes[..4].try_into().expect("four-byte prefix")) as usize;
    if size == 0 || size > limit || limit > MAXIMUM_FRAME_BYTES || bytes.len() > size + 4 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Invalid IPC frame size",
        ));
    }
    Ok((bytes.len() == size + 4).then_some(&bytes[4..])) // Timeout은 buffer 소유권을 바꾸지 않는다.
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 함수 이름: frame_wire_and_partial_reads_are_exact()
    /// 기능: Python과 동일한 prefix와 split header/body 경계를 모두 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/06
    #[test]
    fn frame_wire_and_partial_reads_are_exact() {
        // 같은 bytes를 어느 경계에서 나눠 읽어도 완성 전에는 payload를 공개하지 않는다.
        let mut encoded = Vec::new();
        write_frame(&mut encoded, CLOSED_ACK_PAYLOAD, MAXIMUM_FRAME_BYTES).unwrap();
        assert_eq!(&encoded[..4], &[0, 0, 0, 21]);
        for count in 0..encoded.len() {
            assert!(
                completed_frame_payload(&encoded[..count], MAXIMUM_FRAME_BYTES)
                    .unwrap()
                    .is_none()
            );
        }
        assert_eq!(
            completed_frame_payload(&encoded, MAXIMUM_FRAME_BYTES).unwrap(),
            Some(CLOSED_ACK_PAYLOAD)
        );
    }

    /// 함수 이름: invalid_lengths_and_trailing_frames_fail_closed()
    /// 기능: empty/oversize length와 READY 뒤 추가 frame을 allocation이나 공개 전에 차단한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/06
    #[test]
    fn invalid_lengths_and_trailing_frames_fail_closed() {
        // 메시지별 제한과 전체 제한을 독립적으로 검증하며 거부된 write는 bytes를 남기지 않는다.
        let mut encoded = Vec::new();
        assert!(write_frame(&mut encoded, b"", 4096).is_err());
        assert!(write_frame(&mut encoded, &[0; 5], 4).is_err());
        assert!(encoded.is_empty());
        assert!(completed_frame_payload(&[0; 4], 4096).is_err());
        assert!(completed_frame_payload(&[0, 0, 16, 1], 4096).is_err());
        assert!(completed_frame_payload(&[0, 0, 0, 1, b'{', b'}'], 4096).is_err());
        assert!(completed_frame_payload(&[255; 4], MAXIMUM_FRAME_BYTES).is_err());
        assert_eq!(MAXIMUM_BOOTSTRAP_FRAME_BYTES, 16 * 1024);
    }
    /// 함수 이름: bootstrap_uses_exact_secret_channel_and_readonly_configuration()
    /// 기능: production Windows serializer가 exact envelope와 기존 false/null order gate를 보존하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/06
    #[test]
    fn bootstrap_uses_exact_secret_channel_and_readonly_configuration() {
        // 실제 source serializer에 synthetic credentials만 주입해 외부 저장소 접근 없이 wire를 확인한다.
        let token = "A".repeat(43);
        let configuration = SidecarBootstrapConfiguration {
            schema_version: 3,
            allowed_origin: "http://127.0.0.1:5173",
            history_path: "C:\\fixture\\history.jsonl",
            api_key: "fixture-key",
            api_secret: "fixture-secret",
            allow_testnet_orders: false,
            max_notional: None,
        };
        let payload = serialize_bootstrap_frame(&token, &configuration).unwrap();
        let mut frame = Vec::new();
        write_frame(&mut frame, &payload, MAXIMUM_BOOTSTRAP_FRAME_BYTES).unwrap();
        let decoded: serde_json::Value = serde_json::from_slice(
            completed_frame_payload(&frame, MAXIMUM_BOOTSTRAP_FRAME_BYTES)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(decoded.as_object().unwrap().len(), 3);
        assert_eq!(decoded["type"], "BOOTSTRAP");
        assert_eq!(decoded["token"], token);
        assert_eq!(decoded["configuration"].as_object().unwrap().len(), 7);
        assert_eq!(decoded["configuration"]["allow_testnet_orders"], false);
        assert_eq!(
            decoded["configuration"]["max_notional"],
            serde_json::Value::Null
        );
    }
}
