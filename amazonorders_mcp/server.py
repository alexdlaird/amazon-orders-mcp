__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import json
import logging
from typing import Annotated, Any, Callable, Dict, List, Optional, TypeVar

import anyio
from amazonorders.entity.gift_card_activity import GiftCardActivity
from amazonorders.entity.order import Order
from amazonorders.entity.transaction import Transaction
from amazonorders.exception import AmazonOrdersAuthError, AmazonOrdersError
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
from amazonorders_mcp.session import SIGN_IN_COMMAND, SavedSession

logger = logging.getLogger(__name__)

#: What the server can do, given to the client when it connects.
INSTRUCTIONS = (
    "Reads the user's Amazon order history, transactions, and Gift Card activity. Every tool is read-only. Signing "
    f"in happens outside this server: if a tool says the user isn't signed in, ask them to run `{SIGN_IN_COMMAND}` "
    "in a terminal, then try again."
)

#: The annotations every tool has, since none of them change anything on Amazon.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

#: How many results a paged tool collects when the client doesn't say, so a bare call stays bounded.
DEFAULT_LIMIT = 25

T = TypeVar("T")


class SessionStatus(BaseModel):
    """
    The user's Amazon session status, returned by the ``check_session`` tool.
    """

    signed_in: bool = Field(description="Whether Amazon still accepts the user's saved session. If Amazon couldn't "
                                        "be reached to check, a saved session is assumed to still work.")
    site: str = Field(description="The Amazon site the session is for.")
    sign_in: Optional[str] = Field(default=None, description="What to do to sign in, if the user isn't.")


def create_server(saved_session: Optional[SavedSession] = None) -> MCPServer:
    """
    Create the MCP server, with a tool for each read-only ``amazon-orders`` call.

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

            raise ToolError(f"{e} The user isn't signed in to Amazon, or their session expired. Ask them to run "
                            f"`{SIGN_IN_COMMAND}` in a terminal, then try again.") from e
        except AmazonOrdersError as e:
            logger.debug(f"Tool call failed: {e}")

            raise ToolError(_describe_error(e)) from e

    @server.tool(annotations=READ_ONLY,
                 description="Check with Amazon whether the user's saved session still works, and which Amazon "
                             "site it's for.")
    async def check_session() -> SessionStatus:
        return await anyio.to_thread.run_sync(_session_status, session)

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
                                                                       progress=progress)

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

    return server


def _session_status(session: SavedSession) -> SessionStatus:
    try:
        session.run(_check_signed_in)
    except AmazonOrdersAuthError:
        return SessionStatus(signed_in=False, site=session.base_url(),
                             sign_in=f"Ask the user to run `{SIGN_IN_COMMAND}` in a terminal.")
    except AmazonOrdersError as e:
        raise ToolError(_describe_error(e)) from e

    return SessionStatus(signed_in=True, site=session.base_url())


def _check_signed_in(amazon_session: AmazonSession) -> None:
    """
    Check that Amazon still accepts the saved session, since its cookies outlive it. The Order history is requested
    and checked as ``amazon-orders`` checks every response, so an expired session is logged out and raises
    :class:`~amazonorders.exception.AmazonOrdersAuthRedirectError`. A response that isn't ``ok`` says nothing about
    the session, so it's still trusted then.
    """
    logger.debug("Checking that Amazon still accepts the saved session ...")

    history_response = amazon_session.get(amazon_session.config.constants.ORDER_HISTORY_URL)
    if history_response.response.ok:
        amazon_session.check_response(history_response)


def _progress_reporter(ctx: Context,
                       noun: str) -> Callable[[int], None]:
    """
    Build a ``progress`` callback for an ``amazon-orders`` call, which reports to the client how many ``noun`` have
    been found so far. It's called on the worker thread the call runs on, so it hands the report back to the
    server's event loop.
    """
    def report_progress(count: int) -> None:
        anyio.from_thread.run(ctx.report_progress, count, None, f"{count} {noun} found so far ...")

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
