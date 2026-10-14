__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

from tests.unittestcase import UnitTestCase


class TestAmazonOrders(UnitTestCase):
    async def test_get_order_history_runs_core_under_server_loop(self):
        # GIVEN
        self.given_saved_session()
        self.given_page_exists(self.test_config.constants.ORDER_HISTORY_URL,
                               "orders/order-history-2018-0.html")

        # WHEN
        result = await self.call_tool("get_order_history", {"year": 2018, "limit": 1})

        # THEN
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(10, len(result.structured_content["items"]))
        self.assertEqual("112-0399923-3070642", result.structured_content["items"][3]["order_number"])
        self.assertTrue(result.structured_content["has_more"])
        self.assertEqual(10, result.structured_content["cursor"]["start_index"])
        self.assert_no_none_values(result.structured_content["items"])

    async def test_get_order_parses_details_page(self):
        # GIVEN
        self.given_saved_session()
        self.given_page_exists(self.test_config.constants.ORDER_DETAILS_URL,
                               "orders/order-details-112-0399923-3070642.html")

        # WHEN
        result = await self.call_tool("get_order", {"order_id": "112-0399923-3070642"})

        # THEN
        self.assertFalse(result.is_error, result.content)
        self.assertEqual("112-0399923-3070642", result.structured_content["order_number"])
        self.assertTrue(result.structured_content["full_details"])
        self.assertTrue(result.structured_content["items"])
        self.assert_no_none_values(result.structured_content)
