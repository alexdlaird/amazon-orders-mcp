__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import json
import logging
from functools import partial
from typing import Annotated, Any, Callable, Dict, List, Optional, Protocol, TypeVar, Union

import anyio
from amazonorders.entity.gift_card_activity import GiftCardActivity
from amazonorders.entity.order import Order
from amazonorders.entity.transaction import Transaction
from amazonorders.exception import AmazonOrdersAuthError, AmazonOrdersBrowserNotInstalledError, AmazonOrdersError
from amazonorders.gift_cards import AmazonGiftCards
from amazonorders.orders import AmazonOrders
from amazonorders.paging import PagedResults
from amazonorders.session import AmazonSession
from amazonorders.transactions import AmazonTransactions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from amazonorders_mcp import __version__
from amazonorders_mcp.schema import (
    GiftCardActivityList,
    OrderDetails,
    OrderHistoryPage,
    TransactionsPage,
    parameter_field,
    tool_description,
)
from amazonorders_mcp.session import SIGN_IN_COMMAND, SIGN_IN_TIMEOUT, SavedSession

logger = logging.getLogger(__name__)

#: What the server can do, given to the client when it connects.
INSTRUCTIONS = (
    "Reads the user's Amazon order history, transactions, and Gift Card activity. Every tool but `sign_in` and "
    "`update_config` is read-only. If a tool says the user isn't signed in, offer to sign them in with `sign_in`, and "
    f"call it only once they agree. If that can't work, they can run `{SIGN_IN_COMMAND}` in a terminal instead."
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

#: How many seconds pass between the reports that keep a client waiting on ``sign_in``.
SIGN_IN_REPORT_INTERVAL = 5

#: How many results a paged tool collects when the client doesn't say, so a bare call stays bounded.
DEFAULT_LIMIT = 25

T = TypeVar("T")


class SessionStatus(BaseModel):
    """
    The user's Amazon session status, returned by the ``check_session`` tool.
    """

    signed_in: bool = Field(description="Whether Amazon still accepts the user's saved session. If Amazon couldn't "
                                        "be reached to check, a saved session is assumed to still work.")
    site: str = Field(description="The Amazon site the server is set to read.")
    sign_in: Optional[str] = Field(default=None, description="What to do to sign in, if the user isn't.")


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

    async def run(call: Callable[[AmazonSession], T]) -> T:
        try:
            return await anyio.to_thread.run_sync(session.run, call)
        except AmazonOrdersAuthError as e:
            logger.debug(f"Tool call failed, the user isn't signed in: {e}")

            raise ToolError(f"{e} The user isn't signed in to Amazon, or their session expired. Offer to sign "
                            f"them in with `sign_in`, or they can run `{SIGN_IN_COMMAND}` in a terminal, then "
                            "try again.") from e
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
                            "they're only asked once.")] = None
    ) -> SessionStatus:
        failure: Optional[AmazonOrdersError] = None
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_report_signing_in, ctx, session)
            try:
                await anyio.to_thread.run_sync(partial(session.sign_in, browser=browser))
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

        return SessionStatus(signed_in=True, site=await anyio.to_thread.run_sync(session.base_url))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_order_history"))
    async def get_order_history(
            ctx: Context,
            year: Annotated[Optional[int], parameter_field("get_order_history", "year")] = None,
            time_filter: Annotated[Optional[str], parameter_field("get_order_history", "time_filter")] = None,
            start_index: Annotated[Optional[int], parameter_field("get_order_history", "start_index")] = None,
            full_details: Annotated[bool, parameter_field("get_order_history", "full_details")] = False,
            limit: Annotated[Optional[int], parameter_field("get_order_history", "limit")] = DEFAULT_LIMIT,
            cursor: Annotated[Optional[Dict[str, Any]], parameter_field("get_order_history", "cursor")] = None
    ) -> OrderHistoryPage:
        progress = _progress_reporter(ctx, "Orders")

        def get(amazon_session: AmazonSession) -> PagedResults[Order]:
            return AmazonOrders(amazon_session).get_order_history_page(year=year,
                                                                       start_index=start_index,
                                                                       full_details=full_details,
                                                                       time_filter=time_filter,
                                                                       limit=limit,
                                                                       cursor=cursor,
                                                                       progress_with_total=progress)

        page = await run(get)

        return OrderHistoryPage.model_validate(page.to_dict(exclude_none=True))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_order"))
    async def get_order(
            order_id: Annotated[str, parameter_field("get_order", "order_id")]
    ) -> OrderDetails:
        def get(amazon_session: AmazonSession) -> Order:
            return AmazonOrders(amazon_session).get_order(order_id)

        order = await run(get)

        return OrderDetails.model_validate(order.to_dict(exclude_none=True))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_transactions"))
    async def get_transactions(
            ctx: Context,
            days: Annotated[int, parameter_field("get_transactions", "days")] = 365,
            order_id: Annotated[Optional[str], parameter_field("get_transactions", "order_id")] = None,
            limit: Annotated[Optional[int], parameter_field("get_transactions", "limit")] = DEFAULT_LIMIT,
            cursor: Annotated[Optional[Dict[str, Any]], parameter_field("get_transactions", "cursor")] = None
    ) -> TransactionsPage:
        progress = _progress_reporter(ctx, "Transactions")

        def get(amazon_session: AmazonSession) -> PagedResults[Transaction]:
            return AmazonTransactions(amazon_session).get_transactions_page(days=days,
                                                                            order_id=order_id,
                                                                            limit=limit,
                                                                            cursor=cursor,
                                                                            progress=progress)

        page = await run(get)

        return TransactionsPage.model_validate(page.to_dict(exclude_none=True))

    @server.tool(annotations=READ_ONLY,
                 description=tool_description("get_gift_card_activity"))
    async def get_gift_card_activity(
            ctx: Context,
            days: Annotated[int, parameter_field("get_gift_card_activity", "days")] = 365
    ) -> GiftCardActivityList:
        progress = _progress_reporter(ctx, "Gift Card activity entries")

        def get(amazon_session: AmazonSession) -> List[GiftCardActivity]:
            return AmazonGiftCards(amazon_session).get_gift_card_activity(days=days, progress=progress)

        activities = await run(get)

        return GiftCardActivityList.model_validate(
            {"items": [activity.to_dict(exclude_none=True) for activity in activities]})

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
                             "restarts unless `save` is true, which you can offer if a change keeps helping. After "
                             "changing `domain`, check with `check_session`, since the user may need to sign in "
                             f"again. What each setting does: {CONFIG_DOCS_URL}")
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
                 description="Check with Amazon whether the user's saved session still works, and which Amazon "
                             "site the server is set to read.")
    async def check_session() -> SessionStatus:
        return await anyio.to_thread.run_sync(_session_status, session)

    return server


def _session_status(session: SavedSession) -> SessionStatus:
    try:
        session.run(_check_signed_in)
    except AmazonOrdersAuthError:
        return SessionStatus(signed_in=False, site=session.base_url(),
                             sign_in=f"Offer to sign the user in with `sign_in`, or they can run "
                                     f"`{SIGN_IN_COMMAND}` in a terminal.")
    except AmazonOrdersError as e:
        raise ToolError(_describe_error(e)) from e

    return SessionStatus(signed_in=True, site=session.base_url())


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


async def _report_signing_in(ctx: Context,
                             session: SavedSession) -> None:
    """
    Report to the client every few seconds what the sign-in is doing, which is how a client that resets its timeout
    on progress knows the call is alive. Runs until it's cancelled.
    """
    waited = 0
    while True:
        await ctx.report_progress(waited, None, session.sign_in_status)
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


def main() -> None:
    """
    Run the MCP server over stdio.
    """
    create_server().run()
