__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import logging
import sys
import threading
from typing import Any, Callable, Optional, TypeVar

from amazonorders.conf import AmazonOrdersConfig
from amazonorders.exception import AmazonOrdersAuthError
from amazonorders.session import AmazonSession, IODefault

logger = logging.getLogger(__name__)

#: The command that signs in, run by the user in a terminal.
SIGN_IN_COMMAND = "amazon-orders login --browser"

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
    The Amazon session ``amazon-orders`` saved when the user signed in with :data:`SIGN_IN_COMMAND`, shared by every
    tool call. It's only ever reused: no sign-in form is ever submitted, so credentials are never used and no browser
    is opened. If there's no saved session, or it has expired, an
    :class:`~amazonorders.exception.AmazonOrdersAuthError` is raised.

    Calls are serialized, since an :class:`~amazonorders.session.AmazonSession` isn't made for concurrent use.

    :param config: The config to use, by default the same one the ``amazon-orders`` CLI uses.
    """

    def __init__(self,
                 config: Optional[AmazonOrdersConfig] = None) -> None:
        self.config: Optional[AmazonOrdersConfig] = config
        self._amazon_session: Optional[AmazonSession] = None
        self._lock = threading.Lock()

    def run(self,
            call: Callable[[AmazonSession], T]) -> T:
        """
        Run a call with the signed-in session.

        :param call: The call to make with the signed-in session.
        :return: The call's result.
        :raises AmazonOrdersAuthError: If there's no saved session, or it has expired.
        """
        with self._lock:
            amazon_session = self._signed_in()
            try:
                return call(amazon_session)
            except AmazonOrdersAuthError:
                logger.debug("Amazon rejected the saved session, so it will be rechecked on the next call")

                amazon_session.is_authenticated = False

                raise

    def base_url(self) -> str:
        """
        Get the Amazon site the session is for.

        :return: The Amazon site's base URL.
        """
        with self._lock:
            return self._session().config.constants.BASE_URL

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
            amazon_session = AmazonSession(io=StderrIO(), config=self.config)
            self._amazon_session = self._reuse_only(amazon_session)

        return self._amazon_session

    @staticmethod
    def _reuse_only(amazon_session: AmazonSession) -> AmazonSession:
        amazon_session.auth_forms = []

        return amazon_session
