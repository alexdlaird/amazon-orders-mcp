__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import io
import os
import threading
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from amazonorders.conf import AmazonOrdersConfig
from amazonorders.exception import AmazonOrdersAuthError, AmazonOrdersError
from amazonorders.session import AmazonSession

from amazonorders_mcp.session import SavedSession, StderrIO
from tests.unittestcase import UnitTestCase


class TestSession(UnitTestCase):
    def test_saved_session_never_submits_sign_in_forms(self):
        # GIVEN
        self.given_saved_session()

        # WHEN
        amazon_session = self.saved_session.run(lambda signed_in_session: signed_in_session)

        # THEN
        self.assertEqual([], amazon_session.auth_forms)

    def test_callback_can_use_a_signed_out_session_and_return_a_result(self):
        # WHEN
        result = self.saved_session.run_with_session(
            lambda session: (session.is_authenticated, session.config.site, session.auth_forms),
            "https://www.amazon.de")

        # THEN
        self.assertEqual((False, "amazon.de", []), result)
        self.assertEqual(0, len(self.responses.calls))
        with self.assertRaises(AmazonOrdersAuthError):
            self.saved_session.run(lambda session: None, "amazon.de")

    def test_callback_uses_the_configured_default_site(self):
        # GIVEN
        self.saved_session.update_config("domain", "amazon.de", save=False)

        # WHEN
        site = self.saved_session.run_with_session(lambda session: session.config.site)

        # THEN
        self.assertEqual("amazon.de", site)

    def test_callback_rejects_an_unknown_site_before_running(self):
        # WHEN
        with self.assertRaises(AmazonOrdersError) as cm:
            self.saved_session.run_with_session(lambda session: self.fail("Callback ran"), "example.com")

        # THEN
        self.assertIn("\"example.com\" isn't an Amazon site that can be read", str(cm.exception))

    def test_callback_saves_are_reused_by_the_next_call(self):
        # GIVEN
        url = "https://www.amazon.com/synthetic-session"
        self.responses.get(url, body="saved")

        def save(session):
            cookie_name = session.config.constants.COOKIES_SET_WHEN_AUTHENTICATED[0]
            session.session.cookies.set(cookie_name, "synthetic-session")
            session.get(url, persist_cookies=True)
            session.is_authenticated = True
            return session

        # WHEN
        saved = self.saved_session.run_with_session(save)

        # THEN
        self.assertIs(saved, self.saved_session.run(lambda session: session))
        self.assertTrue(self.saved_session.run(lambda session: session.auth_cookies_stored()))
        self.assertTrue(AmazonSession(config=self.test_config).auth_cookies_stored())

    def test_callback_saves_are_reused_after_it_raises(self):
        # GIVEN
        url = "https://www.amazon.com/synthetic-session"
        self.responses.get(url, body="saved")

        def save_then_fail(session):
            cookie_name = session.config.constants.COOKIES_SET_WHEN_AUTHENTICATED[0]
            session.session.cookies.set(cookie_name, "synthetic-session")
            session.get(url, persist_cookies=True)
            session.is_authenticated = True
            raise RuntimeError("Callback failed after saving")

        # WHEN
        with self.assertRaisesRegex(RuntimeError, "Callback failed after saving"):
            self.saved_session.run_with_session(save_then_fail)

        # THEN
        self.assertTrue(self.saved_session.run(lambda session: session.auth_cookies_stored()))
        self.assertTrue(AmazonSession(config=self.test_config).auth_cookies_stored())

    def test_callback_is_not_retried_on_an_authentication_error(self):
        # GIVEN
        calls = []

        def fail(session):
            calls.append(session)
            raise AmazonOrdersAuthError("Callback failed")

        # WHEN
        with self.assertRaisesRegex(AmazonOrdersAuthError, "Callback failed"):
            self.saved_session.run_with_session(fail)

        # THEN
        self.assertEqual(1, len(calls))
        self.assertFalse(self.saved_session.run_with_session(lambda session: session.is_authenticated))

    def test_callback_blocks_same_site_calls_but_not_other_sites(self):
        # GIVEN
        self.given_saved_session("amazon.com", "amazon.de")
        callback_started, release_callback, same_site_started, same_site_done = (
            threading.Event() for _ in range(4))
        results = []

        def callback(session):
            callback_started.set()
            results.append(release_callback.wait(5))

        def same_site_call():
            same_site_started.set()
            self.saved_session.run(lambda session: same_site_done.set())

        callback_thread = threading.Thread(target=lambda: self.saved_session.run_with_session(callback))
        callback_thread.start()
        same_site_thread = threading.Thread(target=same_site_call)
        try:
            self.assertTrue(callback_started.wait(5))
            same_site_thread.start()
            self.assertTrue(same_site_started.wait(5))

            # WHEN
            other_site = self.saved_session.run(lambda session: session.config.site, "amazon.de")

            # THEN
            self.assertEqual("amazon.de", other_site)
            self.assertFalse(same_site_done.wait(0.1))
        finally:
            release_callback.set()
            callback_thread.join(5)
            if same_site_thread.ident is not None:
                same_site_thread.join(5)

        self.assertEqual([True], results)
        self.assertTrue(same_site_done.is_set())
        self.assertFalse(callback_thread.is_alive())
        self.assertFalse(same_site_thread.is_alive())

    def test_echo_writes_to_stderr(self):
        # GIVEN
        stdout, stderr = io.StringIO(), io.StringIO()

        # WHEN
        with redirect_stdout(stdout), redirect_stderr(stderr):
            StderrIO().echo("Some message")

        # THEN
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("Some message\n", stderr.getvalue())

    def test_prompt_raises_instead_of_reading_input(self):
        # WHEN
        with self.assertRaises(AmazonOrdersAuthError) as cm:
            StderrIO().prompt("Enter OTP")

        # THEN
        self.assertIn("Enter OTP", str(cm.exception))

    def test_each_site_has_its_own_session(self):
        # GIVEN
        self.given_saved_session("amazon.com", "amazon.de")

        # WHEN
        default_session = self.saved_session.run(lambda signed_in_session: signed_in_session)
        other_session = self.saved_session.run(lambda signed_in_session: signed_in_session, "www.amazon.de")

        # THEN
        self.assertIs(self.test_config, default_session.config)
        self.assertEqual("https://www.amazon.com", default_session.config.constants.BASE_URL)
        self.assertEqual("https://www.amazon.de", other_session.config.constants.BASE_URL)
        self.assertEqual(os.path.join(self.config_dir, "cookies.amazon.de.json"), other_session.config.cookie_jar_path)
        self.assertEqual(self.test_config.output_dir, other_session.config.output_dir)
        self.assertIs(other_session, self.saved_session.run(lambda signed_in_session: signed_in_session, "amazon.de"))

    def test_other_site_without_saved_session(self):
        # GIVEN
        self.given_saved_session()

        # WHEN
        with self.assertRaises(AmazonOrdersAuthError) as cm:
            self.saved_session.run(lambda signed_in_session: signed_in_session, "amazon.de")

        # THEN
        self.assertEqual("There's no saved Amazon session.", str(cm.exception))

    def test_unknown_site(self):
        # WHEN
        with self.assertRaises(AmazonOrdersError) as cm:
            self.saved_session.run(lambda signed_in_session: signed_in_session, "ebay.com")

        # THEN
        self.assertIn("\"ebay.com\" isn't an Amazon site that can be read. Supported sites: ", str(cm.exception))
        self.assertIn("amazon.co.uk, amazon.co.za, amazon.com", str(cm.exception))

    def test_environment_domain_pins_one_site(self):
        # GIVEN
        with patch.dict(os.environ, {"AMAZON_DOMAIN": "amazon.de"}):
            self.saved_session = SavedSession(AmazonOrdersConfig(config_path=self.test_config.config_path))
        self.given_saved_session("amazon.de", "amazon.com", "amazon.es")

        # WHEN
        default_session = self.saved_session.run(lambda signed_in_session: signed_in_session)
        with self.assertRaises(AmazonOrdersError) as cm:
            self.saved_session.run(lambda signed_in_session: signed_in_session, "amazon.com")

        # THEN
        self.assertEqual("https://www.amazon.de", default_session.config.constants.BASE_URL)
        self.assertEqual(os.path.join(self.config_dir, "cookies.amazon.de.json"),
                         default_session.config.cookie_jar_path)
        self.assertIn("amazon.com can't be read, since the AMAZON_DOMAIN environment variable", str(cm.exception))
        self.assertEqual(["amazon.de"], self.saved_session.sites())

    def test_other_site_refused_with_one_cookie_jar(self):
        # GIVEN
        cookie_jar_path = os.path.join(self.config_dir, "jars", "cookies.json")
        with patch.dict(os.environ, {"AMAZON_COOKIE_JAR_PATH": cookie_jar_path}):
            self.saved_session = SavedSession(AmazonOrdersConfig(config_path=self.test_config.config_path))

        # WHEN
        with self.assertRaises(AmazonOrdersError) as cm:
            self.saved_session.run(lambda signed_in_session: signed_in_session, "amazon.de")

        # THEN
        self.assertIn("amazon.de can't be read alongside amazon.com", str(cm.exception))
        self.assertIn(cookie_jar_path, str(cm.exception))

    def test_sites_lists_default_and_every_saved_session(self):
        # GIVEN
        self.given_cookie_jar("amazon.es")
        self.given_cookie_jar("amazon.de")

        # WHEN
        sites = self.saved_session.sites()

        # THEN
        self.assertEqual(["amazon.com", "amazon.de", "amazon.es"], sites)

    def test_config_change_reaches_every_site(self):
        # GIVEN
        self.given_saved_session("amazon.com", "amazon.de")
        self.saved_session.run(lambda signed_in_session: signed_in_session, "amazon.de")

        # WHEN
        value, saved = self.saved_session.update_config("request_timeout", 30)
        other_session = self.saved_session.run(lambda signed_in_session: signed_in_session, "amazon.de")

        # THEN
        self.assertEqual((30, False), (value, saved))
        self.assertEqual(30, other_session.config.request_timeout)
        self.assertEqual("https://www.amazon.de", other_session.config.constants.BASE_URL)

    def test_changing_domain_changes_default_site(self):
        # GIVEN
        self.test_config.save()
        self.given_saved_session("amazon.com", "amazon.de")
        de_cookie_jar_path = os.path.join(self.config_dir, "cookies.amazon.de.json")
        with open(self.test_cookie_jar_path, encoding="utf-8") as f:
            com_cookie_jar = f.read()
        with open(de_cookie_jar_path, encoding="utf-8") as f:
            de_cookie_jar = f.read()

        # WHEN
        value, saved = self.saved_session.update_config("domain", "https://www.amazon.de")

        # THEN
        self.assertEqual(("amazon.de", True), (value, saved))
        self.assertEqual("amazon.de", AmazonOrdersConfig(config_path=self.test_config.config_path).domain)
        self.assertEqual("amazon.de", self.saved_session.site())
        with open(self.test_cookie_jar_path, encoding="utf-8") as f:
            self.assertEqual(com_cookie_jar, f.read())
        with open(de_cookie_jar_path, encoding="utf-8") as f:
            self.assertEqual(de_cookie_jar, f.read())
        default_session = self.saved_session.run(lambda signed_in_session: signed_in_session)
        self.assertEqual("https://www.amazon.de", default_session.config.constants.BASE_URL)
        self.assertEqual(de_cookie_jar_path, default_session.config.cookie_jar_path)
        self.assertEqual(["amazon.de", "amazon.com"], self.saved_session.sites())

    def test_changing_domain_without_saving(self):
        # GIVEN
        self.test_config.save()

        # WHEN
        value, saved = self.saved_session.update_config("domain", "amazon.de", save=False)

        # THEN
        self.assertEqual(("amazon.de", False), (value, saved))
        self.assertEqual("amazon.de", self.saved_session.site())
        self.assertNotEqual("amazon.de", AmazonOrdersConfig(config_path=self.test_config.config_path).domain)

    def test_sites_are_read_at_the_same_time(self):
        # GIVEN
        self.given_saved_session("amazon.com", "amazon.de")
        default_call_started, other_call_done = threading.Event(), threading.Event()

        def default_call(signed_in_session):
            default_call_started.set()
            return other_call_done.wait(5)

        default_thread_result = []
        default_thread = threading.Thread(
            target=lambda: default_thread_result.append(self.saved_session.run(default_call)))
        default_thread.start()
        default_call_started.wait(5)

        # WHEN
        self.saved_session.run(lambda signed_in_session: other_call_done.set(), "amazon.de")
        default_thread.join(5)

        # THEN
        self.assertEqual([True], default_thread_result)
