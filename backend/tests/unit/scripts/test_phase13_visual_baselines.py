"""Phase 13 Figma 16-state 기준 이미지 manifest와 PNG 무결성을 검증한다."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


# Repository 기준 경로와 승인한 16-state mapping을 test process의 CWD와 분리한다.
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
BASELINE_MANIFEST_PATH = (
    REPOSITORY_ROOT / "UI" / "visual-regression" / "baseline_manifest.json"
)
BASELINE_DIRECTORY = REPOSITORY_ROOT / "UI" / "visual-regression" / "figma"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EXPECTED_FRAME_REFERENCES = (
    ("realtime-indicator", "01-realtime-indicator.png"),
    ("recent-orders", "02-recent-orders.png"),
    ("indicator-settings", "03-indicator-settings.png"),
    ("trade-history", "04-trade-history.png"),
    ("start-confirmation", "05-start-confirm.png"),
    ("stop-with-position", "06-stop-with-position.png"),
    ("stop-without-position", "07-stop-no-position.png"),
    ("csv-end-calendar", "08-csv-end-calendar.png"),
    ("csv-start-calendar", "09-csv-start-calendar.png"),
    ("csv-dialog", "10-csv-no-calendar.png"),
    ("regime-confirmation", "11-regime-confirm.png"),
    ("regime-required", "12-regime-required.png"),
    ("regime-highlight", "13-regime-highlight.png"),
    ("history-empty", "14-history-empty.png"),
    ("start-loading", "15-start-loading.png"),
    ("csv-validation-error", "16-csv-error.png"),
)


def load_baseline_manifest() -> dict[str, object]:
    """
    함수 이름: load_baseline_manifest()
    기능: versioned Figma baseline manifest를 UTF-8 JSON 객체로 읽는다.
    인자: 없음
    반환값: baseline metadata와 frame 목록을 가진 JSON 객체
    작성 날짜: 2026/08/29
    """
    # Fixed repository path의 UTF-8 manifest를 읽고 JSON root type을 caller 전에 고정한다.
    manifest_value = json.loads(
        BASELINE_MANIFEST_PATH.read_text(encoding="utf-8")
    )
    if not isinstance(manifest_value, dict):
        raise TypeError("baseline manifest root must be an object")

    return manifest_value  # 검증된 object root만 test 본문에 전달한다.


def read_png_dimensions(image_path: Path) -> tuple[int, int]:
    """
    함수 이름: read_png_dimensions()
    기능: PNG signature와 첫 IHDR를 검증하고 pixel width와 height를 읽는다.
    인자: image_path -> 검사할 baseline PNG 경로
    반환값: PNG IHDR의 width와 height
    작성 날짜: 2026/08/29
    """
    image_bytes = image_path.read_bytes()
    if len(image_bytes) < 24:
        raise ValueError("baseline PNG is too short")
    if image_bytes[:8] != PNG_SIGNATURE or image_bytes[12:16] != b"IHDR":
        raise ValueError("baseline file is not a canonical PNG")

    # PNG IHDR의 width와 height는 각각 4-byte big-endian unsigned integer이다.
    width = int.from_bytes(image_bytes[16:20], byteorder="big", signed=False)
    height = int.from_bytes(image_bytes[20:24], byteorder="big", signed=False)

    return width, height


class Phase13VisualBaselineTests(unittest.TestCase):
    """
    클래스 이름: Phase13VisualBaselineTests
    기능: 16개 Figma reference의 mapping, viewport와 byte identity를 검증한다.
    작성 날짜: 2026/08/29
    """

    def test_manifest_maps_exactly_sixteen_fixture_references(self) -> None:
        """
        함수 이름: test_manifest_maps_exactly_sixteen_fixture_references()
        기능: manifest와 directory가 승인한 16개 frame과 파일만 포함하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        manifest = load_baseline_manifest()
        frame_values = manifest.get("frames")
        self.assertIsInstance(frame_values, list)
        assert isinstance(frame_values, list)

        # JSON row의 key와 reference filename을 순서까지 포함해 canonical mapping과 비교한다.
        observed_references = tuple(
            (frame_value["frame_key"], frame_value["reference_file"])
            for frame_value in frame_values
            if isinstance(frame_value, dict)
        )
        actual_file_names = tuple(
            sorted(image_path.name for image_path in BASELINE_DIRECTORY.glob("*.png"))
        )

        self.assertEqual(observed_references, EXPECTED_FRAME_REFERENCES)
        self.assertEqual(
            actual_file_names,
            tuple(sorted(reference[1] for reference in EXPECTED_FRAME_REFERENCES)),
        )

    def test_every_reference_matches_digest_and_viewport(self) -> None:
        """
        함수 이름: test_every_reference_matches_digest_and_viewport()
        기능: 각 PNG가 manifest SHA-256과 1440×1024 IHDR 크기를 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        manifest = load_baseline_manifest()
        frame_values = manifest.get("frames")
        viewport_value = manifest.get("viewport")
        self.assertIsInstance(frame_values, list)
        self.assertIsInstance(viewport_value, dict)
        assert isinstance(frame_values, list)
        assert isinstance(viewport_value, dict)
        expected_dimensions = (
            viewport_value.get("width"),
            viewport_value.get("height"),
        )

        # Reference 교체와 이미지 손상을 모두 감지하도록 digest와 IHDR를 함께 검증한다.
        for frame_value in frame_values:
            with self.subTest(frame=frame_value):
                self.assertIsInstance(frame_value, dict)
                assert isinstance(frame_value, dict)
                reference_file = frame_value.get("reference_file")
                expected_digest = frame_value.get("sha256")
                self.assertIsInstance(reference_file, str)
                self.assertIsInstance(expected_digest, str)
                assert isinstance(reference_file, str)
                assert isinstance(expected_digest, str)
                image_path = BASELINE_DIRECTORY / reference_file
                actual_digest = hashlib.sha256(image_path.read_bytes()).hexdigest()

                self.assertEqual(actual_digest, expected_digest)
                self.assertEqual(read_png_dimensions(image_path), expected_dimensions)


if __name__ == "__main__":
    unittest.main()
