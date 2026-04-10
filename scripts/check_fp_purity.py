"""Fail if any function in the given pure modules is missing @deal.pure/@deal.has().

Usage: python check_fp_purity.py <file> [<file> ...]
Exits 0 if all functions in all given files are properly decorated and
all dataclasses are frozen. Exits 1 with a list of violations otherwise.
"""

import ast
import sys
from pathlib import Path


def _is_deal_attr(node: ast.expr, names: set[str]) -> bool:
    """Check if *node* is ``deal.<name>`` for any name in *names*."""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "deal"
        and node.attr in names
    )


def _is_deal_call(node: ast.expr, names: set[str]) -> bool:
    """Check if *node* is ``deal.<name>(...)`` for any name in *names*."""
    return isinstance(node, ast.Call) and _is_deal_attr(node.func, names)


_VALID_DEAL_NAMES = {"pure", "has", "safe"}


def _has_deal_decorator(decorators: list[ast.expr]) -> bool:
    """Accept @deal.pure, @deal.has(), or @deal.chain(...) containing deal.has/deal.pure."""
    for d in decorators:
        # @deal.pure (bare attribute)
        if _is_deal_attr(d, _VALID_DEAL_NAMES):
            return True
        # @deal.pure() or @deal.has(...) (call form)
        if _is_deal_call(d, _VALID_DEAL_NAMES):
            return True
        # @deal.chain(deal.has(), deal.raises(ValueError)) — accept if any
        # positional arg is a recognised deal contract
        if _is_deal_call(d, {"chain"}) and isinstance(d, ast.Call):
            for arg in d.args:
                if _is_deal_attr(arg, _VALID_DEAL_NAMES):
                    return True
                if _is_deal_call(arg, _VALID_DEAL_NAMES | {"raises"}):
                    return True
    return False


def _is_frozen_dataclass(decorators: list[ast.expr]) -> tuple[bool, bool]:
    """Return (is_dataclass, is_frozen).

    Recognizes both bare and qualified forms:
    - @dataclass
    - @dataclass(frozen=True)
    - @dataclasses.dataclass
    - @dataclasses.dataclass(frozen=True)
    """
    for d in decorators:
        # Bare form: @dataclass
        if isinstance(d, ast.Name) and d.id == "dataclass":
            return (True, False)
        # Bare form with args: @dataclass(frozen=True)
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "dataclass":
            frozen = any(
                isinstance(kw, ast.keyword)
                and kw.arg == "frozen"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in d.keywords
            )
            return (True, frozen)
        # Qualified form: @dataclasses.dataclass
        if (
            isinstance(d, ast.Attribute)
            and d.attr == "dataclass"
            and isinstance(d.value, ast.Name)
            and d.value.id == "dataclasses"
        ):
            return (True, False)
        # Qualified form with args: @dataclasses.dataclass(frozen=True)
        if (
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr == "dataclass"
            and isinstance(d.func.value, ast.Name)
            and d.func.value.id == "dataclasses"
        ):
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
