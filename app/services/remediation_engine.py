"""Auto-remediation engine for ML publish errors.

Matches errors against known rules, applies fixes, and retries publishing.
Logs all errors and attempts for learning and auditing.
"""

import copy
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.remediation import PublishErrorLog, RemediationAttempt, RemediationRule
from app.services.remediation_fixes import FIX_HANDLERS, apply_fix

logger = logging.getLogger(__name__)


def _parse_error(result: Dict[str, Any]) -> Tuple[str, str, List[str], List[str]]:
    """Extract error_code, error_message, cause_codes, and cause_messages from ML result.

    The result dict has these shapes:
        {"error": True, "status": int, "detail": "<raw json string>", "message": "...", "cause": [...]}
    Where cause entries are: {"type": "error"|"warning", "code": "...", "message": "..."}
    """
    error_code = result.get("error", "unknown_error")
    if isinstance(error_code, bool):
        error_code = "ml_validation_error"

    error_message = result.get("message", "")

    # ML embeds structured data directly after response.json() merge
    cause_list = result.get("cause", [])
    if isinstance(cause_list, str):
        cause_list = []

    cause_codes = []
    cause_messages = []
    for cause in cause_list:
        if isinstance(cause, dict):
            code = cause.get("code", "")
            msg = cause.get("message", "")
            if code:
                cause_codes.append(code)
            if msg:
                cause_messages.append(msg)

    # Use the most specific error code available
    if cause_codes:
        error_code = cause_codes[0]

    if not error_message and cause_messages:
        error_message = cause_messages[0]

    if not error_message:
        error_message = str(result.get("detail", "Unknown error"))[:500]

    return error_code, error_message, cause_codes, cause_messages


def _sanitize_payload_for_log(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Create a copy of publish_kwargs safe for storing in the DB.

    Replaces large fields (pictures) with summaries to avoid bloating the log.
    """
    sanitized = {}
    for key, value in kwargs.items():
        if key == "pictures" and isinstance(value, list):
            sanitized[key] = f"[{len(value)} pictures]"
        elif key == "db":
            continue  # Skip the DB session
        elif key == "description" and isinstance(value, str) and len(value) > 500:
            sanitized[key] = value[:500] + "..."
        else:
            try:
                json.dumps(value)
                sanitized[key] = value
            except (TypeError, ValueError):
                sanitized[key] = str(value)[:200]
    return sanitized


async def _log_error(
    db: AsyncSession,
    user_id: int,
    listing_id: int,
    product_id: int,
    meli_category_id: Optional[str],
    error_code: str,
    error_message: str,
    result: Dict[str, Any],
    publish_kwargs: Dict[str, Any],
    request_data: Optional[Dict[str, Any]],
) -> Optional[PublishErrorLog]:
    """Insert a PublishErrorLog row. Returns None on DB failure."""
    try:
        # Build a JSON-safe copy of the ML response
        ml_response = {}
        for key in ("error", "status", "message", "cause"):
            if key in result:
                ml_response[key] = result[key]

        error_log = PublishErrorLog(
            user_id=user_id,
            listing_id=listing_id,
            product_id=product_id,
            meli_category_id=meli_category_id,
            error_code=error_code,
            error_message=error_message[:2000],
            ml_response=ml_response,
            publish_payload=_sanitize_payload_for_log(publish_kwargs),
            request_data=request_data,
        )
        db.add(error_log)
        await db.flush()
        return error_log
    except Exception as e:
        logger.error(f"[REMEDIATION] Failed to log error to DB: {e}")
        return None


async def log_publish_error(
    db: AsyncSession,
    user_id: int,
    listing_id: int,
    product_id: int,
    meli_category_id: Optional[str],
    error_code: str,
    error_message: str,
    request_data: Optional[Dict[str, Any]] = None,
) -> None:
    """Log a pre-validation or non-ML publish error. Fire-and-forget.

    Use this for errors that happen before publish_item() is called
    (e.g., catalog-required, non-leaf category, duplicate detection),
    and also for variant publish errors where attempt_remediation is not called.
    """
    try:
        # Build ml_response — merge full ML response if provided via request_data
        ml_response: Dict[str, Any] = {"error": error_code, "message": error_message}
        if request_data and isinstance(request_data, dict):
            for key in ("error", "status", "message", "cause"):
                if key in request_data:
                    ml_response[key] = request_data[key]

        error_log = PublishErrorLog(
            user_id=user_id,
            listing_id=listing_id,
            product_id=product_id,
            meli_category_id=meli_category_id,
            error_code=error_code,
            error_message=error_message[:2000],
            ml_response=ml_response,
            publish_payload={"source": "pre_validation"},
            request_data=request_data,
        )
        db.add(error_log)
        await db.commit()
        logger.warning(
            f"[REMEDIATION] Error logged: code={error_code}, "
            f"product={product_id}, category={meli_category_id}"
        )
    except Exception as e:
        logger.error(f"[REMEDIATION] Failed to log error: {e}")


async def _find_matching_rules(
    db: AsyncSession,
    error_code: str,
    cause_codes: List[str],
    cause_messages: List[str],
    meli_category_id: Optional[str],
) -> List[RemediationRule]:
    """Find active rules that match the error, ordered by priority."""
    stmt = (
        select(RemediationRule)
        .where(
            RemediationRule.is_active.is_(True),
            RemediationRule.error_code == error_code,
        )
        .order_by(RemediationRule.priority.asc())
        .limit(10)
    )
    result = await db.execute(stmt)
    candidates = list(result.scalars().all())

    # Filter by optional matching criteria
    matched = []
    for rule in candidates:
        # cause_code filter
        if rule.cause_code and rule.cause_code not in cause_codes:
            continue

        # error_pattern regex filter (matches against cause messages)
        if rule.error_pattern:
            pattern_matched = False
            try:
                regex = re.compile(rule.error_pattern, re.IGNORECASE)
                for msg in cause_messages:
                    if regex.search(msg):
                        pattern_matched = True
                        break
            except re.error:
                logger.warning(
                    f"[REMEDIATION] Invalid regex in rule {rule.id}: {rule.error_pattern}"
                )
                continue
            if not pattern_matched:
                continue

        # category_pattern filter
        if rule.category_pattern and meli_category_id:
            try:
                if not re.search(rule.category_pattern, meli_category_id, re.IGNORECASE):
                    continue
            except re.error:
                logger.warning(
                    f"[REMEDIATION] Invalid category regex in rule {rule.id}: "
                    f"{rule.category_pattern}"
                )
                continue
        elif rule.category_pattern and not meli_category_id:
            continue

        matched.append(rule)

    return matched


def _update_confidence(rule: RemediationRule) -> None:
    """Recompute confidence_score from success/failure counts."""
    total = rule.success_count + rule.failure_count
    if total > 0:
        rule.confidence_score = rule.success_count / total
    else:
        rule.confidence_score = 0.0


async def attempt_remediation(
    db: AsyncSession,
    user_id: int,
    listing_id: int,
    product_id: int,
    meli_category_id: Optional[str],
    original_result: Dict[str, Any],
    publish_kwargs: Dict[str, Any],
    request_data: Optional[Dict[str, Any]] = None,
    max_attempts: int = 2,
) -> Optional[Dict[str, Any]]:
    """Try to fix a failed publish by matching rules and retrying.

    Args:
        db: Database session.
        user_id: The user attempting to publish.
        listing_id: The MeliListing.id for the draft.
        product_id: The product being published.
        meli_category_id: ML category for context.
        original_result: The error result from publish_item().
        publish_kwargs: The original kwargs passed to publish_item().
        request_data: The MeliPublishRequest dict (for logging).
        max_attempts: Max number of fix attempts (default 2).

    Returns:
        The successful ML API response if a fix worked, or None if all failed.
    """
    from app.services.meli_client import publish_item

    # Parse the error
    error_code, error_message, cause_codes, cause_messages = _parse_error(
        original_result
    )

    # Always log the error
    error_log = await _log_error(
        db=db,
        user_id=user_id,
        listing_id=listing_id,
        product_id=product_id,
        meli_category_id=meli_category_id,
        error_code=error_code,
        error_message=error_message,
        result=original_result,
        publish_kwargs=publish_kwargs,
        request_data=request_data,
    )

    if error_log is None:
        logger.warning(
            f"[REMEDIATION] Could not log error to DB — skipping remediation. "
            f"error_code={error_code}, product={product_id}, category={meli_category_id}"
        )
        return None

    logger.warning(
        f"[REMEDIATION] Error logged (id={error_log.id}): "
        f"code={error_code}, product={product_id}, category={meli_category_id}"
    )

    # Find matching rules
    rules = await _find_matching_rules(
        db, error_code, cause_codes, cause_messages, meli_category_id
    )

    if not rules:
        logger.warning(
            f"[REMEDIATION] No matching rules for error_code={error_code}. "
            f"Error logged for future learning."
        )
        await db.commit()
        _dispatch_llm_discovery(error_log.id)
        return None

    logger.info(
        f"[REMEDIATION] Found {len(rules)} matching rule(s) for error_code={error_code}: "
        + ", ".join(f"#{r.id} ({r.name})" for r in rules)
    )

    # Try each rule up to max_attempts
    for attempt_num, rule in enumerate(rules[:max_attempts], start=1):
        # Validate fix_type
        if rule.fix_type not in FIX_HANDLERS:
            logger.warning(
                f"[REMEDIATION] Rule #{rule.id} has unknown fix_type={rule.fix_type}, skipping"
            )
            continue

        # Deep-copy kwargs (exclude db session)
        modified_kwargs = {}
        for k, v in publish_kwargs.items():
            if k == "db":
                modified_kwargs[k] = v  # Keep the same db session
            else:
                modified_kwargs[k] = copy.deepcopy(v)

        # Apply the fix
        try:
            diff = apply_fix(rule.fix_type, rule.fix_config, modified_kwargs)
        except Exception as e:
            logger.error(
                f"[REMEDIATION] Failed to apply fix from rule #{rule.id}: {e}"
            )
            continue

        logger.info(
            f"[REMEDIATION] Attempt {attempt_num}: applying rule #{rule.id} "
            f"({rule.name}) — fix_type={rule.fix_type}, diff={diff}"
        )

        # Retry publish with modified kwargs
        start_time = time.monotonic()
        retry_result = await publish_item(**modified_kwargs)
        duration_ms = int((time.monotonic() - start_time) * 1000)

        if retry_result is None:
            # Total failure (no response from ML)
            attempt = RemediationAttempt(
                error_log_id=error_log.id,
                rule_id=rule.id,
                attempt_number=attempt_num,
                fix_type=rule.fix_type,
                fix_applied=diff,
                modified_payload=_sanitize_payload_for_log(modified_kwargs),
                result="failure",
                result_detail={"reason": "no_response"},
                duration_ms=duration_ms,
            )
            db.add(attempt)
            rule.failure_count += 1
            _update_confidence(rule)
            logger.warning(
                f"[REMEDIATION] Attempt {attempt_num} (rule #{rule.id}): "
                f"no response from ML API"
            )
            continue

        if not retry_result.get("error"):
            # Success!
            attempt = RemediationAttempt(
                error_log_id=error_log.id,
                rule_id=rule.id,
                attempt_number=attempt_num,
                fix_type=rule.fix_type,
                fix_applied=diff,
                modified_payload=_sanitize_payload_for_log(modified_kwargs),
                result="success",
                result_detail={
                    "meli_item_id": retry_result.get("id"),
                    "permalink": retry_result.get("permalink"),
                },
                duration_ms=duration_ms,
            )
            db.add(attempt)

            # Update rule stats
            rule.success_count += 1
            _update_confidence(rule)

            # Update error log
            error_log.remediation_attempted = True
            error_log.remediation_rule_id = rule.id
            error_log.remediation_succeeded = True

            await db.commit()

            logger.info(
                f"[REMEDIATION] SUCCESS! Rule #{rule.id} ({rule.name}) fixed the error. "
                f"ML item: {retry_result.get('id')} "
                f"(confidence now {rule.confidence_score:.2%})"
            )
            return retry_result

        # Retry failed — check if same error or different
        retry_error_code, _, _, _ = _parse_error(retry_result)
        result_type = (
            "failure" if retry_error_code == error_code else "different_error"
        )

        retry_ml_response = {}
        for key in ("error", "status", "message", "cause"):
            if key in retry_result:
                retry_ml_response[key] = retry_result[key]

        attempt = RemediationAttempt(
            error_log_id=error_log.id,
            rule_id=rule.id,
            attempt_number=attempt_num,
            fix_type=rule.fix_type,
            fix_applied=diff,
            modified_payload=_sanitize_payload_for_log(modified_kwargs),
            result=result_type,
            result_detail=retry_ml_response,
            duration_ms=duration_ms,
        )
        db.add(attempt)

        rule.failure_count += 1
        _update_confidence(rule)

        logger.warning(
            f"[REMEDIATION] Attempt {attempt_num} (rule #{rule.id}): "
            f"{result_type} — retry_error={retry_error_code}"
        )

    # All attempts exhausted
    error_log.remediation_attempted = True
    error_log.remediation_succeeded = False
    await db.commit()

    logger.info(
        f"[REMEDIATION] All {min(len(rules), max_attempts)} attempt(s) failed for "
        f"error_code={error_code}. Original error will be returned to user."
    )
    _dispatch_llm_discovery(error_log.id)
    return None


def _dispatch_llm_discovery(error_log_id: int) -> None:
    """Fire-and-forget background task to ask Claude for a fix suggestion.

    Only dispatches if LLM fix discovery is enabled in settings.
    The task runs in the same event loop (asyncio.create_task).
    """
    import asyncio

    from app.services.runtime_config import get_config, get_config_bool

    if not get_config_bool("llm_fix_enabled"):
        return
    if not get_config("anthropic_api_key"):
        return

    async def _run() -> None:
        try:
            from app.services.llm_fix_discovery import suggest_fix

            rule_id = await suggest_fix(error_log_id)
            if rule_id:
                logger.info(
                    f"[REMEDIATION] LLM created rule #{rule_id} for error_log #{error_log_id}"
                )
        except Exception as e:
            logger.error(f"[REMEDIATION] LLM discovery background task failed: {e}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run())
        logger.info(
            f"[REMEDIATION] Dispatched LLM discovery for error_log #{error_log_id}"
        )
    except RuntimeError:
        logger.warning("[REMEDIATION] No running event loop, cannot dispatch LLM task")
