.. rst-class:: hide-header

****************************************************************************************
amazon-orders-mcp - An MCP server for Amazon order history, line items, and transactions
****************************************************************************************

.. only:: html

   .. image:: https://amazon-orders.readthedocs.io/_static/logo-light.png
      :alt: amazon-orders-mcp - An MCP server for Amazon order history, line items, and transactions
      :align: center
      :width: 560px
      :class: hero-logo only-light

   .. image:: https://amazon-orders.readthedocs.io/_static/logo-dark.png
      :alt: amazon-orders-mcp - An MCP server for Amazon order history, line items, and transactions
      :align: center
      :width: 560px
      :class: hero-logo only-dark

|

.. only:: builder_html

   .. image:: https://img.shields.io/pypi/v/amazon-orders-mcp
      :target: https://pypi.org/project/amazon-orders-mcp
   .. image:: https://img.shields.io/pypi/pyversions/amazon-orders-mcp.svg
      :target: https://pypi.org/project/amazon-orders-mcp
   .. image:: https://img.shields.io/github/actions/workflow/status/alexdlaird/amazon-orders-mcp/build.yml
      :target: https://github.com/alexdlaird/amazon-orders-mcp/actions/workflows/build.yml
   .. image:: https://img.shields.io/readthedocs/amazon-orders-mcp
      :target: https://amazon-orders-mcp.readthedocs.io
   .. image:: https://img.shields.io/github/license/alexdlaird/amazon-orders-mcp
      :target: https://github.com/alexdlaird/amazon-orders-mcp/blob/main/LICENSE

``amazon-orders-mcp`` is an unofficial `MCP <https://modelcontextprotocol.io>`_ server for Amazon order history, line
items, and transactions, built on |amazon-orders|_. It gives your AI assistant tools for your Amazon account, available
from any MCP client.

The server never sees your credentials. When you sign in, a browser window opens on Amazon's own page, and the server
reuses the session that leaves behind. It also runs on a server without a display, where you sign in from a terminal
instead (see :doc:`remote`).

.. note::

    This server reads its data through ``amazon-orders``, which works by parsing data from Amazon's consumer-facing
    website. As Amazon provides no official API to use, older versions may break at any time, so it's recommended
    that you use the latest version.

Installation
============

``amazon-orders-mcp`` is available on `PyPI <https://pypi.org/project/amazon-orders-mcp/>`_ and is run with
`uvx <https://docs.astral.sh/uv/>`_, so there's nothing to install up front. Add it to your AI assistant:

ChatGPT
-------

**ChatGPT Desktop:** in Settings → Plugins, add an MCP server of type STDIO, with ``uvx`` as the command and
``amazon-orders-mcp`` as its argument.

**Codex:**

.. code-block:: sh

    codex mcp add amazon-orders -- uvx amazon-orders-mcp

Claude
------

**Claude Desktop:** download ``amazon-orders-mcp-<version>.mcpb`` from the
`latest release <https://github.com/alexdlaird/amazon-orders-mcp/releases/latest>`_ and open it.

**Claude Code:**

.. code-block:: sh

    claude mcp add amazon-orders -- uvx amazon-orders-mcp

Other Clients
-------------

Any client that runs local MCP servers can use this JSON, or ``amazon-orders-mcp`` as the command if you've installed
it with ``pip install amazon-orders-mcp``:

.. code-block:: json

    {
      "mcpServers": {
        "amazon-orders": {
          "command": "uvx",
          "args": ["amazon-orders-mcp"]
        }
      }
    }

For clients that connect to a server instead of running one, see :doc:`remote`.

Docker
------

.. code-block:: sh

    docker build -t amazon-orders-mcp .
    docker run -i --rm -v amazon-orders:/home/app/.config/amazonorders amazon-orders-mcp

A container has no display, so sign in with the ``login`` target first. See the
`Dockerfile <https://github.com/alexdlaird/amazon-orders-mcp/blob/main/Dockerfile>`_ for how.

Basic Usage
===========

Just ask your assistant about your orders, and it will use the following tools:

.. list-table::
   :header-rows: 1

   * - Tool
     - Returns
   * - ``find_charge``
     - The transactions behind a card or bank charge, each with its order
   * - ``sign_in``
     - Opens a browser window for you to sign in to Amazon, when you ask
   * - ``get_orders``
     - A batch of orders by year, time filter or order type, optionally detailed
   * - ``search_orders``
     - A batch of items by product, order number, address or recipient, any year
   * - ``get_order``
     - One order, by order number
   * - ``get_tracking``
     - A shipment's carrier and tracking number, from an order's shipment ID
   * - ``get_invoices``
     - An order's printable summary, and any invoice PDFs the seller issues
   * - ``get_transactions``
     - A batch of payment transactions over a number of days, or for an order
   * - ``get_gift_cards``
     - The Gift Card balance, with a batch of its activity over a number of days
   * - ``get_prime_payments``
     - Prime membership fee charges, each with its order number
   * - ``check_session``
     - Whether you're signed in, on each Amazon site you have a session for
   * - ``get_config``
     - The settings your assistant can change, and their values
   * - ``update_config``
     - Changes one of those settings

For assistants that offer prompts, these are listed alongside the tools:

.. list-table::
   :header-rows: 1

   * - Prompt
     - Does
   * - ``review_spending``
     - Reviews what you've spent over a period, grouped by what it was for
   * - ``find_returns``
     - Finds items in recent orders you can still return
   * - ``track_deliveries``
     - Lists what you have arriving, soonest first
   * - ``explain_charge``
     - Works out what an Amazon charge on your statement was

Every tool that reads Amazon takes an optional ``site``, like ``amazon.de``, so your assistant can read each Amazon site
you shop on from one server, e.g. "my last amazon.de orders". Without it, tools read your default site, the ``domain``
setting. Each site has its own sign-in, so ask your assistant to sign you in to each one.

Configuration
-------------

Ask your assistant to change a setting, such as the default Amazon site (e.g. ``amazon.co.uk``) or the browser to sign
in with. Settings are shared with the ``amazon-orders`` CLI, so it follows the same changes. See
`Supported Sites <https://amazon-orders.readthedocs.io/language-packages.html#supported-sites>`_ for the sites
``amazon-orders`` can read.

Budgeting Apps
--------------

Your assistant can use ``find_charge`` alongside your budgeting app's own connector to work out what each Amazon charge
in your budget was, e.g. "what were my Amazon charges last week for?" ``amazon-orders-mcp`` only ever reads Amazon, so
any change to your budget is made by your budgeting app's connector.

A charge can show in your budget on a different day than Amazon completed it, so ``find_charge`` matches Transactions
completed within a few days of the charge's date (``window_days``), and it ignores the amount's sign, since budgets and
Amazon show charges and refunds with different signs. If you pay with more than one card, give the card's last four
digits. For one charge at a time, the ``explain_charge`` prompt walks your assistant through it.

Dive Deeper
===========

For more on the data the server returns, see the |amazon-orders|_ documentation, including the
:class:`~amazonorders.entity.order.Order` entity's fields and
`troubleshooting <https://amazon-orders.readthedocs.io/troubleshooting.html>`_.

.. toctree::
   :maxdepth: 2

   remote
   api

Contributing
============

Want to contribute financially? If you've found ``amazon-orders-mcp`` useful,
`sponsorship <https://github.com/sponsors/alexdlaird>`_ would be greatly appreciated!
