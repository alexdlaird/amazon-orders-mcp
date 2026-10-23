__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import os
from unittest.mock import ANY, patch

import responses
from amazonorders.exception import AmazonOrdersAuthRedirectError, AmazonOrdersError, AmazonOrdersUnavailableError
from amazonorders.paging import PagedResults
from mcp.client import Client

from amazonorders_mcp.schema import SCHEMA
from amazonorders_mcp.server import AUTOMATED_SIGN_IN_DOCS_URL, DEFAULT_LIMIT, create_server
from amazonorders_mcp.session import HEADLESS_SIGN_IN_COMMAND, SIGN_IN_COMMAND, SIGN_IN_TIMEOUT
from tests.unittestcase import UnitTestCase

#: What ``check_session`` says to do when the user isn't signed in to the default site.
SIGN_IN_ADVICE = f"Offer to sign the user in with `sign_in`, or they can run `{SIGN_IN_COMMAND}` in a terminal."


class TestServer(UnitTestCase):
    async def test_every_tool_is_read_only(self):
        # GIVEN
        server = create_server(self.saved_session)

        # WHEN
        async with Client(server) as client:
            tools = (await client.list_tools()).tools

        # THEN
        self.assertEqual({"check_session", "sign_in", "get_config", "update_config", "get_order_history", "get_order",
                          "search_orders", "get_family_members", "get_tracking", "get_invoice_pdfs",
                          "get_transactions", "get_gift_card_activity", "get_gift_card_balance", "get_prime_payments"},
                         {tool.name for tool in tools})
        for tool in tools:
            if tool.name in ("sign_in", "update_config"):
                self.assertFalse(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)
            else:
                self.assertTrue(tool.annotations.read_only_hint, f"{tool.name} isn't marked read-only")

    async def test_every_amazon_tool_takes_site(self):
        # GIVEN
        server = create_server(self.saved_session)

        # WHEN
        async with Client(server) as client:
            tools = (await client.list_tools()).tools

        # THEN
        for tool in tools:
            if tool.name not in ("get_config", "update_config"):
                self.assertIn("site", tool.input_schema["properties"], f"{tool.name} doesn't take site")
                self.assertNotIn("site", tool.input_schema.get("required", []))

    async def test_tools_publish_generated_schema(self):
        # GIVEN
        server = create_server(self.saved_session)

        # WHEN
        async with Client(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}

        # THEN
        self.assertEqual({"get_order_history", "get_family_members", "get_order", "search_orders", "get_tracking",
                          "get_invoice_pdfs", "get_transactions", "get_gift_card_activity", "get_prime_payments"},
                         set(SCHEMA["tools"]))
        for name, tool_schema in SCHEMA["tools"].items():
            tool = tools[name]
            self.assertTrue(tool_schema["output_schema"]["properties"], f"{name} has an empty output schema")
            self.assertEqual(tool_schema["description"], tool.description)
            self.assertEqual(tool_schema["output_schema"], tool.output_schema)
            for parameter, description in tool_schema["parameters"].items():
                self.assertEqual(description, tool.input_schema["properties"][parameter]["description"])

    async def test_check_session_without_saved_session(self):
        # WHEN
        result = await self.call_tool("check_session")

        # THEN
        self.assertFalse(result.is_error)
        signed_out = {"signed_in": False, "site": "https://www.amazon.com", "default": True,
                      "sign_in": SIGN_IN_ADVICE, "error": None}
        self.assertEqual({**signed_out, "sites": [signed_out]}, result.structured_content)

    async def test_check_session_with_saved_session(self):
        # GIVEN
        self.given_saved_session()
        self.given_page_exists(self.test_config.constants.ORDER_HISTORY_URL, "orders/order-history-2018-0.html")

        # WHEN
        result = await self.call_tool("check_session")

        # THEN
        self.assertFalse(result.is_error)
        signed_in = {"signed_in": True, "site": "https://www.amazon.com", "default": True, "sign_in": None,
                     "error": None}
        self.assertEqual({**signed_in, "sites": [signed_in]}, result.structured_content)

    async def test_check_session_with_expired_saved_session(self):
        # GIVEN
        self.given_saved_session()
        self.responses.add(responses.GET, self.test_config.constants.ORDER_HISTORY_URL, status=302,
                           headers={"Location": self.test_config.constants.SIGN_IN_URL})
        self.responses.add(responses.GET, self.test_config.constants.SIGN_IN_URL, status=200)

        # WHEN
        result = await self.call_tool("check_session")

        # THEN
        self.assertFalse(result.is_error)
        signed_out = {"signed_in": False, "site": "https://www.amazon.com", "default": True,
                      "sign_in": SIGN_IN_ADVICE, "error": None}
        self.assertEqual({**signed_out, "sites": [signed_out]}, result.structured_content)

    async def test_check_session_trusts_saved_session_when_amazon_unreachable(self):
        # GIVEN
        self.given_saved_session()
        history_response = self.responses.add(responses.GET, self.test_config.constants.ORDER_HISTORY_URL,
                                              status=503)

        # WHEN
        result = await self.call_tool("check_session")

        # THEN
        self.assertFalse(result.is_error)
        signed_in = {"signed_in": True, "site": "https://www.amazon.com", "default": True, "sign_in": None,
                     "error": None}
        self.assertEqual({**signed_in, "sites": [signed_in]}, result.structured_content)
        self.assertEqual(1, history_response.call_count)

    async def test_check_session_checks_every_site_with_saved_session(self):
        # GIVEN
        self.given_saved_session("amazon.com", "amazon.de")
        self.given_page_exists(self.test_config.constants.ORDER_HISTORY_URL, "orders/order-history-2018-0.html")
        self.responses.add(responses.GET, "https://www.amazon.de/your-orders/orders", status=302,
                           headers={"Location": "https://www.amazon.de/ap/signin"})
        self.responses.add(responses.GET, "https://www.amazon.de/ap/signin", status=200)

        # WHEN
        result = await self.call_tool("check_session")

        # THEN
        self.assertFalse(result.is_error)
        signed_in = {"signed_in": True, "site": "https://www.amazon.com", "default": True, "sign_in": None,
                     "error": None}
        signed_out = {"signed_in": False, "site": "https://www.amazon.de", "default": False,
                      "sign_in": "Offer to sign the user in with `sign_in` and `site` set to `amazon.de`, or they "
                                 "can run `amazon-orders --domain amazon.de login --browser` in a terminal.",
                      "error": None}
        self.assertEqual({**signed_in, "sites": [signed_in, signed_out]}, result.structured_content)

    async def test_check_session_on_site(self):
        # GIVEN
        self.given_saved_session("amazon.com", "amazon.de")
        self.given_page_exists("https://www.amazon.de/your-orders/orders", "orders/order-history-2018-0.html")

        # WHEN
        result = await self.call_tool("check_session", {"site": "amazon.de"})

        # THEN
        self.assertFalse(result.is_error)
        signed_in = {"signed_in": True, "site": "https://www.amazon.de", "default": False, "sign_in": None,
                     "error": None}
        self.assertEqual({**signed_in, "sites": [signed_in]}, result.structured_content)

    async def test_check_session_on_unknown_site(self):
        # WHEN
        result = await self.call_tool("check_session", {"site": "ebay.com"})

        # THEN
        self.assert_tool_error(result, "\"ebay.com\" isn't an Amazon site that can be read.")

    async def test_site_accepts_identifier_check_session_returns(self):
        # GIVEN
        self.given_saved_session("amazon.com", "amazon.de")
        self.given_page_exists(self.test_config.constants.ORDER_HISTORY_URL, "orders/order-history-2018-0.html")
        self.given_page_exists("https://www.amazon.de/your-orders/orders", "orders/order-history-2018-0.html")
        site = (await self.call_tool("check_session")).structured_content["sites"][1]["site"]

        # WHEN
        result = await self.call_tool("check_session", {"site": site})

        # THEN
        self.assertEqual("https://www.amazon.de", site)
        self.assertFalse(result.is_error)
        self.assertEqual([(site, True)],
                         [(status["site"], status["signed_in"]) for status in result.structured_content["sites"]])

    async def test_check_session_reports_site_amazon_could_not_be_checked_on(self):
        # GIVEN
        self.given_saved_session("amazon.com", "amazon.de")
        self.given_page_exists(self.test_config.constants.ORDER_HISTORY_URL, "orders/order-history-2018-0.html")

        # WHEN
        result = await self.call_tool("check_session")

        # THEN
        self.assertFalse(result.is_error)
        self.assertTrue(result.structured_content["signed_in"])
        de_status = result.structured_content["sites"][1]
        self.assertEqual(("https://www.amazon.de", None, False),
                         (de_status["site"], de_status["signed_in"], de_status["default"]))
        self.assertIn("Could not check the session on amazon.de.", de_status["error"])

    @patch("amazonorders_mcp.server.has_display", return_value=True)
    @patch("amazonorders_mcp.session.PlaywrightBrowserLogin")
    async def test_sign_in_on_site(self, mock_browser_login, mock_has_display):
        # WHEN
        result = await self.call_tool("sign_in", {"site": "amazon.de"})

        # THEN
        self.assertFalse(result.is_error)
        signed_in = {"signed_in": True, "site": "https://www.amazon.de", "default": False, "sign_in": None,
                     "error": None}
        self.assertEqual({**signed_in, "sites": [signed_in]}, result.structured_content)
        amazon_session = mock_browser_login.call_args.args[0]
        self.assertEqual("https://www.amazon.de", amazon_session.config.constants.BASE_URL)
        self.assertEqual(os.path.join(self.config_dir, "cookies.amazon.de.json"),
                         amazon_session.config.cookie_jar_path)
        mock_browser_login.return_value.login.assert_called_once_with(ANY)

    @patch("amazonorders_mcp.server.has_display", return_value=True)
    async def test_sign_in_on_unknown_site(self, mock_has_display):
        # WHEN
        result = await self.call_tool("sign_in", {"site": "ebay.com"})

        # THEN
        self.assert_tool_error(result, "\"ebay.com\" isn't an Amazon site that can be read.")

    @patch("amazonorders_mcp.server.has_display", return_value=True)
    @patch("amazonorders_mcp.session.PlaywrightBrowserLogin")
    async def test_sign_in_opens_browser_window(self, mock_browser_login, mock_has_display):
        # WHEN
        result = await self.call_tool("sign_in")

        # THEN
        self.assertFalse(result.is_error)
        signed_in = {"signed_in": True, "site": "https://www.amazon.com", "default": True, "sign_in": None,
                     "error": None}
        self.assertEqual({**signed_in, "sites": [signed_in]}, result.structured_content)
        mock_browser_login.return_value.login.assert_called_once_with(SIGN_IN_TIMEOUT)

    @patch("amazonorders_mcp.server.has_display", return_value=False)
    async def test_sign_in_on_unknown_site_without_display(self, mock_has_display):
        # WHEN
        result = await self.call_tool("sign_in", {"site": "ebay.com"})

        # THEN
        self.assert_tool_error(result, "\"ebay.com\" isn't an Amazon site that can be read.")

    @patch("amazonorders_mcp.server.has_display", return_value=False)
    @patch("amazonorders_mcp.session.PlaywrightBrowserLogin")
    async def test_sign_in_on_site_without_display_names_the_site(self, mock_browser_login, mock_has_display):
        # WHEN
        result = await self.call_tool("sign_in", {"site": "amazon.de"})

        # THEN
        self.assert_tool_error(result, "There's no display where the server runs",
                               "`amazon-orders --domain amazon.de login`")
        mock_browser_login.assert_not_called()

    @patch("amazonorders_mcp.server.has_display", return_value=False)
    @patch("amazonorders_mcp.session.PlaywrightBrowserLogin")
    async def test_sign_in_without_display_says_how_to_sign_in(self, mock_browser_login, mock_has_display):
        # WHEN
        result = await self.call_tool("sign_in", {"browser": "chromium"})

        # THEN
        self.assert_tool_error(result, "There's no display where the server runs", f"`{HEADLESS_SIGN_IN_COMMAND}`",
                               "the server's config directory", AUTOMATED_SIGN_IN_DOCS_URL)
        mock_browser_login.assert_not_called()
        self.assertFalse(os.path.exists(self.test_config.config_path))

    async def test_get_order_history_without_saved_session(self):
        # WHEN
        result = await self.call_tool("get_order_history")

        # THEN
        self.assert_tool_error(result, "There's no saved Amazon session.", SIGN_IN_COMMAND)

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_get_order_history(self, mock_amazon_orders):
        # GIVEN
        self.given_saved_session()
        mock_amazon_orders.return_value.get_order_history_page.return_value = PagedResults(
            self.given_entities({"order_number": "112-0000000-0000001"}, {"order_number": "112-0000000-0000002"}),
            {"start_index": 10, "time_filter": "last30", "order_filter": None})

        # WHEN
        result = await self.call_tool("get_order_history", {"time_filter": "last30", "full_details": True})

        # THEN
        self.assertFalse(result.is_error)
        self.assertEqual({"items": [{"order_number": "112-0000000-0000001"},
                                    {"order_number": "112-0000000-0000002"}],
                          "cursor": {"start_index": 10, "time_filter": "last30", "order_filter": None},
                          "has_more": True}, result.structured_content)
        mock_amazon_orders.return_value.get_order_history_page.assert_called_once_with(
            year=None, start_index=None, full_details=True, time_filter="last30", order_filter=None,
            family_member_id=None, limit=DEFAULT_LIMIT, cursor=None, progress_with_total=ANY)

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_get_order_history_from_cursor(self, mock_amazon_orders):
        # GIVEN
        self.given_saved_session()
        cursor = {"start_index": 10, "time_filter": "year-2026", "order_filter": None}
        mock_amazon_orders.return_value.get_order_history_page.return_value = PagedResults(
            self.given_entities({"order_number": "112-0000000-0000011"}), None)

        # WHEN
        result = await self.call_tool("get_order_history", {"cursor": cursor, "limit": None})

        # THEN
        self.assertFalse(result.is_error)
        self.assertEqual({"items": [{"order_number": "112-0000000-0000011"}], "cursor": None, "has_more": False},
                         result.structured_content)
        mock_amazon_orders.return_value.get_order_history_page.assert_called_once_with(
            year=None, start_index=None, full_details=False, time_filter=None, order_filter=None,
            family_member_id=None, limit=None, cursor=cursor, progress_with_total=ANY)

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_get_order_history_on_site(self, mock_amazon_orders):
        # GIVEN
        self.given_saved_session("amazon.com", "amazon.de")
        mock_amazon_orders.return_value.get_order_history_page.return_value = PagedResults(
            self.given_entities({"order_number": "302-0000000-0000001"}), None)

        # WHEN
        result = await self.call_tool("get_order_history", {"site": "amazon.de", "limit": 1})

        # THEN
        self.assertFalse(result.is_error)
        self.assertEqual({"items": [{"order_number": "302-0000000-0000001"}], "cursor": None, "has_more": False},
                         result.structured_content)
        amazon_session = mock_amazon_orders.call_args.args[0]
        self.assertEqual("https://www.amazon.de", amazon_session.config.constants.BASE_URL)
        self.assertEqual(os.path.join(self.config_dir, "cookies.amazon.de.json"),
                         amazon_session.config.cookie_jar_path)

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_get_order_history_on_default_site(self, mock_amazon_orders):
        # GIVEN
        self.given_saved_session("amazon.com", "amazon.de")
        mock_amazon_orders.return_value.get_order_history_page.return_value = PagedResults([], None)

        # WHEN
        result = await self.call_tool("get_order_history")

        # THEN
        self.assertFalse(result.is_error)
        amazon_session = mock_amazon_orders.call_args.args[0]
        self.assertIs(self.test_config, amazon_session.config)
        self.assertEqual("https://www.amazon.com", amazon_session.config.constants.BASE_URL)

    async def test_get_order_on_site_without_saved_session(self):
        # GIVEN
        self.given_saved_session()

        # WHEN
        result = await self.call_tool("get_order", {"order_id": "302-0000000-0000001", "site": "amazon.es"})

        # THEN
        self.assert_tool_error(result, "There's no saved Amazon session.", "`site` set to `amazon.es`",
                               "`amazon-orders --domain amazon.es login --browser`")

    async def test_get_order_on_unknown_site(self):
        # WHEN
        result = await self.call_tool("get_order", {"order_id": "302-0000000-0000001", "site": "amazon.example"})

        # THEN
        self.assert_tool_error(result, "\"amazon.example\" isn't an Amazon site that can be read. Supported sites: ")

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_get_order_history_reports_progress(self, mock_amazon_orders):
        # GIVEN
        self.given_saved_session()

        def get_order_history_page(**kwargs):
            kwargs["progress_with_total"](10, 12)
            kwargs["progress_with_total"](12, 12)
            return PagedResults([], None)

        mock_amazon_orders.return_value.get_order_history_page.side_effect = get_order_history_page
        reported = []

        async def progress_callback(progress, total, message):
            reported.append((progress, total, message))

        # WHEN
        result = await self.call_tool("get_order_history", progress_callback=progress_callback)

        # THEN
        self.assertFalse(result.is_error)
        self.assertEqual([(10, 12, "10 of 12 Orders found so far ..."), (12, 12, "12 of 12 Orders found so far ...")],
                         reported)

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_get_order(self, mock_amazon_orders):
        # GIVEN
        self.given_saved_session()
        mock_amazon_orders.return_value.get_order.return_value = self.given_entities(
            {"order_number": "112-0000000-0000001"})[0]

        # WHEN
        result = await self.call_tool("get_order", {"order_id": "112-0000000-0000001"})

        # THEN
        self.assertFalse(result.is_error)
        self.assertEqual({"order_number": "112-0000000-0000001"}, result.structured_content)
        mock_amazon_orders.return_value.get_order.assert_called_once_with("112-0000000-0000001")

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_search_orders(self, mock_amazon_orders):
        # GIVEN
        self.given_saved_session()
        next_page = {"next_page": "https://www.amazon.com/your-orders/search?page=2&search=example"}
        mock_amazon_orders.return_value.search_orders_page.return_value = PagedResults(
            self.given_entities({"order_number": "112-0000000-0000001", "item": {"title": "Example Item"}}),
            next_page)

        # WHEN
        result = await self.call_tool("search_orders", {"query": "example"})

        # THEN
        self.assertFalse(result.is_error)
        self.assertEqual({"items": [{"order_number": "112-0000000-0000001", "item": {"title": "Example Item"}}],
                          "cursor": next_page, "has_more": True}, result.structured_content)
        mock_amazon_orders.return_value.search_orders_page.assert_called_once_with(
            query="example", limit=DEFAULT_LIMIT, cursor=None, progress=ANY)

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_search_orders_next_batch(self, mock_amazon_orders):
        # GIVEN
        self.given_saved_session()
        next_page = {"next_page": "https://www.amazon.com/your-orders/search?page=2&search=example"}
        mock_amazon_orders.return_value.search_orders_page.return_value = PagedResults([], None)

        # WHEN
        result = await self.call_tool("search_orders", {"query": "example", "limit": 5, "cursor": next_page})

        # THEN
        self.assertFalse(result.is_error)
        self.assertEqual({"items": [], "cursor": None, "has_more": False}, result.structured_content)
        mock_amazon_orders.return_value.search_orders_page.assert_called_once_with(
            query="example", limit=5, cursor=next_page, progress=ANY)

    @patch("amazonorders_mcp.server.AmazonTransactions")
    async def test_get_transactions(self, mock_amazon_transactions):
        # GIVEN
        self.given_saved_session()
        mock_amazon_transactions.return_value.get_transactions_page.return_value = PagedResults(
            self.given_entities({"grand_total": -12.99}), None)

        # WHEN
        result = await self.call_tool("get_transactions", {"days": 30})

        # THEN
        self.assertFalse(result.is_error)
        self.assertEqual({"items": [{"grand_total": -12.99}], "cursor": None, "has_more": False},
                         result.structured_content)
        mock_amazon_transactions.return_value.get_transactions_page.assert_called_once_with(
            days=30, order_id=None, limit=DEFAULT_LIMIT, cursor=None, progress=ANY)

    @patch("amazonorders_mcp.server.AmazonGiftCards")
    async def test_get_gift_card_activity(self, mock_amazon_gift_cards):
        # GIVEN
        self.given_saved_session()
        mock_amazon_gift_cards.return_value.get_gift_card_activity.return_value = self.given_entities(
            {"amount": 25.0})

        # WHEN
        result = await self.call_tool("get_gift_card_activity")

        # THEN
        self.assertFalse(result.is_error)
        self.assertEqual({"items": [{"amount": 25.0}]}, result.structured_content)
        mock_amazon_gift_cards.return_value.get_gift_card_activity.assert_called_once_with(days=365,
                                                                                           progress=ANY)

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_expired_session_says_to_sign_in_and_is_rechecked(self, mock_amazon_orders):
        # GIVEN
        mock_login = self.given_saved_session()
        mock_amazon_orders.return_value.get_order_history_page.side_effect = [
            AmazonOrdersAuthRedirectError("Amazon redirected to sign-in."), PagedResults([], None)]

        # WHEN
        expired_result = await self.call_tool("get_order_history")
        retried_result = await self.call_tool("get_order_history")

        # THEN
        self.assert_tool_error(expired_result, "Amazon redirected to sign-in.", SIGN_IN_COMMAND)
        self.assertFalse(retried_result.is_error)
        self.assertEqual(2, mock_login.call_count)

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_error_context_is_passed_back(self, mock_amazon_orders):
        # GIVEN
        self.given_saved_session()
        mock_amazon_orders.return_value.get_order_history_page.side_effect = AmazonOrdersError(
            "Order history couldn't be parsed.", meta={"index": 10})

        # WHEN
        result = await self.call_tool("get_order_history")

        # THEN
        self.assert_tool_error(result, "Order history couldn't be parsed.", "Context: {\"index\": 10}")

    @patch("amazonorders_mcp.server.AmazonOrders")
    async def test_unavailable_error_is_passed_back(self, mock_amazon_orders):
        # GIVEN
        self.given_saved_session()
        mock_amazon_orders.return_value.get_order.side_effect = AmazonOrdersUnavailableError(
            "Amazon could not load Order 112-0000000-0000001. Wait a bit before trying again.",
            meta={"amazon_message": "We're unable to load your order details right now."})

        # WHEN
        result = await self.call_tool("get_order", {"order_id": "112-0000000-0000001"})

        # THEN
        self.assert_tool_error(result, "Wait a bit before trying again.",
                               "\"amazon_message\": \"We're unable to load your order details right now.\"")
