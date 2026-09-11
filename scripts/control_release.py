"""Check maturity of pinned native backend inputs before publishing a candidate."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(sources, now):
    age = sources["minimum_age_days"]
    if type(age) is not int or age < 30:
        raise ValueError("Native backend maturity must be at least 30 days")
    for name, spec in sources.items():
        if name == "minimum_age_days":
            continue
        published = datetime.fromisoformat(spec["published"].replace("Z", "+00:00"))
        eligible = published + timedelta(days=age)
        if now < eligible:
            raise ValueError(
                f"{name} {spec['version']} is not mature until {eligible.isoformat()}; candidate qualification may continue, publication must wait"
            )


if __name__ == "__main__":
    check(
        json.loads((ROOT / "nix/control-sources.json").read_text()),
        datetime.now(timezone.utc),
    )
    print("Native backend dependency maturity passed")
