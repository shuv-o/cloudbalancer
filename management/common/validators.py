"""
Validators for values that become Nginx configuration.

Some operator input is interpolated where no quoting is possible -- a
`server_name`, a `location` directive, an upstream symbol name. Those cannot be
escaped on the way out, so they are constrained on the way in.

The rule of thumb: refuse anything that could end the current directive or
start a new one. Every message says what to do about it, because the operator
reading it is mid-task and the gateway is usually live.
"""
import re

from django.core.exceptions import ValidationError

# Ends a directive, opens a block, or starts a comment. None of these can
# appear in a value that is written to the config without quotes.
_STRUCTURAL = re.compile(r"[;{}#\s]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# A hostname label, optionally preceded by a `*.` wildcard, which Nginx accepts
# in server_name.
_HOSTNAME = re.compile(
    r"^(?:\*\.)?"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)

# RFC 7230 token: what may appear in an HTTP header field name.
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# What may appear in a name the config turns into a symbol (upstream blocks,
# zone names, map variables).
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def validate_hostname(value: str) -> None:
    """A name that will be written into server_name."""
    if not value:
        raise ValidationError("A hostname is required.")
    if value != value.strip():
        raise ValidationError("Remove the leading or trailing whitespace from this hostname.")
    if value != value.lower():
        raise ValidationError(
            f"Use lowercase: '{value.lower()}'. Nginx matches server_name "
            "case-insensitively, but two spellings of one name would be stored "
            "as two different domains."
        )
    if "://" in value or "/" in value:
        raise ValidationError(
            "Enter the hostname only, without a scheme or path "
            "(for example 'api.example.com', not 'https://api.example.com/')."
        )
    if len(value) > 253:
        raise ValidationError("A hostname may be at most 253 characters.")
    if not _HOSTNAME.match(value):
        raise ValidationError(
            f"'{value}' is not a valid hostname. Use letters, digits, hyphens "
            "and dots, with an optional leading '*.' wildcard."
        )


def validate_match_value(value: str, *, is_regex: bool = False) -> None:
    """A path prefix, exact path, or regex that becomes a location directive."""
    if not value:
        raise ValidationError("A match value is required.")
    if _CONTROL.search(value):
        raise ValidationError("A match value may not contain control characters.")
    if is_regex:
        # Braces are legitimate in a regex ({3}, {1,2}), so only the characters
        # that would end the directive are refused. A regex that Nginx itself
        # rejects is caught by `nginx -t` before the config is ever loaded.
        if re.search(r"[;#\s]", value):
            raise ValidationError(
                "A regex match may not contain spaces, ';' or '#'. "
                "Use '\\s' for whitespace."
            )
        return
    if not value.startswith("/"):
        raise ValidationError(
            f"A path must start with '/' -- did you mean '/{value}'?"
        )
    if _STRUCTURAL.search(value):
        raise ValidationError(
            "A path may not contain spaces, ';', '#', '{' or '}'. "
            "Percent-encode them, or use a regex match instead."
        )


def validate_header_name(value: str) -> None:
    """A header name that becomes part of an Nginx variable or directive."""
    if not value:
        raise ValidationError("A header name is required.")
    if not _HEADER_NAME.match(value):
        raise ValidationError(
            f"'{value}' is not a valid HTTP header name. Use letters, digits "
            "and hyphens, for example 'X-Route'."
        )


def validate_identifier(value: str, *, label: str = "name") -> None:
    """A name the config turns into an Nginx symbol."""
    if not value:
        raise ValidationError(f"A {label} is required.")
    if not _IDENTIFIER.match(value):
        raise ValidationError(
            f"A {label} may contain only letters, digits, dots, hyphens and "
            f"underscores, and must start with a letter or digit. "
            f"'{value}' cannot be written into the gateway configuration."
        )


def validate_header_value(value: str, *, label: str = "header value") -> None:
    """
    A value that is written as a quoted Nginx string.

    Quoting handles punctuation, so this only refuses what quoting cannot
    carry. Catching it here matters: the escaper raises at render time, and a
    value that reaches the renderer would fail the deploy of every domain, not
    just the route that owns it.
    """
    if _CONTROL.search(value or ""):
        raise ValidationError(
            f"A {label} may not contain control characters such as newlines "
            "or tabs."
        )
