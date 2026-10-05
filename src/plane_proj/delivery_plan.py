"""Declared card scope and dependency assessment.

Parses and renders the 'Delivery plan' section of a card description,
carrying a card's declared repository paths (`Touches`) and dependency
assessment status (`Dependencies: assessed`).
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass

from plane_proj.guards import DeliveryPlanRule

DELIVERY_PLAN_HEADING = "Delivery plan"
_HTML_HEADING = re.compile(
    r"(?i)<h([1-6])[^>]*>\s*delivery\s+plan\s*</h\1>"
)
_MD_HEADING = re.compile(
    r"(?mi)^#{1,6}\s+delivery\s+plan\b.*$"
)


def validate_path(path: str) -> None:
    """Validate a path declaration relative to repository root.

    Refuses absolute paths, '..' segments, and backslashes.
    """
    if not path or not path.strip():
        raise DeliveryPlanRule("Delivery plan rule: path cannot be empty.")
    stripped = path.strip()
    if stripped.startswith("/") or stripped.startswith("\\"):
        raise DeliveryPlanRule(
            f"Delivery plan rule: path must be relative to repository root; got {path!r}."
        )
    if "\\" in stripped:
        raise DeliveryPlanRule(
            f"Delivery plan rule: path must use '/' separators without backslashes; got {path!r}."
        )
    parts = stripped.split("/")
    if any(part == ".." for part in parts):
        raise DeliveryPlanRule(
            f"Delivery plan rule: path cannot contain '..' segments; got {path!r}."
        )


@dataclass(frozen=True)
class TouchedPath:
    """A declared path touched by a card, with an optional (new) mark."""

    path: str
    is_new: bool = False

    def __post_init__(self) -> None:
        validate_path(self.path)

    @classmethod
    def parse(cls, text: str) -> TouchedPath:
        """Parse a path string with an optional (new) suffix."""
        cleaned = text.strip()
        is_new = False
        if cleaned.lower().endswith("(new)"):
            is_new = True
            cleaned = cleaned[:-5].rstrip()
        return cls(path=cleaned, is_new=is_new)

    def __str__(self) -> str:
        return f"{self.path} (new)" if self.is_new else self.path


@dataclass(frozen=True)
class DeliveryPlan:
    """Declared scope and dependency assessment for a work item.

    `touches` is None when undeclared, () when declared as none (no code changed),
    or a tuple of TouchedPath objects.
    `dependencies_assessed` is None when undeclared, or True when assessed.
    """

    touches: tuple[TouchedPath, ...] | None = None
    dependencies_assessed: bool | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        if self.touches is None:
            return ()
        return tuple(p.path for p in self.touches)

    @property
    def is_touches_none(self) -> bool:
        return self.touches is not None and len(self.touches) == 0

    @property
    def touches_declared(self) -> bool:
        return self.touches is not None

    @property
    def deps_declared(self) -> bool:
        return self.dependencies_assessed is not None

    @property
    def is_declared(self) -> bool:
        return self.touches_declared and self.deps_declared


def render_section(
    plan: DeliveryPlan | None = None,
    *,
    touches: Sequence[str | TouchedPath] | None = None,
    touches_none: bool = False,
    deps_assessed: bool | None = None,
) -> str:
    """Render the Markdown 'Delivery plan' section."""
    if plan is not None:
        if plan.touches is not None:
            touches = plan.touches
            touches_none = len(plan.touches) == 0
        if plan.dependencies_assessed is not None:
            deps_assessed = plan.dependencies_assessed

    if touches is None and not touches_none and deps_assessed is None:
        return ""

    lines = [f"## {DELIVERY_PLAN_HEADING}"]
    if touches_none:
        lines.append("Touches: none")
    elif touches is not None:
        lines.append("Touches:")
        for item in touches:
            p = TouchedPath.parse(item) if isinstance(item, str) else item
            lines.append(f"- {p}")

    if deps_assessed:
        lines.append("Dependencies: assessed")

    return "\n".join(lines)


def _strip_html(text: str) -> str:
    """Convert HTML snippet to plain lines with block tags as newlines."""
    t = re.sub(r"(?i)<br\s*/?>", "\n", text)
    t = re.sub(r"(?i)</(p|li|div|h[1-6])>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t)


def parse_delivery_plan(content: str | None) -> DeliveryPlan:
    """Parse the Delivery plan section from Markdown or stored HTML."""
    if not content or not content.strip():
        return DeliveryPlan(touches=None, dependencies_assessed=None)

    html_match = _HTML_HEADING.search(content)
    md_match = _MD_HEADING.search(content)

    if html_match:
        rest = content[html_match.end():]
        next_h = re.search(r"(?i)<h[1-6][^>]*>", rest)
        end = next_h.start() if next_h else len(rest)
        div_idx = rest.rfind("</div>")
        if not next_h and div_idx >= 0:
            end = min(end, div_idx)
        raw_section = _strip_html(rest[:end])
    elif md_match:
        rest = content[md_match.end():]
        next_h = re.search(r"(?mi)^#{1,6}\s+", rest)
        end = next_h.start() if next_h else len(rest)
        raw_section = rest[:end]
    else:
        return DeliveryPlan(touches=None, dependencies_assessed=None)

    lines = [line.strip() for line in raw_section.splitlines() if line.strip()]
    touches: list[TouchedPath] | None = None
    deps_assessed: bool | None = None

    in_touches = False
    for line in lines:
        if re.match(r"(?i)^touches:\s*none\s*$", line):
            touches = []
            in_touches = False
            continue
        m_touches = re.match(r"(?i)^touches:\s*(.*)$", line)
        if m_touches:
            inline = m_touches.group(1).strip()
            touches = []
            if inline.lower() == "none":
                in_touches = False
            elif inline:
                touches.append(TouchedPath.parse(re.sub(r"^[-*•]\s*", "", inline)))
                in_touches = True
            else:
                in_touches = True
            continue

        if re.match(r"(?i)^dependencies:\s*assessed\s*$", line):
            deps_assessed = True
            in_touches = False
            continue

        if in_touches:
            cleaned = re.sub(r"^[-*•]\s*", "", line).strip()
            if cleaned:
                if cleaned.lower() == "none":
                    touches = []
                    in_touches = False
                else:
                    touches.append(TouchedPath.parse(cleaned))

    touches_tuple = tuple(touches) if touches is not None else None
    return DeliveryPlan(touches=touches_tuple, dependencies_assessed=deps_assessed)


def _is_html(text: str) -> bool:
    return bool(re.search(r"(?i)</?(div|p|h[1-6]|span|ul|li)\b", text))


def replace_section(
    description: str,
    new_section: str | DeliveryPlan,
) -> str:
    """Replace only the Delivery plan section of an existing description.

    Leaves the text before and after the section byte-identical.
    """
    if isinstance(new_section, DeliveryPlan):
        if _is_html(description):
            from plane_proj.text import to_html
            rendered = to_html(render_section(new_section))
        else:
            rendered = render_section(new_section)
    else:
        rendered = new_section

    html_match = _HTML_HEADING.search(description)
    if html_match:
        start = html_match.start()
        rest = description[html_match.end():]
        next_h = re.search(r"(?i)<h[1-6][^>]*>", rest)
        if next_h:
            end = html_match.end() + next_h.start()
        else:
            div_idx = description.rfind("</div>")
            end = div_idx if div_idx >= html_match.end() else len(description)
        return description[:start] + rendered + description[end:]

    md_match = _MD_HEADING.search(description)
    if md_match:
        start = md_match.start()
        rest = description[md_match.end():]
        next_h = re.search(r"(?mi)^#{1,6}\s+", rest)
        end = md_match.end() + next_h.start() if next_h else len(description)
        return description[:start] + rendered + description[end:]

    # Section not found; append to description
    if _is_html(description):
        div_idx = description.rfind("</div>")
        if div_idx >= 0:
            return description[:div_idx] + rendered + description[div_idx:]
        return description + rendered

    if description.strip():
        return description.rstrip() + "\n\n" + rendered + "\n"
    return rendered + "\n"
