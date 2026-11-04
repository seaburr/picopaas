"""Human numbers for refusal messages: every limit is stated together with the file's own value."""


def mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def kb(n: int) -> str:
    return f"{n / 1024:.0f} KB"


def megapixels(n: int) -> str:
    return f"{n / 1_000_000:.1f} megapixels"
