"""CLI tool that generates a summary report of inventory items."""

from __future__ import annotations

import argparse

ITEMS = [
    {"name": "Widget A", "quantity": 10, "price": 4.99},
    {"name": "Widget B", "quantity": 25, "price": 9.49},
    {"name": "Gadget C", "quantity": 5, "price": 24.95},
    {"name": "Doohickey D", "quantity": 50, "price": 1.25},
    {"name": "Thingamajig E", "quantity": 12, "price": 15.00},
]


def format_text(items: list[dict]) -> str:
    """Format items as a readable text table."""
    header = f"{'Name':<20} {'Qty':>5} {'Price':>8}"
    separator = "-" * len(header)
    lines = [header, separator]
    total_value = 0.0
    for item in items:
        value = item["quantity"] * item["price"]
        total_value += value
        lines.append(f"{item['name']:<20} {item['quantity']:>5} {item['price']:>8.2f}")
    lines.append(separator)
    lines.append(f"{'Total value:':<26} {total_value:>8.2f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    """Entry point for the report CLI."""
    parser = argparse.ArgumentParser(description="Generate an inventory report.")
    parser.add_argument(
        "--format",
        dest="fmt",
        default="text",
        choices=["text"],
        help="Output format (default: text)",
    )
    args = parser.parse_args(argv)

    if args.fmt == "text":
        format_text(ITEMS)
    else:
        parser.error(f"Unknown format: {args.fmt}")
        return  # unreachable, but keeps type checkers happy


if __name__ == "__main__":
    main()
