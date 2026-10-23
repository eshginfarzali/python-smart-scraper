"""Declarative HTML extraction: CSS selector rules -> named fields."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from selectolax.lexbor import LexborHTMLParser, LexborNode, SelectolaxError

FieldName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]

_EMPTY_DOCUMENT = LexborHTMLParser("<html></html>")


class FieldRule(BaseModel):
    """How to extract one field from a page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: str = Field(min_length=1, max_length=500, description="CSS selector.")
    attr: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Attribute to read (e.g. 'href'). Omit to read the element's text.",
    )
    multiple: bool = Field(default=False, description="Return every match as a list.")

    @field_validator("selector")
    @classmethod
    def _selector_must_parse(cls, value: str) -> str:
        try:
            _EMPTY_DOCUMENT.css(value)
        except SelectolaxError as exc:
            raise ValueError(f"invalid CSS selector: {value!r}") from exc
        return value


ExtractedValue = str | list[str] | None


def _normalise_space(text: str) -> str:
    return " ".join(text.split())


def _value_of(node: LexborNode, rule: FieldRule) -> str | None:
    if rule.attr is None:
        return _normalise_space(node.text(separator=" "))
    return node.attributes.get(rule.attr)


def extract(html: str, rules: dict[str, FieldRule]) -> dict[str, ExtractedValue]:
    """Apply ``rules`` to ``html``.

    Single-value fields yield the first match (``None`` if nothing matches); ``multiple``
    fields yield a list of every non-missing value (possibly empty). Text is
    whitespace-normalised; attribute values are returned verbatim.
    """
    tree = LexborHTMLParser(html)
    data: dict[str, ExtractedValue] = {}
    for name, rule in rules.items():
        if rule.multiple:
            values = (_value_of(node, rule) for node in tree.css(rule.selector))
            data[name] = [value for value in values if value is not None]
        else:
            node = tree.css_first(rule.selector)
            data[name] = None if node is None else _value_of(node, rule)
    return data
