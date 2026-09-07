"""A deterministic function with an explicit input contract."""


def greeting(name: str) -> str:
    name = name.strip()
    if not name or "\n" in name or "\r" in name:
        raise ValueError("name must be a nonempty single line")
    return f"Hello, {name}!"


if __name__ == "__main__":
    import sys

    print(greeting(sys.argv[1] if len(sys.argv) > 1 else "world"))
