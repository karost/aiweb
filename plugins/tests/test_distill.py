"""Unit tests for distill.final_only — vectors V-01 … V-12 (architecture v2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Allow running tests without installing the package
_PLUGIN = Path(__file__).resolve().parents[1]
_PLUGINS = _PLUGIN.parent
if str(_PLUGINS) not in sys.path:
    sys.path.insert(0, str(_PLUGINS))

from aiweb.distill import final_only  # noqa: E402


def test_v01_heading_final_prose():
    text = """Research notes:
- tried A
- tried B

## Final
Use a queue with backoff and jitter.
"""
    r = final_only(text, cap=100)
    assert r.method == "heading_final"
    assert r.capped is False
    assert "Use a queue with backoff" in r.payload
    assert "tried A" not in r.payload


def test_v02_last_heading_wins():
    text = """## Solution
Wrong intermediate.

## Notes
Noise.

## Answer
Correct final answer only.
"""
    r = final_only(text, cap=200)
    assert r.method == "heading_final"
    assert "Correct final answer only" in r.payload
    assert "Wrong intermediate" not in r.payload


def test_v03_fence_largest_code_hint():
    text = """Here is analysis...

    ```python
    x = 1
    def main():
        return 42

"""
    r = final_only(text, cap=100, hint="code", language="python")
    assert r.method == "fence_largest"
    assert "def main()" in r.payload

def test_v04_prefer_matching_language_fence():
    text = """
            ```javascript 
            console.log(1)
            ```python
            def foo():
                return "py"
            """   
    r = final_only(text, cap=200, hint="code", language="python")
    assert r.method == "fence_largest"
    assert "def foo" in r.payload
    assert "console.log" not in r.payload

def test_v05_cap_applied():
    text = "## Final\n" + ("A" * 500)
    r = final_only(text, cap=50)
    assert r.method == "heading_final"
    assert r.capped is True
    assert len(r.payload) == 50

def test_v06_tail_strip_no_heading_no_fence():
    text =  """Nav Home Search
            thinking about the problem
            blah step1 step2 step3
            THE_REAL_TAIL_UNIQUE_XYZ
            """
    r = final_only(text, cap=80)
    assert r.method == "tail_strip"
    assert "THE_REAL_TAIL_UNIQUE_XYZ" in r.payload

def test_v07_empty():
    r = final_only("", cap=100)
    assert r.method == "empty"
    assert r.payload == ""
    assert r.capped is False
    r2 = final_only("   \n\t  ", cap=100)
    assert r2.method == "empty"
    assert r2.payload == ""

def test_v08_heading_case_tolerant():
    text = """### FINAL SOLUTION
    Ship the patch on Monday.
    """
    r = final_only(text, cap=100)
    assert r.method == "heading_final"
    assert "Ship the patch" in r.payload

def test_v09_fence_without_language():
    text = """```
    function hi() { return 1 }
    """
    r = final_only(text, cap=200, hint="code")
    assert r.method == "fence_largest"
    assert "function hi" in r.payload


def test_v10_research_bulk_not_in_final():
    research = "\n".join(f"research_line_{i}" for i in range(300))
    text = f"# Research\n{research}\n\n## Final\nOne line solution.\n"
    r = final_only(text, cap=6000)
    assert r.method == "heading_final"
    assert "One line solution" in r.payload
    assert "research_line_0" not in r.payload


def test_v12_model_pack_cap_6000():
    text = "## Final\n" + ("B" * 10_000)
    r = final_only(text, cap=6000)
    assert r.method == "heading_final"
    assert r.capped is True
    assert len(r.payload) == 6000


def test_cap_must_be_positive():
    with pytest.raises(ValueError):
        final_only("## Final\nhi", cap=0)


# V-11 is service-level (path_only inject), not distill.final_only


if __name__ == "__main__":
    pytest.main([__file__, "-v"])