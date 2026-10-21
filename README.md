<p align="center"><img alt="amazon-orders-mcp - An MCP server for Amazon order history" src="https://amazon-orders.readthedocs.io/_images/logo.png" /></p>

[![Version](https://img.shields.io/pypi/v/amazon-orders-mcp)](https://pypi.org/project/amazon-orders-mcp)
[![Python Versions](https://img.shields.io/pypi/pyversions/amazon-orders-mcp.svg)](https://pypi.org/project/amazon-orders-mcp)
[![Build](https://img.shields.io/github/actions/workflow/status/alexdlaird/amazon-orders-mcp/build.yml)](https://github.com/alexdlaird/amazon-orders-mcp/actions/workflows/build.yml)
[![GitHub License](https://img.shields.io/github/license/alexdlaird/amazon-orders-mcp)](https://github.com/alexdlaird/amazon-orders-mcp/blob/main/LICENSE)

<!-- mcp-name: io.github.alexdlaird/amazon-orders-mcp -->

`amazon-orders-mcp` is an unofficial [MCP](https://modelcontextprotocol.io) server for Amazon order history, line items,
and transactions, built on [`amazon-orders`](https://github.com/alexdlaird/amazon-orders). It gives your AI assistant
tools for your Amazon account, available from Claude and any other MCP client.

The server never sees your credentials. When you sign in, a browser window opens on Amazon's own page, and the server
reuses the session that leaves behind. Signing in needs a display, so run the server on the machine you sign in on. On a
machine without one, like a server or a container, sign in with
[`amazon-orders login`](https://github.com/alexdlaird/amazon-orders) instead.

> **Note:** This server reads its data through `amazon-orders`, which works by parsing data from Amazon's
> consumer-facing website. As Amazon provides no official API to use, older versions may break at any time, so it's
> recommended that you use the latest version.

## Installation

`amazon-orders-mcp` is available on [PyPI](https://pypi.org/project/amazon-orders-mcp/) and is run
with [`uvx`](https://docs.astral.sh/uv/), so there's nothing to install up front. Add it to your AI assistant:

### Claude

**Claude Desktop:** download `amazon-orders-mcp-<version>.mcpb` from the
[latest release](https://github.com/alexdlaird/amazon-orders-mcp/releases/latest) and open it.

**Claude Code:**

```sh
claude mcp add amazon-orders -- uvx amazon-orders-mcp
```

### ChatGPT

**ChatGPT Desktop:** in Settings → Plugins, add an MCP server of type STDIO, with `uvx` as the command and
`amazon-orders-mcp` as its argument.

**Codex:**

```sh
codex mcp add amazon-orders -- uvx amazon-orders-mcp
```

### Other Clients

Any client that runs local MCP servers can use this JSON, or `amazon-orders-mcp` as the command if you've
installed it with `pip install amazon-orders-mcp`:

```json
{
  "mcpServers": {
    "amazon-orders": {
      "command": "uvx",
      "args": ["amazon-orders-mcp"]
    }
  }
}
```

### Remote Clients

For clients that connect to a server instead of running one, the server can also serve
[Streamable HTTP](https://modelcontextprotocol.io/specification/latest/basic/transports/streamable-http):

```sh
uvx amazon-orders-mcp --transport streamable-http
```

It listens at `http://127.0.0.1:8000/mcp`. See `--help` to change that, or to require a bearer token (required off
loopback).

### Docker

```sh
docker build -t amazon-orders-mcp .
docker run -i --rm -v amazon-orders:/home/app/.config/amazonorders amazon-orders-mcp
```

A container has no display, so sign in with the `login` target first. See the `Dockerfile` for how.

## Basic Usage

Just ask your assistant about your orders, and it will use the following tools:

| Tool                     | Returns                                                                    |
|--------------------------|----------------------------------------------------------------------------|
| `sign_in`                | Opens a browser window for you to sign in to Amazon, when you ask          |
| `get_order_history`      | A batch of orders by year, time filter or order type, optionally detailed  |
| `get_order`              | One order, by order number                                                 |
| `search_orders`          | A batch of items by product, order number, address or recipient, any year  |
| `get_tracking`           | A shipment's carrier and tracking number, from an order's shipment ID      |
| `get_invoice_pdfs`       | An order's invoice and credit note PDFs, where the seller issues them      |
| `get_transactions`       | A batch of payment transactions over a number of days, or for an order     |
| `get_gift_card_balance`  | The current Gift Card balance                                              |
| `get_gift_card_activity` | Gift Card activity over a number of days                                   |
| `get_prime_payments`     | Prime membership fee charges, each with its order number                   |
| `get_family_members`     | Amazon Household members whose orders you can view, like Teen accounts     |
| `get_config`             | The settings your assistant can change, and their values                   |
| `update_config`          | Changes a setting, like the default Amazon site (e.g. `amazon.co.uk`)      |
| `check_session`          | Whether you're signed in, on each Amazon site you have a session for       |

Order history and transactions come back in batches of `limit` results (25 by default, and a batch can run up to a
page over), with a `cursor` your assistant passes back to get the next batch.

Every tool that reads Amazon takes an optional `site`, like `amazon.de`, so your assistant can read each Amazon site
you shop on from one server, e.g. "my last amazon.de orders". Without it, tools read your default site, the `domain`
setting. Each site has its own sign-in, so ask your assistant to sign you in to each one.

### Configuration

Ask your assistant to change a setting, such as the default Amazon site (e.g. `amazon.co.uk`) or the browser to sign in
with. Settings are shared with the `amazon-orders` CLI, so it follows the same changes. See
[Supported Sites](https://amazon-orders.readthedocs.io/language-packages.html#supported-sites) for the sites
`amazon-orders` can read.

## Documentation

For more on the data the server returns, `amazon-orders`'s official documentation is available
at [Read the Docs](http://amazon-orders.readthedocs.io), including the
[Order](https://amazon-orders.readthedocs.io/api.html#amazonorders.entity.order.Order) entity's fields
and [troubleshooting](https://amazon-orders.readthedocs.io/troubleshooting.html).

## Contributing

Want to contribute financially? If you've found `amazon-orders-mcp`
useful, [sponsorship](https://github.com/sponsors/alexdlaird) would
be greatly appreciated!
