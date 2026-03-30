"""Pydantic schemas for the remediation system."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# --- Remediation Rules ---


class RuleCreate(BaseModel):
    """Create a new remediation rule."""

    error_code: str = Field(..., max_length=100)
    cause_code: Optional[str] = Field(None, max_length=100)
    error_pattern: Optional[str] = Field(None, max_length=500)
    category_pattern: Optional[str] = Field(None, max_length=100)
    fix_type: str = Field(..., max_length=50)
    fix_config: Dict[str, Any]
    name: str = Field(..., max_length=200)
    description: Optional[str] = None
    priority: int = Field(100, ge=1, le=1000)


class RuleUpdate(BaseModel):
    """Update a remediation rule (partial)."""

    error_code: Optional[str] = Field(None, max_length=100)
    cause_code: Optional[str] = Field(None, max_length=100)
    error_pattern: Optional[str] = Field(None, max_length=500)
    category_pattern: Optional[str] = Field(None, max_length=100)
    fix_type: Optional[str] = Field(None, max_length=50)
    fix_config: Optional[Dict[str, Any]] = None
    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = None
    priority: Optional[int] = Field(None, ge=1, le=1000)
    is_active: Optional[bool] = None


class RuleResponse(BaseModel):
    """Remediation rule response."""

    id: int
    error_code: str
    cause_code: Optional[str] = None
    error_pattern: Optional[str] = None
    category_pattern: Optional[str] = None
    fix_type: str
    fix_config: Dict[str, Any]
    name: str
    description: Optional[str] = None
    source: str
    success_count: int
    failure_count: int
    confidence_score: float
    priority: int
    is_active: bool
    promoted_to_code: bool
    promoted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Publish Error Log ---


class ErrorLogResponse(BaseModel):
    """Publish error log entry."""

    id: int
    user_id: int
    listing_id: int
    product_id: int
    meli_category_id: Optional[str] = None
    error_code: str
    error_message: str
    ml_response: Dict[str, Any]
    publish_payload: Dict[str, Any]
    request_data: Optional[Dict[str, Any]] = None
    remediation_attempted: bool
    remediation_rule_id: Optional[int] = None
    remediation_succeeded: Optional[bool] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AttemptResponse(BaseModel):
    """Remediation attempt record."""

    id: int
    error_log_id: int
    rule_id: Optional[int] = None
    attempt_number: int
    fix_type: str
    fix_applied: Dict[str, Any]
    result: str
    result_detail: Optional[Dict[str, Any]] = None
    duration_ms: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ErrorLogDetailResponse(ErrorLogResponse):
    """Error log with its remediation attempts."""

    attempts: List[AttemptResponse] = []


# --- Error Groups ---


class RuleSummary(BaseModel):
    """Minimal rule info for group display."""

    id: int
    name: str
    fix_type: str
    source: str
    confidence_score: float
    is_active: bool

    model_config = {"from_attributes": True}


class ErrorGroupResponse(BaseModel):
    """Errors grouped by error_code with aggregated counts."""

    error_code: str
    error_message_sample: str
    total: int
    pending: int
    fixed: int
    failed: int
    categories: List[str]
    matching_rules: List[RuleSummary]
    latest_error_id: int
    latest_at: datetime


class BulkRetryResponse(BaseModel):
    """Result of a bulk retry operation."""

    processed: int
    matched: int
    no_rule: int


# --- Dashboard ---


class DashboardStats(BaseModel):
    """Aggregated remediation statistics."""

    total_errors: int
    errors_auto_fixed: int
    errors_pending: int
    errors_unfixable: int
    total_rules: int
    active_rules: int
    llm_rules: int
    top_error_codes: List[Dict[str, Any]]
    auto_fix_rate: float  # percentage 0-100
