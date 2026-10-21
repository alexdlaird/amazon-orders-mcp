__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import io
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager, redirect_stderr
from typing import AsyncIterator, Dict, Optional
from unittest.mock import MagicMock, patch

import anyio
import httpx2
from amazonorders.paging import PagedResults
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from starlette.types import ASGIApp

from amazonorders_mcp.server import HEALTH_PATH, TOKEN_ENVIRONMENT_VARIABLE, create_http_app, create_server, main
from tests.unittestcase import UnitTestCase

BASE_URL = "http://127.0.0.1:8000"

LIST_TOOLS_REQUEST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


class TestStreamableHttp(UnitTestCase):
    @asynccontextmanager
    async def given_http_client(self,
                                json_response: bool = False,
                                token: Optional[str] = None,
                                headers: Optional[Dict[str, str]] = None) -> AsyncIterator[httpx2.AsyncClient]:
        server = create_server(self.saved_session)
        app = create_http_app(server, json_response=json_response, token=token)
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE_URL,
                                          headers=headers) as http_client:
                yield http_client

    def given_client(self,
                     http_client: httpx2.AsyncClient) -> Client:
        return Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http_client))

    def given_free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    async def given_http_server(self,
                                env: Dict[str, str]) -> str:
        port = self.given_free_port()
        process = subprocess.Popen([sys.executable, "-c", "from amazonorders_mcp.server import main; main()",
                                    "--transport", "streamable-http", "--port", str(port)],
                                   env={**os.environ, "HOME": self.config_dir, "USERPROFILE": self.config_dir, **env},
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(process.wait, 10)
        self.addCleanup(process.terminate)

        url = f"http://127.0.0.1:{port}"
        async with httpx2.AsyncClient() as http_client:
            with anyio.fail_after(30):
                while process.poll() is None:
                    try:
                        if (await http_client.get(f"{url}{HEALTH_PATH}")).status_code == 200:
                            return url
                    except httpx2.TransportError:
                        pass
                    await anyio.sleep(0.2)

        self.fail(f"The server exited with {process.returncode} before it served HTTP.")

    def given_app_from_command_line(self,
                                    *args: str) -> MagicMock:
        uvicorn_run_patcher = patch("amazonorders_mcp.server.uvicorn.run")
        self.addCleanup(uvicorn_run_patcher.stop)
        mock_uvicorn_run = uvicorn_run_patcher.start()

        main(["--transport", "streamable-http", *args])

        return mock_uvicorn_run

    async def request(self,
                      app: ASGIApp,
                      method: str,
                      path: str,
                      headers: Optional[Dict[str, str]] = None) -> httpx2.Response:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE_URL) as http_client:
            return await http_client.request(method, path, headers=headers)

    async def test_server_runs_over_streamable_http(self):
        # GIVEN
        async with self.given_http_client() as http_client:
            # WHEN
            async with self.given_client(http_client) as client:
                tools = (await client.list_tools()).tools
                result = await client.call_tool("check_session", {})

        # THEN
        self.assertIn("get_order_history", {tool.name for tool in tools})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["signed_in"])

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_progress_is_reported_over_streamable_http(self, mock_amazon_orders):
        # GIVEN
        self.given_saved_session()

        def get_order_history_page(**kwargs):
            kwargs["progress_with_total"](10, 12)
            return PagedResults([], None)

        mock_amazon_orders.return_value.get_order_history_page.side_effect = get_order_history_page
        reported = []

        async def progress_callback(progress, total, message):
            reported.append((progress, total, message))

        async with self.given_http_client() as http_client:
            # WHEN
            async with self.given_client(http_client) as client:
                result = await client.call_tool("get_order_history", {}, progress_callback=progress_callback)

        # THEN
        self.assertFalse(result.is_error)
        self.assertEqual([(10, 12, "10 of 12 Orders found so far ...")], reported)

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_json_response_answers_without_progress(self, mock_amazon_orders):
        # GIVEN
        self.given_saved_session()

        def get_order_history_page(**kwargs):
            kwargs["progress_with_total"](10, 12)
            return PagedResults([], None)

        mock_amazon_orders.return_value.get_order_history_page.side_effect = get_order_history_page
        reported = []

        async def progress_callback(progress, total, message):
            reported.append((progress, total, message))

        async with self.given_http_client(json_response=True) as http_client:
            # WHEN
            response = await http_client.post("/mcp", json=LIST_TOOLS_REQUEST, headers=MCP_HEADERS)
            async with self.given_client(http_client) as client:
                result = await client.call_tool("get_order_history", {}, progress_callback=progress_callback)

        # THEN
        self.assertEqual(200, response.status_code)
        self.assertEqual("application/json", response.headers["content-type"])
        self.assertFalse(result.is_error)
        self.assertEqual({"items": [], "cursor": None, "has_more": False}, result.structured_content)
        self.assertEqual([], reported)

    async def test_health_check_needs_no_token(self):
        # GIVEN
        app = create_http_app(create_server(self.saved_session), token="some-token")

        # WHEN
        response = await self.request(app, "GET", HEALTH_PATH)

        # THEN
        self.assertEqual(200, response.status_code)
        self.assertEqual("ok", response.text)

    async def test_request_without_token_is_unauthorized(self):
        # GIVEN
        app = create_http_app(create_server(self.saved_session), token="some-token")

        # WHEN
        missing_response = await self.request(app, "POST", "/mcp")
        wrong_response = await self.request(app, "POST", "/mcp", headers={"Authorization": "Bearer other-token"})
        basic_response = await self.request(app, "POST", "/mcp", headers={"Authorization": "Basic some-token"})

        # THEN
        for response in (missing_response, wrong_response, basic_response):
            self.assertEqual(401, response.status_code)
            self.assertEqual("Bearer", response.headers["www-authenticate"])

    async def test_request_with_token_is_served(self):
        # GIVEN
        async with self.given_http_client(token="some-token",
                                          headers={"Authorization": "Bearer some-token"}) as http_client:
            # WHEN
            async with self.given_client(http_client) as client:
                result = await client.call_tool("check_session", {})

        # THEN
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["signed_in"])

    @patch.object(MCPServer, "run")
    def test_command_line_runs_over_stdio_by_default(self, mock_run):
        # WHEN
        main([])

        # THEN
        mock_run.assert_called_once_with()

    async def test_command_line_serves_streamable_http(self):
        # WHEN
        mock_uvicorn_run = self.given_app_from_command_line("--host", "0.0.0.0", "--port", "9000")

        # THEN
        mock_uvicorn_run.assert_called_once()
        self.assertEqual("0.0.0.0", mock_uvicorn_run.call_args.kwargs["host"])
        self.assertEqual(9000, mock_uvicorn_run.call_args.kwargs["port"])
        response = await self.request(mock_uvicorn_run.call_args.args[0], "GET", HEALTH_PATH)
        self.assertEqual(200, response.status_code)

    async def test_command_line_reads_token_file(self):
        # GIVEN
        token_path = os.path.join(self.config_dir, "token")
        with open(token_path, "w", encoding="utf-8") as f:
            f.write("some-token\n")

        # WHEN
        mock_uvicorn_run = self.given_app_from_command_line("--token-file", token_path)

        # THEN
        app = mock_uvicorn_run.call_args.args[0]
        self.assertEqual(401, (await self.request(app, "GET", "/mcp")).status_code)
        self.assertEqual(404, (await self.request(app, "GET", "/unknown",
                                                  headers={"Authorization": "Bearer some-token"})).status_code)

    async def test_command_line_reads_token_from_environment(self):
        # GIVEN
        with patch.dict(os.environ, {TOKEN_ENVIRONMENT_VARIABLE: "some-token"}):
            # WHEN
            mock_uvicorn_run = self.given_app_from_command_line()

        # THEN
        app = mock_uvicorn_run.call_args.args[0]
        self.assertEqual(401, (await self.request(app, "GET", "/mcp")).status_code)
        self.assertEqual(404, (await self.request(app, "GET", "/unknown",
                                                  headers={"Authorization": "Bearer some-token"})).status_code)

    def test_command_line_rejects_two_tokens(self):
        # GIVEN
        token_path = os.path.join(self.config_dir, "token")
        with open(token_path, "w", encoding="utf-8") as f:
            f.write("some-token")
        stderr = io.StringIO()

        # WHEN
        with patch.dict(os.environ, {TOKEN_ENVIRONMENT_VARIABLE: "other-token"}), redirect_stderr(stderr), \
                self.assertRaises(SystemExit):
            main(["--transport", "streamable-http", "--token-file", token_path])

        # THEN
        self.assertIn(f"Set either --token-file or {TOKEN_ENVIRONMENT_VARIABLE}, not both.", stderr.getvalue())

    def test_command_line_rejects_empty_token_file(self):
        # GIVEN
        token_path = os.path.join(self.config_dir, "token")
        with open(token_path, "w", encoding="utf-8") as f:
            f.write("\n")
        stderr = io.StringIO()

        # WHEN
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            main(["--transport", "streamable-http", "--token-file", token_path])

        # THEN
        self.assertIn("is empty.", stderr.getvalue())

    async def test_server_runs_from_command_line_over_streamable_http(self):
        # GIVEN
        url = await self.given_http_server({TOKEN_ENVIRONMENT_VARIABLE: "some-token"})

        # WHEN
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer some-token"}) as http_client:
            unauthorized_response = await http_client.post(f"{url}/mcp", json=LIST_TOOLS_REQUEST, headers={
                **MCP_HEADERS, "Authorization": "Bearer other-token"})
            async with Client(streamable_http_client(f"{url}/mcp", http_client=http_client)) as client:
                result = await client.call_tool("check_session", {})

        # THEN
        self.assertEqual(401, unauthorized_response.status_code)
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["signed_in"])
