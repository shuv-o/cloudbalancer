"""
Common service utilities following the HackSoft pattern.

Services are plain Python functions that:
- Handle write operations / state changes
- Are interface-agnostic (callable from views, management commands, tasks)
- Take keyword-only arguments for clarity
- Return the created/updated object or raise an exception
"""
from typing import Any

from django.db import models


def model_update(
    *,
    instance: models.Model,
    fields: list[str],
    data: dict[str, Any],
) -> tuple[models.Model, bool]:
    """
    Generic model update following HackSoft pattern.

    Only updates fields that are present in `data` AND listed in `fields`.
    Returns (instance, has_updated).
    """
    has_updated = False
    update_fields = []

    for field in fields:
        if field not in data:
            continue
        current_value = getattr(instance, field)
        new_value = data[field]
        if current_value != new_value:
            has_updated = True
            update_fields.append(field)
            setattr(instance, field, new_value)

    if has_updated:
        instance.full_clean()
        instance.save(update_fields=update_fields)

    return instance, has_updated
