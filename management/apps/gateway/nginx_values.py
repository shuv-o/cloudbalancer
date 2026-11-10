"""
Rendering operator-supplied values into Nginx configuration safely.

The control plane writes a configuration file from database rows an operator
controls. Any value interpolated into that file is therefore untrusted in the
way template input is untrusted: a header value that closes its own quote can
open a `location` block of its own, and `nginx -t` will pass it, because the
result is perfectly valid configuration -- just not the configuration anyone
asked for.

Two helpers cover every case in the templates:

    quoted()   a value used as an Nginx quoted string
    comment()  a value placed after a # on a line of its own

Both are registered as Jinja filters. A template that interpolates an operator
string anywhere else is a bug; `test_nginx_injection.py` enumerates the spots.
"""
import re

# Anything in this class ends the current line or confuses the parser. There is
# no escape for them inside an Nginx quoted string, so they are refused rather
# than mangled -- a rejected save is recoverable, a corrupted gateway is not.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class UnsafeNginxValue(ValueError):
    """A value that cannot be represented in the configuration safely."""


def quoted(value) -> str:
    """
    Render a value as a double-quoted Nginx string, escaped.

    Nginx honours backslash escapes inside a quoted string, so a literal quote
    or backslash can be carried through. A control character cannot, and is
    refused.

    Note that `$` is deliberately left alone: Nginx expands variables inside
    quoted strings, and forwarding something like $remote_addr in a custom
    header is a legitimate thing for an operator to want. It is within the
    authority the panel already grants; breaking out of the string is not.
    """
    text = "" if value is None else str(value)
    if _CONTROL_CHARS.search(text):
        raise UnsafeNginxValue(
            "Value contains a control character (such as a newline or tab), "
            "which cannot appear in an Nginx quoted string."
        )
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def comment(value) -> str:
    """
    Render a value safe to place after a `#`.

    A comment runs to the end of the line, so a newline in the value would put
    whatever follows it into the configuration as a directive. Control
    characters collapse to a space rather than raising: a comment is
    descriptive text, and losing a line break in a description is not worth
    refusing an otherwise valid save.
    """
    text = "" if value is None else str(value)
    return _CONTROL_CHARS.sub(" ", text).strip()
