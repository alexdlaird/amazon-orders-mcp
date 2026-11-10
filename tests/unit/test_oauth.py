__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import base64
import hashlib
import io
import json
import os
import sys
import unittest
from contextlib import asynccontextmanager, redirect_stderr
from typing import AsyncIterator, Dict, Tuple
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from amazonorders_mcp.oauth import (
    AUTHORIZATION_SERVER_METADATA_PATH,
    CONSENT_PATH,
    MAX_APPROVAL_ATTEMPTS,
    OAUTH_STATE_FILENAME,
    OwnerOAuthProvider,
)
from amazonorders_mcp.server import (
    AUTHORIZATION_ENVIRONMENT_VARIABLE,
    PUBLIC_URL_ENVIRONMENT_VARIABLE,
    create_http_app,
    create_server,
    main,
)
from tests.unittestcase import UnitTestCase

BASE_URL = "http://127.0.0.1:8000"

#: The public URL the tests' server is reached at, as through a tunnel.
PUBLIC_URL = "https://some-name.ngrok-free.dev"

#: The server's secret, which approves clients and also works as a bearer token.
SECRET = "some-secret"

#: A client that registers dynamically.
REDIRECT_URI = "https://some-client.example/callback"

#: A client identified by a Client ID Metadata Document.
CLIENT_METADATA_URL = "https://some-assistant.example/oauth/client.json"

#: The redirect URI the client's metadata document lists.
CLIENT_METADATA_REDIRECT_URI = "https://some-assistant.example/oauth/callback"


class TestOAuth(UnitTestCase):
    def setUp(self):
        super().setUp()

        self.state_path = os.path.join(self.config_dir, OAUTH_STATE_FILENAME)
        self.provider = OwnerOAuthProvider(PUBLIC_URL, SECRET, self.state_path)

    @asynccontextmanager
    async def given_http_client(self,
                                **headers: str) -> AsyncIterator[httpx2.AsyncClient]:
        server = create_server(self.saved_session, oauth_provider=self.provider)
        app = create_http_app(server, oauth_provider=self.provider)
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE_URL,
                                          headers=headers) as http_client:
                yield http_client

    def given_client(self,
                     http_client: httpx2.AsyncClient) -> Client:
        return Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http_client))

    async def given_registered_client(self,
                                      http_client: httpx2.AsyncClient) -> Dict[str, str]:
        response = await http_client.post("/register", json={"redirect_uris": [REDIRECT_URI],
                                                             "client_name": "Some Client",
                                                             "software_id": "some-software"})
        return response.json()

    async def given_consent_page(self,
                                 http_client: httpx2.AsyncClient,
                                 client_id: str,
                                 redirect_uri: str) -> Tuple[str, str]:
        verifier = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        response = await http_client.get("/authorize?" + urlencode({"response_type": "code", "client_id": client_id,
                                                                    "redirect_uri": redirect_uri,
                                                                    "code_challenge": challenge,
                                                                    "code_challenge_method": "S256",
                                                                    "state": "some-state",
                                                                    "resource": f"{PUBLIC_URL}/mcp"}))
        return parse_qs(urlsplit(response.headers["location"]).query)["request"][0], verifier

    async def given_approved_client(self,
                                    http_client: httpx2.AsyncClient,
                                    client_id: str,
                                    redirect_uri: str) -> Tuple[str, str]:
        request_id, verifier = await self.given_consent_page(http_client, client_id, redirect_uri)
        response = await http_client.post(CONSENT_PATH, data={"request": request_id, "secret": SECRET,
                                                              "action": "allow"})
        return parse_qs(urlsplit(response.headers["location"]).query)["code"][0], verifier

    async def test_metadata_advertises_client_metadata_documents_and_public_clients(self):
        async with self.given_http_client() as http_client:
            # WHEN
            response = await http_client.get(AUTHORIZATION_SERVER_METADATA_PATH)

        # THEN
        metadata = response.json()
        self.assertEqual(200, response.status_code)
        self.assertEqual(PUBLIC_URL, metadata["issuer"])
        self.assertTrue(metadata["client_id_metadata_document_supported"])
        self.assertTrue(metadata["authorization_response_iss_parameter_supported"])
        self.assertIn("none", metadata["token_endpoint_auth_methods_supported"])
        self.assertEqual(["S256"], metadata["code_challenge_methods_supported"])
        self.assertEqual(f"{PUBLIC_URL}/register", metadata["registration_endpoint"])

    async def test_request_without_token_points_to_protected_resource_metadata(self):
        async with self.given_http_client() as http_client:
            # WHEN
            response = await http_client.post("/mcp", json={})
            metadata = (await http_client.get("/.well-known/oauth-protected-resource/mcp")).json()

        # THEN
        self.assertEqual(401, response.status_code)
        self.assertIn(f"resource_metadata=\"{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp\"",
                      response.headers["www-authenticate"])
        self.assertEqual(f"{PUBLIC_URL}/mcp", metadata["resource"])
        self.assertEqual([PUBLIC_URL], metadata["authorization_servers"])

    async def test_dynamically_registered_client_is_approved_and_calls_tools(self):
        async with self.given_http_client() as http_client:
            client = await self.given_registered_client(http_client)
            code, verifier = await self.given_approved_client(http_client, client["client_id"], REDIRECT_URI)

            # WHEN
            response = await http_client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                                              "redirect_uri": REDIRECT_URI,
                                                              "code_verifier": verifier,
                                                              "client_id": client["client_id"],
                                                              "client_secret": client["client_secret"],
                                                              "resource": f"{PUBLIC_URL}/mcp"})
            http_client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
            async with self.given_client(http_client) as mcp_client:
                result = await mcp_client.call_tool("get_config", {})

        # THEN
        self.assertEqual(200, response.status_code)
        self.assertIn("refresh_token", response.json())
        self.assertFalse(result.is_error)

    async def test_consent_page_names_the_client_and_where_it_returns(self):
        async with self.given_http_client() as http_client:
            # GIVEN
            client = await self.given_registered_client(http_client)
            request_id, _ = await self.given_consent_page(http_client, client["client_id"], REDIRECT_URI)

            # WHEN
            response = await http_client.get(f"{CONSENT_PATH}?request={request_id}")

        # THEN
        self.assertEqual(200, response.status_code)
        self.assertIn("Some Client", response.text)
        self.assertIn("some-client.example", response.text)

    async def test_approval_redirects_back_with_state_and_issuer(self):
        async with self.given_http_client() as http_client:
            # GIVEN
            client = await self.given_registered_client(http_client)
            request_id, _ = await self.given_consent_page(http_client, client["client_id"], REDIRECT_URI)

            # WHEN
            response = await http_client.post(CONSENT_PATH, data={"request": request_id, "secret": SECRET,
                                                                  "action": "allow"})

        # THEN
        location = response.headers["location"]
        query = parse_qs(urlsplit(location).query)
        self.assertEqual(302, response.status_code)
        self.assertTrue(location.startswith(REDIRECT_URI))
        self.assertEqual(["some-state"], query["state"])
        self.assertEqual([PUBLIC_URL], query["iss"])
        self.assertIn("code", query)

    async def test_wrong_secret_is_refused_until_too_many_attempts(self):
        async with self.given_http_client() as http_client:
            # GIVEN
            client = await self.given_registered_client(http_client)
            request_id, _ = await self.given_consent_page(http_client, client["client_id"], REDIRECT_URI)

            # WHEN
            responses = [await http_client.post(CONSENT_PATH, data={"request": request_id, "secret": "wrong-secret",
                                                                    "action": "allow"})
                         for _ in range(MAX_APPROVAL_ATTEMPTS)]
            right_response = await http_client.post(CONSENT_PATH, data={"request": request_id, "secret": SECRET,
                                                                        "action": "allow"})

        # THEN
        self.assertEqual([401] * (MAX_APPROVAL_ATTEMPTS - 1) + [403], [response.status_code for response in responses])
        self.assertEqual(400, right_response.status_code)

    async def test_denied_client_is_redirected_back_with_access_denied(self):
        async with self.given_http_client() as http_client:
            # GIVEN
            client = await self.given_registered_client(http_client)
            request_id, _ = await self.given_consent_page(http_client, client["client_id"], REDIRECT_URI)

            # WHEN
            response = await http_client.post(CONSENT_PATH, data={"request": request_id, "action": "deny"})

        # THEN
        self.assertEqual(302, response.status_code)
        self.assertEqual(["access_denied"], parse_qs(urlsplit(response.headers["location"]).query)["error"])

    async def test_authorization_code_works_once(self):
        async with self.given_http_client() as http_client:
            # GIVEN
            client = await self.given_registered_client(http_client)
            code, verifier = await self.given_approved_client(http_client, client["client_id"], REDIRECT_URI)
            data = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
                    "code_verifier": verifier, "client_id": client["client_id"],
                    "client_secret": client["client_secret"]}

            # WHEN
            first_response = await http_client.post("/token", data=data)
            second_response = await http_client.post("/token", data=data)

        # THEN
        self.assertEqual(200, first_response.status_code)
        self.assertEqual(400, second_response.status_code)

    async def test_refresh_token_is_replaced_when_used(self):
        async with self.given_http_client() as http_client:
            # GIVEN
            client = await self.given_registered_client(http_client)
            code, verifier = await self.given_approved_client(http_client, client["client_id"], REDIRECT_URI)
            tokens = (await http_client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                                             "redirect_uri": REDIRECT_URI,
                                                             "code_verifier": verifier,
                                                             "client_id": client["client_id"],
                                                             "client_secret": client["client_secret"]})).json()
            data = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                    "client_id": client["client_id"], "client_secret": client["client_secret"]}

            # WHEN
            refreshed_response = await http_client.post("/token", data=data)
            reused_response = await http_client.post("/token", data=data)

        # THEN
        self.assertEqual(200, refreshed_response.status_code)
        self.assertNotEqual(tokens["access_token"], refreshed_response.json()["access_token"])
        self.assertEqual(400, reused_response.status_code)

    @patch("amazonorders_mcp.oauth._fetch_client_metadata_document")
    async def test_client_metadata_document_client_is_approved_without_a_secret(self,
                                                                                mock_fetch):
        # GIVEN
        mock_fetch.return_value = {"client_id": CLIENT_METADATA_URL, "client_name": "Some Assistant",
                                   "redirect_uris": [CLIENT_METADATA_REDIRECT_URI],
                                   "token_endpoint_auth_method": "private_key_jwt"}
        async with self.given_http_client() as http_client:
            code, verifier = await self.given_approved_client(http_client, CLIENT_METADATA_URL,
                                                              CLIENT_METADATA_REDIRECT_URI)

            # WHEN
            response = await http_client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                                              "redirect_uri": CLIENT_METADATA_REDIRECT_URI,
                                                              "code_verifier": verifier,
                                                              "client_id": CLIENT_METADATA_URL})

        # THEN
        self.assertEqual(200, response.status_code)
        mock_fetch.assert_called_once_with(CLIENT_METADATA_URL)

    @patch("amazonorders_mcp.oauth._fetch_client_metadata_document")
    async def test_client_metadata_document_is_refused_when_invalid(self,
                                                                    mock_fetch):
        cases = {"another client's document": ({"client_id": "https://other.example/client.json",
                                                "redirect_uris": [CLIENT_METADATA_REDIRECT_URI]},
                                               CLIENT_METADATA_REDIRECT_URI),
                 "no redirect URIs": ({"client_id": CLIENT_METADATA_URL, "redirect_uris": []},
                                      CLIENT_METADATA_REDIRECT_URI),
                 "an unlisted redirect URI": ({"client_id": CLIENT_METADATA_URL,
                                               "redirect_uris": [CLIENT_METADATA_REDIRECT_URI]},
                                              "https://elsewhere.example/callback")}
        for case, (document, redirect_uri) in cases.items():
            with self.subTest(case=case):
                # GIVEN
                mock_fetch.return_value = document
                self.provider = OwnerOAuthProvider(PUBLIC_URL, SECRET)
                async with self.given_http_client() as http_client:
                    # WHEN
                    response = await http_client.get("/authorize?" + urlencode({
                        "response_type": "code", "client_id": CLIENT_METADATA_URL, "redirect_uri": redirect_uri,
                        "code_challenge": "x" * 43, "code_challenge_method": "S256", "state": "some-state"}))

                # THEN
                self.assertEqual(400, response.status_code)

    @patch("amazonorders_mcp.oauth.socket.getaddrinfo")
    async def test_client_metadata_document_on_a_private_address_is_not_fetched(self,
                                                                                mock_getaddrinfo):
        # GIVEN
        mock_getaddrinfo.return_value = [(2, 1, 6, "", ("192.168.1.10", 443))]

        # WHEN
        client = await self.provider.get_client(CLIENT_METADATA_URL)

        # THEN
        self.assertIsNone(client)
        mock_getaddrinfo.assert_called_once()

    async def test_secret_works_as_a_bearer_token(self):
        async with self.given_http_client(Authorization=f"Bearer {SECRET}") as http_client:
            # WHEN
            async with self.given_client(http_client) as mcp_client:
                result = await mcp_client.call_tool("get_config", {})

        # THEN
        self.assertFalse(result.is_error)

    async def test_clients_and_tokens_survive_a_restart(self):
        async with self.given_http_client() as http_client:
            # GIVEN
            client = await self.given_registered_client(http_client)
            code, verifier = await self.given_approved_client(http_client, client["client_id"], REDIRECT_URI)
            tokens = (await http_client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                                             "redirect_uri": REDIRECT_URI,
                                                             "code_verifier": verifier,
                                                             "client_id": client["client_id"],
                                                             "client_secret": client["client_secret"]})).json()

        # WHEN
        self.provider = OwnerOAuthProvider(PUBLIC_URL, SECRET, self.state_path)

        # THEN
        self.assertIsNotNone(await self.provider.get_client(client["client_id"]))
        self.assertIsNotNone(await self.provider.load_access_token(tokens["access_token"]))
        with open(self.state_path, "r", encoding="utf-8") as f:
            state = f.read()
        self.assertNotIn(tokens["access_token"], state)
        self.assertNotIn(tokens["refresh_token"], state)
        self.assertEqual(1, len(json.loads(state)["refresh_tokens"]))

    @unittest.skipIf(sys.platform == "win32", reason="Windows does not support POSIX file permissions")
    async def test_saved_clients_and_tokens_are_owner_only(self):
        async with self.given_http_client() as http_client:
            # WHEN
            await self.given_registered_client(http_client)

        # THEN
        self.assertEqual(0o600, os.stat(self.state_path).st_mode & 0o777)

    async def test_sign_in_through_a_proxy_says_where_to_sign_in(self):
        async with self.given_http_client(Authorization=f"Bearer {SECRET}",
                                          **{"X-Forwarded-For": "203.0.113.7"}) as http_client:
            # WHEN
            async with self.given_client(http_client) as mcp_client:
                result = await mcp_client.call_tool("sign_in", {})

        # THEN
        self.assertTrue(result.is_error)
        self.assertIn("connected to the server remotely", result.content[0].text)
        self.assertIn("`uvx amazon-orders login", result.content[0].text)

    def test_command_line_requires_token_with_public_url(self):
        # GIVEN
        stderr = io.StringIO()

        # WHEN
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            main(["--transport", "streamable-http", "--public-url", PUBLIC_URL])

        # THEN
        self.assertIn(f"--public-url needs --token-file or {AUTHORIZATION_ENVIRONMENT_VARIABLE} too",
                      stderr.getvalue())

    def test_command_line_rejects_public_url_over_stdio(self):
        # GIVEN
        stderr = io.StringIO()

        # WHEN
        with patch.dict(os.environ, {AUTHORIZATION_ENVIRONMENT_VARIABLE: SECRET}), redirect_stderr(stderr), \
                self.assertRaises(SystemExit):
            main(["--public-url", PUBLIC_URL])

        # THEN
        self.assertIn("--public-url only applies to --transport streamable-http.", stderr.getvalue())

    async def test_command_line_signs_clients_in_with_public_url_from_environment(self):
        # GIVEN
        with patch.dict(os.environ, {AUTHORIZATION_ENVIRONMENT_VARIABLE: SECRET,
                                     PUBLIC_URL_ENVIRONMENT_VARIABLE: PUBLIC_URL}), \
                patch("amazonorders_mcp.server.uvicorn.run") as mock_uvicorn_run:
            # WHEN
            main(["--transport", "streamable-http"])

        # THEN
        app = mock_uvicorn_run.call_args.args[0]
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE_URL) as http_client:
            metadata = (await http_client.get(AUTHORIZATION_SERVER_METADATA_PATH)).json()
        self.assertEqual(PUBLIC_URL, metadata["issuer"])
