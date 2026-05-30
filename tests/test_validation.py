"""
Tests for the validation service.

Covers: email format, required fields, duplicate detection,
disposable email blocking, spam keyword detection, gibberish detection.
"""

import pytest

from app.services.validation import (
    check_disposable_email,
    check_gibberish,
    check_spam_content,
    generate_payload_hash,
    validate_email_format,
    validate_required_fields,
)


class TestEmailFormat:
    """Tests for email format validation."""

    def test_valid_email(self):
        valid, reason = validate_email_format("jane@acmecorp.com")
        assert valid is True
        assert reason is None

    def test_invalid_email_no_at(self):
        valid, reason = validate_email_format("janeatacmecorp.com")
        assert valid is False
        assert reason == "INVALID_EMAIL_FORMAT"

    def test_invalid_email_no_domain(self):
        valid, reason = validate_email_format("jane@")
        assert valid is False
        assert reason == "INVALID_EMAIL_FORMAT"

    def test_invalid_email_empty(self):
        valid, reason = validate_email_format("")
        assert valid is False
        assert reason == "INVALID_EMAIL_FORMAT"


class TestRequiredFields:
    """Tests for required field validation."""

    def test_all_fields_present(self):
        valid, reason = validate_required_fields("Jane", "jane@test.com", "Acme", "Need help")
        assert valid is True
        assert reason is None

    def test_missing_name(self):
        valid, reason = validate_required_fields("", "jane@test.com", "Acme", "Need help")
        assert valid is False
        assert reason == "MISSING_REQUIRED_FIELD: name"

    def test_missing_email(self):
        valid, reason = validate_required_fields("Jane", "", "Acme", "Need help")
        assert valid is False
        assert reason == "MISSING_REQUIRED_FIELD: email"

    def test_missing_company(self):
        valid, reason = validate_required_fields("Jane", "jane@test.com", "", "Need help")
        assert valid is False
        assert reason == "MISSING_REQUIRED_FIELD: company"

    def test_missing_message(self):
        valid, reason = validate_required_fields("Jane", "jane@test.com", "Acme", "")
        assert valid is False
        assert reason == "MISSING_REQUIRED_FIELD: message"

    def test_whitespace_only_name(self):
        valid, reason = validate_required_fields("   ", "jane@test.com", "Acme", "Need help")
        assert valid is False
        assert reason == "MISSING_REQUIRED_FIELD: name"


class TestDisposableEmail:
    """Tests for disposable email domain detection."""

    def test_disposable_domain(self):
        is_spam, reason = check_disposable_email("test@mailinator.com")
        assert is_spam is True
        assert "disposable" in reason.lower()

    def test_legitimate_domain(self):
        is_spam, reason = check_disposable_email("jane@acmecorp.com")
        assert is_spam is False
        assert reason is None

    def test_gmail_is_not_disposable(self):
        is_spam, reason = check_disposable_email("jane@gmail.com")
        assert is_spam is False


class TestSpamContent:
    """Tests for spam keyword detection in messages."""

    def test_spam_message(self):
        is_spam, reason = check_spam_content("Buy now! Limited offer!")
        assert is_spam is True
        assert "spam keyword" in reason.lower()

    def test_clean_message(self):
        is_spam, reason = check_spam_content("We need AI automation for our support pipeline")
        assert is_spam is False
        assert reason is None


class TestGibberish:
    """Tests for gibberish text detection."""

    def test_gibberish_short(self):
        is_gibberish, reason = check_gibberish("ab")
        assert is_gibberish is True

    def test_gibberish_repetition(self):
        is_gibberish, reason = check_gibberish("aaaaaaaaa")
        assert is_gibberish is True

    def test_normal_text(self):
        is_gibberish, reason = check_gibberish("We need help with AI automation")
        assert is_gibberish is False


class TestPayloadHash:
    """Tests for deterministic payload hashing."""

    def test_same_input_same_hash(self):
        hash1 = generate_payload_hash("jane@test.com", "Acme", "Hello")
        hash2 = generate_payload_hash("jane@test.com", "Acme", "Hello")
        assert hash1 == hash2

    def test_different_input_different_hash(self):
        hash1 = generate_payload_hash("jane@test.com", "Acme", "Hello")
        hash2 = generate_payload_hash("john@test.com", "Acme", "Hello")
        assert hash1 != hash2

    def test_case_insensitive_email(self):
        hash1 = generate_payload_hash("JANE@TEST.COM", "Acme", "Hello")
        hash2 = generate_payload_hash("jane@test.com", "Acme", "Hello")
        assert hash1 == hash2

    def test_hash_is_64_chars(self):
        hash1 = generate_payload_hash("jane@test.com", "Acme", "Hello")
        assert len(hash1) == 64

    def test_whitespace_normalization(self):
        hash1 = generate_payload_hash("  jane@test.com  ", "  Acme  ", "  Hello  ")
        hash2 = generate_payload_hash("jane@test.com", "Acme", "Hello")
        assert hash1 == hash2
