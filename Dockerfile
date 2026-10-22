# The server runs over stdio as UID 1000, with its config and saved session on a volume (a directory mounted there
# instead must be writable by UID 1000):
#
#   docker build -t amazon-orders-mcp .
#   docker run -i --rm -v amazon-orders:/home/app/.config/amazonorders amazon-orders-mcp
#
# To serve remote clients, listen on all of the container's interfaces, which requires a bearer token:
#
#   docker run -d -p 127.0.0.1:8000:8000 -e AMAZON_ORDERS_MCP_TOKEN \
#       -v amazon-orders:/home/app/.config/amazonorders amazon-orders-mcp --transport streamable-http --host 0.0.0.0
#
# There's no display for `sign_in`, so sign in with `amazon-orders login` in a one-off container on the same volume.
# The server picks up the new session on its next call:
#
#   docker build --target login -t amazon-orders-login .
#   docker run -it --rm -v amazon-orders:/home/app/.config/amazonorders amazon-orders-login
#
# To sign in without being asked, pass `-e AMAZON_USERNAME -e AMAZON_PASSWORD -e AMAZON_OTP_SECRET_KEY` to that one-off
# container (https://amazon-orders.readthedocs.io/#automating-authentication). Never pass them to the server's.

FROM python:3.12-slim-trixie AS base

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY amazonorders_mcp ./amazonorders_mcp

RUN pip install --no-cache-dir .

# The config and saved session (config.yml and cookies.json) go in a volume at /home/app/.config/amazonorders
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /home/app/.config/amazonorders \
    && chown -R app:app /home/app/.config

# The one-off sign-in image, `docker build --target login -t amazon-orders-login .`, which runs `amazon-orders` with
# the Chromium it uses to clear Amazon's JavaScript challenges. The server never opens a browser, so only this has it.
FROM base AS login

ARG DEBIAN_FRONTEND=noninteractive

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

COPY requirements-login.txt ./

# Chromium runs headless itself, so Playwright's separate headless shell is left out, amazon-orders launches Chromium
# by its path, and the forms that clear JavaScript challenges are registered.
RUN pip install --no-cache-dir -r requirements-login.txt \
    && python -m playwright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /opt/playwright/chromium-*/chrome-linux*/chrome /usr/local/bin/chromium \
    && chromium --version

ENV AMAZON_BROWSER_EXECUTABLE_PATH=/usr/local/bin/chromium \
    AMAZON_AUTH_FORMS_CLASSES="[amazonorders.contrib.browser.playwright.PlaywrightAcicForm, amazonorders.contrib.browser.playwright.PlaywrightJSAuthForm]"

USER 1000:1000

ENTRYPOINT ["amazon-orders"]
CMD ["login"]

# The server, the default target: the slim image without a browser, since it only uses the saved session
FROM base

USER 1000:1000

ENTRYPOINT ["amazon-orders-mcp"]
