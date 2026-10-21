__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from amazonorders.exception import AmazonOrdersAuthError

from amazonorders_mcp.session import StderrIO, has_display
from tests.unittestcase import UnitTestCase


class TestSession(UnitTestCase):
    def test_saved_session_never_submits_sign_in_forms(self):
        # GIVEN
        self.given_saved_session()

        # WHEN
        amazon_session = self.saved_session.run(lambda signed_in_session: signed_in_session)

        # THEN
        self.assertEqual([], amazon_session.auth_forms)

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

    @patch.object(sys, "platform", "linux")
    def test_linux_without_display(self):
        # GIVEN
        with patch.dict(os.environ):
            os.environ.pop("DISPLAY", None)
            os.environ.pop("WAYLAND_DISPLAY", None)

            # WHEN
            display = has_display()

        # THEN
        self.assertFalse(display)

    @patch.object(sys, "platform", "linux")
    def test_linux_with_display(self):
        # GIVEN
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
            os.environ.pop("DISPLAY", None)

            # WHEN
            display = has_display()

        # THEN
        self.assertTrue(display)

    @patch.object(sys, "platform", "darwin")
    def test_display_assumed_off_linux(self):
        # GIVEN
        with patch.dict(os.environ):
            os.environ.pop("DISPLAY", None)
            os.environ.pop("WAYLAND_DISPLAY", None)

            # WHEN
            display = has_display()

        # THEN
        self.assertTrue(display)
