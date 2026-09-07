"""Small pure text helpers."""


def summarize(labels: list[str]) -> str:
    """Join trimmed, nonempty labels with a stable separator."""
    return ", ".join(label.strip() for label in labels if label.strip())
