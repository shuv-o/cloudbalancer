"""
Reaching Nginx without the Docker socket.

The control plane used to run `docker exec cloudbalancer-nginx nginx -s
reload`, which required /var/run/docker.sock mounted into the management
container. That socket is the Docker API with no authorisation in front of
it: anything that can write to it can start a container with the host's root
filesystem mounted. The panel wanted two commands and was given a way to run
anything as root on the host.

It now writes a request into a shared directory and a watcher in the Nginx
container answers. These tests stand in for that watcher, because the
protocol between them is the part that can go wrong silently -- a reload that
reports success having done nothing is indistinguishable from one that
worked, until the next restart.
"""
import threading
import time
from pathlib import Path

from django.test import TestCase, override_settings

from apps.gateway.services import nginx_reload, nginx_test


class FakeWatcher:
    """
    The Nginx-side half, as a thread.

    Answers requests the way reload-watcher.sh does: read the command, delete
    the request, write the exit code on the first line and the output after.
    """

    def __init__(self, control: Path, *, code: int = 0, output: str = "ok", delay: float = 0.0):
        self.requests = control / "requests"
        self.results = control / "results"
        self.requests.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)
        self.code = code
        self.output = output
        self.delay = delay
        self.seen: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            for request in sorted(self.requests.glob("*")):
                if request.name.startswith("."):
                    continue
                try:
                    command = request.read_text()
                except OSError:
                    continue
                request.unlink(missing_ok=True)
                self.seen.append(command)

                if self.delay:
                    time.sleep(self.delay)

                partial = self.results / f".{request.name}.partial"
                partial.write_text(f"{self.code}\n{self.output}\n")
                partial.rename(self.results / request.name)
            time.sleep(0.02)


class ControlDirectoryTests(TestCase):
    def setUp(self):
        import tempfile

        self.control = Path(tempfile.mkdtemp(prefix="cb-control-"))
        patch = override_settings(
            NGINX_CONTROL_DIR=str(self.control), NGINX_CONTROL_TIMEOUT=1
        )
        patch.enable()
        self.addCleanup(patch.disable)

    def test_a_config_test_goes_through_the_directory(self):
        with FakeWatcher(self.control, code=0, output="syntax is ok") as watcher:
            result = nginx_test()

        self.assertTrue(result.success)
        self.assertIn("syntax is ok", result.output)
        self.assertEqual(watcher.seen, ["test"])

    def test_a_reload_asks_for_a_reload_not_a_test(self):
        with FakeWatcher(self.control) as watcher:
            nginx_reload()
        self.assertEqual(watcher.seen, ["reload"])

    def test_a_failing_command_is_reported_as_failure(self):
        with FakeWatcher(self.control, code=1, output="[emerg] unknown directive"):
            result = nginx_test()

        self.assertFalse(result.success)
        self.assertIn("unknown directive", result.output)

    def test_the_result_is_collected_so_the_directory_does_not_grow(self):
        """
        One file per deploy, left behind forever, on the volume Nginx reads
        its configuration from.
        """
        with FakeWatcher(self.control):
            nginx_test()
            nginx_test()

        self.assertEqual(list((self.control / "results").glob("*")), [])

    def test_each_request_gets_its_own_answer(self):
        """
        Several Gunicorn threads can deploy at once.

        A single shared request file would have them overwrite each other, and
        a single result file would have one thread read another's answer --
        reporting a successful reload for a command that failed.
        """
        results = []

        with FakeWatcher(self.control, output="fine"):
            threads = [
                threading.Thread(target=lambda: results.append(nginx_test()))
                for _ in range(5)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(len(results), 5)
        self.assertTrue(all(r.success for r in results))

    def test_a_watcher_that_never_answers_times_out_rather_than_hanging(self):
        """
        A stopped Nginx container must not wedge a request thread forever.

        The directory exists -- the volume is still mounted -- so nothing
        else signals that the other side is gone.
        """
        (self.control / "requests").mkdir(parents=True, exist_ok=True)
        (self.control / "results").mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        result = nginx_test()
        elapsed = time.monotonic() - started

        self.assertFalse(result.success)
        self.assertIn("did not answer", result.output)
        self.assertLess(elapsed, 5)

    def test_a_timed_out_request_is_left_for_the_watcher(self):
        """
        Deleting it and retrying would run the command twice.

        A slow reload is still a reload; withdrawing the request and falling
        back to another route means Nginx reloads twice, and the second one
        drops the keepalive pools the first just rebuilt.
        """
        (self.control / "requests").mkdir(parents=True, exist_ok=True)
        (self.control / "results").mkdir(parents=True, exist_ok=True)

        nginx_test()

        self.assertEqual(len(list((self.control / "requests").glob("*"))), 1)

    def test_a_slow_watcher_is_waited_for(self):
        with FakeWatcher(self.control, delay=0.5, output="eventually"):
            result = nginx_test()

        self.assertTrue(result.success)
        self.assertIn("eventually", result.output)

    def test_a_partial_result_is_not_read(self):
        """
        The watcher renames results into place for this reason.

        Reading a half-written file would parse an empty first line as a
        non-zero exit code and report a failure that did not happen.
        """
        (self.control / "requests").mkdir(parents=True, exist_ok=True)
        results = self.control / "results"
        results.mkdir(parents=True, exist_ok=True)

        # A partial file, named the way the watcher names one, must be
        # invisible to the reader.
        (results / ".someid.partial").write_text("")

        started = time.monotonic()
        result = nginx_test()

        self.assertFalse(result.success)
        self.assertIn("did not answer", result.output)
        self.assertLess(time.monotonic() - started, 5)


class NoControlDirectoryTests(TestCase):
    """Outside Compose there is no watcher, and Nginx is called directly."""

    @override_settings(NGINX_CONTROL_DIR="/nonexistent/cloudbalancer-control")
    def test_it_falls_back_to_running_nginx_here(self):
        result = nginx_test()

        # No Nginx in the test environment either, so this reports that
        # rather than pretending. What matters is that it did not hang
        # waiting for a watcher that cannot exist.
        self.assertFalse(result.success)
        self.assertIn("nginx", result.output.lower())
