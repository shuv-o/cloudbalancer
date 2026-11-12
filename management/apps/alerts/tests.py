"""
Alerts, and whether anybody actually hears them.

The failure this is written against is not an alert that does not fire. It is
an alerting system that looks configured, records everything, and delivers
nothing -- discovered during the incident it was installed for.
"""
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.alerts.checks import check_backends, check_certificates, check_deploys
from apps.alerts.models import (
    Alert,
    NotificationAttempt,
    NotificationChannel,
    Severity,
)
from apps.alerts.services import (
    alert_acknowledge,
    alert_raise,
    alert_resolve,
    alert_summary,
    channel_test,
    deliver,
    fingerprint,
)
from apps.backends.services import backend_create, instance_add
from apps.security.roles import Role
from apps.security.testing import make_operator


class AlertTestCase(TestCase):
    """
    No alert test may touch the network.

    Celery runs eagerly in the suite, so alert_raise() delivers synchronously
    -- the first version of these tests was making real HTTPS requests to the
    example domains in their fixtures. Patching here means every path through
    delivery is exercised against a stub, and a test that wants a failure
    says so by setting a side effect.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch("apps.alerts.services.httpx.post")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)
        self.post.return_value = mock.Mock(
            status_code=200, raise_for_status=lambda: None
        )

    def raise_quietly(self, **kwargs) -> Alert:
        """Raise an alert and forget the delivery it triggered on the way."""
        alert = alert_raise(**kwargs)
        self.post.reset_mock()
        return alert


def a_channel(**overrides) -> NotificationChannel:
    defaults = {
        "name": "ops-webhook",
        "kind": NotificationChannel.Kind.WEBHOOK,
        "config": {"url": "https://hooks.example.org/abc"},
        "min_severity": Severity.WARNING,
    }
    defaults.update(overrides)
    return NotificationChannel.objects.create(**defaults)


class DeduplicationTests(AlertTestCase):
    def test_the_same_problem_twice_is_one_alert(self):
        """
        A backend flapping every thirty seconds is one problem.

        Three hundred rows for it is a history nobody reads, and it buries
        the one thing that happened once.
        """
        for _ in range(5):
            alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)

        self.assertEqual(Alert.objects.count(), 1)
        self.assertEqual(Alert.objects.get().occurrences, 5)

    def test_a_recurrence_after_resolution_starts_a_new_episode(self):
        """
        'It came back' is a different fact from 'it never went away'.

        Keeping the original started_at would show a four-hour outage for a
        problem that lasted two minutes, recurred, and lasted two more.
        """
        alert_raise(key="k", title="Backend down")
        alert_resolve(key="k")
        first = Alert.objects.get()
        original_start = first.started_at

        alert_raise(key="k", title="Backend down")

        alert = Alert.objects.get()
        self.assertEqual(alert.status, Alert.Status.FIRING)
        self.assertEqual(alert.occurrences, 1)
        self.assertGreater(alert.started_at, original_start)

    def test_a_recurrence_clears_the_acknowledgement(self):
        """
        Otherwise acknowledging once silences the problem forever.

        Somebody said they were dealing with that episode. A new one has not
        been seen by anyone.
        """
        user = make_operator(username="op", superuser=False, role=Role.OPERATOR)
        alert = alert_raise(key="k", title="Backend down")
        alert_acknowledge(alert=alert, user=user)
        alert_resolve(key="k")

        alert_raise(key="k", title="Backend down")

        self.assertFalse(Alert.objects.get().is_acknowledged)

    def test_fingerprints_differ_per_subject(self):
        self.assertNotEqual(fingerprint("backend", 1), fingerprint("backend", 2))
        self.assertEqual(fingerprint("backend", 1), fingerprint("backend", 1))


class SeverityRoutingTests(AlertTestCase):
    def test_a_channel_only_gets_what_it_asked_for(self):
        channel = a_channel(min_severity=Severity.CRITICAL)

        self.assertFalse(channel.wants(Severity.INFO))
        self.assertFalse(channel.wants(Severity.WARNING))
        self.assertTrue(channel.wants(Severity.CRITICAL))

    def test_a_disabled_channel_wants_nothing(self):
        channel = a_channel(is_enabled=False, min_severity=Severity.INFO)
        self.assertFalse(channel.wants(Severity.CRITICAL))

    def test_an_info_alert_does_not_reach_a_warning_channel(self):
        a_channel(min_severity=Severity.WARNING)

        alert_raise(key="k", title="Something", severity=Severity.INFO)

        self.post.assert_not_called()
        self.assertEqual(NotificationAttempt.objects.count(), 0)


class DeliveryTests(AlertTestCase):
    def test_a_webhook_alert_is_posted(self):
        a_channel()
        alert = self.raise_quietly(
            key="k", title="Backend down", severity=Severity.CRITICAL
        )
        alert.last_notified_at = None
        alert.save(update_fields=["last_notified_at"])

        deliver(alert)

        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[0], "https://hooks.example.org/abc")

    def test_a_webhook_can_carry_a_shared_secret(self):
        """An endpoint that accepts these should not accept them from anyone."""
        a_channel(config={"url": "https://hooks.example.org/abc", "token": "s3cret"})
        alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)

        self.assertEqual(
            self.post.call_args.kwargs["headers"]["Authorization"], "Bearer s3cret"
        )

    @override_settings(
        EMAIL_HOST="smtp.example.org",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_an_email_alert_is_sent(self):
        a_channel(
            name="ops-mail",
            kind=NotificationChannel.Kind.EMAIL,
            config={"to": ["ops@example.org"]},
        )
        alert_raise(key="k", title="Certificate expiring", severity=Severity.CRITICAL)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Certificate expiring", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, ["ops@example.org"])

    def test_an_email_channel_with_no_mail_server_fails_loudly(self):
        """
        Django's default backend prints to the console.

        In production that is a channel which reports success and delivers to
        nobody, which is the exact failure this app exists to prevent.
        """
        channel = a_channel(
            name="ops-mail",
            kind=NotificationChannel.Kind.EMAIL,
            config={"to": ["ops@example.org"]},
        )
        alert_raise(key="k", title="Something", severity=Severity.CRITICAL)

        channel.refresh_from_db()
        self.assertFalse(channel.is_healthy)
        self.assertIn("mail server", channel.last_error)

    def test_a_failing_channel_is_recorded_not_raised(self):
        """
        Whatever raised the alert was reporting a problem.

        Turning that into a second exception loses the first one -- the
        renewal task reports that Slack is unreachable, and not that the
        certificate expires on Friday.
        """
        channel = a_channel()
        self.post.side_effect = OSError("no route")

        alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)

        attempt = NotificationAttempt.objects.get()
        self.assertFalse(attempt.succeeded)

        channel.refresh_from_db()
        self.assertEqual(channel.consecutive_failures, 1)
        self.assertIn("no route", channel.last_error)

    def test_a_channel_recovers_when_delivery_works_again(self):
        channel = a_channel()
        channel.consecutive_failures = 7
        channel.last_error = "old failure"
        channel.save()

        alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)

        channel.refresh_from_db()
        self.assertTrue(channel.is_healthy)
        self.assertEqual(channel.last_error, "")
        self.assertIsNotNone(channel.last_success_at)

    def test_one_broken_channel_does_not_stop_the_others(self):
        a_channel(name="broken", config={"url": "https://broken.example.org/x"})
        a_channel(name="working", config={"url": "https://working.example.org/x"})

        def post(url, *args, **kwargs):
            if "broken" in url:
                raise OSError("refused")
            return mock.Mock(status_code=200, raise_for_status=lambda: None)

        self.post.side_effect = post
        alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)

        attempts = NotificationAttempt.objects.all()
        self.assertEqual(sum(1 for a in attempts if a.succeeded), 1)
        self.assertEqual(sum(1 for a in attempts if not a.succeeded), 1)


class RepetitionTests(AlertTestCase):
    def setUp(self):
        super().setUp()
        a_channel(min_severity=Severity.INFO)

    def send(self, alert, **kwargs):
        self.post.reset_mock()
        return deliver(alert, **kwargs), self.post

    @override_settings(ALERT_REPEAT_MINUTES=60)
    def test_a_still_firing_alert_is_not_resent_immediately(self):
        """
        A problem lasting a week must not send a message every five minutes
        for a week. That is how a channel gets muted.
        """
        alert = alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)
        alert.refresh_from_db()
        self.assertIsNotNone(alert.last_notified_at)

        again = alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)
        attempts, post = self.send(again)

        self.assertEqual(attempts, [])
        post.assert_not_called()

    @override_settings(ALERT_REPEAT_MINUTES=60)
    def test_it_is_resent_once_the_quiet_period_passes(self):
        alert = alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)

        alert.refresh_from_db()
        alert.last_notified_at = timezone.now() - timezone.timedelta(hours=2)
        alert.save(update_fields=["last_notified_at"])

        attempts, _ = self.send(alert)
        self.assertEqual(len(attempts), 1)

    def test_acknowledging_stops_the_repetition(self):
        """
        Not the same as resolving. The problem is still there and still
        shown; what stops is the repeating, because its purpose was to find
        somebody and somebody has been found.
        """
        user = make_operator(username="op", superuser=False, role=Role.OPERATOR)
        alert = alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)
        alert_acknowledge(alert=alert, user=user)

        attempts, post = self.send(alert)

        self.assertEqual(attempts, [])
        post.assert_not_called()
        self.assertEqual(Alert.objects.get().status, Alert.Status.FIRING)

    def test_a_resolution_is_sent_even_inside_the_quiet_period(self):
        alert = alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)

        alert.refresh_from_db()
        attempts, _ = self.send(alert, resolution=True)

        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0].is_resolution)

    def test_resolving_something_nobody_was_told_about_sends_nothing(self):
        """
        A message saying an alert cleared, to someone who never heard it
        fire, teaches people to ignore the channel.
        """
        alert_raise(key="k", title="Brief blip", severity=Severity.CRITICAL)
        Alert.objects.update(last_notified_at=None)
        self.post.reset_mock()

        alert_resolve(key="k")

        self.post.assert_not_called()


class ControlPlaneCheckTests(AlertTestCase):
    def test_a_backend_with_nothing_healthy_is_critical(self):
        backend = backend_create(name="api")
        instance = instance_add(backend=backend, address="10.0.0.1", port=8080)
        instance.is_healthy = False
        instance.save()

        check_backends()

        alert = Alert.objects.get()
        self.assertEqual(alert.severity, Severity.CRITICAL)
        self.assertIn("api", alert.title)

    def test_a_partly_healthy_backend_is_a_warning(self):
        backend = backend_create(name="api")
        healthy = instance_add(backend=backend, address="10.0.0.1", port=8080)
        sick = instance_add(backend=backend, address="10.0.0.2", port=8080)
        healthy.is_healthy = True
        healthy.save()
        sick.is_healthy = False
        sick.save()

        check_backends()

        self.assertEqual(Alert.objects.get().severity, Severity.WARNING)

    def test_a_recovered_backend_resolves_its_alert(self):
        """
        Running the checks repeatedly is how a problem that fixes itself
        stops being mentioned.
        """
        backend = backend_create(name="api")
        instance = instance_add(backend=backend, address="10.0.0.1", port=8080)
        instance.is_healthy = False
        instance.save()
        check_backends()

        instance.is_healthy = True
        instance.save()
        check_backends()

        self.assertEqual(Alert.objects.get().status, Alert.Status.RESOLVED)

    def test_a_healthy_backend_raises_nothing(self):
        backend = backend_create(name="api")
        instance = instance_add(backend=backend, address="10.0.0.1", port=8080)
        instance.is_healthy = True
        instance.save()

        check_backends()

        self.assertEqual(Alert.objects.count(), 0)

    def test_an_expiring_certificate_is_raised(self):
        from apps.certificates.models import Certificate
        from apps.domains.services import domain_create

        domain = domain_create(name="expiring.example.com")
        Certificate.objects.create(
            domain=domain,
            not_after=timezone.now() + timezone.timedelta(days=2),
            status=Certificate.Status.ACTIVE,
        )

        check_certificates()

        alert = Alert.objects.get()
        self.assertEqual(alert.severity, Severity.CRITICAL)
        self.assertIn("expiring.example.com", alert.title)

    def test_a_certificate_with_months_left_raises_nothing(self):
        from apps.certificates.models import Certificate
        from apps.domains.services import domain_create

        domain = domain_create(name="fine.example.com")
        Certificate.objects.create(
            domain=domain,
            not_after=timezone.now() + timezone.timedelta(days=80),
            status=Certificate.Status.ACTIVE,
        )

        check_certificates()

        self.assertEqual(Alert.objects.count(), 0)

    def test_a_failed_deploy_is_raised_and_clears_on_the_next_success(self):
        from apps.routing.models import ConfigDeployLog

        ConfigDeployLog.objects.create(status=ConfigDeployLog.Status.FAILED)
        check_deploys()
        self.assertEqual(Alert.objects.filter(status=Alert.Status.FIRING).count(), 1)

        ConfigDeployLog.objects.create(status=ConfigDeployLog.Status.DEPLOYED)
        check_deploys()
        self.assertEqual(Alert.objects.filter(status=Alert.Status.FIRING).count(), 0)

    def test_one_failing_check_does_not_stop_the_others(self):
        """
        They are independent questions, and the one that crashes is the
        least likely to be the one that mattered this minute.
        """
        from apps.alerts.checks import run_all_checks

        with mock.patch(
            "apps.alerts.checks.check_certificates", side_effect=RuntimeError("boom")
        ):
            results = run_all_checks()

        self.assertIn("error", str(results["check_certificates"]))
        self.assertEqual(results["check_backends"], 0)


class AlertmanagerWebhookTests(AlertTestCase):
    URL = "/api/v1/alerts/prometheus/"

    def payload(self, **overrides):
        alert = {
            "status": "firing",
            "fingerprint": "abc123",
            "labels": {"alertname": "GatewayDown", "severity": "critical"},
            "annotations": {"summary": "The gateway is not answering"},
        }
        alert.update(overrides)
        return {"alerts": [alert]}

    @override_settings(ALERTMANAGER_WEBHOOK_SECRET="")
    def test_a_firing_alert_is_recorded(self):
        response = self.client.post(
            self.URL, self.payload(), content_type="application/json"
        )

        self.assertEqual(response.status_code, 200)
        alert = Alert.objects.get()
        self.assertEqual(alert.source, Alert.Source.PROMETHEUS)
        self.assertEqual(alert.severity, Severity.CRITICAL)
        self.assertEqual(alert.title, "The gateway is not answering")

    @override_settings(ALERTMANAGER_WEBHOOK_SECRET="")
    def test_a_resolved_alert_resolves_it(self):
        self.client.post(self.URL, self.payload(), content_type="application/json")
        self.client.post(
            self.URL,
            self.payload(status="resolved"),
            content_type="application/json",
        )

        self.assertEqual(Alert.objects.get().status, Alert.Status.RESOLVED)

    @override_settings(ALERTMANAGER_WEBHOOK_SECRET="shared")
    def test_the_secret_is_required_when_set(self):
        response = self.client.post(
            self.URL, self.payload(), content_type="application/json"
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(Alert.objects.count(), 0)

    @override_settings(ALERTMANAGER_WEBHOOK_SECRET="shared")
    def test_the_right_secret_is_accepted(self):
        response = self.client.post(
            self.URL,
            self.payload(),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer shared",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Alert.objects.count(), 1)

    @override_settings(ALERTMANAGER_WEBHOOK_SECRET="")
    def test_an_unknown_severity_becomes_a_warning(self):
        """
        Rather than a 500 or a row with a severity nothing can filter on.
        """
        response = self.client.post(
            self.URL,
            {"alerts": [{
                "status": "firing",
                "fingerprint": "x",
                "labels": {"alertname": "Odd", "severity": "page-the-ceo"},
                "annotations": {},
            }]},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Alert.objects.get().severity, Severity.WARNING)


class AlertApiTests(AlertTestCase):
    def test_an_operator_can_read_and_acknowledge(self):
        operator = make_operator(username="op", superuser=False, role=Role.OPERATOR)
        alert = alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)
        self.client.force_login(operator)

        self.assertEqual(self.client.get("/api/v1/alerts/").status_code, 200)
        self.assertEqual(
            self.client.post(f"/api/v1/alerts/{alert.pk}/acknowledge/").status_code, 200
        )
        self.assertTrue(Alert.objects.get().is_acknowledged)

    def test_a_viewer_can_read_but_not_acknowledge(self):
        viewer = make_operator(username="v", superuser=False, role=Role.VIEWER)
        alert = alert_raise(key="k", title="Backend down", severity=Severity.CRITICAL)
        self.client.force_login(viewer)

        self.assertEqual(self.client.get("/api/v1/alerts/").status_code, 200)
        self.assertEqual(
            self.client.post(f"/api/v1/alerts/{alert.pk}/acknowledge/").status_code, 403
        )

    def test_channels_are_admin_only(self):
        """A webhook URL is a credential, and so is the list of them."""
        operator = make_operator(username="op", superuser=False, role=Role.OPERATOR)
        self.client.force_login(operator)
        self.assertEqual(self.client.get("/api/v1/alerts/channels/").status_code, 403)

    def test_an_admin_can_add_a_channel(self):
        self.client.force_login(make_operator(username="boss", superuser=True))

        response = self.client.post(
            "/api/v1/alerts/channels/",
            {
                "name": "ops",
                "kind": "slack",
                "config": {"url": "https://hooks.slack.com/services/x/y/z"},
                "min_severity": "critical",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(NotificationChannel.objects.count(), 1)

    def test_a_channel_that_cannot_possibly_deliver_is_refused(self):
        """
        Saving cleanly and being undeliverable is the state worth preventing.
        """
        self.client.force_login(make_operator(username="boss", superuser=True))

        for body in (
            {"name": "a", "kind": "email", "config": {}},
            {"name": "b", "kind": "email", "config": {"to": ["not-an-address"]}},
            {"name": "c", "kind": "slack", "config": {"url": "ftp://example.org"}},
            {"name": "d", "kind": "webhook", "config": {}},
        ):
            with self.subTest(body=body):
                response = self.client.post(
                    "/api/v1/alerts/channels/", body, content_type="application/json"
                )
                self.assertEqual(response.status_code, 400, response.content)

        self.assertEqual(NotificationChannel.objects.count(), 0)

    def test_a_webhook_url_is_not_shown_in_full(self):
        """
        It is a credential: anyone holding one can post as you.
        """
        a_channel(config={"url": "https://hooks.slack.com/services/T0/B0/secrettoken"})
        self.client.force_login(make_operator(username="boss", superuser=True))

        body = self.client.get("/api/v1/alerts/channels/").json()

        self.assertNotIn("secrettoken", str(body))
        self.assertIn("hooks.slack.com", body["channels"][0]["target"])

    def test_testing_a_channel_reports_why_it_failed(self):
        a_channel()
        self.client.force_login(make_operator(username="boss", superuser=True))

        self.post.side_effect = OSError("name not resolved")
        response = self.client.post(
            f"/api/v1/alerts/channels/{NotificationChannel.objects.get().pk}/test/"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("name not resolved", response.json()["error"])

    def test_a_successful_test_clears_an_earlier_failure(self):
        channel = a_channel()
        channel.consecutive_failures = 3
        channel.save()
        self.client.force_login(make_operator(username="boss", superuser=True))

        response = self.client.post(f"/api/v1/alerts/channels/{channel.pk}/test/")

        self.assertEqual(response.status_code, 200)
        channel.refresh_from_db()
        self.assertTrue(channel.is_healthy)


class SummaryTests(AlertTestCase):
    def test_it_warns_when_no_channel_exists(self):
        """
        An alerting system recording everything and telling nobody is the
        state this app exists to make visible.
        """
        summary = alert_summary()
        self.assertTrue(any("reaches anybody" in w for w in summary["warnings"]))

    def test_it_warns_when_a_channel_has_been_failing(self):
        channel = a_channel()
        channel.consecutive_failures = 4
        channel.save()

        summary = alert_summary()
        self.assertTrue(any("Delivery is failing" in w for w in summary["warnings"]))

    def test_it_counts_what_is_firing_and_unacknowledged(self):
        a_channel()
        user = make_operator(username="op", superuser=False, role=Role.OPERATOR)
        alert_raise(key="a", title="One", severity=Severity.CRITICAL)
        second = alert_raise(key="b", title="Two", severity=Severity.WARNING)
        alert_acknowledge(alert=second, user=user)

        summary = alert_summary()
        self.assertEqual(summary["firing"], 2)
        self.assertEqual(summary["critical"], 1)
        self.assertEqual(summary["unacknowledged"], 1)
