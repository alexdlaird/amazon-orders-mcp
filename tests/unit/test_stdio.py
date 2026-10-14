__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import os
import sys

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from tests.unittestcase import UnitTestCase


class TestStdio(UnitTestCase):
    def given_stdio_server(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-c", "from amazonorders_mcp.server import main; main()"],
            env={**os.environ, "HOME": self.config_dir, "USERPROFILE": self.config_dir})

    async def test_server_runs_over_stdio(self):
        # GIVEN
        server = self.given_stdio_server()

        # WHEN
        async with Client(server) as client:
            result = await client.call_tool("check_session", {})

        # THEN
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["signed_in"])

    async def test_session_failure_keeps_stdio_intact(self):
        # GIVEN
        server = self.given_stdio_server()

        # WHEN
        async with Client(server) as client:
            failed_result = await client.call_tool("get_order_history", {})
            next_result = await client.call_tool("check_session", {})

        # THEN
        self.assert_tool_error(failed_result, "There's no saved Amazon session.")
        self.assertFalse(next_result.is_error)
