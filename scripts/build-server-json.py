#!/usr/bin/env python

__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import json
import os

from amazonorders_mcp import __version__

ROOT_DIR = os.path.normpath(os.path.join(os.path.abspath(os.path.dirname(__file__)), ".."))
TEMPLATE_PATH = os.path.join(ROOT_DIR, "server.json")
OUTPUT_PATH = os.path.join(ROOT_DIR, "build", "server.json")


def build_server_json() -> None:
    """
    Write ``build/server.json`` for the MCP Registry: the ``server.json`` template, stamped with this version of
    ``amazon-orders-mcp``.
    """
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        server = json.load(f)
    server["version"] = __version__
    for package in server["packages"]:
        package["version"] = __version__

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(server, f, indent=2)
        f.write("\n")

    print(OUTPUT_PATH)


if __name__ == "__main__":
    build_server_json()
