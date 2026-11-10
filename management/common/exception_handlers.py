"""
Turning model-level validation into an answer the panel can show.

Validation lives on the models, because that is the one place every write goes
through -- the API, a management command, the Django shell. But `full_clean()`
raises Django's ValidationError, and DRF's default handler does not recognise
it: it returns None, the exception escapes, and the operator gets a 500 with no
indication of what was wrong.

That is the worst of both worlds -- the save is correctly refused, and the
reason is thrown away. This handler translates it into the 400 the serializers
already produce, so a model check and a field check look the same from outside.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.serializers import as_serializer_error
from rest_framework.views import exception_handler as drf_default_handler


def exception_handler(exc, context):
    """DRF entry point, wired up in REST_FRAMEWORK["EXCEPTION_HANDLER"]."""
    if isinstance(exc, DjangoValidationError):
        exc = DRFValidationError(detail=as_serializer_error(exc))
    return drf_default_handler(exc, context)
