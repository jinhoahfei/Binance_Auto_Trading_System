//! Windows anonymous stdio pipe를 bounded framing과 deadline으로 연결한다.

use super::super::{
    framed, parse_ready_descriptor, ReadyDescriptorReadFailure, ReadyDescriptorWire,
    SidecarFailure, MAXIMUM_READY_BYTES,
};
use super::super::{ControlWriter, ReadyReader};
use std::io::{self, Read};
use std::os::windows::io::AsRawHandle;
use std::thread;
use std::time::{Duration, Instant};
use windows_sys::Win32::System::Pipes::PeekNamedPipe;
use zeroize::{Zeroize, Zeroizing};

/// 함수 이름: write_closed_ack()
/// 기능: HTTP CLOSED barrier 뒤 parent의 exact CLOSED_ACK frame을 stdio에 전달한다.
/// 인자: writer -> child stdin을 소유한 parent pipe
/// 반환값: bounded frame write 결과
/// 작성 날짜: 2026/09/06
pub(in crate::sidecar) fn write_closed_ack(writer: &mut ControlWriter) -> io::Result<()> {
    framed::write_frame(writer, framed::CLOSED_ACK_PAYLOAD, 256) // Writer drop이 뒤이어 부모 EOF를 전달한다.
}

/// 함수 이름: available_pipe_bytes()
/// 기능: anonymous pipe의 준비된 bytes를 block 없이 조회한다.
/// 인자: reader -> child stdout을 소유한 parent pipe
/// 반환값: 읽을 수 있는 bytes 또는 EOF/OS 오류
/// 작성 날짜: 2026/09/06
fn available_pipe_bytes(reader: &ReadyReader) -> io::Result<usize> {
    let mut available = 0;
    // Std anonymous pipe도 PeekNamedPipe를 지원하며 이 handle의 reader는 하나뿐이다.
    if unsafe {
        PeekNamedPipe(
            reader.as_raw_handle(),
            std::ptr::null_mut(),
            0,
            std::ptr::null_mut(),
            &mut available,
            std::ptr::null_mut(),
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(available as usize)
}

/// 함수 이름: read_ready_descriptor()
/// 기능: partial prefix/body를 timeout 사이에 보존하며 bounded secret-free READY를 검증한다.
/// 인자: reader -> stdout reader, payload -> 보존할 framing bytes, timeout -> 현재 attempt의 deadline
/// 반환값: strict READY, retryable timeout 또는 terminal failure
/// 작성 날짜: 2026/09/06
pub(in crate::sidecar) fn read_ready_descriptor(
    reader: &mut ReadyReader,
    payload: &mut Zeroizing<Vec<u8>>,
    timeout: Duration,
) -> Result<ReadyDescriptorWire, ReadyDescriptorReadFailure> {
    let deadline = Instant::now() + timeout;
    let mut chunk = [0_u8; 512];

    // Peek 후 available 만큼만 읽으므로 blocking pipe에서도 deadline과 late-ready 복구를 유지한다.
    loop {
        match framed::completed_frame_payload(payload, MAXIMUM_READY_BYTES) {
            Ok(Some(json)) => {
                let descriptor = parse_ready_descriptor(json);
                payload.zeroize();
                return descriptor.map_err(ReadyDescriptorReadFailure::Terminal);
            }
            Err(_) => {
                payload.zeroize();
                return Err(ReadyDescriptorReadFailure::Terminal(
                    SidecarFailure::descriptor_rejected(),
                ));
            }
            Ok(None) => (),
        }
        if Instant::now() >= deadline {
            return Err(ReadyDescriptorReadFailure::Timeout);
        }
        let available = match available_pipe_bytes(reader) {
            Ok(available) => available,
            Err(_) => {
                payload.zeroize();
                return Err(ReadyDescriptorReadFailure::Terminal(
                    SidecarFailure::startup(),
                ));
            }
        };
        if available == 0 {
            thread::sleep(Duration::from_millis(10));
            continue;
        }
        if available > MAXIMUM_READY_BYTES + 4 - payload.len() {
            payload.zeroize();
            return Err(ReadyDescriptorReadFailure::Terminal(
                SidecarFailure::descriptor_rejected(),
            ));
        }
        let read_size = available.min(chunk.len());
        match reader.read(&mut chunk[..read_size]) {
            Ok(0) | Err(_) => {
                payload.zeroize();
                return Err(ReadyDescriptorReadFailure::Terminal(
                    SidecarFailure::startup(),
                ));
            }
            Ok(count) => payload.extend_from_slice(&chunk[..count]),
        }
    }
}
