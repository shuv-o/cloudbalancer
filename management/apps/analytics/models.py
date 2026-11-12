"""
What the access logs are rolled up into.

Raw lines are never stored. A busy gateway writes millions a day and keeping
them would turn this into a log store, which is a different product and a
worse one than the several that already exist. Everything here is a daily
aggregate, and each table keeps what it needs for as long as it is useful.

Retention differs per table, and deliberately:

    ClientDay   holds complete IP addresses, which are personal data in most
                places this gateway will run. Thirty days: long enough to
                investigate abuse, short enough that it is not a standing
                record of who visited what.
    GeoDay      the same traffic with the address removed. Country and
                network survive for as long as the metrics do, because
                "where does our traffic come from" is a question about
                years.
    PathDay     ninety days, and bounded per domain, because paths are
                unbounded in a way countries are not.
"""
from django.db import models


class LogCursor(models.Model):
    """
    How far each log file has been read.

    Without this every pass would re-read from the beginning and add every
    count again -- which does not look like a bug, it looks like the traffic
    doubled.
    """

    file_name = models.CharField(max_length=255, unique=True)
    # Three ways to notice the file is not the one the offset refers to.
    # The inode is the obvious one and is not sufficient on its own: a
    # filesystem may hand the same inode to the next file created, and some
    # do. The fingerprint of the first bytes catches that case.
    inode = models.BigIntegerField(default=0)
    head_digest = models.CharField(max_length=32, blank=True, default="")
    offset = models.BigIntegerField(default=0)

    lines_read = models.BigIntegerField(default=0)
    lines_unparsed = models.BigIntegerField(default=0)
    last_read_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["file_name"]

    def __str__(self) -> str:
        return f"{self.file_name} @ {self.offset}"


class ClientDay(models.Model):
    """One client, one domain, one day. Holds a complete address."""

    date = models.DateField(db_index=True)
    domain = models.CharField(max_length=253, db_index=True)
    ip = models.GenericIPAddressField()

    # Resolved once, when the row is first written, so the lookup cost is
    # paid per client per day rather than per request.
    country = models.CharField(max_length=2, blank=True, default="")
    country_name = models.CharField(max_length=100, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    asn = models.PositiveIntegerField(null=True, blank=True)
    asn_name = models.CharField(max_length=160, blank=True, default="")

    requests = models.BigIntegerField(default=0)
    bytes_sent = models.BigIntegerField(default=0)
    errors = models.BigIntegerField(default=0)
    server_errors = models.BigIntegerField(default=0)
    total_ms = models.FloatField(default=0)
    last_seen = models.DateTimeField()

    class Meta:
        unique_together = [("date", "domain", "ip")]
        ordering = ["-date", "-requests"]
        indexes = [
            models.Index(fields=["-date", "-requests"]),
            models.Index(fields=["domain", "-date"]),
            models.Index(fields=["ip"]),
        ]

    def __str__(self) -> str:
        return f"{self.ip} -> {self.domain} on {self.date}"

    @property
    def average_ms(self) -> float:
        return self.total_ms / self.requests if self.requests else 0.0


class GeoDay(models.Model):
    """
    The same traffic with the address dropped.

    Written alongside ClientDay rather than derived from it later, so that
    the long view survives the thirty-day deletion of the addresses it came
    from.
    """

    date = models.DateField(db_index=True)
    domain = models.CharField(max_length=253, db_index=True)
    country = models.CharField(max_length=2, blank=True, default="")
    country_name = models.CharField(max_length=100, blank=True, default="")
    asn = models.PositiveIntegerField(null=True, blank=True)
    asn_name = models.CharField(max_length=160, blank=True, default="")

    clients = models.PositiveIntegerField(default=0)
    requests = models.BigIntegerField(default=0)
    bytes_sent = models.BigIntegerField(default=0)
    errors = models.BigIntegerField(default=0)

    class Meta:
        unique_together = [("date", "domain", "country", "asn")]
        ordering = ["-date", "-requests"]
        indexes = [models.Index(fields=["-date", "-requests"])]

    def __str__(self) -> str:
        return f"{self.country or '??'} AS{self.asn or 0} -> {self.domain}"


class PathDay(models.Model):
    """One path, one domain, one day."""

    date = models.DateField(db_index=True)
    domain = models.CharField(max_length=253, db_index=True)
    # Query strings are stripped before this is written: they multiply a
    # single endpoint into thousands of rows and answer nothing the path
    # does not.
    path = models.CharField(max_length=512)

    requests = models.BigIntegerField(default=0)
    bytes_sent = models.BigIntegerField(default=0)
    errors = models.BigIntegerField(default=0)
    server_errors = models.BigIntegerField(default=0)
    cache_hits = models.BigIntegerField(default=0)
    total_ms = models.FloatField(default=0)
    max_ms = models.FloatField(default=0)

    class Meta:
        unique_together = [("date", "domain", "path")]
        ordering = ["-date", "-requests"]
        indexes = [
            models.Index(fields=["-date", "-requests"]),
            models.Index(fields=["domain", "-date"]),
        ]

    def __str__(self) -> str:
        return f"{self.domain}{self.path} on {self.date}"

    @property
    def average_ms(self) -> float:
        return self.total_ms / self.requests if self.requests else 0.0

    @property
    def cache_hit_ratio(self) -> float | None:
        return self.cache_hits / self.requests * 100 if self.requests else None


class AgentDay(models.Model):
    """What kind of client, by day. Not which one -- that is ClientDay."""

    class Kind(models.TextChoices):
        BROWSER = "browser", "Browser"
        BOT = "bot", "Crawler or bot"
        TOOL = "tool", "Script or tool"
        UNKNOWN = "unknown", "Unidentified"

    date = models.DateField(db_index=True)
    domain = models.CharField(max_length=253, db_index=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    # The most common agent string seen for this kind, kept because "bot"
    # alone never answers the next question.
    example = models.CharField(max_length=300, blank=True, default="")

    requests = models.BigIntegerField(default=0)
    bytes_sent = models.BigIntegerField(default=0)

    class Meta:
        unique_together = [("date", "domain", "kind")]
        ordering = ["-date", "-requests"]

    def __str__(self) -> str:
        return f"{self.kind} -> {self.domain} on {self.date}"


class HourBucket(models.Model):
    """
    Requests by hour, which is what a weekday-by-hour heatmap is drawn from.

    Prometheus could answer this too, and more awkwardly: a year of it is a
    subquery over 8,760 points per domain, computed on every page load. A row
    per domain per hour is 8,760 rows a year and reads instantly.
    """

    date = models.DateField(db_index=True)
    hour = models.PositiveSmallIntegerField()
    domain = models.CharField(max_length=253, db_index=True)

    requests = models.BigIntegerField(default=0)
    bytes_sent = models.BigIntegerField(default=0)
    errors = models.BigIntegerField(default=0)
    total_ms = models.FloatField(default=0)

    class Meta:
        unique_together = [("date", "hour", "domain")]
        ordering = ["-date", "-hour"]
        indexes = [models.Index(fields=["-date", "-hour"])]

    def __str__(self) -> str:
        return f"{self.domain} {self.date} {self.hour:02d}:00"

    @property
    def average_ms(self) -> float:
        return self.total_ms / self.requests if self.requests else 0.0
