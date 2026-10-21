__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import json
import os
import re
import shutil
import tempfile
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import responses
from amazonorders import conf
from amazonorders.conf import AmazonOrdersConfig
from amazonorders.constants import Constants
from amazonorders.session import AmazonSession
from mcp.client import Client
from mcp.shared.dispatcher import ProgressFnT
from mcp.types import CallToolResult

from amazonorders_mcp.server import create_server
from amazonorders_mcp.session import SavedSession


class UnitTestCase(unittest.IsolatedAsyncioTestCase):
    RESOURCES_DIR = os.path.normpath(os.path.join(os.path.abspath(os.path.dirname(__file__)), "resources"))

    def setUp(self):
        self.cleared_environment = {name: os.environ.pop(name) for name in list(os.environ)
                                    if name.startswith(conf.ENVIRONMENT_VARIABLE_PREFIX)}

        self.config_dir = tempfile.mkdtemp()
        # amazon-orders' default config directory, where it keeps each site's cookie jar
        config_dir_patcher = patch.object(conf, "DEFAULT_CONFIG_DIR", self.config_dir)
        config_dir_patcher.start()
        self.addCleanup(config_dir_patcher.stop)
        self.test_cookie_jar_path = os.path.join(self.config_dir, "cookies.json")
        self.test_config = AmazonOrdersConfig(config_path=os.path.join(self.config_dir, "config.yml"), data={
            "output_dir": os.path.join(self.config_dir, "output"),
            "cookie_jar_path": self.test_cookie_jar_path,
        })
        self.saved_session = SavedSession(self.test_config)

        self.responses = responses.RequestsMock()
        self.responses.start()

    def tearDown(self):
        self.responses.stop()
        self.responses.reset()

        shutil.rmtree(self.config_dir, ignore_errors=True)
        os.environ.update(self.cleared_environment)

    def given_saved_session(self,
                            *sites: str) -> MagicMock:
        for site in sites or (self.saved_session.site(),):
            self.given_cookie_jar(site)

        def login(amazon_session: AmazonSession) -> None:
            amazon_session.is_authenticated = True

        login_patcher = patch.object(AmazonSession, "login", autospec=True, side_effect=login)
        self.addCleanup(login_patcher.stop)
        return login_patcher.start()

    def given_cookie_jar(self,
                         site: str) -> str:
        cookie_jar_path = AmazonOrdersConfig(config_path=self.test_config.config_path,
                                             data={"domain": site}).cookie_jar_path
        os.makedirs(os.path.dirname(cookie_jar_path), exist_ok=True)
        with open(cookie_jar_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({Constants.region_auth_cookie(site.removeprefix("amazon.")): "some-value"}))
        return cookie_jar_path

    def given_entities(self, *dicts: Dict[str, Any]) -> List[MagicMock]:
        return [MagicMock(**{"to_dict.return_value": entity_dict}) for entity_dict in dicts]

    def given_page_exists(self,
                          url: str,
                          html_file: str) -> None:
        with open(os.path.join(self.RESOURCES_DIR, html_file), "r", encoding="utf-8") as f:
            self.responses.add(responses.GET, re.compile(rf"^{re.escape(url)}(\?.*)?$"), body=f.read(), status=200)

    async def call_tool(self,
                        name: str,
                        arguments: Optional[Dict[str, Any]] = None,
                        progress_callback: Optional[ProgressFnT] = None) -> CallToolResult:
        async with Client(create_server(self.saved_session)) as client:
            return await client.call_tool(name, arguments or {}, progress_callback=progress_callback)

    def assert_tool_error(self,
                          result: CallToolResult,
                          *expected_text: str) -> None:
        self.assertTrue(result.is_error)
        text = " ".join(content.text for content in result.content)
        for expected in expected_text:
            self.assertIn(expected, text)

    def assert_no_none_values(self,
                              value: Any) -> None:
        self.assertIsNotNone(value)
        if isinstance(value, dict):
            for nested in value.values():
                self.assert_no_none_values(nested)
        elif isinstance(value, list):
            for nested in value:
                self.assert_no_none_values(nested)
