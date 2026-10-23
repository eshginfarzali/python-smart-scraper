from __future__ import annotations

import pytest
from pydantic import ValidationError

from smart_scraper.scraping.extract import FieldRule, extract
from tests.helpers import fixture_html

HTML = fixture_html("product_list.html")


def test_text_of_first_match_is_whitespace_normalised() -> None:
    data = extract(HTML, {"title": FieldRule(selector="h1.page-title")})
    assert data == {"title": "Our Teas"}


def test_nested_text_is_joined_with_spaces() -> None:
    data = extract(HTML, {"name": FieldRule(selector="article:nth-of-type(2) .name")})
    assert data == {"name": "Darjeeling First Flush"}


def test_attribute_extraction() -> None:
    rules = {
        "description": FieldRule(selector='meta[name="description"]', attr="content"),
        "first_sku": FieldRule(selector="article.product", attr="data-sku"),
    }
    assert extract(HTML, rules) == {
        "description": "Loose-leaf teas from small farms.",
        "first_sku": "TEA-001",
    }


def test_multiple_returns_every_match() -> None:
    rules = {
        "names": FieldRule(selector="article.product h2.name", multiple=True),
        "prices": FieldRule(selector=".price", multiple=True),
        "skus": FieldRule(selector="article.product", attr="data-sku", multiple=True),
    }
    assert extract(HTML, rules) == {
        "names": ["Sencha", "Darjeeling First Flush", "Rooibos"],
        "prices": ["12.50", "18.00", "9.90"],
        "skus": ["TEA-001", "TEA-002", "TEA-003"],
    }


def test_multiple_attribute_skips_elements_without_the_attribute() -> None:
    rules = {"links": FieldRule(selector="a.details", attr="href", multiple=True)}
    assert extract(HTML, rules) == {"links": ["/teas/sencha", "/teas/darjeeling"]}


def test_missing_values() -> None:
    rules = {
        "absent": FieldRule(selector=".does-not-exist"),
        "absent_list": FieldRule(selector=".does-not-exist", multiple=True),
        "absent_attr": FieldRule(selector="h1", attr="data-missing"),
    }
    assert extract(HTML, rules) == {"absent": None, "absent_list": [], "absent_attr": None}


def test_pseudo_classes_and_combinators() -> None:
    rules = {
        "in_stock": FieldRule(selector="article .price:not(.sold-out)", multiple=True),
        "nav_count": FieldRule(selector="header > nav > a", multiple=True),
    }
    data = extract(HTML, rules)
    assert data["in_stock"] == ["12.50", "18.00"]
    assert data["nav_count"] == ["Home", "Catalogue", "Blog"]


def test_malformed_html_is_tolerated() -> None:
    data = extract(
        "<div><p>unclosed <b>bold<p>second", {"p": FieldRule(selector="p", multiple=True)}
    )
    assert data == {"p": ["unclosed bold", "second"]}


@pytest.mark.parametrize("selector", ["p[", "div..x", "a >", ""])
def test_invalid_selectors_are_rejected(selector: str) -> None:
    with pytest.raises(ValidationError):
        FieldRule(selector=selector)


def test_unknown_rule_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        FieldRule.model_validate({"selector": "h1", "xpath": "//h1"})
