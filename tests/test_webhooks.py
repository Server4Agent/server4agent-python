import hashlib
import hmac
import time

from server4agent import verify_webhook_signature

# Server4Agent signs webhook deliveries as HMAC-SHA256("{unix_ts}.{raw_body}")
# with the webhook's secret, header = "t=<ts>,v1=<hex>" (see
# lib/server/webhooks.ts in the main app). These vectors are computed
# independently with hmac/hashlib directly, not by calling back into the
# SDK's own signer, so a bug in verify_webhook_signature can't cancel out
# against a matching bug in how the vector was produced.


def independent_header(secret: str, body: str, ts: int) -> str:
    digest = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def test_accepts_a_validly_signed_payload():
    body = '{"event":"deployment.live","server_id":"srv_1"}'
    ts = int(time.time())
    header = independent_header("whsec_test", body, ts)
    assert verify_webhook_signature("whsec_test", body, header) is True


def test_rejects_a_wrong_secret():
    body = '{"event":"test"}'
    ts = int(time.time())
    header = independent_header("whsec_real", body, ts)
    assert verify_webhook_signature("whsec_wrong", body, header) is False


def test_rejects_a_tampered_body():
    body = '{"event":"test"}'
    ts = int(time.time())
    header = independent_header("whsec_test", body, ts)
    assert verify_webhook_signature("whsec_test", '{"event":"tampered"}', header) is False


def test_rejects_an_expired_timestamp():
    body = '{"event":"test"}'
    ts = int(time.time()) - 600
    header = independent_header("whsec_test", body, ts)
    assert verify_webhook_signature("whsec_test", body, header, tolerance_sec=300) is False


def test_respects_a_custom_tolerance():
    body = '{"event":"test"}'
    ts = int(time.time()) - 600
    header = independent_header("whsec_test", body, ts)
    assert verify_webhook_signature("whsec_test", body, header, tolerance_sec=3600) is True


def test_rejects_a_malformed_header():
    assert verify_webhook_signature("whsec_test", "{}", "not-a-real-header") is False


def test_rejects_a_missing_header():
    assert verify_webhook_signature("whsec_test", "{}", None) is False
