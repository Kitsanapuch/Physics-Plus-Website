"""Local QR inspection is advisory and never proves a bank transfer."""

from __future__ import annotations

import binascii
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import PhysicsPlusApp, _emv_qr_checksum_valid, inspect_slip_qr


HAS_QR_DECODER = (importlib.util.find_spec("PIL") is not None and
                  importlib.util.find_spec("zxingcpp") is not None)


@unittest.skipUnless(HAS_QR_DECODER, "Pillow and zxing-cpp are needed for QR image tests")
class SlipQrImageTests(unittest.TestCase):
    @staticmethod
    def qr_png(payload: str) -> bytes:
        import zxingcpp
        from PIL import Image

        barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode)
        image = Image.fromarray(barcode.to_image(scale=6))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def test_real_url_qr_decodes_without_exposing_or_opening_url(self) -> None:
        payload = "https://bank.example/slip/transaction-123"
        result = inspect_slip_qr(self.qr_png(payload))
        self.assertEqual(result["status"], "decoded")
        self.assertEqual(result["payload_type"], "url")
        self.assertIsNone(result["checksum_valid"])
        self.assertEqual(len(result["payload_fingerprint"]), 64)
        self.assertNotIn(payload, str(result))
        self.assertIn("ไม่ยืนยันว่าโอนเงินจริง", result["message"])

    def test_emv_crc_is_structural_only_and_tampering_is_visible(self) -> None:
        prefix = "00020101021153037646304"
        payload = prefix + f"{binascii.crc_hqx(prefix.encode('ascii'), 0xFFFF):04X}"
        self.assertTrue(_emv_qr_checksum_valid(payload))
        decoded = inspect_slip_qr(self.qr_png(payload))
        self.assertEqual(decoded["status"], "decoded")
        self.assertEqual(decoded["payload_type"], "emv_payment")
        self.assertTrue(decoded["checksum_valid"])
        self.assertIn("ไม่ยืนยันว่าโอนเงินจริง", decoded["message"])
        tampered = payload[:-1] + ("0" if payload[-1] != "0" else "1")
        self.assertFalse(_emv_qr_checksum_valid(tampered))
        invalid = inspect_slip_qr(self.qr_png(tampered))
        self.assertEqual(invalid["status"], "invalid")
        self.assertFalse(invalid["checksum_valid"])

    def test_missing_qr_is_not_a_claim_of_fraud(self) -> None:
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(output, format="PNG")
        result = inspect_slip_qr(output.getvalue())
        self.assertEqual(result["status"], "not_found")
        self.assertIsNone(result["checksum_valid"])

    def test_malformed_qr_url_does_not_break_admin_review(self) -> None:
        result = inspect_slip_qr(self.qr_png("https://[bad-ipv6/slip"))
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["payload_type"], "url")


class SlipQrCacheTests(unittest.TestCase):
    def test_result_cached_by_file_digest_without_retaining_image_bytes(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        app = PhysicsPlusApp(Path(temporary.name) / "qr.sqlite3", "https://school.example")
        sample = b"safe-synthetic-image-byte-sequence"
        result = {"status": "not_found", "payload_type": None, "checksum_valid": None,
                  "payload_fingerprint": None, "message": "No QR"}
        with patch("backend.inspect_slip_qr", return_value=result) as decoder:
            self.assertEqual(app._slip_qr_result(sample), result)
            self.assertEqual(app._slip_qr_result(sample), result)
            decoder.assert_called_once_with(sample)
        self.assertEqual(len(app._slip_qr_cache), 1)
        self.assertTrue(all(isinstance(key, str) and len(key) == 64
                            for key in app._slip_qr_cache))


if __name__ == "__main__":
    unittest.main()
