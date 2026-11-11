__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import argparse
import hmac
import json
import logging
import os
from datetime import date, timedelta
from functools import partial
from typing import Annotated, Any, Callable, Dict, List, Optional, Protocol, TypeVar, Union, cast

import anyio
import uvicorn
from amazonauth.util import has_display
from amazonorders.conf import AmazonOrdersConfig
from amazonorders.entity.family_member import FamilyMember
from amazonorders.entity.invoice import Invoice
from amazonorders.entity.order import Order
from amazonorders.entity.order_search_result import OrderSearchResult
from amazonorders.entity.prime_payment import PrimePayment
from amazonorders.entity.tracking import Tracking
from amazonorders.entity.transaction import Transaction
from amazonorders.exception import AmazonOrdersAuthError, AmazonOrdersBrowserNotInstalledError, AmazonOrdersError
from amazonorders.gift_cards import AmazonGiftCards, GiftCards
from amazonorders.orders import AmazonOrders
from amazonorders.paging import PagedResults
from amazonorders.prime import AmazonPrime
from amazonorders.session import AmazonSession
from amazonorders.transactions import AmazonTransactions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Icon, Tool, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from amazonorders_mcp import __version__
from amazonorders_mcp.oauth import (
    CONSENT_PATH,
    OAUTH_STATE_FILENAME,
    AuthorizationServerMetadataMiddleware,
    OwnerOAuthProvider,
)
from amazonorders_mcp.schema import (
    ChargeMatches,
    FamilyMemberList,
    GiftCardsPage,
    InvoiceList,
    OrderDetails,
    OrderSearchPage,
    OrdersPage,
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
    "Reads the user's Amazon order history, invoices, package tracking, transactions, Gift Card balance and "
    "activity, and Prime membership payments. The links from `get_invoices` change on every call and may only "
    "open in a browser signed in to the user's Amazon account, so give them to the user right away rather than "
    "keeping them. "
    "To find an order by what was bought, its number, or who it went to, use `search_orders` rather than paging "
    "through `get_orders`. "
    "For an account in an Amazon Household, `get_orders` returns the whole household's orders by default, and its "
    "`household_members` lists the members, like linked Teen accounts: pass a member's `id` as `family_member_id` "
    "for only their orders, or `self` for only the user's own. "
    "Each Amazon site, like amazon.com or amazon.de, has its own orders and its own sign-in. Tools read the user's "
    "default site unless given another as `site`, so pass the site an order was placed on, and `check_session` "
    "says which sites the user is signed in to. "
    "The user may also have signed in to someone else's own Amazon account, like a partner's. Tools read the user's "
    "default account unless given another as `account`, so pass one only when the user asks about that person, and "
    "`check_session` lists the accounts. "
    "For what the user spent, use `get_transactions`, the charges and refunds as they posted, rather than adding up "
    "order totals. Each site's amounts are in its own currency, so total each site separately. To find what a card "
    "or bank charge was, use `find_charge` with its amount and date. "
    "For what's arriving, get recent orders (`time_filter` `last30`) and read each shipment's `delivery_status`; "
    "`get_tracking` gives a shipment's carrier and tracking number. "
    "Answer with what the user asked for, summarized, and repeat addresses or payment details only when they ask. "
    "Every tool but `sign_in` and `update_config` is read-only. If a tool says the user isn't signed in, offer to "
    "sign them in with `sign_in`, and call it only once they agree. If that can't work, they can run "
    f"`{SIGN_IN_COMMAND}` in a terminal instead."
)

#: The ``review_spending`` prompt.
SPENDING_PROMPT = (
    "Review what I've spent on Amazon over a period I give you, and ask me for one if I haven't. Use "
    "`get_transactions` for what was charged and refunded, and the Orders behind those charges for what was bought, "
    "then group my spending by what it was for (e.g. groceries, household, electronics). Total each Amazon site "
    "separately, since each is in its own currency. Point out anything that stands out, like a large charge, a "
    "repeat purchase, or a recurring one, and only read as far back as the period needs."
)

#: The ``find_returns`` prompt.
RETURNS_PROMPT = (
    "Find the Items in my recent Amazon orders that I can still return. Use `get_orders` with `full_details` for my "
    "recent orders, and list the Items whose `return_eligible_date` hasn't passed, the soonest to close first, each "
    "with its Order number and the date its return window closes."
)

#: The ``track_deliveries`` prompt.
DELIVERIES_PROMPT = (
    "Tell me what I have arriving from Amazon. Use `get_orders` with `time_filter` `last30` for my recent orders, "
    "and list each shipment that hasn't been delivered yet, the soonest to arrive first, with its `delivery_status` "
    "and what's in it. For any I ask about, `get_tracking` gives its carrier and tracking number."
)

#: The ``explain_charge`` prompt.
CHARGE_PROMPT = (
    "Help me work out what an Amazon charge on my card or bank statement was. Ask me for its amount and date, and "
    "the card's last four digits if I know them, then use `find_charge` and tell me what was bought and in which "
    "Order. If more than one Transaction matches, show me each so I can tell which it is."
)

#: How many days either side of a charge's date ``find_charge`` matches Transactions by default, since Amazon
#: completes a charge on a different day than a statement can show it.
CHARGE_WINDOW_DAYS = 3

#: What ``get_invoices`` adds to links it returns, since not every client applies ``INSTRUCTIONS``, but each reads the
#: tool's result.
INVOICE_LINKS_NOTE = ("These links change on every call and may only open in a browser signed in to the user's "
                      "Amazon account, so give them to the user now rather than keeping them.")

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

#: How many results a paged tool collects when the client doesn't say, so a bare call stays bounded: one page of
#: Order history.
DEFAULT_LIMIT = 10

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

#: The path of the server's icon over HTTP, for clients that show a site's icon, served without a token.
FAVICON_PATH = "/favicon.ico"

#: The paths served over HTTP without a token.
PUBLIC_PATHS = (HEALTH_PATH, FAVICON_PATH)

#: The environment variable that can hold the bearer token HTTP clients must send, instead of ``--token-file``.
AUTHORIZATION_ENVIRONMENT_VARIABLE = "AMAZON_ORDERS_MCP_TOKEN"

#: The environment variable that can hold the server's public URL, instead of ``--public-url``.
PUBLIC_URL_ENVIRONMENT_VARIABLE = "AMAZON_ORDERS_MCP_PUBLIC_URL"

#: The server's icon, for clients that show one.
ICON_URL = "https://raw.githubusercontent.com/alexdlaird/amazon-orders-mcp/main/mcpb/icon.png"

#: The ``site`` parameter every tool that reads Amazon takes.
SITE = Field(description="The Amazon site to read, e.g. `amazon.de`, since each site has its own orders. By default, "
                         "the user's default site (see `check_session`).")

#: The ``account`` parameter every tool that reads Amazon takes.
ACCOUNT = Field(description="Another Amazon account to read, e.g. `spouse`, only when the user asks about that "
                            "person (see `check_session`).")

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
    account: str = Field(description="The Amazon account checked, as `account` accepts it.")
    accounts: List[str] = Field(description="Every Amazon account with a saved session, the default one first.")


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


def create_server(saved_session: Optional[SavedSession] = None,
                  *,
                  additional_instructions: str = "",
                  oauth_provider: Optional[OwnerOAuthProvider] = None) -> MCPServer:
    """
    Create the MCP server, with a tool for each ``amazon-orders`` read, and one to sign in.

    :param saved_session: The session the tools use, by default the one the ``amazon-orders`` CLI saved.
    :param additional_instructions: Instructions appended to the defaults when composing the server with other tools.
    :param oauth_provider: The authorization server that signs clients in over HTTP, if they sign in with OAuth.
    :return: The server.
    """
    session = saved_session or SavedSession()
    additional_instructions = additional_instructions.strip()
    instructions = INSTRUCTIONS + (f" {additional_instructions}" if additional_instructions else "")
    server = AmazonOrdersServer(name="amazon-orders", instructions=instructions, version=__version__,
                                icons=[Icon(src=ICON_URL, mime_type="image/png", sizes=["512x512"])],
                                log_level="WARNING", auth_server_provider=oauth_provider,
                                auth=oauth_provider.auth_settings() if oauth_provider else None)
    if oauth_provider:
        server.custom_route(CONSENT_PATH, methods=["GET", "POST"], include_in_schema=False)(oauth_provider.consent)

    async def run(call: Callable[[AmazonSession], T],
                  site: Optional[str],
                  account: Optional[str]) -> T:
        try:
            return await anyio.to_thread.run_sync(session.run, call, site, account)
        except AmazonOrdersAuthError as e:
            logger.debug(f"Tool call failed, the user isn't signed in: {e}")

            advice = await anyio.to_thread.run_sync(_sign_in_advice, session, site, account)
            raise ToolError(f"{e} The user isn't signed in to Amazon, or their session expired. {advice}, then try "
                            "again.") from e
        except AmazonOrdersError as e:
            logger.debug(f"Tool call failed: {e}")

            raise ToolError(_describe_error(e)) from e

    @server.tool(annotations=READ_ONLY,
                 description="Find the Amazon Transactions behind a card or bank charge, by its amount and date, each "
                             "with its Order and the Items in it. A Transaction matches when its amount equals "
                             "`amount` and it completed within `window_days` of `charge_date`, since Amazon can "
                             "complete a charge on a different day than a statement shows it.")
    async def find_charge(
            ctx: Context,
            amount: Annotated[float, Field(
                description="The charge or refund's amount, e.g. `12.99`. Its sign is ignored, since statements and "
                            "Amazon show charges and refunds with different signs.")],
            charge_date: Annotated[date, Field(description="The date the charge shows, e.g. `2026-11-10`.")],
            last_four: Annotated[Optional[str], Field(
                description="The last four digits of the card it was charged to, to match only that card's "
                            "Transactions.")] = None,
            window_days: Annotated[int, Field(
                description="How many days either side of `charge_date` a Transaction can have completed.")
            ] = CHARGE_WINDOW_DAYS,
            site: Annotated[Optional[str], SITE] = None,
            account: Annotated[Optional[str], ACCOUNT] = None
    ) -> ChargeMatches:
        progress = _progress_reporter(ctx, "Transactions")

        def find(amazon_session: AmazonSession) -> List[Dict[str, Any]]:
            earliest = charge_date - timedelta(days=window_days)
            transactions = AmazonTransactions(amazon_session).get_transactions(
                days=max((date.today() - earliest).days + 1, 1), progress=progress)

            orders: Dict[str, Order] = {}
            matches = []
            for transaction in transactions:
                if not _matches_charge(transaction, amount, charge_date, window_days, last_four):
                    continue
                match = transaction.to_dict(exclude_none=True)
                if transaction.order_number:
                    if transaction.order_number not in orders:
                        orders[transaction.order_number] = AmazonOrders(amazon_session).get_order(
                            transaction.order_number)
                    match["order"] = orders[transaction.order_number].to_dict(exclude_none=True)
                matches.append(match)

            return matches

        matches = await run(find, site, account)

        result: Dict[str, Any] = {"items": matches}
        if not matches:
            card = f" on the card ending in {last_four}" if last_four else ""
            result["note"] = (f"No Transaction of {abs(amount):.2f} completed within {window_days} days of "
                              f"{charge_date.isoformat()}{card}. A larger `window_days`, or another `site`, may "
                              "find it.")
            other_accounts = await anyio.to_thread.run_sync(_other_accounts, session, account)
            if other_accounts:
                result["note"] += (f" So may another of the user's accounts, if the card is shared: "
                                   f"{', '.join(f'`{name}`' for name in other_accounts)}, as `account`.")
        return ChargeMatches.model_validate(result)

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
                            "By default, the user's default site.")] = None,
            account: Annotated[Optional[str], Field(
                description="The Amazon account to sign in to, by a name for it, e.g. `spouse`, to add someone "
                            "else's own account, like a partner's. By default, the user's default account.")] = None
    ) -> SessionStatus:
        failure: Optional[AmazonOrdersError] = None
        try:
            site = await anyio.to_thread.run_sync(session.site, site)
            account = await anyio.to_thread.run_sync(session.account, account)
        except AmazonOrdersError as e:
            raise ToolError(_describe_error(e)) from e

        if _is_remote_request(ctx):
            logger.debug("Tool call failed, the user is connected remotely, so a browser window wouldn't reach them")

            command = await anyio.to_thread.run_sync(
                _terminal_command, session, SIGN_IN_COMMAND if has_display() else HEADLESS_SIGN_IN_COMMAND, site,
                account)
            raise ToolError("The user is connected to the server remotely, so a browser window where it runs "
                            f"wouldn't reach them. When they're at that machine, they can sign in there with "
                            f"`{command}`, using the server's config directory: {AUTOMATED_SIGN_IN_DOCS_URL}. The "
                            "server uses the new session on its next call.")

        if not has_display():
            logger.debug("Tool call failed, there's no display to open a browser window on")

            command = await anyio.to_thread.run_sync(_terminal_command, session, HEADLESS_SIGN_IN_COMMAND, site,
                                                     account)
            raise ToolError("There's no display where the server runs, so it can't open a browser window for the "
                            f"user to sign in. They can sign in there with `{command}`, using the server's config "
                            f"directory: {AUTOMATED_SIGN_IN_DOCS_URL}. The server uses the new session on its next "
                            "call.")

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_report_signing_in, ctx, session, site, account)
            try:
                await anyio.to_thread.run_sync(partial(session.sign_in, browser=browser, site=site, account=account))
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

        signed_in = await anyio.to_thread.run_sync(_site_session_status, session, site, account, True)
        accounts = await anyio.to_thread.run_sync(session.accounts)

        return SessionStatus(**signed_in.model_dump(), sites=[signed_in], account=account, accounts=accounts)

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_orders"))
    async def get_orders(
            ctx: Context,
            year: Annotated[Optional[int], parameter_field("get_orders", "year")] = None,
            time_filter: Annotated[Optional[str], parameter_field("get_orders", "time_filter")] = None,
            order_filter: Annotated[Optional[str], parameter_field("get_orders", "order_filter")] = None,
            family_member_id: Annotated[Optional[str], parameter_field("get_orders", "family_member_id")] = None,
            start_index: Annotated[Optional[int], parameter_field("get_orders", "start_index")] = None,
            full_details: Annotated[bool, parameter_field("get_orders", "full_details")] = False,
            limit: Annotated[Optional[int], parameter_field("get_orders", "limit")] = DEFAULT_LIMIT,
            cursor: Annotated[Optional[Dict[str, Any]], parameter_field("get_orders", "cursor")] = None,
            site: Annotated[Optional[str], SITE] = None,
            account: Annotated[Optional[str], ACCOUNT] = None
    ) -> OrdersPage:
        progress = _progress_reporter(ctx, "Orders")

        def get(amazon_session: AmazonSession) -> PagedResults[Order]:
            return AmazonOrders(amazon_session).get_orders_page(year=year,
                                                                start_index=start_index,
                                                                full_details=full_details,
                                                                time_filter=time_filter,
                                                                order_filter=order_filter,
                                                                family_member_id=family_member_id,
                                                                limit=limit,
                                                                cursor=cursor,
                                                                progress_with_total=progress)

        page = await run(get, site, account)

        result = page.to_dict(exclude_none=True)
        if not result.get("items") and cursor is None:
            note = _empty_orders_note(year, time_filter, order_filter)
            if note:
                result["note"] = note
        return OrdersPage.model_validate(result)

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("search_orders"))
    async def search_orders(
            ctx: Context,
            query: Annotated[str, parameter_field("search_orders", "query")],
            limit: Annotated[Optional[int], parameter_field("search_orders", "limit")] = DEFAULT_LIMIT,
            cursor: Annotated[Optional[Dict[str, Any]], parameter_field("search_orders", "cursor")] = None,
            site: Annotated[Optional[str], SITE] = None,
            account: Annotated[Optional[str], ACCOUNT] = None
    ) -> OrderSearchPage:
        progress = _progress_reporter(ctx, "matching Items")

        def get(amazon_session: AmazonSession) -> PagedResults[OrderSearchResult]:
            return AmazonOrders(amazon_session).search_orders_page(query=query,
                                                                   limit=limit,
                                                                   cursor=cursor,
                                                                   progress=progress)

        page = await run(get, site, account)

        return OrderSearchPage.model_validate(page.to_dict(exclude_none=True))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_order"))
    async def get_order(
            order_id: Annotated[str, parameter_field("get_order", "order_id")],
            site: Annotated[Optional[str], SITE] = None,
            account: Annotated[Optional[str], ACCOUNT] = None
    ) -> OrderDetails:
        def get(amazon_session: AmazonSession) -> Order:
            return AmazonOrders(amazon_session).get_order(order_id)

        order = await run(get, site, account)

        return OrderDetails.model_validate(order.to_dict(exclude_none=True))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_tracking"))
    async def get_tracking(
            order_id: Annotated[str, parameter_field("get_tracking", "order_id")],
            shipment_id: Annotated[str, parameter_field("get_tracking", "shipment_id")],
            site: Annotated[Optional[str], SITE] = None,
            account: Annotated[Optional[str], ACCOUNT] = None
    ) -> TrackingDetails:
        def get(amazon_session: AmazonSession) -> Tracking:
            return AmazonOrders(amazon_session).get_tracking(order_id, shipment_id)

        tracking = await run(get, site, account)

        return TrackingDetails.model_validate(tracking.to_dict(exclude_none=True))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_invoices"))
    async def get_invoices(
            order_id: Annotated[str, parameter_field("get_invoices", "order_id")],
            site: Annotated[Optional[str], SITE] = None,
            account: Annotated[Optional[str], ACCOUNT] = None
    ) -> InvoiceList:
        def get(amazon_session: AmazonSession) -> List[Invoice]:
            return AmazonOrders(amazon_session).get_invoices(order_id)

        invoices = await run(get, site, account)

        result: Dict[str, Any] = {"items": [invoice.to_dict(exclude_none=True) for invoice in invoices]}
        if invoices:
            result["note"] = INVOICE_LINKS_NOTE
        return InvoiceList.model_validate(result)

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_family_members"))
    async def get_family_members(
            site: Annotated[Optional[str], SITE] = None,
            account: Annotated[Optional[str], ACCOUNT] = None
    ) -> FamilyMemberList:
        def get(amazon_session: AmazonSession) -> List[FamilyMember]:
            return AmazonOrders(amazon_session).get_family_members()

        members = await run(get, site, account)

        return FamilyMemberList.model_validate({"items": [member.to_dict(exclude_none=True) for member in members]})

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_transactions"))
    async def get_transactions(
            ctx: Context,
            days: Annotated[int, parameter_field("get_transactions", "days")] = 365,
            order_id: Annotated[Optional[str], parameter_field("get_transactions", "order_id")] = None,
            limit: Annotated[Optional[int], parameter_field("get_transactions", "limit")] = DEFAULT_LIMIT,
            cursor: Annotated[Optional[Dict[str, Any]], parameter_field("get_transactions", "cursor")] = None,
            site: Annotated[Optional[str], SITE] = None,
            account: Annotated[Optional[str], ACCOUNT] = None
    ) -> TransactionsPage:
        progress = _progress_reporter(ctx, "Transactions")

        def get(amazon_session: AmazonSession) -> PagedResults[Transaction]:
            return AmazonTransactions(amazon_session).get_transactions_page(days=days,
                                                                            order_id=order_id,
                                                                            limit=limit,
                                                                            cursor=cursor,
                                                                            progress=progress)

        page = await run(get, site, account)

        return TransactionsPage.model_validate(page.to_dict(exclude_none=True))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_gift_cards"))
    async def get_gift_cards(
            ctx: Context,
            days: Annotated[int, parameter_field("get_gift_cards", "days")] = 365,
            limit: Annotated[Optional[int], parameter_field("get_gift_cards", "limit")] = DEFAULT_LIMIT,
            cursor: Annotated[Optional[Dict[str, Any]], parameter_field("get_gift_cards", "cursor")] = None,
            site: Annotated[Optional[str], SITE] = None,
            account: Annotated[Optional[str], ACCOUNT] = None
    ) -> GiftCardsPage:
        progress = _progress_reporter(ctx, "Gift Card activity entries")

        def get(amazon_session: AmazonSession) -> GiftCards:
            return AmazonGiftCards(amazon_session).get_gift_cards_page(days=days,
                                                                       limit=limit,
                                                                       cursor=cursor,
                                                                       progress=progress)

        page = await run(get, site, account)

        return GiftCardsPage.model_validate(page.to_dict(exclude_none=True))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_prime_payments"))
    async def get_prime_payments(
            site: Annotated[Optional[str], SITE] = None,
            account: Annotated[Optional[str], ACCOUNT] = None
    ) -> PrimePaymentList:
        def get(amazon_session: AmazonSession) -> List[PrimePayment]:
            return AmazonPrime(amazon_session).get_prime_payments()

        payments = await run(get, site, account)

        return PrimePaymentList.model_validate({"items": [payment.to_dict(exclude_none=True) for payment in payments]})

    @server.tool(annotations=READ_ONLY,
                 description="Check with Amazon whether the user's saved sessions still work: on `site`, or else on "
                             "their default site and every other Amazon site they have a saved session for. Also "
                             "says which site is the default, and lists the user's accounts.")
    async def check_session(
            site: Annotated[Optional[str], Field(
                description="The Amazon site to check, e.g. `amazon.de`. By default, the default site and every "
                            "other site with a saved session are checked.")] = None,
            account: Annotated[Optional[str], Field(
                description="The Amazon account to check, e.g. `spouse`. By default, the default account.")] = None
    ) -> SessionStatus:
        return await anyio.to_thread.run_sync(_session_status, session, site, account)

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
                             "`check_session`, since the user may need to sign in to the new default site. "
                             "`account` is the default account the same way, one from `check_session`. What each "
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

    @server.prompt(title="Review Amazon spending",
                   description="Review what you've spent on Amazon over a period, grouped by what it was for.")
    def review_spending() -> str:
        return SPENDING_PROMPT

    @server.prompt(title="Find returnable orders",
                   description="Find the Items in your recent Amazon orders you can still return.")
    def find_returns() -> str:
        return RETURNS_PROMPT

    @server.prompt(title="What's arriving",
                   description="See what you have arriving from Amazon, and when.")
    def track_deliveries() -> str:
        return DELIVERIES_PROMPT

    @server.prompt(title="Explain a charge",
                   description="Work out what an Amazon charge on your card or bank statement was.")
    def explain_charge() -> str:
        return CHARGE_PROMPT

    return server


class AmazonOrdersServer(MCPServer):
    """
    The MCP server, publishing tool schemas any client can read whole.
    """

    async def list_tools(self) -> List[Tool]:
        tools = await super().list_tools()
        for tool in tools:
            tool.input_schema = _inline_definitions(tool.input_schema)
            _drop_field_titles(tool.input_schema)
            if tool.output_schema:
                tool.output_schema = _inline_definitions(tool.output_schema)
                _drop_field_titles(tool.output_schema)
        return tools


def _matches_charge(transaction: Transaction,
                    amount: float,
                    charge_date: date,
                    window_days: int,
                    last_four: Optional[str]) -> bool:
    return (round(abs(transaction.grand_total), 2) == round(abs(amount), 2)
            and abs((transaction.completed_date - charge_date).days) <= window_days
            and (last_four is None or transaction.payment_method_last_4 == last_four))


def _inline_definitions(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Self-contain a tool schema: each ``$ref`` becomes the definition it names, and ``$defs`` goes. ChatGPT validates
    against ``$defs`` but doesn't show them to its model, which then guesses the nested shapes.

    :param schema: A pydantic-generated JSON schema.
    :return: The same schema with no ``$ref`` or ``$defs``.
    """
    return cast(Dict[str, Any], _resolve_references(schema, schema.get("$defs", {})))


def _resolve_references(node: Any,
                        definitions: Dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_resolve_references(item, definitions) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        siblings = {key: value for key, value in node.items() if key != "$ref"}
        return _resolve_references({**definitions[node["$ref"].removeprefix("#/$defs/")], **siblings}, definitions)
    return {key: ({name: _resolve_references(field, definitions) for name, field in value.items()}
                  if key == "properties" else _resolve_references(value, definitions))
            for key, value in node.items() if key != "$defs"}


def _drop_field_titles(schema: Any) -> None:
    """
    Drop the titles pydantic generates from field names ("Order Id" for ``order_id``): they restate the name and cost
    tokens in every tool listing.

    :param schema: A JSON schema, changed in place.
    """
    if isinstance(schema, dict):
        for name, field in (schema.get("properties") or {}).items():
            if isinstance(field, dict) and str(field.get("title", "")).replace(" ", "_").casefold() == name.casefold():
                del field["title"]
        for value in schema.values():
            _drop_field_titles(value)
    elif isinstance(schema, list):
        for value in schema:
            _drop_field_titles(value)


def _empty_orders_note(year: Optional[int],
                       time_filter: Optional[str],
                       order_filter: Optional[str]) -> Optional[str]:
    """
    Say where else to look when ``get_orders`` finds nothing: the default history is only this year's, and leaves out
    digital orders.

    :param year: The ``year`` the call was given.
    :param time_filter: The ``time_filter`` the call was given.
    :param order_filter: The ``order_filter`` the call was given.
    :return: Where else to look, if anywhere.
    """
    places = []
    if year is None and time_filter is None:
        places.append(f"No orders so far in {date.today().year}: pass `year` or `time_filter` for earlier ones.")
    if order_filter is None:
        places.append("Digital orders, like Kindle books, aren't in the default history: pass `order_filter` "
                      "`digital-orders` for those.")
    return " ".join(places) or None


def create_http_app(server: MCPServer,
                    host: str = DEFAULT_HOST,
                    json_response: bool = False,
                    token: Optional[str] = None,
                    oauth_provider: Optional[OwnerOAuthProvider] = None) -> ASGIApp:
    """
    Create the app that serves the MCP server over Streamable HTTP, at ``/mcp``. It's stateless, since every client
    is served from the one saved session, and :data:`HEALTH_PATH` answers ``ok`` without calling Amazon.

    :param server: The server to serve.
    :param host: The address the app listens on. On localhost, only requests addressed to localhost are accepted, so
        another site can't reach the server through the user's browser.
    :param json_response: Whether to answer each request with JSON, instead of an event stream that also carries the
        call's progress.
    :param token: A bearer token every request but the health check must carry, if the server checks one.
    :param oauth_provider: The authorization server that signs clients in, if they sign in with OAuth. It accepts
        ``token`` too, as a bearer token.
    :return: The app.
    """
    app = server.streamable_http_app(stateless_http=True, json_response=json_response, host=host)
    app.add_route(HEALTH_PATH, _health, methods=["GET"], include_in_schema=False)
    app.add_route(FAVICON_PATH, _favicon, methods=["GET"], include_in_schema=False)
    if oauth_provider is not None:
        protected_app: ASGIApp = AuthorizationServerMetadataMiddleware(app, oauth_provider)
    elif token is not None:
        protected_app = BearerTokenMiddleware(app, token)
    else:
        protected_app = app

    return ResponseHeadersMiddleware(protected_app, {"X-Muppets": "no strings attached"})


class ResponseHeadersMiddleware:
    """
    Adds headers to every HTTP response.

    :param app: The app.
    :param headers: The headers to add.
    """

    def __init__(self,
                 app: ASGIApp,
                 headers: Dict[str, str]) -> None:
        self.app = app
        self._headers = [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers.items()]

    async def __call__(self,
                       scope: Scope,
                       receive: Receive,
                       send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *self._headers]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BearerTokenMiddleware:
    """
    Requires ``Authorization: Bearer <token>`` on every HTTP request but those to :data:`PUBLIC_PATHS`, and answers
    ``401`` to one without it.

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
        if (scope["type"] == "http" and scope["path"] not in PUBLIC_PATHS
                and not self._authorized(Headers(scope=scope))):
            response = PlainTextResponse("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _authorized(self,
                    headers: Headers) -> bool:
        scheme, _, token = headers.get("authorization", "").partition(" ")

        return scheme.lower() == "bearer" and hmac.compare_digest(token.strip().encode("utf-8"), self._token)


def _is_remote_request(ctx: Context) -> bool:
    """
    Whether a request reached the server through a proxy or tunnel, so the user likely isn't at the machine it runs on.
    """
    headers = ctx.headers or {}

    return any(name in headers for name in ("x-forwarded-for", "forwarded"))


async def _favicon(request: Request) -> Response:
    return FileResponse(os.path.join(os.path.dirname(__file__), "favicon.ico"), media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


async def _health(request: Request) -> Response:
    return PlainTextResponse("ok")


def _session_status(session: SavedSession,
                    site: Optional[str],
                    account: Optional[str]) -> SessionStatus:
    try:
        account = session.account(account)
        sites = [session.site(site)] if site else session.sites(account)
    except AmazonOrdersError as e:
        raise ToolError(_describe_error(e)) from e

    statuses = [_site_session_status(session, checked_site, account) for checked_site in sites]

    return SessionStatus(**statuses[0].model_dump(), sites=statuses, account=account, accounts=session.accounts())


def _site_session_status(session: SavedSession,
                         site: str,
                         account: str,
                         signed_in: Optional[bool] = None) -> SiteSessionStatus:
    """
    Get a site's session status for an account, checking with Amazon whether it still works unless ``signed_in`` is
    known. A site that can't be checked is reported with its error, so it doesn't hide the other sites'.
    """
    default = site == session.site()
    try:
        if signed_in is None:
            try:
                signed_in = session.run(partial(AmazonSession.check_session, verify=True), site, account)
            except AmazonOrdersAuthError:
                signed_in = False
        base_url = session.base_url(site, account)
    except AmazonOrdersError as e:
        return SiteSessionStatus(signed_in=None, site=_site_base_url(session, site, account), default=default,
                                 error=f"Could not check the session on {site}. {_describe_error(e)}")

    return SiteSessionStatus(signed_in=signed_in, site=base_url, default=default,
                             sign_in=None if signed_in else f"{_sign_in_advice(session, site, account)}.")


def _site_base_url(session: SavedSession,
                   site: str,
                   account: str) -> str:
    try:
        return session.base_url(site, account)
    except AmazonOrdersError:
        return site


def _other_accounts(session: SavedSession,
                    account: Optional[str]) -> List[str]:
    account = session.account(account)

    return [name for name in session.accounts() if name != account]


def _describe_browser_choices(error: AmazonOrdersBrowserNotInstalledError) -> str:
    downloadable = (error.meta or {}).get("browser_types") or []
    if not downloadable:
        return "a browser they already have installed"

    return (f"{', '.join(f'`{name}`' for name in downloadable)} (downloaded for them, with `{downloadable[0]}` being "
            "the simplest), or a browser they already have installed")


def _sign_in_advice(session: SavedSession,
                    site: Optional[str],
                    account: Optional[str]) -> str:
    """
    Say how the user can sign in to a site and account: with the ``sign_in`` tool, or in a terminal.
    """
    site = session.site(site)
    account = session.account(account)
    arguments = []
    if site != session.site():
        arguments.append(f"`site` set to `{site}`")
    if account != session.account():
        arguments.append(f"`account` set to `{account}`")
    with_arguments = f" and {' and '.join(arguments)}" if arguments else ""

    return (f"Offer to sign the user in with `sign_in`{with_arguments}, or they can run "
            f"`{_terminal_command(session, SIGN_IN_COMMAND, site, account)}` in a terminal")


def _terminal_command(session: SavedSession,
                      command: str,
                      site: str,
                      account: str) -> str:
    """
    Make an ``amazon-orders`` command for a site and account other than the defaults.
    """
    options = ""
    if site != session.site():
        options += f"--domain {site} "
    if account != session.account():
        options += f"--account {account} "

    return command.replace("amazon-orders ", f"amazon-orders {options}", 1)


async def _report_signing_in(ctx: Context,
                             session: SavedSession,
                             site: str,
                             account: str) -> None:
    """
    Report to the client every few seconds what the sign-in is doing, which is how a client that resets its timeout
    on progress knows the call is alive. Runs until it's cancelled.
    """
    waited = 0
    while True:
        await ctx.report_progress(waited, None, session.sign_in_status(site, account))
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
                             f"clients that connect to it over HTTP, at /mcp. GET {HEALTH_PATH} answers ok, without "
                             "calling Amazon, for health checks.")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"The address to listen on over HTTP (default: {DEFAULT_HOST}, which only this machine "
                             "can reach). On a loopback address, only requests whose Host is a loopback address are "
                             "accepted, so a proxy on this machine must pass one.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"The port to listen on over HTTP (default: {DEFAULT_PORT}).")
    parser.add_argument("--json-response", action="store_true",
                        help="Answer each HTTP request with JSON instead of an event stream, for a proxy that doesn't "
                             "stream. Progress isn't reported then.")
    parser.add_argument("--token-file",
                        help="A file holding a bearer token that every HTTP request must carry, or set "
                             f"{AUTHORIZATION_ENVIRONMENT_VARIABLE} instead. Required unless --host is a loopback "
                             "address; there, no token is checked by default.")
    parser.add_argument("--public-url",
                        default=os.environ.get(PUBLIC_URL_ENVIRONMENT_VARIABLE, "").strip() or None,
                        help="The server's public HTTPS URL (e.g. a tunnel's, https://<name>.ngrok-free.dev), to sign "
                             "clients in with OAuth, each approved on a page that asks for the bearer token. Or set "
                             f"{PUBLIC_URL_ENVIRONMENT_VARIABLE} instead.")
    parsed_args = parser.parse_args(args)
    parsed_args.token = _read_token(parser, parsed_args.token_file)
    if parsed_args.public_url and parsed_args.transport != "streamable-http":
        parser.error("--public-url only applies to --transport streamable-http.")
    if parsed_args.public_url and not parsed_args.token:
        parser.error(f"--public-url needs --token-file or {AUTHORIZATION_ENVIRONMENT_VARIABLE} too, since clients "
                     "are approved with it.")

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
    saved_session = None
    oauth_provider = None
    if parsed_args.public_url:
        config = AmazonOrdersConfig()
        saved_session = SavedSession(config)
        oauth_provider = OwnerOAuthProvider(parsed_args.public_url, parsed_args.token,
                                            os.path.join(os.path.dirname(config.config_path), OAUTH_STATE_FILENAME))
    server = create_server(saved_session, oauth_provider=oauth_provider)
    if parsed_args.transport == "stdio":
        server.run()
        return

    app = create_http_app(server, host=parsed_args.host, json_response=parsed_args.json_response,
                          token=parsed_args.token, oauth_provider=oauth_provider)
    uvicorn.run(app, host=parsed_args.host, port=parsed_args.port, log_level=server.settings.log_level.lower())
