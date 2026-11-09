"""Isolated optional runtime entry for attestation and manual npm publication."""

from collections.abc import Sequence
from pathlib import Path
import sys

from attestation import arguments
from attestation_common import parse
from attestation_model import contained_file, policy
from attestation_provenance import create_attestations, verify_manifest
from attestation_publication import npm_publish


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[-2] != "--policy" or args.count("--policy") != 1:
        raise ValueError("An explicit --policy JSON file is required")
    policy_path = Path(args[-1]).absolute()
    configured = policy(parse(contained_file(policy_path.parent, policy_path.name)))
    args = args[:-2]
    if not args or args[0] not in {"attest", "npm-publish"}:
        raise ValueError("Expected attest create, attest verify, or npm-publish")
    parsed = arguments(args[0], args[1:])
    if parsed[0] == "npm-publish":
        npm_publish(parsed[1], parsed[3], configured)
    elif parsed[1] == "create":
        create_attestations(
            parsed[2], configured, parsed[4] if len(parsed) == 5 else None
        )
    else:
        verified = verify_manifest(parsed[2], parsed[4], configured)
        verified.dispose()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
