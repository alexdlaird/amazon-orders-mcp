#!/usr/bin/env python

__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import asyncio
import json
import os
import re
import shutil
from typing import Any, Dict, List

from mcp.client import Client

from amazonorders_mcp import __version__
from amazonorders_mcp.server import create_server

ROOT_DIR = os.path.normpath(os.path.join(os.path.abspath(os.path.dirname(__file__)), ".."))
TEMPLATE_DIR = os.path.join(ROOT_DIR, "mcpb")
STAGING_DIR = os.path.join(ROOT_DIR, "build", "mcpb")


async def _list_tools() -> List[Dict[str, Any]]:
    async with Client(create_server()) as client:
        tools = (await client.list_tools()).tools
    return [{"name": tool.name, "description": tool.description} for tool in tools]


def _stage_template() -> None:
    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    shutil.copytree(TEMPLATE_DIR, STAGING_DIR)


def _stamp_manifest(tools: List[Dict[str, Any]]) -> None:
    manifest_path = os.path.join(STAGING_DIR, "manifest.json")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest["version"] = __version__
    manifest["tools"] = tools
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def _pin_dependency() -> None:
    pyproject_path = os.path.join(STAGING_DIR, "pyproject.toml")
    with open(pyproject_path, encoding="utf-8") as f:
        pyproject = f.read()
    pyproject = re.sub(r'^version = ".*"$', f'version = "{__version__}"', pyproject, flags=re.MULTILINE)
    pyproject = pyproject.replace('"amazon-orders-mcp",', f'"amazon-orders-mcp=={__version__}",')
    with open(pyproject_path, "w", encoding="utf-8") as f:
        f.write(pyproject)


def build_mcpb() -> None:
    """
    Stage the MCPB bundle in ``build/mcpb``: the ``mcpb/`` template, pinned to this version of ``amazon-orders-mcp``,
    with the manifest's tools read from the server itself.
    """
    _stage_template()
    _stamp_manifest(asyncio.run(_list_tools()))
    _pin_dependency()

    print(STAGING_DIR)


if __name__ == "__main__":
    build_mcpb()
