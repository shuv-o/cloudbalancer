"""Scheduled ingestion and retention."""
import logging

from celery import shared_task

from apps.analytics.services import ingest, prune

logger = logging.getLogger(__name__)


@shared_task(name="analytics.ingest")
def ingest_access_logs() -> dict:
    result = ingest()
    if result.get("lines"):
        logger.info(
            "Ingested %d lines from %d logs (%d unparsed)",
            result["lines"], result["files"], result.get("unparsed", 0),
        )
    return result


@shared_task(name="analytics.prune")
def prune_analytics() -> dict:
    """
    Retention, including the deletion of every address older than its window.

    Runs on a schedule rather than at ingest so that it happens whether or
    not traffic does: a gateway nobody is using must still forget.
    """
    result = prune()
    if result["clients"]:
        logger.info("Deleted %d client rows past their retention", result["clients"])
    return result


@shared_task(name="analytics.update_geoip")
def update_geoip() -> list:
    """
    Refresh the geolocation databases.

    Monthly, because that is how often both providers publish. A database
    that is a year old still answers -- incorrectly, about every network that
    has changed hands since -- which is the kind of wrong nobody notices.
    """
    from apps.analytics.geoip_download import update_all

    results = update_all()
    for result in results:
        if "error" in result:
            logger.error("GeoIP %s: %s", result["kind"], result["error"])
        else:
            logger.info(
                "GeoIP %s updated: %d bytes at %s",
                result["kind"], result["bytes"], result["path"],
            )
    return results
