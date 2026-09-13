"""Named immutable dependency coordinates, with the existing five-string wire form.

Decoding validates structure only. Registry/source adapters remain responsible
for canonical names, permitted sources, hashes and publication evidence.
"""

from typing import NamedTuple


class Identity(NamedTuple):
    provider: str
    package: str
    version: str
    url: str
    digest: str


def decode(value: object) -> Identity:
    if not isinstance(value, (list, tuple)) or len(value) != 5:
        raise ValueError("Dependency identity requires five string fields")
    fields: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("Dependency identity requires five string fields")
        fields.append(item)
    return Identity(
        provider=fields[0],
        package=fields[1],
        version=fields[2],
        url=fields[3],
        digest=fields[4],
    )


def inventory(values: object) -> set[Identity]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("Dependency inventory requires an array or set of identities")
    return {decode(value) for value in values}
