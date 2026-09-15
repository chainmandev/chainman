"""A small project command; replace it with your application's verification."""

import sys


def greeting(name: str) -> str:
    return f"Hello, {name}!"


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        assert greeting("Chainman") == "Hello, Chainman!"
        print("Example verification passed.")
    else:
        print(greeting("Chainman"))
