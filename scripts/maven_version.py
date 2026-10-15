# SPDX-License-Identifier: Apache-2.0
# Comparison adapted from Apache Maven 3.9.11 ComparableVersion; see NOTICE.
"""Bounded Maven ordering, with raw identities separate from release eligibility."""

from functools import total_ordering
from itertools import zip_longest
import re

type Item = int | str | list[Item]

_QUALIFIERS = ("alpha", "beta", "milestone", "rc", "snapshot", "", "sp")
_ALIASES = {"ga": "", "final": "", "release": "", "cr": "rc"}
_NUMBER = r"[0-9]+(?:\.[0-9]+)*"


def _qualifier(value: str, followed_by_digit: bool = False) -> str:
    if followed_by_digit:
        value = {"a": "alpha", "b": "beta", "m": "milestone"}.get(value, value)
    return _ALIASES.get(value, value)


def _qualifier_key(value: str) -> tuple[int, str]:
    return (_QUALIFIERS.index(value), "") if value in _QUALIFIERS else (7, value)


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _compare(left: Item | None, right: Item | None) -> int:
    if left is None:
        return 0 if right is None else -_compare(right, None)
    if right is None:
        if isinstance(left, int):
            return _sign(left)
        if isinstance(left, str):
            key, release = _qualifier_key(left), _qualifier_key("")
            return (key > release) - (key < release)
        for item in left:
            result = _compare(item, None)
            if result:
                return result
        return 0
    if isinstance(left, int):
        return _sign(left - right) if isinstance(right, int) else 1
    if isinstance(left, str):
        if not isinstance(right, str):
            return -1
        a, b = _qualifier_key(left), _qualifier_key(right)
        return (a > b) - (a < b)
    if isinstance(right, int):
        return -1
    if isinstance(right, str):
        return 1
    for a_item, b_item in zip_longest(left, right):
        result = _compare(a_item, b_item)
        if result:
            return result
    return 0


def _parse(value: str) -> list[Item]:
    root: list[Item] = []
    current = root
    stack = [root]

    def nest() -> None:
        nonlocal current
        child: list[Item] = []
        current.append(child)
        current = child
        stack.append(child)

    start, digit = 0, False
    for i, char in enumerate(value):
        if char in ".-":
            part = value[start:i]
            current.append(int(part) if digit else _qualifier(part))
            start = i + 1
            if char == "-":
                nest()
        elif char.isdigit():
            if not digit and i > start:
                if current:
                    nest()
                current.append(_qualifier(value[start:i], True))
                start = i
                nest()
            digit = True
        else:
            if digit and i > start:
                current.append(int(value[start:i]))
                start = i
                nest()
            digit = False
    if len(value) > start:
        if not digit and current:
            nest()
        current.append(int(value[start:]) if digit else _qualifier(value[start:]))
    for items in reversed(stack):
        for i in range(len(items) - 1, -1, -1):
            item = items[i]
            if item == 0 or item == "" or item == []:
                del items[i]
            elif not isinstance(item, list):
                break
    return root


@total_ordering
class MavenVersion:
    """Maven precedence never substitutes for an artifact's exact version string."""

    def __init__(self, value: str) -> None:
        if len(value) > 256 or not re.fullmatch(
            r"[0-9][A-Za-z0-9_+]*(?:[.-][A-Za-z0-9_+]+)*", value
        ):
            raise ValueError("Unsupported Maven version")
        self.raw = value
        self._items = _parse(value.lower())

    @property
    def stable(self) -> bool:
        # Arbitrary suffixes are valid locked identities, not automatic candidates.
        return bool(
            re.fullmatch(
                _NUMBER + r"(?:[.-](?:ga|final|release|sp[0-9]*|jre|android))?",
                self.raw.lower(),
            )
            or re.fullmatch(_NUMBER + r"(?:-[0-9]+)+", self.raw)
            or re.fullmatch(
                _NUMBER + r"-robolectric-[0-9]+(?:-[0-9]+)*(?:-i[0-9]+)?",
                self.raw.lower(),
            )
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MavenVersion):
            return NotImplemented
        return _compare(self._items, other._items) == 0

    def __lt__(self, other: "MavenVersion") -> bool:
        return _compare(self._items, other._items) < 0

    def __repr__(self) -> str:
        return f"MavenVersion({self.raw!r})"


def compatible(value: str, constraint: str) -> bool:
    """Keep the established comparison syntax; qualified literals are exact pins."""
    candidate = MavenVersion(value)
    if not constraint:
        return True
    if len(constraint) > 4096:
        raise ValueError("Maven constraint exceeds its bound")
    checks = []
    for clause in constraint.split(","):
        match = re.fullmatch(r"\s*(===|==|!=|>=|<=|~=|>|<)?\s*([^\s,]+)\s*", clause)
        if match is None:
            raise ValueError("Unsupported Maven constraint")
        operator, operand = match.groups()
        if operator is None:
            operator = "==" if re.fullmatch(_NUMBER, operand) else "==="
        if operand.endswith(".*") and operator in ("==", "!="):
            prefix = operand[:-2]
            if not re.fullmatch(_NUMBER, prefix):
                raise ValueError("Maven wildcard requires numeric components")
            actual = re.match(_NUMBER, value)
            assert actual is not None  # MavenVersion validated the leading number.
            members = actual[0].split(".")
            expected = prefix.split(".")
            equal = [int(x) for x in members[: len(expected)]] == [
                int(x) for x in expected
            ]
            checks.append(equal if operator == "==" else not equal)
            continue
        bound = MavenVersion(operand)
        if operator == "~=":
            if not re.fullmatch(_NUMBER, operand) or "." not in operand:
                raise ValueError(
                    "Maven compatible bounds require two numeric components"
                )
            parts = [int(x) for x in operand.split(".")[:-1]]
            parts[-1] += 1
            upper = MavenVersion(".".join(str(x) for x in parts))
            checks.append(bound <= candidate < upper)
        else:
            checks.append(
                {
                    "===": candidate.raw == bound.raw,
                    "==": candidate == bound,
                    "!=": candidate != bound,
                    ">=": candidate >= bound,
                    "<=": candidate <= bound,
                    ">": candidate > bound,
                    "<": candidate < bound,
                }[operator]
            )
    return all(checks)
