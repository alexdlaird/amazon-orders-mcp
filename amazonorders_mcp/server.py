__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import argparse
import hmac
import json
import logging
import os
from functools import partial
from typing import Annotated, Any, Callable, Dict, List, Optional, Protocol, TypeVar, Union

import anyio
import uvicorn
from amazonorders.entity.family_member import FamilyMember
from amazonorders.entity.gift_card_activity import GiftCardActivity
from amazonorders.entity.invoice_pdf import InvoicePdf
from amazonorders.entity.order import Order
from amazonorders.entity.prime_payment import PrimePayment
from amazonorders.entity.tracking import Tracking
from amazonorders.entity.transaction import Transaction
from amazonorders.exception import AmazonOrdersAuthError, AmazonOrdersBrowserNotInstalledError, AmazonOrdersError
from amazonorders.gift_cards import AmazonGiftCards
from amazonorders.orders import AmazonOrders
from amazonorders.paging import PagedResults
from amazonorders.prime import AmazonPrime
from amazonorders.session import AmazonSession
from amazonorders.transactions import AmazonTransactions
from amazonorders.util import has_display
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from amazonorders_mcp import __version__
from amazonorders_mcp.schema import (
    FamilyMemberList,
    GiftCardActivityList,
    InvoicePdfList,
    OrderDetails,
    OrderHistoryPage,
    PrimePaymentList,
    TrackingDetails,
    TransactionsPage,
    parameter_field,
    tool_description,
)
from amazonorders_mcp.session import (
    HEADLESS_SIGN_IN_COMMAND,
    SIGN_IN_COMMAND,
    SIGN_IN_TIMEOUT,
    SavedSession,
)

logger = logging.getLogger(__name__)

#: What the server can do, given to the client when it connects.
INSTRUCTIONS = (
    "Reads the user's Amazon order history, invoice PDFs, package tracking, transactions, Gift Card balance and "
    "activity, and Prime membership payments. The links from `get_invoice_pdfs` change on every call and may only "
    "open in a browser signed in to the user's Amazon account, so give them to the user right away rather than "
    "keeping them. "
    "For the orders of an Amazon Household member, like a linked Teen account, get their `id` from "
    "`get_family_members` and pass it to `get_order_history` as `family_member_id`. "
    "Each Amazon site, like amazon.com or amazon.de, has its own orders and its own sign-in. Tools read the user's "
    "default site unless given another as `site`, so pass the site an order was placed on, and `check_session` "
    "says which sites the user is signed in to. "
    "Every tool but `sign_in` and `update_config` is read-only. If a tool says the user isn't signed in, offer to "
    "sign them in with `sign_in`, and call it only once they agree. If that can't work, they can run "
    f"`{SIGN_IN_COMMAND}` in a terminal instead."
)

#: The annotations every tool has, since none of them change anything on Amazon.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

#: The annotations of ``sign_in``, which opens a browser window but changes nothing on Amazon.
SIGN_IN = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

#: The annotations of ``update_config``, which changes how ``amazon-orders`` reads, and nothing on Amazon.
UPDATE_CONFIG = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                open_world_hint=False)

#: Where what each config key does is documented.
CONFIG_DOCS_URL = "https://amazon-orders.readthedocs.io/api.html#amazonorders.conf.AmazonOrdersConfig"

#: Where signing in with credentials, instead of a browser window, is documented.
AUTOMATED_SIGN_IN_DOCS_URL = "https://amazon-orders.readthedocs.io/#automating-authentication"

#: How many seconds pass between the reports that keep a client waiting on ``sign_in``.
SIGN_IN_REPORT_INTERVAL = 5

#: How many results a paged tool collects when the client doesn't say, so a bare call stays bounded.
DEFAULT_LIMIT = 25

#: The transports the server runs over: ``stdio`` for a client that runs the server, and ``streamable-http`` for
#: clients that connect to it.
TRANSPORTS = ("stdio", "streamable-http")

#: The address the server listens on over HTTP by default, which only this machine can reach.
DEFAULT_HOST = "127.0.0.1"

#: The addresses only this machine can reach, on which the SDK also rejects requests addressed to another host (DNS
#: rebinding). On any other address, the server only starts with a bearer token.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

#: The port the server listens on over HTTP by default.
DEFAULT_PORT = 8000

#: The path that answers a health check over HTTP, without a token and without calling Amazon.
HEALTH_PATH = "/health"

#: The environment variable that can hold the bearer token HTTP clients must send, instead of ``--token-file``.
AUTHORIZATION_ENVIRONMENT_VARIABLE = "AMAZON_ORDERS_MCP_TOKEN"

#: The ``site`` parameter every tool that reads Amazon takes.
SITE = Field(description="The Amazon site to read, e.g. `amazon.de`, since each site has its own orders. By default, "
                         "the user's default site (see `check_session`).")

T = TypeVar("T")


class SiteSessionStatus(BaseModel):
    """
    The user's session status on an Amazon site.
    """

    signed_in: Optional[bool] = Field(description="Whether Amazon still accepts the user's saved session for the "
                                                  "site. If Amazon answers the check with an error page, a saved "
                                                  "session is assumed to still work. Null if the session couldn't be "
                                                  "checked at all.")
    site: str = Field(description="The Amazon site, as `site` accepts it.")
    default: bool = Field(description="Whether it's the user's default site, which tools read when they aren't "
                                      "given `site`.")
    sign_in: Optional[str] = Field(default=None, description="What to do to sign in, if the user isn't.")
    error: Optional[str] = Field(default=None, description="Why the session couldn't be checked, if it couldn't. "
                                                           "`signed_in` is null then.")


class SessionStatus(SiteSessionStatus):
    """
    The user's Amazon session status, returned by the ``check_session`` and ``sign_in`` tools: for the site asked
    about, or else the default site, and for every site checked.
    """

    sites: List[SiteSessionStatus] = Field(description="Every site checked: the one asked about, or else the default "
                                                       "site and every other site with a saved session.")


class GiftCardBalance(BaseModel):
    """
    The user's Amazon Gift Card balance, returned by the ``get_gift_card_balance`` tool.
    """

    balance: float = Field(description="The current Gift Card balance, in the currency of the Amazon site read.")


class ConfigValues(BaseModel):
    """
    The settings that can be changed, returned by the ``get_config`` tool.
    """

    preferences: Dict[str, Any] = Field(description="The user's choices, changed only when they ask. A null value "
                                                    "means the default.")
    tuning: Dict[str, Any] = Field(description="Settings you may adjust yourself when calls keep failing. A null "
                                               "value means the default.")


class ConfigValue(BaseModel):
    """
    A setting's new value, returned by the ``update_config`` tool.
    """

    key: str = Field(description="The setting.")
    value: Any = Field(default=None, description="The value now in effect. A null value means the default.")
    saved: bool = Field(description="Whether the change was saved, or only made until the server restarts.")


def create_server(saved_session: Optional[SavedSession] = None) -> MCPServer:
    """
    Create the MCP server, with a tool for each ``amazon-orders`` read, and one to sign in.

    :param saved_session: The session the tools use, by default the one the ``amazon-orders`` CLI saved.
    :return: The server.
    """
    session = saved_session or SavedSession()
    server = MCPServer(name="amazon-orders", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING")

    async def run(call: Callable[[AmazonSession], T],
                  site: Optional[str]) -> T:
        try:
            return await anyio.to_thread.run_sync(session.run, call, site)
        except AmazonOrdersAuthError as e:
            logger.debug(f"Tool call failed, the user isn't signed in: {e}")

            advice = await anyio.to_thread.run_sync(_sign_in_advice, session, site)
            raise ToolError(f"{e} The user isn't signed in to Amazon, or their session expired. {advice}, then try "
                            "again.") from e
        except AmazonOrdersError as e:
            logger.debug(f"Tool call failed: {e}")

            raise ToolError(_describe_error(e)) from e

    @server.tool(annotations=SIGN_IN,
                 description="Sign the user in to Amazon, when they ask you to or after a tool says they aren't "
                             "signed in. Opens a browser window on the user's computer, where they sign in on "
                             "Amazon's own page, so their credentials are never seen. Wait for them to finish, "
                             f"which they have {SIGN_IN_TIMEOUT} seconds to do. If it says there's no browser to "
                             "sign in with, ask the user which to use, then call this again with `browser`.")
    async def sign_in(
            ctx: Context,
            browser: Annotated[Optional[str], Field(
                description="Only when there's no browser to sign in with: the one the user chose, either one "
                            "offered to download for them or a browser they already have installed. It's saved, so "
                            "they're only asked once.")] = None,
            site: Annotated[Optional[str], Field(
                description="The Amazon site to sign in to, e.g. `amazon.de`, since each site has its own sign-in. "
                            "By default, the user's default site.")] = None
    ) -> SessionStatus:
        failure: Optional[AmazonOrdersError] = None
        try:
            site = await anyio.to_thread.run_sync(session.site, site)
        except AmazonOrdersError as e:
            raise ToolError(_describe_error(e)) from e

        if not has_display():
            logger.debug("Tool call failed, there's no display to open a browser window on")

            command = HEADLESS_SIGN_IN_COMMAND
            if site != await anyio.to_thread.run_sync(session.site):
                command = command.replace("amazon-orders ", f"amazon-orders --domain {site} ", 1)
            raise ToolError("There's no display where the server runs, so it can't open a browser window for the "
                            f"user to sign in. Instead, they can sign in there with `{command}`, using the server's "
                            "config directory (for a server in a container, from a one-off container on the same "
                            "volume), and enter their credentials, or set them in that command's environment: "
                            f"{AUTOMATED_SIGN_IN_DOCS_URL}. The server uses the new session on its next call.")

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_report_signing_in, ctx, session, site)
            try:
                await anyio.to_thread.run_sync(partial(session.sign_in, browser=browser, site=site))
            except AmazonOrdersError as e:
                failure = e
            task_group.cancel_scope.cancel()

        if isinstance(failure, AmazonOrdersBrowserNotInstalledError):
            logger.debug(f"Tool call failed, there's no browser to sign in with: {failure}")

            raise ToolError("There's no browser set up to sign the user in with. Ask them which to use: "
                            f"{_describe_browser_choices(failure)}. Then call `sign_in` again with `browser` set to "
                            "their choice.") from failure
        elif failure is not None:
            logger.debug(f"Tool call failed: {failure}")

            raise ToolError(_describe_error(failure)) from failure

        signed_in = await anyio.to_thread.run_sync(_site_session_status, session, site, True)

        return SessionStatus(**signed_in.model_dump(), sites=[signed_in])

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_order_history"))
    async def get_order_history(
            ctx: Context,
            year: Annotated[Optional[int], parameter_field("get_order_history", "year")] = None,
            time_filter: Annotated[Optional[str], parameter_field("get_order_history", "time_filter")] = None,
            order_filter: Annotated[Optional[str], parameter_field("get_order_history", "order_filter")] = None,
            family_member_id: Annotated[Optional[str],
                                        parameter_field("get_order_history", "family_member_id")] = None,
            start_index: Annotated[Optional[int], parameter_field("get_order_history", "start_index")] = None,
            full_details: Annotated[bool, parameter_field("get_order_history", "full_details")] = False,
            limit: Annotated[Optional[int], parameter_field("get_order_history", "limit")] = DEFAULT_LIMIT,
            cursor: Annotated[Optional[Dict[str, Any]], parameter_field("get_order_history", "cursor")] = None,
            site: Annotated[Optional[str], SITE] = None
    ) -> OrderHistoryPage:
        progress = _progress_reporter(ctx, "Orders")

        def get(amazon_session: AmazonSession) -> PagedResults[Order]:
            return AmazonOrders(amazon_session).get_order_history_page(year=year,
                                                                       start_index=start_index,
                                                                       full_details=full_details,
                                                                       time_filter=time_filter,
                                                                       order_filter=order_filter,
                                                                       family_member_id=family_member_id,
                                                                       limit=limit,
                                                                       cursor=cursor,
                                                                       progress_with_total=progress)

        page = await run(get, site)

        return OrderHistoryPage.model_validate(page.to_dict(exclude_none=True))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_order"))
    async def get_order(
            order_id: Annotated[str, parameter_field("get_order", "order_id")],
            site: Annotated[Optional[str], SITE] = None
    ) -> OrderDetails:
        def get(amazon_session: AmazonSession) -> Order:
            return AmazonOrders(amazon_session).get_order(order_id)

        order = await run(get, site)

        return OrderDetails.model_validate(order.to_dict(exclude_none=True))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_tracking"))
    async def get_tracking(
            order_id: Annotated[str, parameter_field("get_tracking", "order_id")],
            shipment_id: Annotated[str, parameter_field("get_tracking", "shipment_id")],
            site: Annotated[Optional[str], SITE] = None
    ) -> TrackingDetails:
        def get(amazon_session: AmazonSession) -> Tracking:
            return AmazonOrders(amazon_session).get_tracking(order_id, shipment_id)

        tracking = await run(get, site)

        return TrackingDetails.model_validate(tracking.to_dict(exclude_none=True))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_invoice_pdfs"))
    async def get_invoice_pdfs(
            order_id: Annotated[str, parameter_field("get_invoice_pdfs", "order_id")],
            site: Annotated[Optional[str], SITE] = None
    ) -> InvoicePdfList:
        def get(amazon_session: AmazonSession) -> List[InvoicePdf]:
            return AmazonOrders(amazon_session).get_invoice_pdfs(order_id)

        invoice_pdfs = await run(get, site)

        return InvoicePdfList.model_validate(
            {"items": [invoice_pdf.to_dict(exclude_none=True) for invoice_pdf in invoice_pdfs]})

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_transactions"))
    async def get_transactions(
            ctx: Context,
            days: Annotated[int, parameter_field("get_transactions", "days")] = 365,
            order_id: Annotated[Optional[str], parameter_field("get_transactions", "order_id")] = None,
            limit: Annotated[Optional[int], parameter_field("get_transactions", "limit")] = DEFAULT_LIMIT,
            cursor: Annotated[Optional[Dict[str, Any]], parameter_field("get_transactions", "cursor")] = None,
            site: Annotated[Optional[str], SITE] = None
    ) -> TransactionsPage:
        progress = _progress_reporter(ctx, "Transactions")

        def get(amazon_session: AmazonSession) -> PagedResults[Transaction]:
            return AmazonTransactions(amazon_session).get_transactions_page(days=days,
                                                                            order_id=order_id,
                                                                            limit=limit,
                                                                            cursor=cursor,
                                                                            progress=progress)

        page = await run(get, site)

        return TransactionsPage.model_validate(page.to_dict(exclude_none=True))

    @server.tool(annotations=READ_ONLY,
                 description="Get the user's current Amazon Gift Card balance. For what was spent or added, and "
                             "when, use `get_gift_card_activity`.")
    async def get_gift_card_balance(
            site: Annotated[Optional[str], SITE] = None
    ) -> GiftCardBalance:
        def get(amazon_session: AmazonSession) -> float:
            return AmazonGiftCards(amazon_session).get_balance()

        return GiftCardBalance(balance=await run(get, site))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_gift_card_activity"))
    async def get_gift_card_activity(
            ctx: Context,
            days: Annotated[int, parameter_field("get_gift_card_activity", "days")] = 365,
            site: Annotated[Optional[str], SITE] = None
    ) -> GiftCardActivityList:
        progress = _progress_reporter(ctx, "Gift Card activity entries")

        def get(amazon_session: AmazonSession) -> List[GiftCardActivity]:
            return AmazonGiftCards(amazon_session).get_gift_card_activity(days=days, progress=progress)

        activities = await run(get, site)

        return GiftCardActivityList.model_validate(
            {"items": [activity.to_dict(exclude_none=True) for activity in activities]})

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_prime_payments"))
    async def get_prime_payments(
            site: Annotated[Optional[str], SITE] = None
    ) -> PrimePaymentList:
        def get(amazon_session: AmazonSession) -> List[PrimePayment]:
            return AmazonPrime(amazon_session).get_prime_payments()

        payments = await run(get, site)

        return PrimePaymentList.model_validate({"items": [payment.to_dict(exclude_none=True) for payment in payments]})

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_family_members"))
    async def get_family_members(
            site: Annotated[Optional[str], SITE] = None
    ) -> FamilyMemberList:
        def get(amazon_session: AmazonSession) -> List[FamilyMember]:
            return AmazonOrders(amazon_session).get_family_members()

        members = await run(get, site)

        return FamilyMemberList.model_validate({"items": [member.to_dict(exclude_none=True) for member in members]})

    @server.tool(annotations=READ_ONLY,
                 description="Get the settings that can be changed with `update_config`, and their values. "
                             f"What each one does: {CONFIG_DOCS_URL}")
    async def get_config() -> ConfigValues:
        return ConfigValues.model_validate(await anyio.to_thread.run_sync(session.get_config))

    @server.tool(annotations=UPDATE_CONFIG,
                 description="Change a setting. Change a preference, like the Amazon site (`domain`) or the browser "
                             "to sign in with (`browser`), only when the user asks. You may change a tuning setting "
                             "yourself when calls keep failing (e.g. a longer `request_timeout` after timeouts, or a "
                             "smaller `thread_pool_size` when Amazon throttles requests), and tell the user what you "
                             "changed. `warn_on_missing_required_field` returns partial data instead of an error, so "
                             "say so if you change it. A preference is saved, and tuning lasts until the server "
                             "restarts unless `save` is true, which you can offer if a change keeps helping. "
                             "`domain` is the default site, which tools read when they aren't given `site`, so to "
                             "read another site once, pass it as `site` instead. After changing `domain`, check with "
                             "`check_session`, since the user may need to sign in to the new default site. What each "
                             f"setting does: {CONFIG_DOCS_URL}")
    async def update_config(
            key: Annotated[str, Field(description="The setting, from `get_config`.")],
            value: Annotated[Optional[Union[str, int, float, bool]], Field(
                description="The new value. Null or an empty string sets it back to the default.")],
            save: Annotated[Optional[bool], Field(
                description="Whether to save the change. By default, a preference is saved and tuning isn't.")] = None
    ) -> ConfigValue:
        try:
            new_value, saved = await anyio.to_thread.run_sync(partial(session.update_config, key, value, save))
        except AmazonOrdersError as e:
            logger.debug(f"Tool call failed: {e}")

            raise ToolError(_describe_error(e)) from e

        return ConfigValue(key=key, value=new_value, saved=saved)

    @server.tool(annotations=READ_ONLY,
                 description="Check with Amazon whether the user's saved sessions still work: on `site`, or else on "
                             "their default site and every other Amazon site they have a saved session for. Also "
                             "says which site is the default.")
    async def check_session(
            site: Annotated[Optional[str], Field(
                description="The Amazon site to check, e.g. `amazon.de`. By default, the default site and every "
                            "other site with a saved session are checked.")] = None
    ) -> SessionStatus:
        return await anyio.to_thread.run_sync(_session_status, session, site)

    return server


def create_http_app(server: MCPServer,
                    host: str = DEFAULT_HOST,
                    json_response: bool = False,
                    token: Optional[str] = None) -> ASGIApp:
    """
    Create the app that serves the MCP server over Streamable HTTP, at ``/mcp``. It's stateless, since every client
    is served from the one saved session, and :data:`HEALTH_PATH` answers ``ok`` without calling Amazon.

    :param server: The server to serve.
    :param host: The address the app listens on. On localhost, only requests addressed to localhost are accepted, so
        another site can't reach the server through the user's browser.
    :param json_response: Whether to answer each request with JSON, instead of an event stream that also carries the
        call's progress.
    :param token: A bearer token every request but the health check must carry, if the server checks one.
    :return: The app.
    """
    app = server.streamable_http_app(stateless_http=True, json_response=json_response, host=host)
    app.add_route(HEALTH_PATH, _health, methods=["GET"], include_in_schema=False)
    if token is None:
        return app

    return BearerTokenMiddleware(app, token)


class BearerTokenMiddleware:
    """
    Requires ``Authorization: Bearer <token>`` on every HTTP request but the health check, and answers ``401`` to one
    without it.

    :param app: The app to protect.
    :param token: The bearer token.
    """

    def __init__(self,
                 app: ASGIApp,
                 token: str) -> None:
        self.app = app
        self._token = token.encode("utf-8")

    async def __call__(self,
                       scope: Scope,
                       receive: Receive,
                       send: Send) -> None:
        if scope["type"] == "websocket" and not self._authorized(Headers(scope=scope)):
            await WebSocketClose()(scope, receive, send)
            return
        if scope["type"] == "http" and scope["path"] != HEALTH_PATH and not self._authorized(Headers(scope=scope)):
            response = PlainTextResponse("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _authorized(self,
                    headers: Headers) -> bool:
        scheme, _, token = headers.get("authorization", "").partition(" ")

        return scheme.lower() == "bearer" and hmac.compare_digest(token.strip().encode("utf-8"), self._token)


async def _health(request: Request) -> Response:
    return PlainTextResponse("ok")


def _session_status(session: SavedSession,
                    site: Optional[str]) -> SessionStatus:
    try:
        sites = [session.site(site)] if site else session.sites()
    except AmazonOrdersError as e:
        raise ToolError(_describe_error(e)) from e

    statuses = [_site_session_status(session, checked_site) for checked_site in sites]

    return SessionStatus(**statuses[0].model_dump(), sites=statuses)


def _site_session_status(session: SavedSession,
                         site: str,
                         signed_in: Optional[bool] = None) -> SiteSessionStatus:
    """
    Get a site's session status, checking with Amazon whether it still works unless ``signed_in`` is known. A site
    that can't be checked is reported with its error, so it doesn't hide the other sites'.
    """
    default = site == session.site()
    try:
        if signed_in is None:
            try:
                session.run(_check_signed_in, site)
                signed_in = True
            except AmazonOrdersAuthError:
                signed_in = False
        base_url = session.base_url(site)
    except AmazonOrdersError as e:
        return SiteSessionStatus(signed_in=None, site=_site_base_url(session, site), default=default,
                                 error=f"Could not check the session on {site}. {_describe_error(e)}")

    return SiteSessionStatus(signed_in=signed_in, site=base_url, default=default,
                             sign_in=None if signed_in else f"{_sign_in_advice(session, site)}.")


def _site_base_url(session: SavedSession,
                   site: str) -> str:
    try:
        return session.base_url(site)
    except AmazonOrdersError:
        return site


def _check_signed_in(amazon_session: AmazonSession) -> None:
    """
    Check that Amazon still accepts the saved session, since its cookies outlive it. The Order history is requested
    and checked as ``amazon-orders`` checks every response, so a session Amazon no longer accepts raises
    :class:`~amazonorders.exception.AmazonOrdersAuthRedirectError`. A response that isn't ``ok`` says nothing about
    the session, so it's still trusted then.
    """
    logger.debug("Checking that Amazon still accepts the saved session ...")

    history_response = amazon_session.get(amazon_session.config.constants.ORDER_HISTORY_URL)
    if history_response.response.ok:
        amazon_session.check_response(history_response)


def _describe_browser_choices(error: AmazonOrdersBrowserNotInstalledError) -> str:
    downloadable = (error.meta or {}).get("browser_types") or []
    if not downloadable:
        return "a browser they already have installed"

    return (f"{', '.join(f'`{name}`' for name in downloadable)} (downloaded for them, with `{downloadable[0]}` being "
            "the simplest), or a browser they already have installed")


def _sign_in_advice(session: SavedSession,
                    site: Optional[str]) -> str:
    """
    Say how the user can sign in to a site: with the ``sign_in`` tool, or in a terminal.
    """
    site = session.site(site)
    if site == session.site():
        return f"Offer to sign the user in with `sign_in`, or they can run `{SIGN_IN_COMMAND}` in a terminal"

    return (f"Offer to sign the user in with `sign_in` and `site` set to `{site}`, or they can run "
            f"`amazon-orders --domain {site} login --browser` in a terminal")


async def _report_signing_in(ctx: Context,
                             session: SavedSession,
                             site: str) -> None:
    """
    Report to the client every few seconds what the sign-in is doing, which is how a client that resets its timeout
    on progress knows the call is alive. Runs until it's cancelled.
    """
    waited = 0
    while True:
        await ctx.report_progress(waited, None, session.sign_in_status(site))
        await anyio.sleep(SIGN_IN_REPORT_INTERVAL)
        waited += SIGN_IN_REPORT_INTERVAL


class ProgressReporter(Protocol):
    """
    A callback that fits both ``progress`` and ``progress_with_total``, since the total is optional.
    """

    def __call__(self,
                 count: int,
                 total: Optional[int] = None) -> None: ...


def _progress_reporter(ctx: Context,
                       noun: str) -> ProgressReporter:
    """
    Build a ``progress`` or ``progress_with_total`` callback for an ``amazon-orders`` call, which reports to the
    client how many ``noun`` have been found so far, and how many are expected if a total is known. It's called on
    the worker thread the call runs on, so it hands the report back to the server's event loop.
    """
    def report_progress(count: int,
                        total: Optional[int] = None) -> None:
        optional_total = f" of {total}" if total is not None else ""
        anyio.from_thread.run(ctx.report_progress, count, total, f"{count}{optional_total} {noun} found so far ...")

    return report_progress


def _describe_error(error: AmazonOrdersError) -> str:
    if not error.meta:
        return str(error)

    return f"{error} Context: {json.dumps(error.meta, default=str)}"


def _parse_args(args: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="amazon-orders-mcp",
                                     description="An MCP server for Amazon order history, line items, and "
                                                 "transactions, built on amazon-orders.")
    parser.add_argument("--transport", choices=TRANSPORTS, default="stdio",
                        help="stdio (the default) for a client that runs the server, or streamable-http to serve "
                             "clients that connect to it over HTTP.")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"The address to listen on over HTTP (default: {DEFAULT_HOST}, which only this machine "
                             "can reach).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"The port to listen on over HTTP (default: {DEFAULT_PORT}).")
    parser.add_argument("--json-response", action="store_true",
                        help="Answer each HTTP request with JSON instead of an event stream, for a proxy that doesn't "
                             "stream. Progress isn't reported then.")
    parser.add_argument("--token-file",
                        help="A file holding a bearer token that every HTTP request must carry, or set "
                             f"{AUTHORIZATION_ENVIRONMENT_VARIABLE} instead. Required unless --host is a loopback "
                             "address; there, no token is checked by default.")
    parsed_args = parser.parse_args(args)
    parsed_args.token = _read_token(parser, parsed_args.token_file)

    if parsed_args.transport == "streamable-http" and parsed_args.host not in LOOPBACK_HOSTS and not parsed_args.token:
        parser.error(f"--host {parsed_args.host} can be reached from other machines, so set --token-file or "
                     f"{AUTHORIZATION_ENVIRONMENT_VARIABLE} too, or anyone who can reach it could use its tools.")

    return parsed_args


def _read_token(parser: argparse.ArgumentParser,
                token_file: Optional[str]) -> Optional[str]:
    environment_token = os.environ.get(AUTHORIZATION_ENVIRONMENT_VARIABLE, "").strip() or None
    if token_file is None:
        return environment_token
    if environment_token is not None:
        parser.error(f"Set either --token-file or {AUTHORIZATION_ENVIRONMENT_VARIABLE}, not both.")

    try:
        with open(token_file, "r", encoding="utf-8") as f:
            token = f.read().strip()
    except OSError as e:
        parser.error(f"Could not read --token-file: {e}")
    if not token:
        parser.error(f"--token-file {token_file} is empty.")

    return token


def main(args: Optional[List[str]] = None) -> None:
    """
    Run the MCP server, over stdio, or over Streamable HTTP with ``--transport streamable-http``.

    :param args: The command line arguments, by default the ones the server was started with.
    """
    parsed_args = _parse_args(args)
    server = create_server()
    if parsed_args.transport == "stdio":
        server.run()
        return

    app = create_http_app(server, host=parsed_args.host, json_response=parsed_args.json_response,
                          token=parsed_args.token)
    uvicorn.run(app, host=parsed_args.host, port=parsed_args.port, log_level=server.settings.log_level.lower())
