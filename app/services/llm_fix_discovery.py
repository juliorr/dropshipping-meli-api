"""LLM-powered fix discovery for unknown publish errors.

Uses Claude API to analyze failed publishes and suggest fixes that can be
stored as RemediationRules for future auto-application.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import anthropic
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.services.runtime_config import get_config
from app.models.remediation import PublishErrorLog, RemediationAttempt, RemediationRule
from app.services.remediation_fixes import FIX_HANDLERS

logger = logging.getLogger(__name__)


class LLMFixError(Exception):
    """Raised when the LLM fix suggestion fails due to infrastructure issues
    (invalid API key, unsupported model, network error, etc.)."""
    pass

_SYSTEM_PROMPT = """\
You are an expert on the MercadoLibre API for Mexico (MLM site).
You are analyzing a failed product publish attempt and need to suggest a data fix.

The available fix types are:
- modify_attribute: Change an attribute value.
  Config: {"attribute_id": "ATTR_ID", "action": "set_value", "value": "new_value"}
  or:     {"attribute_id": "ATTR_ID", "action": "set_value_id", "value_id": "catalog_id"}
- remove_attribute: Remove an attribute from the list.
  Config: {"attribute_id": "ATTR_ID"}
- add_attribute: Add a missing attribute.
  Config: {"attribute_id": "ATTR_ID", "value_name": "value"}
- modify_title: Change the item title.
  Config: {"action": "prepend"|"append"|"replace"|"remove_word"|"set", ...}
  For "prepend"/"append": {"action": "prepend", "value": "text to add "}
  For "replace": {"action": "replace", "old": "word", "new": "replacement"}
  For "remove_word": {"action": "remove_word", "word": "prohibited_word"}
  For "set": {"action": "set", "value": "complete new title"}
- modify_payload_field: Change a top-level publish field (condition, price, etc).
  Config: {"field": "field_name", "value": "new_value"}
- retry_without_field: Remove a field entirely and retry.
  Config: {"field": "field_name"}

IMPORTANT RULES:
- Only suggest fixes that modify data in the publish payload. You cannot fix issues \
that require human decisions (like choosing a completely different category).
- ML titles must be <= 60 characters.
- All values should be in Spanish (for Mexican market).
- Be conservative — only suggest a fix if you're reasonably confident it will resolve the error.

Respond ONLY with a valid JSON object (no markdown, no explanation outside JSON):
{
  "fixable": true/false,
  "reasoning": "explanation of what went wrong and why this fix should work",
  "fix_type": "one of the types above",
  "fix_config": { ... type-specific config ... },
  "rule_name": "short descriptive name for this fix pattern (in English)",
  "rule_description": "longer description of when this rule applies",
  "error_code_match": "the ML error code this rule should match",
  "cause_code_match": "optional cause code for finer matching, or null",
  "error_pattern_match": "optional regex to match cause messages, or null",
  "confidence": 0.0-1.0
}

If the error is NOT fixable by data modification, set fixable=false and explain why \
in the reasoning field. Still return valid JSON.
"""


def _build_user_prompt(
    error_log: PublishErrorLog,
    similar_resolutions: List[Dict[str, Any]],
) -> str:
    """Build the user message with error context."""
    parts = [
        "## Failed Publish Attempt\n",
        f"**Error Code:** {error_log.error_code}\n",
        f"**Error Message:** {error_log.error_message}\n",
        f"**Category ID:** {error_log.meli_category_id or 'unknown'}\n",
        "\n**ML Error Response:**\n```json\n",
        json.dumps(error_log.ml_response, indent=2, ensure_ascii=False),
        "\n```\n",
        "\n**Publish Payload (images redacted):**\n```json\n",
        json.dumps(error_log.publish_payload, indent=2, ensure_ascii=False),
        "\n```\n",
    ]

    if error_log.request_data:
        parts.extend([
            "\n**Request Data (frontend input):**\n```json\n",
            json.dumps(error_log.request_data, indent=2, ensure_ascii=False),
            "\n```\n",
        ])

    if similar_resolutions:
        parts.extend([
            "\n**Similar past errors and their successful resolutions:**\n```json\n",
            json.dumps(similar_resolutions, indent=2, ensure_ascii=False),
            "\n```\n",
        ])
    else:
        parts.append(
            "\n**No similar past resolutions found.** This appears to be a new error pattern.\n"
        )

    return "".join(parts)


async def _get_similar_resolutions(
    db: AsyncSession, error_code: str, limit: int = 3
) -> List[Dict[str, Any]]:
    """Find past errors with the same code that were successfully remediated."""
    stmt = (
        select(PublishErrorLog)
        .where(
            PublishErrorLog.error_code == error_code,
            PublishErrorLog.remediation_succeeded.is_(True),
        )
        .order_by(desc(PublishErrorLog.created_at))
        .limit(limit)
    )
    result = await db.execute(stmt)
    resolved_errors = result.scalars().all()

    resolutions = []
    for err in resolved_errors:
        # Find the successful attempt
        attempt_stmt = (
            select(RemediationAttempt)
            .where(
                RemediationAttempt.error_log_id == err.id,
                RemediationAttempt.result == "success",
            )
            .limit(1)
        )
        attempt = (await db.execute(attempt_stmt)).scalar_one_or_none()

        if attempt:
            resolutions.append({
                "error_code": err.error_code,
                "error_message": err.error_message[:200],
                "category_id": err.meli_category_id,
                "fix_type": attempt.fix_type,
                "fix_applied": attempt.fix_applied,
            })

    return resolutions


def _validate_llm_response(data: Dict[str, Any]) -> Optional[str]:
    """Validate the LLM response structure. Returns error message or None."""
    if not isinstance(data, dict):
        return "Response is not a JSON object"

    if "fixable" not in data:
        return "Missing 'fixable' field"

    if not data.get("fixable"):
        return None  # Valid non-fixable response

    required = ["fix_type", "fix_config", "rule_name", "error_code_match"]
    for field in required:
        if field not in data or data[field] is None:
            return f"Missing required field: {field}"

    if data["fix_type"] not in FIX_HANDLERS:
        return f"Unknown fix_type: {data['fix_type']}"

    if not isinstance(data["fix_config"], dict):
        return "fix_config must be a JSON object"

    confidence = data.get("confidence", 0)
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        return f"Invalid confidence: {confidence}"

    return None


async def suggest_fix(error_log_id: int) -> Optional[int]:
    """Use Claude API to suggest a fix for an unknown publish error.

    Creates a new RemediationRule if the suggestion is actionable.
    Runs in background (called via asyncio.create_task).

    Args:
        error_log_id: The PublishErrorLog.id to analyze.

    Returns:
        The new RemediationRule.id if created, or None.
    """
    api_key = get_config("anthropic_api_key")
    if not api_key:
        logger.warning("[LLM_FIX] ANTHROPIC_API_KEY not set, skipping LLM discovery")
        return None

    async with async_session() as db:
        # Load the error log
        error_log = (
            await db.execute(
                select(PublishErrorLog).where(PublishErrorLog.id == error_log_id)
            )
        ).scalar_one_or_none()

        if not error_log:
            logger.warning(f"[LLM_FIX] Error log {error_log_id} not found")
            return None

        # Check if a rule already exists for this exact error code
        existing_rule = (
            await db.execute(
                select(RemediationRule).where(
                    RemediationRule.error_code == error_log.error_code,
                    RemediationRule.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()

        if existing_rule:
            logger.info(
                f"[LLM_FIX] Rule already exists for error_code={error_log.error_code} "
                f"(rule #{existing_rule.id}), skipping LLM discovery"
            )
            return None

        # Get similar past resolutions for context
        similar = await _get_similar_resolutions(db, error_log.error_code)

        # Build the prompt
        user_prompt = _build_user_prompt(error_log, similar)

        # Call Claude API
        logger.info(
            f"[LLM_FIX] Calling Claude API for error_log #{error_log_id} "
            f"(error_code={error_log.error_code})"
        )

        try:
            client = anthropic.AsyncAnthropic(api_key=api_key)
            message = await client.messages.create(
                model=get_config("anthropic_model", "claude-sonnet-4-20250514"),
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            response_text = message.content[0].text.strip()

            # Parse JSON response (handle markdown code blocks)
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                response_text = "\n".join(
                    lines[1:-1] if lines[-1].startswith("```") else lines[1:]
                )

            llm_data = json.loads(response_text)

        except json.JSONDecodeError as e:
            logger.error(f"[LLM_FIX] Failed to parse LLM JSON response: {e}")
            logger.debug(f"[LLM_FIX] Raw response: {response_text[:500]}")
            raise LLMFixError(f"Failed to parse LLM response: {e}")
        except anthropic.APIError as e:
            logger.error(f"[LLM_FIX] Claude API error: {e}")
            raise LLMFixError(f"Claude API error: {e}")
        except LLMFixError:
            raise
        except Exception as e:
            logger.error(f"[LLM_FIX] Unexpected error calling Claude: {e}")
            raise LLMFixError(f"Unexpected error: {e}")

        # Validate the response
        validation_error = _validate_llm_response(llm_data)
        if validation_error:
            logger.warning(f"[LLM_FIX] Invalid LLM response: {validation_error}")
            return None

        if not llm_data.get("fixable"):
            logger.info(
                f"[LLM_FIX] LLM says error is not fixable: "
                f"{llm_data.get('reasoning', 'no reason')}"
            )
            # Record the analysis as an attempt for future reference
            attempt = RemediationAttempt(
                error_log_id=error_log_id,
                rule_id=None,
                attempt_number=0,
                fix_type="llm_analysis",
                fix_applied={"fixable": False, "reasoning": llm_data.get("reasoning")},
                result="skipped",
                result_detail=llm_data,
                duration_ms=None,
            )
            db.add(attempt)
            await db.commit()
            return None

        confidence = llm_data.get("confidence", 0)
        if confidence < 0.3:
            logger.info(
                f"[LLM_FIX] LLM confidence too low ({confidence:.2f} < "
                f"{0.3}), skipping rule creation"
            )
            return None

        # Create a new RemediationRule
        rule = RemediationRule(
            error_code=llm_data["error_code_match"],
            cause_code=llm_data.get("cause_code_match"),
            error_pattern=llm_data.get("error_pattern_match"),
            fix_type=llm_data["fix_type"],
            fix_config=llm_data["fix_config"],
            name=llm_data["rule_name"][:200],
            description=llm_data.get("rule_description", llm_data.get("reasoning", ""))[:2000],
            source="llm",
            priority=100,  # LLM rules get default priority
            is_active=True,
        )
        db.add(rule)
        await db.flush()

        logger.info(
            f"[LLM_FIX] Created LLM rule #{rule.id}: {rule.name} "
            f"(fix_type={rule.fix_type}, confidence={confidence:.2f})"
        )

        # Try the fix immediately on the original error
        try:
            from app.services.remediation_engine import attempt_remediation

            remediation_result = await attempt_remediation(
                db=db,
                user_id=error_log.user_id,
                listing_id=error_log.listing_id,
                product_id=error_log.product_id,
                meli_category_id=error_log.meli_category_id,
                original_result=error_log.ml_response,
                publish_kwargs=error_log.publish_payload,
                max_attempts=1,
            )

            if remediation_result and not remediation_result.get("error"):
                logger.info(
                    f"[LLM_FIX] Immediate retry SUCCESS with rule #{rule.id}!"
                )
            else:
                logger.info(
                    f"[LLM_FIX] Immediate retry did not succeed for rule #{rule.id}. "
                    f"Rule saved for future errors."
                )
        except Exception as e:
            logger.warning(f"[LLM_FIX] Immediate retry failed: {e}")

        await db.commit()
        return rule.id
