from __future__ import annotations

import pytest

from plane_proj.delivery_plan import (
    DeliveryPlan,
    DeliveryPlanRule,
    TouchedPath,
    parse_delivery_plan,
    render_section,
    replace_section,
    validate_path,
)
from plane_proj.text import to_html


def test_roundtrip_render_html_parse() -> None:
    """pytest renders a section, converts it to HTML with the project's Markdown

    conversion, parses it back, and gets the same paths, new marks and assessed flag.
    """
    paths = (
        TouchedPath("src/api/members.ts", is_new=False),
        TouchedPath("src/shop/pickup.ts", is_new=True),
        TouchedPath("skills/delivery-plane/", is_new=False),
    )
    plan = DeliveryPlan(touches=paths, dependencies_assessed=True)

    rendered_md = render_section(plan)
    assert "## Delivery plan" in rendered_md
    assert "- src/api/members.ts" in rendered_md
    assert "- src/shop/pickup.ts (new)" in rendered_md
    assert "- skills/delivery-plane/" in rendered_md
    assert "Dependencies: assessed" in rendered_md

    html = to_html(rendered_md)
    parsed = parse_delivery_plan(html)

    assert parsed.touches is not None
    assert [p.path for p in parsed.touches] == [p.path for p in paths]
    assert [p.is_new for p in parsed.touches] == [p.is_new for p in paths]
    assert parsed.dependencies_assessed is True
    assert parsed.paths == ("src/api/members.ts", "src/shop/pickup.ts", "skills/delivery-plane/")
    assert parsed.is_declared is True


def test_roundtrip_touches_none() -> None:
    """Render and parse Touches: none through HTML."""
    plan = DeliveryPlan(touches=(), dependencies_assessed=True)
    rendered_md = render_section(plan)
    assert "Touches: none" in rendered_md
    assert "Dependencies: assessed" in rendered_md

    html = to_html(rendered_md)
    parsed = parse_delivery_plan(html)

    assert parsed.touches == ()
    assert parsed.is_touches_none is True
    assert parsed.dependencies_assessed is True
    assert parsed.is_declared is True


def test_undeclared_parsing() -> None:
    """pytest shows a description with no section parses as undeclared for both

    touches and dependencies, and a section with only one line as undeclared for the other.
    """
    # 1. No section at all
    no_section_md = "## What to build\nSome description without delivery plan."
    no_section_html = to_html(no_section_md)

    p1_md = parse_delivery_plan(no_section_md)
    assert p1_md.touches is None
    assert p1_md.dependencies_assessed is None
    assert p1_md.touches_declared is False
    assert p1_md.deps_declared is False
    assert p1_md.is_declared is False

    p1_html = parse_delivery_plan(no_section_html)
    assert p1_html.touches is None
    assert p1_html.dependencies_assessed is None

    # Empty string or None
    assert parse_delivery_plan("").touches is None
    assert parse_delivery_plan(None).touches is None

    # 2. Section with only Touches (dependencies undeclared)
    touches_only_md = "## Delivery plan\nTouches:\n- src/api/members.ts\n"
    p2_md = parse_delivery_plan(touches_only_md)
    assert p2_md.touches is not None
    assert len(p2_md.touches) == 1
    assert p2_md.touches[0].path == "src/api/members.ts"
    assert p2_md.dependencies_assessed is None
    assert p2_md.touches_declared is True
    assert p2_md.deps_declared is False
    assert p2_md.is_declared is False

    p2_html = parse_delivery_plan(to_html(touches_only_md))
    assert p2_html.touches is not None
    assert len(p2_html.touches) == 1
    assert p2_html.touches[0].path == "src/api/members.ts"
    assert p2_html.dependencies_assessed is None

    # Touches: none only
    touches_none_only_md = "## Delivery plan\nTouches: none\n"
    p2_none = parse_delivery_plan(touches_none_only_md)
    assert p2_none.touches == ()
    assert p2_none.dependencies_assessed is None

    # 3. Section with only Dependencies: assessed (touches undeclared)
    deps_only_md = "## Delivery plan\nDependencies: assessed\n"
    p3_md = parse_delivery_plan(deps_only_md)
    assert p3_md.touches is None
    assert p3_md.dependencies_assessed is True
    assert p3_md.touches_declared is False
    assert p3_md.deps_declared is True
    assert p3_md.is_declared is False

    p3_html = parse_delivery_plan(to_html(deps_only_md))
    assert p3_html.touches is None
    assert p3_html.dependencies_assessed is True


def test_replace_section_byte_identical() -> None:
    """pytest shows replacing the section leaves the text before and after it byte-identical."""
    # Markdown in middle of document
    prefix_md = "## What to build\nBuild this feature cleanly.\n\n"
    suffix_md = "## Acceptance criteria\n- Must pass all tests.\n"
    old_section_md = "## Delivery plan\nTouches:\n- old/path.ts\nDependencies: assessed\n\n"
    full_md = prefix_md + old_section_md + suffix_md

    new_section_md = (
        "## Delivery plan\nTouches:\n- new/path.ts (new)\nDependencies: assessed\n\n"
    )
    result_md = replace_section(full_md, new_section_md)

    assert result_md[:len(prefix_md)] == prefix_md
    assert result_md[len(prefix_md) + len(new_section_md):] == suffix_md

    # HTML in middle of document
    prefix_html = "<div><h2>What to build</h2><p>Build this cleanly.</p>"
    suffix_html = "<h2>Acceptance criteria</h2><p>Must pass.</p></div>"
    old_section_html = (
        "<h2>Delivery plan</h2><p>Touches:<br />- old/path.ts<br />Dependencies: assessed</p>"
    )
    full_html = prefix_html + old_section_html + suffix_html

    new_section_html = (
        "<h2>Delivery plan</h2><p>Touches:<br />- new/path.ts<br />Dependencies: assessed</p>"
    )
    result_html = replace_section(full_html, new_section_html)

    assert result_html[:len(prefix_html)] == prefix_html
    assert result_html[len(prefix_html) + len(new_section_html):] == suffix_html

    # Replace with DeliveryPlan dataclass directly
    plan = DeliveryPlan(
        touches=(TouchedPath("src/replaced.py", is_new=False),),
        dependencies_assessed=True,
    )
    res_from_plan = replace_section(full_md, plan)
    assert res_from_plan[:len(prefix_md)] == prefix_md
    assert res_from_plan.endswith(suffix_md)


def test_path_validation_refusals() -> None:
    """pytest refuses absolute paths, .. segments and backslashes."""
    # Absolute paths
    with pytest.raises(DeliveryPlanRule, match="relative to repository root"):
        validate_path("/src/plane_proj/cli.py")

    with pytest.raises(DeliveryPlanRule, match="relative to repository root"):
        validate_path("/etc/passwd")

    with pytest.raises(DeliveryPlanRule, match="relative to repository root"):
        TouchedPath("/absolute/path.py")

    # .. segments
    with pytest.raises(DeliveryPlanRule, match=r"\.\."):
        validate_path("../outside.py")

    with pytest.raises(DeliveryPlanRule, match=r"\.\."):
        validate_path("src/../outside.py")

    with pytest.raises(DeliveryPlanRule, match=r"\.\."):
        validate_path("src/deep/..")

    with pytest.raises(DeliveryPlanRule, match=r"\.\."):
        TouchedPath("src/../foo.py")

    # Backslashes
    with pytest.raises(DeliveryPlanRule, match="backslashes"):
        validate_path(r"src\plane_proj\cli.py")

    with pytest.raises(DeliveryPlanRule, match="backslashes"):
        TouchedPath(r"src\plane_proj\cli.py")

    # Empty / whitespace
    with pytest.raises(DeliveryPlanRule, match="cannot be empty"):
        validate_path("")

    with pytest.raises(DeliveryPlanRule, match="cannot be empty"):
        validate_path("   ")


def test_valid_paths() -> None:
    """Valid paths include files, (new) marks, and directories ending in /."""
    p1 = TouchedPath.parse("src/api/members.ts")
    assert p1.path == "src/api/members.ts"
    assert p1.is_new is False
    assert str(p1) == "src/api/members.ts"

    p2 = TouchedPath.parse("src/shop/pickup.ts (new)")
    assert p2.path == "src/shop/pickup.ts"
    assert p2.is_new is True
    assert str(p2) == "src/shop/pickup.ts (new)"

    p3 = TouchedPath.parse("skills/delivery-plane/")
    assert p3.path == "skills/delivery-plane/"
    assert p3.is_new is False
