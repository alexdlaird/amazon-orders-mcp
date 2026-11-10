==============
Remote Clients
==============

Some MCP clients connect to a server instead of running one, like an AI assistant on the web. ``amazon-orders-mcp``
can serve them over `Streamable HTTP <https://modelcontextprotocol.io/specification/latest/basic/transports/streamable-http>`_,
and sign them in with `OAuth <https://modelcontextprotocol.io/specification/latest/basic/authorization>`_, so any
client that can add a server by its URL can use it. The server still runs on your machine, and your Amazon session
never leaves it. Clients only ever get what the server's tools return.

Anyone who finds the server's public URL can reach it, so never make it public without auth. With ``--public-url``,
every request needs a token, and a client only gets one after you approve it with the server's secret. The secret also
works as a bearer token on its own, so keep it the way you'd keep a password.

Getting Started
---------------

This example serves the server through an `ngrok <https://ngrok.com>`_ tunnel with |pyngrok|_, but any proxy or tunnel
that gives the server a public HTTPS URL that stays the same works the same way.

First, sign in to Amazon on the machine the server runs on:

.. code-block:: sh

    uvx amazon-orders login --browser

Without a display, leave off ``--browser``, and you'll be asked for your username and password instead.

Next, generate the server's secret, and keep it in a file only you can read:

.. code-block:: sh

    python -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.amazon-orders-mcp-token
    chmod 600 ~/.amazon-orders-mcp-token

Then open a tunnel to the port the server listens on. Create ``tunnel.py`` with the following code:

.. code-block:: python

    import os

    from pyngrok import ngrok

    domain = os.environ.get("NGROK_DOMAIN")
    if not domain:
        raise SystemExit("Set NGROK_DOMAIN to your ngrok domain")

    # Rewrite the Host header to localhost on requests to the tunnel, since the server only accepts those
    traffic_policy = {
        "on_http_request": [{
            "actions": [{"type": "add-headers", "config": {"headers": {"host": "localhost:8000"}}}]
        }]
    }

    # Open a ngrok tunnel on your domain, without keeping copies of requests in ngrok's local inspector
    tunnel = ngrok.connect("8000", url=f"https://{domain}", traffic_policy=traffic_policy, inspect=False)
    print(f"Server URL: {tunnel.public_url}/mcp")

    ngrok.get_ngrok_process().proc.wait()

A domain that stays the same is what lets clients stay connected when the tunnel restarts. On ngrok's free plan, your
account's dev domain is under **Domains** in the `ngrok dashboard <https://dashboard.ngrok.com>`_. Start the tunnel
with it:

.. code-block:: sh

    pip install pyngrok
    NGROK_DOMAIN=<your-domain> python tunnel.py

Finally, start the server, with the tunnel's URL as its public URL:

.. code-block:: sh

    uvx amazon-orders-mcp --transport streamable-http --token-file ~/.amazon-orders-mcp-token \
        --public-url https://<your-domain>

That's it! Add ``https://<your-domain>/mcp`` to your assistant as a custom connector. When it connects, a page from
the server asks you to approve it. Check that it names the assistant you're connecting, then enter the server's secret.
The assistant stays connected from then on, even when the server restarts.

Signing In to Amazon
--------------------

The server only ever reuses the Amazon session saved where it runs. When ``sign_in`` is called through a proxy or
tunnel, it doesn't open a browser window, since that wouldn't reach whoever's connected, and instead says how to sign
in on the machine the server runs on. The server uses the new session on its next call.

Configuration
-------------

``--public-url`` and ``--token-file`` can also be set with the ``AMAZON_ORDERS_MCP_PUBLIC_URL`` and
``AMAZON_ORDERS_MCP_TOKEN`` environment variables, which is how a container is configured (see the
`Dockerfile <https://github.com/alexdlaird/amazon-orders-mcp/blob/main/Dockerfile>`_).

A client that can send a header doesn't need to sign in. It can send the secret as a bearer token instead,
``Authorization: Bearer <secret>``.

Approved clients and their tokens are kept in ``mcp-oauth.json``, in ``amazon-orders``' config directory, readable
only by you, with each token stored as a hash. To disconnect every client, stop the server, delete that file, and
change the secret.

Troubleshooting
---------------

- A ``401 Unauthorized`` means the request had no token, or one that expired or was revoked, so connect the client
  again.
- A ``421 Misdirected Request`` means the request reached the server without its ``Host`` header rewritten to
  ``localhost``, so check that the ``add-headers`` action is in the tunnel's Traffic Policy.
- If the approval page says the link expired, more than ten minutes passed since connecting started, so start
  connecting again from your assistant.
- If clients stop connecting after a restart, check that ``--public-url`` still matches the tunnel's URL. A tunnel
  without a fixed domain gets a new URL every time it starts.
