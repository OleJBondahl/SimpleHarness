"""Fail if any function in the given pure modules is missing @deal.pure/@deal.has().

Usage: python check_fp_purity.py <file> [<file> ...]
Exits 0 if all functions in all given files are properly decorated and
all dataclasses are frozen. Exits 1 with a list of violations otherwise.
"""

import ast
import sys
from pathlib import Path


def _has_deal_decorator(decorators: list[ast.expr]) -> bool:
    """Accept either @deal.pure or @deal.has() as a valid FP-purity decorator."""
    for d in decorators:
        # @deal.pure (bare attribute, no parens)
        if (
            isinstance(d, ast.Attribute)
            and isinstance(d.value, ast.Name)
            and d.value.id == "deal"
            and d.attr in ("pure", "has", "safe")
        ):
            return True
        # @deal.pure() or @deal.has(...) (call form)
        if (
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name)
            and d.func.value.id == "deal"
            and d.func.attr in ("pure", "has", "safe")
        ):
            return True
    return False


def _is_frozen_dataclass(decorators: list[ast.expr]) -> tuple[bool, bool]:
    """Return (is_dataclass, is_frozen)."""
    for d in decorators:
        if isinstance(d, ast.Name) and d.id == "dataclass":
            return (True, False)
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "dataclass":
            frozen = any(
                isinstance(kw, ast.keyword)
                and kw.arg == "frozen"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in d.keywords
            )
            return (True, frozen)
    return (False, False)


def check_file(path: Path) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _has_deal_decorator(node.decorator_list):
                violations.append(
                    f"{path}:{node.lineno}: function `{node.name}` missing @deal.pure/@deal.has()"
                )
        elif isinstance(node, ast.ClassDef):
            is_dc, is_frozen = _is_frozen_dataclass(node.decorator_list)
            if is_dc and not is_frozen:
                violations.append(
                    f"{path}:{node.lineno}: dataclass `{node.name}` must use frozen=True"
                )
            if not is_dc:
                for sub in node.body:
                    if isinstance(
                        sub, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ) and not _has_deal_decorator(sub.decorator_list):
                        violations.append(
                            f"{path}:{sub.lineno}: method "
                            f"`{node.name}.{sub.name}` missing @deal.pure/@deal.has()"
                        )
    return violations


def main() -> int:
    files = [Path(p) for p in sys.argv[1:]]
    if not files:
        print("usage: check_fp_purity.py <file> [<file> ...]", file=sys.stderr)
        return 2
    all_violations: list[str] = []
    for f in files:
        all_violations.extend(check_file(f))
    if all_violations:
        print("FP purity gate: violations found", file=sys.stderr)
        for v in all_violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
