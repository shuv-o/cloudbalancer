"""
Discord: alerts that reach a channel people already read.

What is pinned here is what goes wrong with Discord specifically: a pasted
link that is not a webhook, a message over Discord's limits (rejected whole),
an @everyone inside an alert body paging a whole server, a rate limit during
the alert storm it was meant to report, and the webhook token -- a credential
-- ending up on the alerts page inside an error message.
"""
import json
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from apps.alerts.models import Alert, NotificationAttempt, NotificationChannel, Severity
from apps.alerts.services import AlertError, _discord_payload, alert_raise, channel_test
from apps.alerts.tests import AlertTestCase
from apps.security.testing import make_operator

TOKEN = "Ab3_xYz-" + "q" * 60
WEBHOOK = f"https://discord.com/api/webhooks/123456789012345678/{TOKEN}"


def discord_channel(**config) -> NotificationChannel:
    return NotificationChannel.objects.create(
        name="discord-ops",
        kind=NotificationChannel.Kind.DISCORD,
        config={"url": WEBHOOK, **config},
        min_severity=Severity.WARNING,
    )


def an_alert(**overrides) -> Alert:
    fields = {
        "fingerprint": "f", "source": Alert.Source.CONTROL_PLANE,
        "severity": Severity.CRITICAL, "title": "Backend down",
        "body": "Every instance is failing.", "labels": {"backend": "ugc"},
        "started_at": timezone.now(),
    }
    fields.update(overrides)
    return Alert(**fields)


def response(status=200, body=None):
    return mock.Mock(status_code=status, json=lambda: body or {})


class ValidationTests(AlertTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(make_operator(username="boss", superuser=True))

    def _add(self, **config):
        return self.client.post(
            "/api/v1/alerts/channels/",
            {"name": "d", "kind": "discord", "config": config, "min_severity": "warning"},
            content_type="application/json",
        )

    def test_a_real_webhook_is_accepted(self):
        for url in (
            WEBHOOK,
            f"https://discordapp.com/api/webhooks/123456789012345678/{TOKEN}",
            f"https://ptb.discord.com/api/v10/webhooks/123456789012345678/{TOKEN}",
        ):
            with self.subTest(url=url):
                NotificationChannel.objects.all().delete()
                self.assertEqual(self._add(url=url).status_code, 201)

    def test_anything_else_is_refused_on_save(self):
        for url in (
            "https://discord.com/channels/1/2",           # a channel link, the usual mistake
            f"http://discord.com/api/webhooks/123456789012345678/{TOKEN}",
            f"https://discord.com.evil.example/api/webhooks/123456789012345678/{TOKEN}",
            "https://hooks.slack.com/services/x/y/z",
            "",
        ):
            with self.subTest(url=url):
                self.assertEqual(self._add(url=url).status_code, 400)

    def test_mentions(self):
        for mention, ok in (("@here", True), ("@everyone", True),
                            ("<@&123456789012345678>", True), ("<@123456789012345678>", True),
                            ("here", False), ("@channel", False), ("<@&abc>", False)):
            with self.subTest(mention=mention):
                NotificationChannel.objects.all().delete()
                self.assertEqual(self._add(url=WEBHOOK, mention=mention).status_code,
                                 201 if ok else 400)

    def test_only_known_keys_are_stored(self):
        self._add(url=WEBHOOK, mention="@here", stray="x")
        self.assertEqual(NotificationChannel.objects.get().config,
                         {"url": WEBHOOK, "mention": "@here"})

    def test_the_panel_never_shows_the_token(self):
        self._add(url=WEBHOOK)
        listed = json.dumps(self.client.get("/api/v1/alerts/channels/").json(),
                            ensure_ascii=False)
        self.assertNotIn(TOKEN, listed)
        self.assertIn("discord.com/…", listed)


class PayloadTests(AlertTestCase):
    def test_colour_follows_severity_and_resolution(self):
        channel = discord_channel()
        critical = _discord_payload(channel, an_alert(), resolution=False)
        resolved = _discord_payload(
            channel, an_alert(resolved_at=timezone.now()), resolution=True
        )
        self.assertEqual(critical["embeds"][0]["color"], 0xD14B4B)
        self.assertEqual(resolved["embeds"][0]["color"], 0x4AA564)
        self.assertTrue(resolved["embeds"][0]["title"].startswith("[RESOLVED]"))

    def test_nothing_pings_by_default(self):
        """An "@everyone" quoted in an alert body must not page the server."""
        payload = _discord_payload(
            discord_channel(), an_alert(body="log line: @everyone hi"), resolution=False
        )
        self.assertEqual(payload["content"], "")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_a_mention_pings_only_when_a_critical_alert_starts(self):
        channel = discord_channel(mention="<@&123456789012345678>")
        firing = _discord_payload(channel, an_alert(), resolution=False)
        self.assertEqual(firing["content"], "<@&123456789012345678>")
        self.assertEqual(firing["allowed_mentions"],
                         {"parse": [], "roles": ["123456789012345678"]})

        for alert, resolution in ((an_alert(severity=Severity.WARNING), False),
                                  (an_alert(resolved_at=timezone.now()), True)):
            with self.subTest(severity=alert.severity, resolution=resolution):
                quiet = _discord_payload(channel, alert, resolution=resolution)
                self.assertEqual(quiet["content"], "")
                self.assertEqual(quiet["allowed_mentions"], {"parse": []})

    def test_here_is_allowed_to_ping_when_configured(self):
        payload = _discord_payload(discord_channel(mention="@here"), an_alert(),
                                   resolution=False)
        self.assertEqual(payload["allowed_mentions"], {"parse": ["everyone"]})

    def test_everything_fits_discords_limits(self):
        """Discord rejects the whole message if any one part is too long."""
        huge = an_alert(
            title="T" * 1000, body="B" * 10000, runbook="R" * 5000,
            labels={f"{'k' * 300}{i}": "v" * 5000 for i in range(10)},
        )
        embed = _discord_payload(discord_channel(), huge, resolution=False)["embeds"][0]
        self.assertLessEqual(len(embed["title"]), 256)
        self.assertLessEqual(len(embed["description"]), 4096)
        self.assertLessEqual(len(embed["fields"]), 25)
        for field in embed["fields"]:
            self.assertLessEqual(len(field["name"]), 256)
            self.assertLessEqual(len(field["value"]), 1024)
        total = (len(embed["title"]) + len(embed["description"]) + len("CloudBalancer")
                 + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"]))
        self.assertLessEqual(total, 6000)

    @override_settings(ALERT_PANEL_URL="https://cloudbalancer.example.org")
    def test_links_to_the_panel(self):
        embed = _discord_payload(discord_channel(), an_alert(), resolution=False)["embeds"][0]
        self.assertEqual(embed["url"], "https://cloudbalancer.example.org/alerts")


class DeliveryTests(AlertTestCase):
    def test_a_raised_alert_reaches_discord(self):
        discord_channel()
        self.post.return_value = response(200)

        alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)

        url = self.post.call_args.args[0]
        self.assertEqual(url, WEBHOOK)
        self.assertEqual(self.post.call_args.kwargs["params"], {"wait": "true"})
        self.assertTrue(NotificationAttempt.objects.get().succeeded)

    def test_a_deleted_webhook_says_so_without_the_token(self):
        channel = discord_channel()
        self.post.return_value = response(404, {"message": "Unknown Webhook"})

        alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)

        channel.refresh_from_db()
        self.assertIn("deleted or regenerated", channel.last_error)
        self.assertNotIn(TOKEN, channel.last_error)
        self.assertNotIn(TOKEN, NotificationAttempt.objects.get().detail)

    def test_a_network_failure_does_not_leak_the_token(self):
        import httpx

        self.post.side_effect = httpx.ConnectError(f"failed to connect to {WEBHOOK}")
        with self.assertRaises(AlertError) as caught:
            channel_test(channel=discord_channel())
        self.assertNotIn(TOKEN, str(caught.exception))

    @mock.patch("apps.alerts.services.time.sleep")
    def test_a_short_rate_limit_is_waited_out_once(self, sleep):
        self.post.side_effect = [response(429, {"retry_after": 1.5}), response(200)]
        self.assertIn("posted", channel_test(channel=discord_channel()))
        sleep.assert_called_once_with(1.5)
        self.assertEqual(self.post.call_count, 2)

    @mock.patch("apps.alerts.services.time.sleep")
    def test_a_long_rate_limit_fails_rather_than_stall_the_worker(self, sleep):
        self.post.return_value = response(429, {"retry_after": 60})
        with self.assertRaisesMessage(AlertError, "rate limiting"):
            channel_test(channel=discord_channel())
        sleep.assert_not_called()

    def test_the_test_button_works_for_discord(self):
        self.post.return_value = response(204)
        self.assertIn("posted to Discord", channel_test(channel=discord_channel()))
