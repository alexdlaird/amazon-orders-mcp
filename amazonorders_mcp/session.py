__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import logging
import os
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar, Union
from urllib.parse import urlparse

from amazonorders.conf import AmazonOrdersConfig
from amazonorders.contrib.browser.playwright import PlaywrightBrowserLogin
from amazonorders.exception import AmazonOrdersAuthError, AmazonOrdersBrowserNotInstalledError, AmazonOrdersError
from amazonorders.region_data import REGION_SITES
from amazonorders.session import AmazonSession, IODefault
from amazonorders.util import to_type

logger = logging.getLogger(__name__)

#: The command that signs in from a terminal, for a client that can't use the ``sign_in`` tool.
SIGN_IN_COMMAND = "amazon-orders login --browser"

#: The command that signs in without a browser window, with the user's credentials, where there's no display.
HEADLESS_SIGN_IN_COMMAND = "amazon-orders login"

#: What a sign-in reports while it waits for the user.
WAITING_TO_SIGN_IN_STATUS = "Waiting for the user to sign in to Amazon ..."

#: How many seconds the user has to sign in, kept short enough that a client's tool call doesn't time out first.
SIGN_IN_TIMEOUT = 120

#: The config keys that are the user's choice, changed only when they ask, and saved.
PREFERENCE_KEYS: Tuple[str, ...] = ("domain", "browser")

#: The config keys the assistant may adjust when calls keep failing, for this server run unless saved.
TUNING_KEYS: Tuple[str, ...] = ("request_timeout", "browser_timeout", "thread_pool_size", "connection_pool_size",
                                "max_auth_attempts", "max_auth_retries", "max_cookie_attempts",
                                "cookie_reattempt_wait", "auth_reattempt_wait", "bs4_parser",
                                "warn_on_missing_required_field")

T = TypeVar("T")


class StderrIO(IODefault):
    """
    I/O for running over stdio, where ``stdout`` carries the MCP protocol and nobody is there to answer a prompt.
    Echoes go to ``stderr``, and a prompt raises instead of waiting for input.
    """

    def echo(self,
             msg: str,
             **kwargs: Any) -> None:
        print(msg, file=sys.stderr)

    def prompt(self,
               msg: str,
               type: Optional[Any] = None,
               **kwargs: Any) -> Any:
        raise AmazonOrdersAuthError(f"Amazon asked for input (\"{msg}\"), which the MCP server can't give.")


class SavedSession:
    """
    The Amazon session ``amazon-orders`` saved when the user signed in, shared by every tool call. A tool call only
    ever reuses it: no sign-in form is ever submitted, so credentials are never used and no browser is opened. If
    there's no saved session, or it has expired, an :class:`~amazonorders.exception.AmazonOrdersAuthError` is raised,
    and the user signs in again with :meth:`sign_in`, or with :data:`SIGN_IN_COMMAND` in a terminal.

    Calls are serialized, since an :class:`~amazonorders.session.AmazonSession` isn't made for concurrent use. After
    Amazon rejects the session, it's loaded again from disk, so a sign-in saved meanwhile (e.g. by the CLI's ``login``)
    is used, and the rejected call is retried once with it.

    :param config: The config to use, by default the same one the ``amazon-orders`` CLI uses.
    """

    def __init__(self,
                 config: Optional[AmazonOrdersConfig] = None) -> None:
        self.config: Optional[AmazonOrdersConfig] = config
        self._amazon_session: Optional[AmazonSession] = None
        self._loaded_cookie_jar_mtime: Optional[float] = None
        self._lock = threading.Lock()
        #: What an ongoing :meth:`sign_in` is doing, for the client to show while it waits.
        self.sign_in_status: str = WAITING_TO_SIGN_IN_STATUS

    def run(self,
            call: Callable[[AmazonSession], T]) -> T:
        """
        Run a call with the signed-in session.

        :param call: The call to make with the signed-in session.
        :return: The call's result.
        :raises AmazonOrdersAuthError: If there's no saved session, or it has expired.
        """
        with self._lock:
            try:
                return self._run_signed_in(call)
            except AmazonOrdersAuthError:
                if not self._saved_session_changed():
                    raise

                logger.debug("The saved session changed since it was loaded, so the call is retried with it")

                return self._run_signed_in(call)

    def _run_signed_in(self,
                       call: Callable[[AmazonSession], T]) -> T:
        try:
            return call(self._signed_in())
        except AmazonOrdersAuthError:
            logger.debug("Amazon rejected the saved session, so it will be loaded again from disk on the next call")

            if self._amazon_session is not None:
                self._loaded_cookie_jar_mtime = self._amazon_session.cookie_jar_mtime
            self._amazon_session = None

            raise

    def _saved_session_changed(self) -> bool:
        return self.config is not None and _cookie_jar_mtime(self.config) != self._loaded_cookie_jar_mtime

    def sign_in(self,
                timeout: int = SIGN_IN_TIMEOUT,
                browser: Optional[str] = None) -> None:
        """
        Open a browser window for the user to sign in on Amazon's own page, then save the session. Credentials are
        never seen, and other calls wait until the sign-in is done.

        :param timeout: The number of seconds the user has to sign in.
        :param browser: The browser to sign in with, which is saved to the config so it's only chosen once. A
            Playwright browser type that isn't installed yet is downloaded first. By default, an installed Chrome,
            then Edge, or a Chromium that's been downloaded.
        :raises AmazonOrdersBrowserNotInstalledError: If no browser is installed to sign in with.
        :raises AmazonOrdersError: If a browser can't be downloaded, or the sign-in isn't completed in time, or Amazon
            doesn't accept it.
        """
        with self._lock:
            self.sign_in_status = WAITING_TO_SIGN_IN_STATUS
            amazon_session = self._session()
            if browser and browser != amazon_session.config.browser:
                self._save_config("browser", browser)
                amazon_session.config.update_config("browser", browser, save=False)

            browser_login = PlaywrightBrowserLogin(amazon_session)
            try:
                browser_login.login(timeout)
            except AmazonOrdersBrowserNotInstalledError as e:
                if not browser or (e.meta or {}).get("browser_types") != [browser]:
                    raise

                self.sign_in_status = f"Downloading {browser} to sign in with ..."
                _download_browser(browser)

                self.sign_in_status = WAITING_TO_SIGN_IN_STATUS
                browser_login.login(timeout)
            finally:
                self._loaded_cookie_jar_mtime = amazon_session.cookie_jar_mtime

    def get_config(self) -> Dict[str, Dict[str, Any]]:
        """
        Get the current value of each config key in :data:`PREFERENCE_KEYS` and :data:`TUNING_KEYS`.

        :return: The values, by key, under ``preferences`` and ``tuning``.
        """
        with self._lock:
            config = self._session().config

            return {"preferences": {key: getattr(config, key) for key in PREFERENCE_KEYS},
                    "tuning": {key: getattr(config, key) for key in TUNING_KEYS}}

    def update_config(self,
                      key: str,
                      value: Union[str, int, float, bool, None],
                      save: Optional[bool] = None) -> Tuple[Any, bool]:
        """
        Change a config key in :data:`PREFERENCE_KEYS` or :data:`TUNING_KEYS`. A saved change is made with the
        ``amazon-orders`` CLI's ``update-config`` command, so the CLI follows it. A ``domain`` must be an Amazon site
        ``amazon-orders`` knows, since the saved session's cookies are sent to it.

        :param key: The config key.
        :param value: The new value, read as the CLI reads it.
        :param save: Whether to save the change, or only make it for this server run. By default, a preference is
            saved and tuning isn't.
        :return: The value now in effect, and whether it was saved.
        :raises AmazonOrdersError: If the key can't be changed, or the value isn't valid for it.
        """
        if key not in PREFERENCE_KEYS + TUNING_KEYS:
            raise AmazonOrdersError(f"\"{key}\" isn't a setting that can be changed. Settings that can: "
                                    f"{', '.join(PREFERENCE_KEYS + TUNING_KEYS)}.")

        text = "" if value is None else str(value)
        if key == "domain":
            text = _to_amazon_domain(text)

        with self._lock:
            config = self._session().config
            if key == "domain":
                config.set_domain(text)
            else:
                config.update_config(key, to_type(text), save=False)
            saved = key in PREFERENCE_KEYS if save is None else save
            if saved:
                self._save_config(key, text)
            self._amazon_session = None

            return getattr(config, key), saved

    def base_url(self) -> str:
        """
        Get the Amazon site the session is for.

        :return: The Amazon site's base URL.
        """
        with self._lock:
            return self._session().config.constants.BASE_URL

    def _save_config(self,
                     key: str,
                     value: str) -> None:
        command = [sys.executable, "-m", "amazonorders.cli"]
        if self.config:
            command += ["--config-path", self.config.config_path]
        command += ["update-config", key, value]

        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            logger.debug(f"Updating config \"{key}\" failed: {(result.stderr or result.stdout).strip()}")

            raise AmazonOrdersError(f"Could not change the {key} setting.")

    def _signed_in(self) -> AmazonSession:
        amazon_session = self._session()
        if not amazon_session.is_authenticated:
            if not amazon_session.auth_cookies_stored():
                raise AmazonOrdersAuthError("There's no saved Amazon session.")

            logger.debug("Reusing the saved Amazon session ...")

            amazon_session.login()

        return amazon_session

    def _session(self) -> AmazonSession:
        if self._amazon_session is None:
            if self.config is None:
                self.config = AmazonOrdersConfig()
            amazon_session = AmazonSession(io=StderrIO(), config=self.config)
            self._loaded_cookie_jar_mtime = amazon_session.cookie_jar_mtime
            self._amazon_session = self._reuse_only(amazon_session)

        return self._amazon_session

    @staticmethod
    def _reuse_only(amazon_session: AmazonSession) -> AmazonSession:
        amazon_session.auth_forms = []

        return amazon_session


def has_display() -> bool:
    """
    Check whether a browser window can be opened for the user to sign in, as ``amazon-orders`` checks before it opens
    one: on Linux, only with a display, which a server or a container usually doesn't have.

    :return: Whether there's a display.
    """
    return (not sys.platform.startswith("linux") or
            bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))


def _cookie_jar_mtime(config: AmazonOrdersConfig) -> Optional[float]:
    try:
        return os.path.getmtime(config.cookie_jar_path)
    except OSError:
        return None


def _download_browser(browser_type: str) -> None:
    """
    Download a Playwright browser type, as ``playwright install`` does. Its output is captured, because ``stdout``
    carries the MCP protocol and nothing else may be written to it.
    """
    result = subprocess.run([sys.executable, "-m", "playwright", "install", browser_type],
                            capture_output=True, text=True)
    if result.returncode != 0:
        detail = ((result.stderr or result.stdout).strip().splitlines() or [""])[-1]

        raise AmazonOrdersError(f"Could not download {browser_type}. {detail}".strip())


def _to_amazon_domain(site: str) -> str:
    value = site.strip().lower()
    host = (urlparse(value if "//" in value else f"//{value}").hostname or "").removeprefix("www.")
    suffix = host.removeprefix("amazon.") if host.startswith("amazon.") else ""
    if suffix not in REGION_SITES:
        supported = ", ".join(f"amazon.{suffix}" for suffix in sorted(REGION_SITES))

        raise AmazonOrdersError(f"\"{site}\" isn't an Amazon site that can be read. Supported sites: {supported}.")

    return f"amazon.{suffix}"
