FROM python:3.12-slim-trixie

# The Playwright whose Chromium amazon-orders uses to clear Amazon's JavaScript challenges when signing in
ARG PLAYWRIGHT_VERSION=1.64.0
ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

# Chromium and the libraries it needs. Chromium runs headless itself, so Playwright's separate headless shell is left
# out, amazon-orders launches Chromium by its path, and the forms that clear JavaScript challenges are registered.
RUN pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" \
    && python -m playwright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /opt/playwright/chromium-*/chrome-linux*/chrome /usr/local/bin/chromium \
    && chromium --version

ENV AMAZON_BROWSER_EXECUTABLE_PATH=/usr/local/bin/chromium \
    AMAZON_AUTH_FORMS_CLASSES="[amazonorders.contrib.browser.playwright.PlaywrightAcicForm, amazonorders.contrib.browser.playwright.PlaywrightJSAuthForm]"

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY amazonorders_mcp ./amazonorders_mcp

RUN pip install --no-cache-dir . "playwright==${PLAYWRIGHT_VERSION}"

# The config and saved session (config.yml and cookies.json) go in a volume at /home/app/.config/amazonorders
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /home/app/.config/amazonorders \
    && chown -R app:app /home/app/.config

USER 1000:1000

ENTRYPOINT ["amazon-orders-mcp"]
