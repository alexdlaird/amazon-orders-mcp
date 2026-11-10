__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import anyio
import urllib3
from amazonauth.util import make_private_dirs, write_private_file
from amazonorders.exception import AmazonOrdersError
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: The path of the page where the server's owner approves a client.
CONSENT_PATH = "/oauth/consent"

#: The path of the authorization server's metadata (RFC 8414).
AUTHORIZATION_SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server"

#: The file in the config directory where registered clients and issued tokens are kept, so they survive a restart.
OAUTH_STATE_FILENAME = "mcp-oauth.json"

#: How many seconds an access token lasts.
ACCESS_TOKEN_SECONDS = 60 * 60

#: How many seconds a refresh token lasts.
REFRESH_TOKEN_SECONDS = 60 * 60 * 24 * 90

#: How many seconds an authorization code, or a client waiting to be approved, lasts.
AUTHORIZATION_SECONDS = 10 * 60

#: How many dynamically registered clients are kept, since some clients register again every time they connect.
MAX_REGISTERED_CLIENTS = 100

#: How many wrong secrets a client waiting to be approved allows before it's refused.
MAX_APPROVAL_ATTEMPTS = 5

#: How many seconds a client's fetched metadata document is reused.
CLIENT_METADATA_SECONDS = 60 * 60

#: The most bytes of a client's metadata document that are read.
MAX_CLIENT_METADATA_BYTES = 64 * 1024

#: How many seconds fetching a client's metadata document may take.
CLIENT_METADATA_TIMEOUT = 5


class ClientMetadataError(AmazonOrdersError):
    """
    Raised when a client's metadata document can't be fetched, or isn't valid.
    """
    pass


class OwnerOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """
    An OAuth authorization server for the one person who runs the server. Any client can register, either dynamically
    or with a Client ID Metadata Document, but only the owner can approve one, by entering the server's secret on the
    consent page. The secret also works as a bearer token, for clients that send one.

    Registered clients and issued tokens are kept in :data:`OAUTH_STATE_FILENAME`, readable only by the user, with each
    token stored as a hash, so a client stays connected when the server restarts.

    :param public_url: The server's public base URL (e.g. ``https://<name>.ngrok-free.dev``).
    :param secret: The server's secret, which the owner approves clients with.
    :param state_path: Where registered clients and issued tokens are kept, if anywhere.
    """

    def __init__(self,
                 public_url: str,
                 secret: str,
                 state_path: Optional[str] = None) -> None:
        #: The server's public base URL, which is also the authorization server's issuer.
        self.public_url: str = public_url.rstrip("/")
        #: Where registered clients and issued tokens are kept, if anywhere.
        self.state_path: Optional[str] = state_path
        self._secret = secret.encode("utf-8")
        self._lock = threading.Lock()
        self._clients: Dict[str, OAuthClientInformationFull] = {}
        self._access_tokens: Dict[str, AccessToken] = {}
        self._refresh_tokens: Dict[str, RefreshToken] = {}
        self._client_metadata: Dict[str, Tuple[float, OAuthClientInformationFull]] = {}
        self._pending: Dict[str, Tuple[float, OAuthClientInformationFull, AuthorizationParams, List[int]]] = {}
        self._codes: Dict[str, AuthorizationCode] = {}
        self._load_state()

    def auth_settings(self) -> AuthSettings:
        """
        Get the SDK's settings for this authorization server, which also requires a token on ``/mcp``. Tokens aren't
        checked against the resource they were issued for, since this server only issues them for itself, and a
        client doesn't have to say which resource it wants one for.

        :return: The settings.
        """
        # The URLs are given as strings, which the SDK keeps as-is, so the issuer matches ``iss`` exactly
        return AuthSettings.model_validate({"issuer_url": self.public_url,
                                            "resource_server_url": f"{self.public_url}/mcp",
                                            "client_registration_options": ClientRegistrationOptions(enabled=True),
                                            "revocation_options": RevocationOptions(enabled=True),
                                            "validate_token_resource": False})

    def authorization_server_metadata(self) -> Dict[str, Any]:
        """
        Get the authorization server's metadata: the SDK's, plus support for Client ID Metadata Documents, public
        clients, and the ``iss`` authorization response parameter, which the SDK doesn't advertise.

        :return: The metadata.
        """
        settings = self.auth_settings()
        metadata = build_metadata(settings.issuer_url, settings.service_documentation_url,
                                  settings.client_registration_options or ClientRegistrationOptions(),
                                  settings.revocation_options or RevocationOptions())
        metadata.token_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]
        metadata.client_id_metadata_document_supported = True
        metadata.authorization_response_iss_parameter_supported = True

        return metadata.model_dump(mode="json", exclude_none=True)

    async def get_client(self,
                         client_id: str) -> Optional[OAuthClientInformationFull]:
        with self._lock:
            client = self._clients.get(client_id)
        if client is not None or not _is_client_metadata_url(client_id):
            return client

        try:
            return await anyio.to_thread.run_sync(self._client_from_metadata_document, client_id)
        except ClientMetadataError as e:
            logger.warning(f"Refused the client \"{client_id}\", its metadata document isn't usable: {e}")

            return None

    async def register_client(self,
                              client_info: OAuthClientInformationFull) -> None:
        with self._lock:
            self._clients[str(client_info.client_id)] = client_info
            for client_id in list(self._clients)[:-MAX_REGISTERED_CLIENTS]:
                del self._clients[client_id]
            self._save_state()

    async def authorize(self,
                        client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(24)
        with self._lock:
            self._pending[request_id] = (time.time() + AUTHORIZATION_SECONDS, client, params, [0])

        return f"{self.public_url}{CONSENT_PATH}?request={request_id}"

    async def load_authorization_code(self,
                                      client: OAuthClientInformationFull,
                                      authorization_code: str) -> Optional[AuthorizationCode]:
        with self._lock:
            code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id or code.expires_at < time.time():
            return None

        return code

    async def exchange_authorization_code(self,
                                          client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        with self._lock:
            self._codes.pop(authorization_code.code, None)

            return self._issue_tokens(str(client.client_id), authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self,
                                 client: OAuthClientInformationFull,
                                 refresh_token: str) -> Optional[RefreshToken]:
        with self._lock:
            token = self._refresh_tokens.get(_token_hash(refresh_token))
        if token is None or token.client_id != client.client_id or _expired(token.expires_at):
            return None

        return token.model_copy(update={"token": refresh_token})

    async def exchange_refresh_token(self,
                                     client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken,
                                     scopes: List[str]) -> OAuthToken:
        with self._lock:
            self._refresh_tokens.pop(_token_hash(refresh_token.token), None)

            return self._issue_tokens(str(client.client_id), scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self,
                                token: str) -> Optional[AccessToken]:
        if hmac.compare_digest(token.encode("utf-8"), self._secret):
            return AccessToken(token=token, client_id="owner", scopes=[], resource=f"{self.public_url}/mcp")

        with self._lock:
            access_token = self._access_tokens.get(_token_hash(token))
        if access_token is None or _expired(access_token.expires_at):
            return None

        return access_token.model_copy(update={"token": token})

    async def revoke_token(self,
                           token: Any) -> None:
        with self._lock:
            self._access_tokens.pop(_token_hash(token.token), None)
            self._refresh_tokens.pop(_token_hash(token.token), None)
            self._save_state()

    async def consent(self,
                      request: Request) -> Response:
        """
        Show the owner which client is asking to connect, and approve it when they enter the server's secret.

        :param request: The request, a ``GET`` to show the page, or a ``POST`` to approve or deny the client.
        :return: The consent page, or the redirect back to the client.
        """
        form = await request.form() if request.method == "POST" else None
        request_id = str(form.get("request", "")) if form else request.query_params.get("request", "")
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None or pending[0] < time.time():
            return HTMLResponse(_page("This link expired. Start connecting again from your assistant."),
                                status_code=400)
        _, client, params, attempts = pending

        if form is None:
            return HTMLResponse(_consent_page(request_id, client, params))

        if form.get("action") == "deny":
            with self._lock:
                self._pending.pop(request_id, None)

            return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), error="access_denied",
                                                           state=params.state, iss=self.public_url), 302)

        if not hmac.compare_digest(str(form.get("secret", "")).strip().encode("utf-8"), self._secret):
            attempts[0] += 1
            if attempts[0] >= MAX_APPROVAL_ATTEMPTS:
                with self._lock:
                    self._pending.pop(request_id, None)

                return HTMLResponse(_page("Too many wrong secrets. Start connecting again from your assistant."),
                                    status_code=403)

            return HTMLResponse(_consent_page(request_id, client, params, error="That secret isn't right."),
                                status_code=401)

        code = AuthorizationCode(code=secrets.token_urlsafe(32), scopes=params.scopes or [],
                                 expires_at=time.time() + AUTHORIZATION_SECONDS, client_id=str(client.client_id),
                                 code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                                 redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                                 resource=params.resource)
        with self._lock:
            self._pending.pop(request_id, None)
            self._codes[code.code] = code

        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code.code, state=params.state,
                                                       iss=self.public_url), 302)

    def _issue_tokens(self,
                      client_id: str,
                      scopes: List[str],
                      resource: Optional[str]) -> OAuthToken:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        now = int(time.time())
        self._access_tokens[_token_hash(access_token)] = AccessToken(token="", client_id=client_id, scopes=scopes,
                                                                     expires_at=now + ACCESS_TOKEN_SECONDS,
                                                                     resource=resource)
        self._refresh_tokens[_token_hash(refresh_token)] = RefreshToken(token="", client_id=client_id, scopes=scopes,
                                                                        expires_at=now + REFRESH_TOKEN_SECONDS,
                                                                        resource=resource)
        self._save_state()

        return OAuthToken(access_token=access_token, token_type="Bearer", expires_in=ACCESS_TOKEN_SECONDS,
                          scope=" ".join(scopes) or None, refresh_token=refresh_token)

    def _client_from_metadata_document(self,
                                       client_id: str) -> OAuthClientInformationFull:
        """
        Get a client from its Client ID Metadata Document, fetched again once :data:`CLIENT_METADATA_SECONDS` pass.
        The document's own auth method is ignored, since every client registered this way is a public one.
        """
        with self._lock:
            cached = self._client_metadata.get(client_id)
        if cached is not None and cached[0] > time.time():
            return cached[1]

        document = _fetch_client_metadata_document(client_id)
        redirect_uris = document.get("redirect_uris")
        if document.get("client_id") != client_id:
            raise ClientMetadataError("Its client_id isn't its own URL.")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise ClientMetadataError("It has no redirect_uris.")

        client = OAuthClientInformationFull(client_id=client_id,
                                            client_name=str(document.get("client_name") or urlsplit(client_id).netloc),
                                            redirect_uris=redirect_uris, token_endpoint_auth_method="none",
                                            grant_types=["authorization_code", "refresh_token"],
                                            response_types=["code"])
        with self._lock:
            self._client_metadata[client_id] = (time.time() + CLIENT_METADATA_SECONDS, client)

        return client

    def _load_state(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            self._clients = {client_id: OAuthClientInformationFull.model_validate(client)
                             for client_id, client in state.get("clients", {}).items()}
            self._access_tokens = {token_hash: AccessToken.model_validate({**token, "token": ""})
                                   for token_hash, token in state.get("access_tokens", {}).items()}
            self._refresh_tokens = {token_hash: RefreshToken.model_validate({**token, "token": ""})
                                    for token_hash, token in state.get("refresh_tokens", {}).items()}
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read \"{self.state_path}\", so clients will need to connect again: {e}")

    def _save_state(self) -> None:
        if not self.state_path:
            return

        self._access_tokens = {token_hash: token for token_hash, token in self._access_tokens.items()
                               if not _expired(token.expires_at)}
        self._refresh_tokens = {token_hash: token for token_hash, token in self._refresh_tokens.items()
                                if not _expired(token.expires_at)}
        state = {"clients": {client_id: client.model_dump(mode="json", exclude_none=True)
                             for client_id, client in self._clients.items()},
                 "access_tokens": {token_hash: token.model_dump(mode="json", exclude={"token"}, exclude_none=True)
                                   for token_hash, token in self._access_tokens.items()},
                 "refresh_tokens": {token_hash: token.model_dump(mode="json", exclude={"token"}, exclude_none=True)
                                    for token_hash, token in self._refresh_tokens.items()}}
        make_private_dirs(os.path.dirname(self.state_path))
        write_private_file(self.state_path, json.dumps(state, indent=2))


class AuthorizationServerMetadataMiddleware:
    """
    Answers the authorization server metadata request with
    :meth:`~OwnerOAuthProvider.authorization_server_metadata`, in place of the SDK's.

    :param app: The app.
    :param provider: The authorization server.
    """

    def __init__(self,
                 app: ASGIApp,
                 provider: OwnerOAuthProvider) -> None:
        self.app = app
        self.provider = provider

    async def __call__(self,
                       scope: Scope,
                       receive: Receive,
                       send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == AUTHORIZATION_SERVER_METADATA_PATH:
            response = JSONResponse(self.provider.authorization_server_metadata(),
                                    headers={"Cache-Control": "public, max-age=3600",
                                             "Access-Control-Allow-Origin": "*"})
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _expired(expires_at: Optional[int]) -> bool:
    return expires_at is not None and expires_at < time.time()


def _is_client_metadata_url(client_id: str) -> bool:
    parts = urlsplit(client_id)

    return parts.scheme == "https" and bool(parts.hostname) and parts.path not in ("", "/")


def _fetch_client_metadata_document(url: str) -> Dict[str, Any]:
    """
    Fetch a Client ID Metadata Document from a public address only. The request goes to the address that was checked,
    so the host can't resolve to a private one in between, and redirects aren't followed.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        addresses = [str(info[4][0]) for info in socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)]
    except OSError as e:
        raise ClientMetadataError(f"Its host couldn't be resolved: {e}") from e
    if not addresses or not all(ipaddress.ip_address(address).is_global for address in addresses):
        raise ClientMetadataError("Its host isn't a public address.")

    pool = urllib3.HTTPSConnectionPool(addresses[0], port=parts.port or 443, server_hostname=host,
                                       assert_hostname=host, timeout=CLIENT_METADATA_TIMEOUT, retries=False)
    try:
        response = pool.request("GET", parts.path + (f"?{parts.query}" if parts.query else ""),
                                headers={"Host": host, "Accept": "application/json"}, redirect=False,
                                preload_content=False)
        body = response.read(MAX_CLIENT_METADATA_BYTES + 1)
    except urllib3.exceptions.HTTPError as e:
        raise ClientMetadataError(f"It couldn't be fetched: {e}") from e
    finally:
        pool.close()
    if response.status != 200:
        raise ClientMetadataError(f"It answered {response.status}.")
    if len(body) > MAX_CLIENT_METADATA_BYTES:
        raise ClientMetadataError("It's too large.")

    try:
        document = json.loads(body)
    except ValueError as e:
        raise ClientMetadataError("It isn't JSON.") from e
    if not isinstance(document, dict):
        raise ClientMetadataError("It isn't a JSON object.")

    return document


def _consent_page(request_id: str,
                  client: OAuthClientInformationFull,
                  params: AuthorizationParams,
                  error: Optional[str] = None) -> str:
    name = html.escape(client.client_name or str(client.client_id))
    redirect_host = html.escape(urlsplit(str(params.redirect_uri)).netloc)
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""

    return _page(f"""
        <h1>Connect to your Amazon orders?</h1>
        <p><strong>{name}</strong> wants to read your Amazon order history through this server. After you allow it,
        you'll be sent back to <strong>{redirect_host}</strong>.</p>
        <p>Only allow it if you just started connecting. To allow it, enter the server's secret.</p>
        {error_html}
        <form method="post" action="{CONSENT_PATH}">
          <input type="hidden" name="request" value="{html.escape(request_id)}">
          <input type="password" name="secret" placeholder="Server secret" autocomplete="current-password" autofocus>
          <button type="submit" name="action" value="allow">Allow</button>
          <button type="submit" name="action" value="deny" class="secondary">Deny</button>
        </form>""")


def _page(body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="muppets" content="no strings attached">
<title>amazon-orders-mcp</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 32rem; margin: 3rem auto; padding: 0 1rem;
         line-height: 1.5; color: #1a1a1a; background: #fff; }}
  input {{ width: 100%; box-sizing: border-box; padding: .6rem; margin: .5rem 0 1rem; font-size: 1rem; }}
  button {{ padding: .6rem 1.2rem; font-size: 1rem; margin-right: .5rem; }}
  .secondary {{ background: none; border: 1px solid #999; }}
  .error {{ color: #b00020; }}
  @media (prefers-color-scheme: dark) {{ body {{ color: #eee; background: #121212; }} }}
</style></head><body>{body}</body></html>"""
