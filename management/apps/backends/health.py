"""
What counts as healthy.

The default — any 2xx or 3xx to a request for /health — assumes the backend is
an application that was built with a health endpoint. Plenty are not. A reverse
proxy answers 404 for a path no route matches; an application that never
defined /health answers 404 from its own router; a service that speaks a
protocol other than HTTP answers nothing at all, and is working perfectly.

So the question the probe asks is configurable, and the answer that counts is
too. Two decisions, deliberately separate:

  * the method — open a connection, or make a request
  * the statuses that mean healthy, when a request is made
"""
from django.core.exceptions import ValidationError

_MAX_STATUS = 599
_MIN_STATUS = 100


def parse_expected(spec: str) -> tuple[tuple[int, int], ...]:
    """
    Turn "200-399, 404" into the ranges it names.

    Raises ValidationError with a usable message rather than returning
    something empty, because a spec nobody can parse would silently mark every
    instance unhealthy.
    """
    ranges: list[tuple[int, int]] = []

    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            low_text, _, high_text = part.partition("-")
            low, high = _as_status(low_text, part), _as_status(high_text, part)
            if low > high:
                raise ValidationError(
                    f"'{part}' runs backwards. Write the lower code first, as "
                    f"'{high}-{low}'."
                )
            ranges.append((low, high))
        else:
            code = _as_status(part, part)
            ranges.append((code, code))

    if not ranges:
        raise ValidationError(
            "Name at least one status code that means healthy, for example "
            "'200-399' or '200,404'."
        )

    return tuple(ranges)


def _as_status(text: str, part: str) -> int:
    text = text.strip()
    if not text.isdigit():
        raise ValidationError(
            f"'{part}' is not a status code or a range of them. Use digits, "
            "as in '200', '200-399' or '200,301,404'."
        )

    code = int(text)
    if not _MIN_STATUS <= code <= _MAX_STATUS:
        raise ValidationError(
            f"{code} is not an HTTP status code. They run from {_MIN_STATUS} "
            f"to {_MAX_STATUS}."
        )
    return code


def validate_expected(spec: str) -> None:
    """Form-level check; parse_expected carries the messages."""
    parse_expected(spec)


def matches(spec: str, status_code: int) -> bool:
    """Whether this response counts as healthy under this spec."""
    try:
        ranges = parse_expected(spec)
    except ValidationError:
        # A spec that cannot be parsed should not quietly condemn every
        # instance. Fall back to the default rather than failing closed on
        # something the operator cannot see.
        ranges = ((200, 399),)

    return any(low <= status_code <= high for low, high in ranges)
