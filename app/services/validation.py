"""
Validation service — Step 2 of the pipeline.

Validates incoming leads for:
- Email format (via Pydantic EmailStr)
- Required fields (name, email, company, message)
- Duplicate detection (SHA-256 content hash)
- Spam/fake lead detection (disposable domains, gibberish)

Invalid leads are rejected immediately and never enter the async pipeline.
"""

import hashlib
import re

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import Lead


# --- Disposable email domains (common spam sources) ---
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "tempmail.com", "throwaway.email",
    "yopmail.com", "sharklasers.com", "guerrillamailblock.com", "grr.la",
    "dispostable.com", "maildrop.cc", "tempail.com", "fakeinbox.com",
    "trashmail.com", "temp-mail.org", "10minutemail.com", "getnada.com",
    "mohmal.com", "mailnesia.com", "mintemail.com", "burnermail.io",
}

# --- Spam keywords in message ---
SPAM_KEYWORDS = [
    "buy now", "click here", "limited offer", "act now", "free money",
    "congratulations you won", "nigerian prince", "wire transfer",
    "make money fast", "100% free", "no obligation",
]


def generate_payload_hash(email: str, company: str, message: str) -> str:
    """Generate a deterministic content hash for deduplication.

    Uses SHA-256 of normalized email + company + message.
    No timestamp component — same content always produces same hash.

    Args:
        email: Lead's email address.
        company: Lead's company name.
        message: Lead's message text.

    Returns:
        str: 64-character hex digest of the SHA-256 hash.
    """
    content = f"{email.lower().strip()}::{company.lower().strip()}::{message.strip()}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_email_format(email: str) -> tuple[bool, str | None]:
    """Validate email format using regex.

    Note: Pydantic's EmailStr already validates in the schema layer.
    This is a defense-in-depth check for non-schema entry points (CSV, webhook).

    Args:
        email: Email address to validate.

    Returns:
        tuple: (is_valid, error_reason or None)
    """
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(pattern, email):
        return False, "INVALID_EMAIL_FORMAT"
    return True, None


def check_disposable_email(email: str) -> tuple[bool, str | None]:
    """Check if the email uses a disposable/temporary domain.

    Args:
        email: Email address to check.

    Returns:
        tuple: (is_spam, reason or None)
    """
    domain = email.lower().split("@")[-1]
    if domain in DISPOSABLE_DOMAINS:
        return True, f"SPAM_DETECTED: disposable email domain ({domain})"
    return False, None


def check_spam_content(message: str) -> tuple[bool, str | None]:
    """Check message content for spam keywords.

    Args:
        message: Lead's message text.

    Returns:
        tuple: (is_spam, reason or None)
    """
    message_lower = message.lower()
    for keyword in SPAM_KEYWORDS:
        if keyword in message_lower:
            return True, f"SPAM_DETECTED: contains spam keyword ({keyword})"
    return False, None


def check_gibberish(text: str) -> tuple[bool, str | None]:
    """Detect gibberish text (low letter-to-character ratio, excessive repetition).

    Args:
        text: Text to check for gibberish patterns.

    Returns:
        tuple: (is_gibberish, reason or None)
    """
    if len(text.strip()) < 3:
        return True, "SPAM_DETECTED: text too short to be meaningful"

    # Check letter-to-total ratio
    letters = sum(1 for c in text if c.isalpha())
    if len(text) > 10 and letters / len(text) < 0.3:
        return True, "SPAM_DETECTED: text appears to be gibberish (low letter ratio)"

    # Check for excessive character repetition (e.g., "aaaaaa")
    if re.search(r'(.)\1{5,}', text):
        return True, "SPAM_DETECTED: excessive character repetition"

    return False, None


def validate_required_fields(
    name: str | None,
    email: str | None,
    company: str | None,
    message: str | None,
) -> tuple[bool, str | None]:
    """Check that all required fields are present and non-empty.

    Args:
        name: Lead's name.
        email: Lead's email.
        company: Lead's company.
        message: Lead's message.

    Returns:
        tuple: (is_valid, error_reason or None)
    """
    fields = {"name": name, "email": email, "company": company, "message": message}
    for field_name, value in fields.items():
        if not value or not str(value).strip():
            return False, f"MISSING_REQUIRED_FIELD: {field_name}"
    return True, None


async def check_duplicate(
    db: AsyncSession,
    payload_hash: str,
) -> tuple[bool, str | None, str | None]:
    """Check if a lead with the same content hash already exists.

    Args:
        db: Async database session.
        payload_hash: SHA-256 hash of the lead content.

    Returns:
        tuple: (is_duplicate, reason or None, original_lead_id or None)
    """
    result = await db.execute(
        select(Lead.id).where(Lead.payload_hash == payload_hash)
    )
    existing = result.scalar_one_or_none()
    if existing:
        return True, f"DUPLICATE_LEAD (original: {existing})", str(existing)
    return False, None, None


async def validate_lead(
    name: str,
    email: str,
    company: str,
    message: str,
    db: AsyncSession,
) -> tuple[bool, str | None, str | None]:
    """Run all validation checks on an incoming lead.

    Checks are ordered by cost: cheap checks first, DB lookup last.

    Args:
        name: Lead's name.
        email: Lead's email address.
        company: Lead's company name.
        message: Lead's message text.
        db: Async database session.

    Returns:
        tuple: (is_valid, failure_reason or None, payload_hash)
    """
    # 1. Required fields (cheapest check)
    valid, reason = validate_required_fields(name, email, company, message)
    if not valid:
        logger.warning("Validation failed: missing fields", reason=reason, email=email)
        return False, reason, None

    # 2. Email format
    valid, reason = validate_email_format(email)
    if not valid:
        logger.warning("Validation failed: invalid email", reason=reason, email=email)
        return False, reason, None

    # 3. Disposable email check
    is_spam, reason = check_disposable_email(email)
    if is_spam:
        logger.warning("Validation failed: disposable email", reason=reason, email=email)
        return False, reason, None

    # 4. Spam content check
    is_spam, reason = check_spam_content(message)
    if is_spam:
        logger.warning("Validation failed: spam content", reason=reason, email=email)
        return False, reason, None

    # 5. Gibberish check on name and message
    is_gibberish, reason = check_gibberish(name)
    if is_gibberish:
        logger.warning("Validation failed: gibberish name", reason=reason, email=email)
        return False, f"SPAM_DETECTED: gibberish name", None

    is_gibberish, reason = check_gibberish(message)
    if is_gibberish:
        logger.warning("Validation failed: gibberish message", reason=reason, email=email)
        return False, reason, None

    # 6. Duplicate check (most expensive — hits DB)
    payload_hash = generate_payload_hash(email, company, message)
    is_dup, reason, _ = await check_duplicate(db, payload_hash)
    if is_dup:
        logger.warning("Validation failed: duplicate", reason=reason, email=email)
        return False, reason, payload_hash

    logger.info("Validation passed", email=email, payload_hash=payload_hash)
    return True, None, payload_hash
