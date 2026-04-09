"""Tests for the report CLI tool."""

from __future__ import annotations

import json

from report import ITEMS, main


def test_text_output(capsys):
    """Text format includes all item names."""
    main(["--format", "text"])
    captured = capsys.readouterr()
    for item in ITEMS:
        assert item["name"] in captured.out


def test_default_is_text(capsys):
    """Running with no arguments produces text output."""
    main([])
    captured = capsys.readouterr()
    # Should contain the table header
    assert "Name" in captured.out
    assert "Qty" in captured.out
    for item in ITEMS:
        assert item["name"] in captured.out


def test_json_output(capsys):
    """JSON format outputs a valid JSON array with correct keys."""
    main(["--format", "json"])
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, list)
    assert len(data) == len(ITEMS)
    for entry in data:
        assert "name" in entry
        assert "quantity" in entry
        assert "price" in entry
