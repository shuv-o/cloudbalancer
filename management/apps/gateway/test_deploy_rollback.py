"""
What a deploy does when it cannot reach Nginx.

Written after an outage it would have deepened. The gateway was restart-
looping on a stale generated file; the fix was to render a fresh one. Had
anyone run a deploy, it would have written exactly that fix, failed to reach
the looping container, treated the silence as "Nginx rejected this", and
restored the broken file from backup -- leaving the gateway down and the
deploy log saying the configuration had failed a test that never ran.

So these tests are about one distinction: Nginx saying no, and Nginx not
answering. They arrive at the same place -- success=False -- and they want
opposite handling.
"""
from unittest import mock

from django.test import TestCase

from apps.backends.services import backend_create, instance_add
from apps.domains.services import domain_create
from apps.gateway.services import config_deploy, config_render, config_write
from apps.routing.models import ConfigDeployLog
from apps.routing.services import rule_create
from common.types import NginxTestResult


def unreachable(output="Nginx did not answer 'test' within 30s"):
    return NginxTestResult(success=False, output=output, reachable=False)


def rejected(output="nginx: [emerg] unknown directive"):
    return NginxTestResult(success=False, output=output, reachable=True)


def accepted():
    return NginxTestResult(success=True, output="syntax is ok", reachable=True)


class DeployTestCase(TestCase):
    def setUp(self):
        backend = backend_create(name="api")
        instance_add(backend=backend, address="10.0.0.1", port=8080)
        domain = domain_create(name="api.example.com")
        rule_create(domain_id=domain.pk, backend_id=backend.pk)

    def live_config(self) -> str:
        """What is on disk right now, as one string."""
        from pathlib import Path

        from django.conf import settings

        return "\n".join(
            path.read_text()
            for path in sorted(Path(settings.NGINX_CONF_DIR).glob("*.conf"))
        )


class UnreachableNginxTests(DeployTestCase):
    def test_a_rejected_config_is_rolled_back(self):
        """The case rollback exists for, unchanged."""
        marker = "# the configuration that was already on disk\n"
        config_write({"20-servers.conf": marker})

        with mock.patch("apps.gateway.services.nginx_test", return_value=rejected()):
            log = config_deploy()

        self.assertEqual(log.status, ConfigDeployLog.Status.FAILED)
        self.assertIn(marker.strip(), self.live_config())

    def test_an_unreachable_nginx_does_not_roll_back(self):
        """
        The case that deepened an outage.

        A gateway that is down is usually down because of what is on disk, so
        restoring it puts the broken file back -- and the deploy that would
        have fixed things becomes the one that kept them broken.
        """
        stale = "# the stale file the gateway is failing to load\n"
        config_write({"20-servers.conf": stale})

        with mock.patch("apps.gateway.services.nginx_test", return_value=unreachable()):
            log = config_deploy()

        self.assertEqual(log.status, ConfigDeployLog.Status.FAILED)
        self.assertNotIn(stale.strip(), self.live_config())
        self.assertIn("api.example.com", self.live_config())

    def test_it_says_the_config_was_kept_and_why(self):
        """
        "Deploy failed" plus a config that silently stayed is worse than
        either outcome on its own: the next person has to work out which of
        the two files is on disk before they can do anything.
        """
        with mock.patch("apps.gateway.services.nginx_test", return_value=unreachable()):
            log = config_deploy()

        self.assertIn("kept", log.error_output)
        self.assertIn("could not be checked", log.error_output)
        self.assertIn("docker compose up -d", log.error_output)

    def test_an_unreachable_nginx_is_never_asked_to_reload(self):
        with mock.patch("apps.gateway.services.nginx_test", return_value=unreachable()):
            with mock.patch("apps.gateway.services.nginx_reload") as reload_call:
                config_deploy()

        reload_call.assert_not_called()


class ReloadFailureTests(DeployTestCase):
    def test_a_refused_reload_rolls_back(self):
        marker = "# previously deployed\n"
        config_write({"20-servers.conf": marker})

        with mock.patch("apps.gateway.services.nginx_test", return_value=accepted()):
            with mock.patch(
                "apps.gateway.services.nginx_reload", return_value=rejected("refused")
            ):
                log = config_deploy()

        self.assertEqual(log.status, ConfigDeployLog.Status.FAILED)
        self.assertIn(marker.strip(), self.live_config())

    def test_a_reload_that_could_not_be_asked_keeps_the_tested_config(self):
        """
        Nginx passed the test and then went away. It is serving neither
        configuration now, and the one that passed is the better thing to
        leave on disk for when it comes back.
        """
        stale = "# previously deployed\n"
        config_write({"20-servers.conf": stale})

        with mock.patch("apps.gateway.services.nginx_test", return_value=accepted()):
            with mock.patch(
                "apps.gateway.services.nginx_reload", return_value=unreachable()
            ):
                log = config_deploy()

        self.assertEqual(log.status, ConfigDeployLog.Status.FAILED)
        self.assertNotIn(stale.strip(), self.live_config())


class SuccessPathTests(DeployTestCase):
    def test_a_good_deploy_still_deploys(self):
        with mock.patch("apps.gateway.services.nginx_test", return_value=accepted()):
            with mock.patch("apps.gateway.services.nginx_reload", return_value=accepted()):
                log = config_deploy()

        self.assertEqual(log.status, ConfigDeployLog.Status.DEPLOYED)
        self.assertIsNotNone(log.completed_at)
        self.assertIn("api.example.com", self.live_config())


class ReachabilityReportingTests(TestCase):
    """The helpers have to report the distinction for any of the above to work."""

    def test_a_timeout_waiting_for_the_watcher_is_unreachable(self):
        import tempfile
        from pathlib import Path

        from django.test import override_settings

        control = Path(tempfile.mkdtemp(prefix="cb-unreach-"))
        (control / "requests").mkdir()
        (control / "results").mkdir()

        with override_settings(NGINX_CONTROL_DIR=str(control), NGINX_CONTROL_TIMEOUT=1):
            from apps.gateway.services import nginx_test

            result = nginx_test()

        self.assertFalse(result.success)
        self.assertFalse(result.reachable)

    def test_no_nginx_and_no_control_directory_is_unreachable(self):
        from django.test import override_settings

        with override_settings(NGINX_CONTROL_DIR="/nonexistent/cloudbalancer"):
            from apps.gateway.services import nginx_test

            result = nginx_test()

        self.assertFalse(result.success)
        self.assertFalse(result.reachable)

    def test_a_watcher_that_answers_badly_is_reachable(self):
        """A real rejection, which must still roll back."""
        import tempfile
        import threading
        import time
        from pathlib import Path

        from django.test import override_settings

        control = Path(tempfile.mkdtemp(prefix="cb-reject-"))
        requests = control / "requests"
        results = control / "results"
        requests.mkdir()
        results.mkdir()

        stop = threading.Event()

        def answer():
            while not stop.is_set():
                for request in list(requests.glob("*")):
                    if request.name.startswith("."):
                        continue
                    request.unlink(missing_ok=True)
                    partial = results / f".{request.name}.partial"
                    partial.write_text("1\nnginx: [emerg] unknown directive\n")
                    partial.rename(results / request.name)
                time.sleep(0.02)

        worker = threading.Thread(target=answer, daemon=True)
        worker.start()
        try:
            with override_settings(
                NGINX_CONTROL_DIR=str(control), NGINX_CONTROL_TIMEOUT=3
            ):
                from apps.gateway.services import nginx_test

                result = nginx_test()
        finally:
            stop.set()
            worker.join(timeout=2)

        self.assertFalse(result.success)
        self.assertTrue(result.reachable)
        self.assertIn("unknown directive", result.output)
