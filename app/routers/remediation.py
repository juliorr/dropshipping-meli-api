"""Remediation admin router — view errors, manage rules, dashboard stats, promotion."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import AuthUser, get_current_user, get_db
from app.models.remediation import PublishErrorLog, RemediationAttempt, RemediationRule
from app.schemas.remediation import (
    AttemptResponse,
    BulkRetryResponse,
    DashboardStats,
    ErrorGroupResponse,
    ErrorLogDetailResponse,
    ErrorLogResponse,
    RuleCreate,
    RuleResponse,
    RuleSummary,
    RuleUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/remediation", tags=["Remediation"])


def _require_superuser(user: AuthUser) -> None:
    if not user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superuser access required",
        )


# ─── Error Logs ──────────────────────────────────────────────────────────────


@router.get("/errors", response_model=list[ErrorLogResponse])
async def list_errors(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    error_code: Optional[str] = Query(None),
    remediation_succeeded: Optional[bool] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List recent publish errors (paginated)."""
    _require_superuser(current_user)

    query = select(PublishErrorLog)

    if error_code:
        query = query.where(PublishErrorLog.error_code == error_code)
    if remediation_succeeded is not None:
        query = query.where(
            PublishErrorLog.remediation_succeeded == remediation_succeeded
        )

    query = (
        query.order_by(desc(PublishErrorLog.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    result = await db.execute(query)
    return result.scalars().all()


# ─── Grouped Errors ─────────────────────────────────────────────────────────
# NOTE: These /errors/grouped* routes MUST be registered BEFORE /errors/{error_id}
# to avoid FastAPI matching "grouped" as an error_id path parameter.


@router.get("/errors/grouped", response_model=list[ErrorGroupResponse])
async def list_errors_grouped(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List errors grouped by error_code with aggregate counts."""
    _require_superuser(current_user)

    # Aggregate query
    pending_count = func.count(
        case((PublishErrorLog.remediation_succeeded.is_(None), 1))
    )
    fixed_count = func.count(
        case((PublishErrorLog.remediation_succeeded.is_(True), 1))
    )
    failed_count = func.count(
        case((PublishErrorLog.remediation_succeeded.is_(False), 1))
    )

    stmt = (
        select(
            PublishErrorLog.error_code,
            func.count(PublishErrorLog.id).label("total"),
            pending_count.label("pending"),
            fixed_count.label("fixed"),
            failed_count.label("failed"),
            func.array_agg(
                func.distinct(PublishErrorLog.meli_category_id)
            ).label("categories_raw"),
            func.max(PublishErrorLog.id).label("latest_error_id"),
            func.max(PublishErrorLog.created_at).label("latest_at"),
        )
        .group_by(PublishErrorLog.error_code)
        .order_by(desc("total"))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    result = await db.execute(stmt)
    rows = result.all()

    if not rows:
        return []

    # Get error_message for each group's latest error
    latest_ids = [row.latest_error_id for row in rows]
    msg_result = await db.execute(
        select(PublishErrorLog.id, PublishErrorLog.error_message).where(
            PublishErrorLog.id.in_(latest_ids)
        )
    )
    msg_map = {r.id: r.error_message for r in msg_result.all()}

    # Get matching active rules for all error_codes in this page
    error_codes = [row.error_code for row in rows]
    rules_result = await db.execute(
        select(RemediationRule).where(
            RemediationRule.error_code.in_(error_codes),
            RemediationRule.is_active.is_(True),
        )
    )
    all_rules = rules_result.scalars().all()

    rules_by_code: Dict[str, List[RuleSummary]] = {}
    for rule in all_rules:
        summary = RuleSummary.model_validate(rule)
        rules_by_code.setdefault(rule.error_code, []).append(summary)

    # Build response
    groups = []
    for row in rows:
        categories_raw = row.categories_raw or []
        categories = [c for c in categories_raw if c is not None]

        groups.append(
            ErrorGroupResponse(
                error_code=row.error_code,
                error_message_sample=msg_map.get(row.latest_error_id, "")[:200],
                total=row.total,
                pending=row.pending,
                fixed=row.fixed,
                failed=row.failed,
                categories=categories,
                matching_rules=rules_by_code.get(row.error_code, []),
                latest_error_id=row.latest_error_id,
                latest_at=row.latest_at,
            )
        )

    return groups


@router.post("/errors/grouped/{error_code}/llm-suggest")
async def group_llm_suggest(
    error_code: str,
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Trigger LLM fix suggestion using a representative error from the group."""
    _require_superuser(current_user)

    # Pick the most recent pending error, or most recent overall
    stmt = (
        select(PublishErrorLog.id)
        .where(
            PublishErrorLog.error_code == error_code,
            PublishErrorLog.remediation_succeeded.is_(None),
        )
        .order_by(desc(PublishErrorLog.created_at))
        .limit(1)
    )
    result = await db.execute(stmt)
    error_id = result.scalar_one_or_none()

    if not error_id:
        # Fallback: most recent error of any status
        stmt = (
            select(PublishErrorLog.id)
            .where(PublishErrorLog.error_code == error_code)
            .order_by(desc(PublishErrorLog.created_at))
            .limit(1)
        )
        result = await db.execute(stmt)
        error_id = result.scalar_one_or_none()

    if not error_id:
        raise HTTPException(status_code=404, detail="No errors found for this error_code")

    from app.services.llm_fix_discovery import LLMFixError, suggest_fix

    try:
        rule_id = await suggest_fix(error_id)
    except LLMFixError as e:
        return {"status": "error", "rule_id": None, "detail": str(e)}

    if rule_id:
        return {"status": "rule_created", "rule_id": rule_id}
    return {"status": "no_fix_suggested", "rule_id": None, "detail": None}


@router.post(
    "/errors/grouped/{error_code}/bulk-retry",
    response_model=BulkRetryResponse,
)
async def group_bulk_retry(
    error_code: str,
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BulkRetryResponse:
    """Dry-run remediation on pending errors for a given error_code.

    Finds matching rules for each pending error, applies the fix to the stored
    payload, and records attempts. Does NOT re-publish (payload is sanitized).
    """
    _require_superuser(current_user)

    from app.services.remediation_engine import _find_matching_rules, _parse_error
    from app.services.remediation_fixes import apply_fix

    import copy

    # Load pending errors (capped)
    stmt = (
        select(PublishErrorLog)
        .where(
            PublishErrorLog.error_code == error_code,
            PublishErrorLog.remediation_succeeded.is_(None),
        )
        .order_by(desc(PublishErrorLog.created_at))
        .limit(50)
    )
    result = await db.execute(stmt)
    pending_errors = list(result.scalars().all())

    if not pending_errors:
        return BulkRetryResponse(processed=0, matched=0, no_rule=0)

    matched = 0
    no_rule = 0

    for error_log in pending_errors:
        # Parse the stored ML response to get cause_codes/messages
        ml_response = error_log.ml_response or {}
        _, _, cause_codes, cause_messages = _parse_error(ml_response)

        rules = await _find_matching_rules(
            db,
            error_code=error_log.error_code,
            cause_codes=cause_codes,
            cause_messages=cause_messages,
            meli_category_id=error_log.meli_category_id,
        )

        if not rules:
            no_rule += 1
            continue

        # Apply the first matching rule as a dry-run
        rule = rules[0]
        payload_copy = copy.deepcopy(error_log.publish_payload or {})
        try:
            diff = apply_fix(rule.fix_type, rule.fix_config, payload_copy)
        except (ValueError, KeyError) as e:
            logger.warning(
                f"[REMEDIATION] Bulk retry fix failed for error {error_log.id}: {e}"
            )
            no_rule += 1
            continue

        # Record attempt
        attempt = RemediationAttempt(
            error_log_id=error_log.id,
            rule_id=rule.id,
            attempt_number=1,
            fix_type=rule.fix_type,
            fix_applied=diff,
            result="dry_run_success",
            result_detail={"note": "Bulk dry-run; re-publish from listings to apply."},
        )
        db.add(attempt)

        error_log.remediation_attempted = True
        error_log.remediation_rule_id = rule.id
        matched += 1

    await db.commit()

    logger.info(
        f"[REMEDIATION] Bulk retry for {error_code}: "
        f"processed={len(pending_errors)}, matched={matched}, no_rule={no_rule}"
    )

    return BulkRetryResponse(
        processed=len(pending_errors),
        matched=matched,
        no_rule=no_rule,
    )


# ─── Error Detail ────────────────────────────────────────────────────────────


@router.get("/errors/{error_id}", response_model=ErrorLogDetailResponse)
async def get_error_detail(
    error_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get error detail with all remediation attempts."""
    _require_superuser(current_user)

    error_log = (
        await db.execute(
            select(PublishErrorLog).where(PublishErrorLog.id == error_id)
        )
    ).scalar_one_or_none()

    if not error_log:
        raise HTTPException(status_code=404, detail="Error log not found")

    attempts = (
        await db.execute(
            select(RemediationAttempt)
            .where(RemediationAttempt.error_log_id == error_id)
            .order_by(RemediationAttempt.attempt_number)
        )
    ).scalars().all()

    return ErrorLogDetailResponse(
        **{
            col.name: getattr(error_log, col.name)
            for col in PublishErrorLog.__table__.columns
        },
        attempts=[
            AttemptResponse(
                **{
                    col.name: getattr(a, col.name)
                    for col in RemediationAttempt.__table__.columns
                }
            )
            for a in attempts
        ],
    )


# ─── Rules CRUD ──────────────────────────────────────────────────────────────


@router.get("/rules", response_model=list[RuleResponse])
async def list_rules(
    is_active: Optional[bool] = Query(None),
    source: Optional[str] = Query(None),
    error_code: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List remediation rules with optional filters."""
    _require_superuser(current_user)

    query = select(RemediationRule)

    if is_active is not None:
        query = query.where(RemediationRule.is_active == is_active)
    if source:
        query = query.where(RemediationRule.source == source)
    if error_code:
        query = query.where(RemediationRule.error_code == error_code)

    query = query.order_by(RemediationRule.priority.asc())

    result = await db.execute(query)
    return result.scalars().all()


@router.post("/rules", response_model=RuleResponse, status_code=201)
async def create_rule(
    data: RuleCreate,
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new remediation rule."""
    _require_superuser(current_user)

    rule = RemediationRule(
        error_code=data.error_code,
        cause_code=data.cause_code,
        error_pattern=data.error_pattern,
        category_pattern=data.category_pattern,
        fix_type=data.fix_type,
        fix_config=data.fix_config,
        name=data.name,
        description=data.description,
        source="manual",
        priority=data.priority,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)

    logger.info(f"[REMEDIATION] Rule created: #{rule.id} ({rule.name})")
    return rule


@router.put("/rules/{rule_id}", response_model=RuleResponse)
async def update_rule(
    rule_id: int,
    data: RuleUpdate,
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a remediation rule."""
    _require_superuser(current_user)

    rule = (
        await db.execute(
            select(RemediationRule).where(RemediationRule.id == rule_id)
        )
    ).scalar_one_or_none()

    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(rule, field, value)

    await db.commit()
    await db.refresh(rule)

    logger.info(f"[REMEDIATION] Rule updated: #{rule.id} ({rule.name})")
    return rule


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a remediation rule (deactivate)."""
    _require_superuser(current_user)

    rule = (
        await db.execute(
            select(RemediationRule).where(RemediationRule.id == rule_id)
        )
    ).scalar_one_or_none()

    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    rule.is_active = False
    await db.commit()

    logger.info(f"[REMEDIATION] Rule deactivated: #{rule.id} ({rule.name})")


# ─── Dashboard ───────────────────────────────────────────────────────────────


@router.get("/dashboard", response_model=DashboardStats)
async def dashboard_stats(
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Aggregated remediation statistics."""
    _require_superuser(current_user)

    # Error counts
    total_errors = (
        await db.execute(select(func.count(PublishErrorLog.id)))
    ).scalar() or 0

    errors_auto_fixed = (
        await db.execute(
            select(func.count(PublishErrorLog.id)).where(
                PublishErrorLog.remediation_succeeded.is_(True)
            )
        )
    ).scalar() or 0

    errors_unfixable = (
        await db.execute(
            select(func.count(PublishErrorLog.id)).where(
                PublishErrorLog.remediation_succeeded.is_(False)
            )
        )
    ).scalar() or 0

    errors_pending = total_errors - errors_auto_fixed - errors_unfixable

    # Rule counts
    total_rules = (
        await db.execute(select(func.count(RemediationRule.id)))
    ).scalar() or 0

    active_rules = (
        await db.execute(
            select(func.count(RemediationRule.id)).where(
                RemediationRule.is_active.is_(True)
            )
        )
    ).scalar() or 0

    llm_rules = (
        await db.execute(
            select(func.count(RemediationRule.id)).where(
                RemediationRule.source == "llm"
            )
        )
    ).scalar() or 0

    # Top error codes
    top_codes_result = await db.execute(
        select(
            PublishErrorLog.error_code,
            func.count(PublishErrorLog.id).label("count"),
        )
        .group_by(PublishErrorLog.error_code)
        .order_by(desc(func.count(PublishErrorLog.id)))
        .limit(10)
    )
    top_error_codes = [
        {"error_code": row.error_code, "count": row.count}
        for row in top_codes_result.all()
    ]

    auto_fix_rate = (
        (errors_auto_fixed / total_errors * 100) if total_errors > 0 else 0.0
    )

    return DashboardStats(
        total_errors=total_errors,
        errors_auto_fixed=errors_auto_fixed,
        errors_pending=errors_pending,
        errors_unfixable=errors_unfixable,
        total_rules=total_rules,
        active_rules=active_rules,
        llm_rules=llm_rules,
        top_error_codes=top_error_codes,
        auto_fix_rate=round(auto_fix_rate, 1),
    )


# ─── Runtime Config ──────────────────────────────────────────────────────────


@router.get("/config")
async def get_config_values(
    current_user: AuthUser = Depends(get_current_user),
) -> list[Dict[str, Any]]:
    """Get all runtime config keys with masked secret values."""
    _require_superuser(current_user)

    from app.services.runtime_config import get_all_config_masked

    return get_all_config_masked()


@router.put("/config")
async def update_config_value(
    data: Dict[str, str],
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Update a runtime config value. Body: {"key": "...", "value": "..."}."""
    _require_superuser(current_user)

    key = data.get("key", "")
    value = data.get("value", "")

    if not key:
        raise HTTPException(status_code=400, detail="Missing 'key' field")

    from app.services.runtime_config import CONFIG_KEYS, set_config

    if key not in CONFIG_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown config key: {key}. Valid keys: {list(CONFIG_KEYS.keys())}",
        )

    await set_config(db=db, key=key, value=value, updated_by=current_user.id)
    return {"status": "updated", "key": key}


# ─── LLM Discovery (Phase 2) ────────────────────────────────────────────────


@router.post("/llm/suggest/{error_log_id}")
async def trigger_llm_suggestion(
    error_log_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Manually trigger LLM fix suggestion for a specific error."""
    _require_superuser(current_user)

    error_log = (
        await db.execute(
            select(PublishErrorLog).where(PublishErrorLog.id == error_log_id)
        )
    ).scalar_one_or_none()

    if not error_log:
        raise HTTPException(status_code=404, detail="Error log not found")

    from app.services.llm_fix_discovery import LLMFixError, suggest_fix

    try:
        rule_id = await suggest_fix(error_log_id)
    except LLMFixError as e:
        return {"status": "error", "rule_id": None, "detail": str(e)}

    if rule_id:
        return {"status": "rule_created", "rule_id": rule_id}
    return {"status": "no_fix_suggested", "rule_id": None, "detail": None}


# ─── Code Promotion (Phase 3) ───────────────────────────────────────────────


@router.get("/promotable", response_model=list[RuleResponse])
async def list_promotable_rules(
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List rules that are candidates for code promotion."""
    _require_superuser(current_user)

    from datetime import datetime, timedelta, timezone

    from app.config import settings as app_settings

    cutoff = datetime.now(timezone.utc) - timedelta(
        days=app_settings.pr_promotion_min_age_days
    )
    stmt = (
        select(RemediationRule)
        .where(
            RemediationRule.is_active.is_(True),
            RemediationRule.promoted_to_code.is_(False),
            RemediationRule.source.in_(["llm", "manual"]),
            RemediationRule.success_count >= app_settings.pr_promotion_min_successes,
            RemediationRule.confidence_score >= app_settings.pr_promotion_min_confidence,
            RemediationRule.created_at <= cutoff,
        )
        .order_by(RemediationRule.success_count.desc())
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/promote/{rule_id}")
async def generate_promotion_code(
    rule_id: int,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Generate a code suggestion for promoting a rule to permanent code."""
    _require_superuser(current_user)

    from app.services.pr_generator import generate_code_suggestion

    suggestion = await generate_code_suggestion(rule_id)

    if suggestion:
        return {"status": "suggestion_generated", "suggestion": suggestion}
    raise HTTPException(
        status_code=500,
        detail="Failed to generate code suggestion. Check logs.",
    )


@router.post("/promote/{rule_id}/create-pr")
async def create_promotion_pr(
    rule_id: int,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Create a GitHub issue with the code suggestion for a promoted rule."""
    _require_superuser(current_user)

    from app.services.pr_generator import create_github_pr

    issue_url = await create_github_pr(rule_id)

    if issue_url:
        return {"status": "issue_created", "url": issue_url}
    raise HTTPException(
        status_code=500,
        detail="Failed to create GitHub issue. Check GITHUB_TOKEN and logs.",
    )
