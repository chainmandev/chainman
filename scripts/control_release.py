"""Check maturity of pinned native backend inputs before publishing a candidate."""

from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(sources: Mapping[str, object], now: datetime) -> None:
    age = sources["minimum_age_days"]
    if type(age) is not int or age < 30:
        raise ValueError("Native backend maturity must be at least 30 days")
    for name, spec in sources.items():
        if name == "minimum_age_days":
            continue
        if not isinstance(spec, dict):
            raise ValueError(f"{name} must declare a native source record")
        version, publication = spec.get("version"), spec.get("published")
        if not isinstance(version, str) or not isinstance(publication, str):
            raise ValueError(f"{name} requires version and publication strings")
        published = datetime.fromisoformat(publication.replace("Z", "+00:00"))
        if published.utcoffset() is None or now.utcoffset() is None:
            raise ValueError("Native backend maturity requires timezone-aware dates")
        eligible = published + timedelta(days=age)
        if now < eligible:
            raise ValueError(
                f"{name} {version} is not mature until {eligible.isoformat()}; candidate qualification may continue, publication must wait"
            )


if __name__ == "__main__":
    check(
        json.loads((ROOT / "nix/control-sources.json").read_text()),
        datetime.now(timezone.utc),
    )
    print("Native backend dependency maturity passed")
