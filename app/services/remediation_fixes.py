"""Fix handlers for the auto-remediation engine.

Each handler receives a fix_config dict and a publish_kwargs dict,
mutates publish_kwargs in-place, and returns a diff dict describing what changed.
"""

import copy
import logging
import re
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


def _fix_modify_attribute(config: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Change the value of an existing attribute, or add it if missing.

    Config examples:
        {"attribute_id": "BRAND", "action": "set_value", "value": "Genérica"}
        {"attribute_id": "GTIN", "action": "set_value_id", "value_id": "12345"}
    """
    attr_id = config["attribute_id"]
    action = config.get("action", "set_value")
    attrs = kwargs.get("extra_attributes") or []

    old_value = None
    found = False

    for attr in attrs:
        if attr.get("id") == attr_id:
            old_value = attr.get("value_name") or attr.get("value_id")
            found = True
            if action == "set_value":
                attr["value_name"] = config["value"]
                attr.pop("value_id", None)
            elif action == "set_value_id":
                attr["value_id"] = config["value_id"]
                attr.pop("value_name", None)
            break

    if not found:
        new_attr: Dict[str, Any] = {"id": attr_id}
        if action == "set_value":
            new_attr["value_name"] = config["value"]
        elif action == "set_value_id":
            new_attr["value_id"] = config["value_id"]
        attrs.append(new_attr)

    kwargs["extra_attributes"] = attrs

    new_value = config.get("value") or config.get("value_id")
    return {
        "attribute_id": attr_id,
        "action": action,
        "old_value": old_value,
        "new_value": new_value,
        "was_added": not found,
    }


def _fix_remove_attribute(config: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Remove an attribute from the attributes list.

    Config: {"attribute_id": "GTIN"}
    """
    attr_id = config["attribute_id"]
    attrs = kwargs.get("extra_attributes") or []

    old_value = None
    original_len = len(attrs)

    attrs_filtered = []
    for attr in attrs:
        if attr.get("id") == attr_id:
            old_value = attr.get("value_name") or attr.get("value_id")
        else:
            attrs_filtered.append(attr)

    kwargs["extra_attributes"] = attrs_filtered

    return {
        "attribute_id": attr_id,
        "removed": len(attrs_filtered) < original_len,
        "old_value": old_value,
    }


def _fix_add_attribute(config: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Add a new attribute (only if not already present).

    Config: {"attribute_id": "EMPTY_GTIN_REASON", "value_name": "El producto no tiene código registrado"}
    """
    attr_id = config["attribute_id"]
    attrs = kwargs.get("extra_attributes") or []

    # Check if already present
    for attr in attrs:
        if attr.get("id") == attr_id:
            return {"attribute_id": attr_id, "added": False, "reason": "already_present"}

    new_attr: Dict[str, Any] = {"id": attr_id}
    if "value_name" in config:
        new_attr["value_name"] = config["value_name"]
    if "value_id" in config:
        new_attr["value_id"] = config["value_id"]

    attrs.append(new_attr)
    kwargs["extra_attributes"] = attrs

    return {
        "attribute_id": attr_id,
        "added": True,
        "value": config.get("value_name") or config.get("value_id"),
    }


def _fix_modify_title(config: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Modify the title.

    Config examples:
        {"action": "prepend", "value": "Brand Model "}
        {"action": "append", "value": " - Pack x1"}
        {"action": "replace", "old": "foo", "new": "bar"}
        {"action": "remove_word", "word": "prohibited_word"}
        {"action": "set", "value": "New Title"}
    """
    action = config["action"]
    old_title = kwargs.get("title", "")

    if action == "prepend":
        kwargs["title"] = config["value"] + old_title
    elif action == "append":
        kwargs["title"] = old_title + config["value"]
    elif action == "replace":
        kwargs["title"] = old_title.replace(config["old"], config["new"])
    elif action == "remove_word":
        word = config["word"]
        # Case-insensitive removal, clean up double spaces
        kwargs["title"] = re.sub(
            rf"\b{re.escape(word)}\b", "", old_title, flags=re.IGNORECASE
        ).strip()
        kwargs["title"] = re.sub(r"\s{2,}", " ", kwargs["title"])
    elif action == "set":
        kwargs["title"] = config["value"]

    # Enforce ML title length limit
    if len(kwargs["title"]) > 60:
        kwargs["title"] = kwargs["title"][:60]

    return {
        "field": "title",
        "action": action,
        "old_value": old_title,
        "new_value": kwargs["title"],
    }


def _fix_modify_payload_field(
    config: Dict[str, Any], kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Change a top-level publish_item kwarg.

    Config: {"field": "condition", "value": "new"}
    """
    field = config["field"]
    old_value = kwargs.get(field)
    kwargs[field] = config["value"]

    return {"field": field, "old_value": old_value, "new_value": config["value"]}


def _fix_retry_without_field(
    config: Dict[str, Any], kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Remove a field from kwargs and retry.

    Config: {"field": "family_name"}
    """
    field = config["field"]
    old_value = kwargs.pop(field, None)

    return {"field": field, "removed": old_value is not None, "old_value": old_value}


# Registry of fix handlers by fix_type
FIX_HANDLERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = {
    "modify_attribute": _fix_modify_attribute,
    "remove_attribute": _fix_remove_attribute,
    "add_attribute": _fix_add_attribute,
    "modify_title": _fix_modify_title,
    "modify_payload_field": _fix_modify_payload_field,
    "retry_without_field": _fix_retry_without_field,
}


def apply_fix(
    fix_type: str, fix_config: Dict[str, Any], publish_kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Apply a fix to the publish kwargs. Returns a diff dict.

    Raises ValueError if fix_type is unknown.
    """
    handler = FIX_HANDLERS.get(fix_type)
    if not handler:
        raise ValueError(f"Unknown fix_type: {fix_type}")
    return handler(fix_config, publish_kwargs)
