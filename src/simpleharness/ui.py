from __future__ import annotations

from rich.console import Console

console = Console()


def say(msg: str, *, style: str = "cyan") -> None:
    """Print a harness-prefixed message to the terminal."""
    console.print(rf"[{style}]\[harness][/] {msg}")


def warn(msg: str) -> None:
    console.print(rf"[yellow]\[harness WARNING][/] {msg}")


def err(msg: str) -> None:
    console.print(rf"[red]\[harness ERROR][/] {msg}")
