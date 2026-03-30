"""PR generator for promoting proven remediation rules to permanent code.

Uses Claude API to generate Python code that implements a fix pattern,
then creates a GitHub PR via the GitHub REST API.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import anthropic
import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.services.runtime_config import get_config
from app.models.remediation import RemediationAttempt, RemediationRule

logger = logging.getLogger(__name__)

_CODE_GEN_SYSTEM_PROMPT = """\
You are a senior Python developer working on a FastAPI service that publishes \
products to MercadoLibre (Mexico site: MLM).

Your task is to add a permanent auto-fix to the `publish_item()` function in \
`app/services/meli_client.py`. This fix handles a specific ML API error that has \
been successfully auto-remediated by a rule-based engine multiple times.

The function signature is:
```python
async def publish_item(
    db, user_id, title, category_id, price, currency_id="MXN",
    available_quantity=1, buying_mode="buy_it_now", listing_type_id="gold_special",
    condition="new", description="", pictures=None, brand=None, model=None,
    extra_attributes=None, family_name=None, sale_terms=None, shipping=None,
    variations=None, seller_custom_field=None,
) -> Optional[Dict[str, Any]]:
```

Existing auto-fix patterns in the code (follow these styles):
1. Brand validation: checks `_is_real_brand()`, replaces with "Genérica"
2. GTIN sanitization: pads to valid length, validates checksum, uses `_sanitize_attribute_value()`
3. WEIGHT unit normalization: appends " g" to bare numbers in `_sanitize_attribute_value()`
4. Title enrichment: prepends brand+model if < 20 chars (around line 735-749)
5. Description error detection: checks for scraper error patterns, clears description (lines 697-718)

RULES:
- Generate ONLY the Python code block that implements the fix
- Include a descriptive comment explaining what the fix does
- Use `logger.info()` or `logger.warning()` for logging (import already available)
- Follow the existing code style: clean, with comments
- The code should operate on the local variables available in `publish_item()` \
  (title, brand, model, extra_attributes, family_name, etc.)
- Do NOT add new imports — only use what's already available (re, logging, etc.)

Respond with a JSON object:
{
  "code": "the Python code to insert",
  "insertion_point": "description of where to insert (e.g., 'after brand validation', \
'before the _meli_request call')",
  "commit_message": "feat(meli-client): short commit message describing the fix",
  "explanation": "brief explanation of what the code does and why"
}
"""


async def generate_code_suggestion(rule_id: int) -> Optional[Dict[str, Any]]:
    """Generate a code suggestion for a promotable rule using Claude.

    Args:
        rule_id: The RemediationRule to generate code for.

    Returns:
        Dict with code, insertion_point, commit_message, explanation — or None on failure.
    """
    api_key = get_config("anthropic_api_key")
    if not api_key:
        logger.warning("[PR_GEN] ANTHROPIC_API_KEY not set")
        return None

    async with async_session() as db:
        rule = (
            await db.execute(
                select(RemediationRule).where(RemediationRule.id == rule_id)
            )
        ).scalar_one_or_none()

        if not rule:
            logger.warning(f"[PR_GEN] Rule #{rule_id} not found")
            return None

        # Get successful attempt examples
        examples_stmt = (
            select(RemediationAttempt)
            .where(
                RemediationAttempt.rule_id == rule_id,
                RemediationAttempt.result == "success",
            )
            .order_by(desc(RemediationAttempt.created_at))
            .limit(5)
        )
        examples = (await db.execute(examples_stmt)).scalars().all()

        # Build user prompt
        user_prompt = _build_code_gen_prompt(rule, examples)

        logger.info(f"[PR_GEN] Calling Claude API for rule #{rule_id} ({rule.name})")

        try:
            client = anthropic.AsyncAnthropic(api_key=api_key)
            message = await client.messages.create(
                model=get_config("anthropic_model", "claude-sonnet-4-20250514"),
                max_tokens=2048,
                system=_CODE_GEN_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            response_text = message.content[0].text.strip()

            # Parse JSON (handle markdown code blocks)
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                response_text = "\n".join(
                    lines[1:-1] if lines[-1].startswith("```") else lines[1:]
                )

            suggestion = json.loads(response_text)

        except json.JSONDecodeError as e:
            logger.error(f"[PR_GEN] Failed to parse LLM response: {e}")
            return None
        except anthropic.APIError as e:
            logger.error(f"[PR_GEN] Claude API error: {e}")
            return None
        except Exception as e:
            logger.error(f"[PR_GEN] Unexpected error: {e}")
            return None

        # Validate response structure
        required_fields = ["code", "insertion_point", "commit_message", "explanation"]
        for field in required_fields:
            if field not in suggestion:
                logger.warning(f"[PR_GEN] Missing field in response: {field}")
                return None

        # Store the suggestion on the rule
        current_config = dict(rule.fix_config)
        current_config["code_suggestion"] = suggestion
        rule.fix_config = current_config

        await db.commit()

        logger.info(
            f"[PR_GEN] Code suggestion generated for rule #{rule_id}: "
            f"{suggestion['commit_message']}"
        )
        return suggestion


def _build_code_gen_prompt(
    rule: RemediationRule, examples: List[RemediationAttempt]
) -> str:
    """Build the user prompt for code generation."""
    parts = [
        "## Rule to Codify as Permanent Fix\n\n",
        f"**Rule name:** {rule.name}\n",
        f"**Description:** {rule.description or 'N/A'}\n",
        f"**Error code:** {rule.error_code}\n",
    ]

    if rule.cause_code:
        parts.append(f"**Cause code:** {rule.cause_code}\n")
    if rule.error_pattern:
        parts.append(f"**Error pattern (regex):** {rule.error_pattern}\n")
    if rule.category_pattern:
        parts.append(f"**Category pattern:** {rule.category_pattern}\n")

    parts.extend([
        f"\n**Fix type:** {rule.fix_type}\n",
        f"**Fix config:** {json.dumps(rule.fix_config, indent=2, ensure_ascii=False)}\n",
        f"\n**Success count:** {rule.success_count}\n",
        f"**Failure count:** {rule.failure_count}\n",
        f"**Confidence:** {float(rule.confidence_score):.2%}\n",
        f"**Source:** {rule.source}\n",
    ])

    if examples:
        parts.append("\n## Successful Application Examples (last 5):\n\n")
        for i, ex in enumerate(examples, 1):
            parts.append(f"### Example {i}\n")
            parts.append(f"- **Fix applied:** {json.dumps(ex.fix_applied, ensure_ascii=False)}\n")
            if ex.result_detail:
                parts.append(
                    f"- **Result:** {json.dumps(ex.result_detail, ensure_ascii=False)}\n"
                )
            parts.append(f"- **Duration:** {ex.duration_ms}ms\n\n")

    parts.append(
        "\nGenerate the Python code that should be added to `publish_item()` in "
        "`meli_client.py` to handle this error pattern permanently.\n"
    )

    return "".join(parts)


async def create_github_pr(rule_id: int) -> Optional[str]:
    """Create a GitHub PR with the code suggestion for a rule.

    The PR contains the generated code as a description — the developer
    applies the actual code change manually after review.

    Args:
        rule_id: The RemediationRule with a code_suggestion in fix_config.

    Returns:
        The PR URL if created, or None.
    """
    github_token = get_config("github_token")
    if not github_token:
        logger.warning("[PR_GEN] GITHUB_TOKEN not set, cannot create PR")
        return None

    async with async_session() as db:
        rule = (
            await db.execute(
                select(RemediationRule).where(RemediationRule.id == rule_id)
            )
        ).scalar_one_or_none()

        if not rule:
            logger.warning(f"[PR_GEN] Rule #{rule_id} not found")
            return None

        suggestion = rule.fix_config.get("code_suggestion")
        if not suggestion:
            logger.warning(f"[PR_GEN] Rule #{rule_id} has no code suggestion")
            return None

        # Build PR body
        pr_title = suggestion.get("commit_message", f"auto-fix: {rule.name}")[:70]
        pr_body = _build_pr_body(rule, suggestion)

        # Create PR via GitHub API as an issue-based PR (no branch needed)
        # We create it as an issue with a code suggestion in the body,
        # since automated file patching is fragile
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"https://api.github.com/repos/{settings.github_repo}/issues",
                    headers={
                        "Authorization": f"Bearer {github_token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    json={
                        "title": f"[Auto-Fix] {pr_title}",
                        "body": pr_body,
                        "labels": ["auto-remediation", "code-promotion"],
                    },
                )

                if response.status_code == 201:
                    issue_data = response.json()
                    issue_url = issue_data["html_url"]

                    # Mark rule as promoted
                    from datetime import datetime, timezone

                    rule.promoted_to_code = True
                    rule.promoted_at = datetime.now(timezone.utc)
                    await db.commit()

                    logger.info(
                        f"[PR_GEN] GitHub issue created for rule #{rule_id}: {issue_url}"
                    )
                    return issue_url
                else:
                    logger.error(
                        f"[PR_GEN] GitHub API error: {response.status_code} — "
                        f"{response.text[:500]}"
                    )
                    return None

        except Exception as e:
            logger.error(f"[PR_GEN] Failed to create GitHub issue: {e}")
            return None


def _build_pr_body(rule: RemediationRule, suggestion: Dict[str, Any]) -> str:
    """Build the GitHub issue/PR body with the code suggestion."""
    return f"""\
## Auto-Remediation Code Promotion

**Rule:** #{rule.id} — {rule.name}
**Source:** {rule.source}
**Error code:** `{rule.error_code}`
**Success rate:** {float(rule.confidence_score):.0%} ({rule.success_count} successes / \
{rule.failure_count} failures)

### Explanation

{suggestion.get('explanation', 'N/A')}

### Insertion Point

`{suggestion.get('insertion_point', 'See code')}`

### Suggested Code

```python
{suggestion.get('code', '# No code generated')}
```

### How to Apply

1. Open `app/services/meli_client.py`
2. Find the insertion point described above in `publish_item()`
3. Add the suggested code
4. Run tests to verify
5. Close this issue

---

*Generated automatically by the auto-remediation system.*
*Rule created: {rule.created_at} | Promoted: now*
"""
