"""Offline checks for privacy and retry behavior of transactional mail."""

import json
import os
import smtplib
import ssl
import unittest
from unittest.mock import MagicMock, patch

import app as site
from mailer import delivery_configured, send_update_email


class _Response:
    def __init__(self, status, body=b'{}', retry_after=None):
        self.status = status
        self.body = body
        self.retry_after = retry_after

    def getheader(self, name):
        return self.retry_after if name == "Retry-After" else None

    def read(self, count):
        return self.body[:count]


class _Connection:
    responses = []
    requests = []

    def __init__(self, host, timeout, context):
        self.host = host
        self.timeout = timeout

    def request(self, method, path, body, headers):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        pass


class _SMTP:
    instances = []
    error_on_login = None
    error_on_send = None
    refused = {}

    def __init__(self, host, port, timeout, context):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.login_args = None
        self.message = None
        self.closed = False
        self.instances.append(self)

    def login(self, username, password):
        self.login_args = (username, password)
        if self.error_on_login:
            raise self.error_on_login

    def send_message(self, message, *, from_addr, to_addrs):
        self.message = message
        self.from_addr, self.to_addrs = from_addr, to_addrs
        if self.error_on_send:
            raise self.error_on_send
        return self.refused

    def close(self):
        self.closed = True


class MailerTests(unittest.TestCase):
    def setUp(self):
        _Connection.responses = []
        _Connection.requests = []
        _SMTP.instances = []
        _SMTP.error_on_login = None
        _SMTP.error_on_send = None
        _SMTP.refused = {}
        self.settings = patch.dict(
            os.environ,
            {"RESEND_API_KEY": "re_test_validtoken", "PH_EMAIL_FROM": "updates@example.com",
             "PH_GMAIL_USER": "", "PH_GMAIL_APP_PASSWORD": ""},
        )
        self.transport = patch("mailer.http.client.HTTPSConnection", _Connection)
        self.gmail_transport = patch("mailer.smtplib.SMTP_SSL", _SMTP)
        self.settings.start()
        self.transport.start()
        self.gmail_transport.start()
        self.addCleanup(self.settings.stop)
        self.addCleanup(self.transport.stop)
        self.addCleanup(self.gmail_transport.stop)

    def test_accepted_mail_contains_only_generic_update_and_stable_key(self):
        _Connection.responses = [_Response(200, b'{"id":"email_1"}'), _Response(200, b'{"id":"email_1"}')]
        first = send_update_email("student@gmail.com", event_id="invoice-123")
        second = send_update_email("student@gmail.com", event_id="invoice-123")
        self.assertTrue(first.sent)
        self.assertEqual(first.provider_id, "email_1")
        self.assertTrue(second.sent)
        self.assertEqual(len(_Connection.requests), 2)
        keys = [request[3]["Idempotency-Key"] for request in _Connection.requests]
        self.assertEqual(keys[0], keys[1])
        self.assertNotIn("invoice-123", keys[0])
        payload = json.loads(_Connection.requests[0][2])
        self.assertEqual(payload["to"], ["student@gmail.com"])
        self.assertNotIn("123", payload["text"])
        self.assertNotIn("student@gmail.com", payload["text"])

    def test_class_cancellation_has_specific_private_resend_notice(self):
        _Connection.responses = [_Response(200, b'{"id":"email_cancel"}')]
        result = send_update_email("student@gmail.com", event_id="class-event-1",
                                   event_type="class_cancelled")
        self.assertTrue(result.sent)
        payload = json.loads(_Connection.requests[0][2])
        self.assertIn("ยกคลาส", payload["subject"])
        self.assertIn("ยกคลาส", payload["text"])
        self.assertNotIn("student@gmail.com", payload["text"])
        self.assertNotIn("class-event-1", payload["text"])

    def test_rate_limit_is_retryable_without_exposing_provider_response(self):
        _Connection.responses = [_Response(429, b'{"error":"private detail"}', "30")]
        result = send_update_email("teacher@gmail.com", event_id="event-1")
        self.assertFalse(result.sent)
        self.assertTrue(result.retryable)
        self.assertEqual(result.retry_after_seconds, 30)
        self.assertNotIn("private detail", str(result))

    def test_invalid_address_or_unconfigured_mail_sends_nothing(self):
        self.assertEqual(send_update_email("bad\n@example.com", event_id="x").code, "invalid_recipient")
        with patch.dict(os.environ, {"RESEND_API_KEY": ""}):
            self.assertEqual(send_update_email("student@gmail.com", event_id="x").code, "not_configured")
        self.assertEqual(_Connection.requests, [])
        self.assertEqual(_SMTP.instances, [])

    def test_gmail_takes_priority_and_uses_verified_tls_with_generic_message(self):
        with patch.dict(os.environ, {
            "PH_GMAIL_USER": "physicsplus.test@gmail.com",
            "PH_GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
        }):
            self.assertTrue(delivery_configured())
            result = send_update_email("student@gmail.com", event_id="invoice-123")
        self.assertTrue(result.sent)
        self.assertEqual(result.code, "accepted")
        self.assertIsNone(result.provider_id)
        self.assertEqual(_Connection.requests, [])
        self.assertEqual(len(_SMTP.instances), 1)
        smtp = _SMTP.instances[0]
        self.assertEqual((smtp.host, smtp.port, smtp.timeout), ("smtp.gmail.com", 465, 5.0))
        self.assertEqual(smtp.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(smtp.context.check_hostname)
        self.assertEqual(smtp.login_args, ("physicsplus.test@gmail.com", "abcdefghijklmnop"))
        self.assertEqual((smtp.from_addr, smtp.to_addrs), ("physicsplus.test@gmail.com", ["student@gmail.com"]))
        self.assertEqual(smtp.message["From"], "physicsplus.test@gmail.com")
        self.assertEqual(smtp.message["To"], "student@gmail.com")
        self.assertNotIn("invoice-123", smtp.message.get_content())
        self.assertNotIn("student@gmail.com", smtp.message.get_content())
        self.assertTrue(smtp.closed)
        self.assertNotIn("abcdefghijklmnop", str(result))

    def test_class_cancellation_has_specific_private_gmail_notice(self):
        with patch.dict(os.environ, {
            "PH_GMAIL_USER": "physicsplus.test@gmail.com",
            "PH_GMAIL_APP_PASSWORD": "abcdefghijklmnop",
        }):
            result = send_update_email("student@gmail.com", event_id="class-event-2",
                                       event_type="class_cancelled")
        self.assertTrue(result.sent)
        message = _SMTP.instances[0].message
        self.assertIn("ยกคลาส", str(message["Subject"]))
        self.assertIn("ยกคลาส", message.get_content())
        self.assertNotIn("student@gmail.com", message.get_content())

    def test_gmail_app_password_accepts_copied_whitespace_only(self):
        with patch.dict(os.environ, {
            "PH_GMAIL_USER": "physicsplus.test@gmail.com",
            "PH_GMAIL_APP_PASSWORD": "abcd efgh\nijkl\tmnop",
        }):
            result = send_update_email("student@gmail.com", event_id="event-whitespace")
        self.assertTrue(result.sent)
        self.assertEqual(_SMTP.instances[0].login_args,
                         ("physicsplus.test@gmail.com", "abcdefghijklmnop"))

        _SMTP.instances = []
        with patch.dict(os.environ, {
            "PH_GMAIL_USER": "physicsplus.test@gmail.com",
            "PH_GMAIL_APP_PASSWORD": "abcd-efgh-ijkl-mnop",
        }):
            invalid = send_update_email("student@gmail.com", event_id="event-punctuation")
        self.assertEqual(invalid.code, "invalid_configuration")
        self.assertEqual(_SMTP.instances, [])

    def test_email_diagnostic_has_fixed_private_gmail_template(self):
        with patch.dict(os.environ, {
            "PH_GMAIL_USER": "physicsplus.test@gmail.com",
            "PH_GMAIL_APP_PASSWORD": "abcdefghijklmnop",
        }):
            result = send_update_email("admin@gmail.com", event_id="test-event-1",
                                       event_type="email_test")
        self.assertTrue(result.sent)
        message = _SMTP.instances[0].message
        self.assertIn("ทดสอบระบบแจ้งเตือน", str(message["Subject"]))
        self.assertIn("อีเมลทดสอบ", message.get_content())
        self.assertNotIn("admin@gmail.com", message.get_content())
        self.assertNotIn("test-event-1", message.get_content())

    def test_gmail_auth_and_provider_errors_are_private_and_classified(self):
        with patch.dict(os.environ, {
            "PH_GMAIL_USER": "physicsplus.test@gmail.com",
            "PH_GMAIL_APP_PASSWORD": "abcdefghijklmnop",
        }):
            _SMTP.error_on_login = smtplib.SMTPAuthenticationError(535, b"private auth detail")
            invalid = send_update_email("student@gmail.com", event_id="event-1")
            self.assertEqual(invalid.code, "invalid_configuration")
            self.assertFalse(invalid.retryable)
            self.assertNotIn("private", str(invalid))
            self.assertTrue(_SMTP.instances[-1].closed)

            _SMTP.error_on_login = smtplib.SMTPAuthenticationError(454, b"private temporary auth detail")
            temporary_auth = send_update_email("student@gmail.com", event_id="event-1b")
            self.assertEqual((temporary_auth.code, temporary_auth.retryable, temporary_auth.status),
                             ("temporary_failure", True, 454))

            _SMTP.error_on_login = None
            _SMTP.error_on_send = smtplib.SMTPDataError(421, b"private transient detail")
            transient = send_update_email("student@gmail.com", event_id="event-2")
            self.assertEqual((transient.code, transient.retryable, transient.status),
                             ("temporary_failure", True, 421))
            self.assertNotIn("private", str(transient))

            _SMTP.error_on_send = smtplib.SMTPDataError(550, b"private rejection detail")
            rejected = send_update_email("student@gmail.com", event_id="event-3")
            self.assertEqual((rejected.code, rejected.retryable, rejected.status),
                             ("provider_rejected", False, 550))
            self.assertNotIn("private", str(rejected))

            _SMTP.error_on_send = None
            _SMTP.refused = {"student@gmail.com": (450, b"private temporary refusal")}
            refused = send_update_email("student@gmail.com", event_id="event-4")
            self.assertEqual((refused.code, refused.retryable, refused.status),
                             ("temporary_failure", True, 450))

    def test_invalid_gmail_configuration_does_not_connect_or_downgrade_to_resend(self):
        with patch.dict(os.environ, {"PH_GMAIL_USER": "bad\n@gmail.com", "PH_GMAIL_APP_PASSWORD": "abcdefghijklmnop"}):
            self.assertEqual(send_update_email("student@gmail.com", event_id="x").code,
                             "invalid_configuration")
        self.assertEqual(_SMTP.instances, [])
        self.assertEqual(_Connection.requests, [])

    def test_partial_gmail_configuration_uses_resend_when_available(self):
        _Connection.responses = [_Response(200, b'{"id":"email_2"}')]
        with patch.dict(os.environ, {"PH_GMAIL_USER": "physicsplus.test@gmail.com", "PH_GMAIL_APP_PASSWORD": ""}):
            self.assertTrue(delivery_configured())
            self.assertTrue(send_update_email("student@gmail.com", event_id="x").sent)
        self.assertEqual(len(_Connection.requests), 1)
        self.assertEqual(_SMTP.instances, [])

    def test_dispatch_gate_accepts_gmail_without_resend(self):
        owner = MagicMock()
        owner.dispatch_outbox.return_value = {"processed": 0, "sent": 0, "retrying": 0, "dead": 0}
        with patch.dict(os.environ, {
            "PH_GMAIL_USER": "physicsplus.test@gmail.com",
            "PH_GMAIL_APP_PASSWORD": "abcdefghijklmnop",
            "RESEND_API_KEY": "", "PH_EMAIL_FROM": "",
            "PH_ADMIN_NOTIFICATION_EMAIL": "owner@gmail.com",
        }), patch.object(site, "_backend", return_value=owner):
            result = site._dispatch_notifications(limit=2)
        self.assertEqual(result, owner.dispatch_outbox.return_value)
        owner.dispatch_outbox.assert_called_once_with(
            site.send_update_email, limit=2, admin_email="owner@gmail.com"
        )


if __name__ == "__main__":
    unittest.main()
