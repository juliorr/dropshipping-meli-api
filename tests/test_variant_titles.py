"""Unit tests for variant title construction (type + color + base)."""

from app.routers.mercadolibre import _get_variant_labels, _get_dimension_display


ML_TITLE_LIMIT = 60
SEP = " - "


def _build_variant_title(attributes: dict, display_labels: dict, base_title: str) -> str:
    type_label, color_label = _get_variant_labels(attributes, display_labels)
    parts = [p for p in (type_label, color_label) if p]
    if parts:
        prefix = SEP.join(parts) + SEP
        if len(prefix) >= ML_TITLE_LIMIT:
            return prefix[:ML_TITLE_LIMIT].rstrip(" -")
        remaining = ML_TITLE_LIMIT - len(prefix)
        return (prefix + base_title[:remaining].rstrip()).rstrip()
    return base_title[:ML_TITLE_LIMIT].rstrip()


def test_labels_color_only():
    assert _get_variant_labels({"color_name": "Rojo"}, {}) == ("", "Rojo")


def test_labels_size_only():
    assert _get_variant_labels({"size_name": "M"}, {}) == ("M", "")


def test_labels_size_and_color():
    assert _get_variant_labels(
        {"size_name": "M", "color_name": "Rojo"}, {}
    ) == ("M", "Rojo")


def test_labels_flavor_and_color():
    assert _get_variant_labels(
        {"flavor_name": "Vainilla", "color_name": "Beige"}, {}
    ) == ("Vainilla", "Beige")


def test_labels_flavor_wins_over_size_as_type():
    assert _get_variant_labels(
        {"flavor_name": "Chocolate", "size_name": "L", "color_name": "Negro"}, {}
    ) == ("Chocolate", "Negro")


def test_labels_fallback_unknown_attribute():
    assert _get_variant_labels({"weird_attr": "foo"}, {}) == ("foo", "")


def test_labels_empty():
    assert _get_variant_labels({}, {}) == ("", "")


def test_dimension_display_backwards_compatible():
    assert _get_dimension_display({"size_name": "M", "color_name": "Rojo"}, {}) == "M - Rojo"
    assert _get_dimension_display({"color_name": "Rojo"}, {}) == "Rojo"
    assert _get_dimension_display({}, {}) == ""


def test_title_color_only():
    title = _build_variant_title({"color_name": "Rojo"}, {}, "Awesome Product")
    assert title == "Rojo - Awesome Product"


def test_title_size_only():
    title = _build_variant_title({"size_name": "M"}, {}, "Awesome Product")
    assert title == "M - Awesome Product"


def test_title_size_and_color():
    title = _build_variant_title(
        {"size_name": "M", "color_name": "Rojo"}, {}, "Awesome Product"
    )
    assert title == "M - Rojo - Awesome Product"


def test_title_flavor_and_color():
    title = _build_variant_title(
        {"flavor_name": "Vainilla", "color_name": "Beige"}, {}, "Protein Shake"
    )
    assert title == "Vainilla - Beige - Protein Shake"


def test_title_no_attributes_truncates_base():
    long_base = "x" * 80
    title = _build_variant_title({}, {}, long_base)
    assert title == "x" * 60
    assert len(title) <= ML_TITLE_LIMIT


def test_title_prefix_preserved_base_truncated():
    long_base = "Super Long Product Title That Exceeds Sixty Characters Easily Yes"
    title = _build_variant_title(
        {"size_name": "Medium", "color_name": "Crimson"}, {}, long_base
    )
    assert len(title) <= ML_TITLE_LIMIT
    assert title.startswith("Medium - Crimson - ")


def test_title_prefix_alone_too_long_truncated():
    # Edge case: prefix itself >= 60 chars; rare in practice
    title = _build_variant_title(
        {"size_name": "X" * 40, "color_name": "Y" * 40}, {}, "base"
    )
    assert len(title) <= ML_TITLE_LIMIT
