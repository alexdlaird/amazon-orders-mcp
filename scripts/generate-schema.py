#!/usr/bin/env python

__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import inspect
import json
import os
import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Type

import docstring_parser
from amazonorders.entity.family_member import FamilyMember
from amazonorders.entity.gift_card_activity import GiftCardActivity
from amazonorders.entity.invoice_pdf import InvoicePdf
from amazonorders.entity.item import Item
from amazonorders.entity.order import Order
from amazonorders.entity.order_search_result import OrderSearchResult
from amazonorders.entity.parsable import Parsable
from amazonorders.entity.prime_payment import PrimePayment
from amazonorders.entity.recipient import Recipient
from amazonorders.entity.seller import Seller
from amazonorders.entity.shipment import Shipment
from amazonorders.entity.tracking import Tracking
from amazonorders.entity.transaction import Transaction
from amazonorders.gift_cards import AmazonGiftCards
from amazonorders.orders import AmazonOrders
from amazonorders.paging import PagedResults
from amazonorders.prime import AmazonPrime
from amazonorders.transactions import AmazonTransactions
from sphinx.pycode import ModuleAnalyzer

ROOT_DIR = os.path.normpath(os.path.join(os.path.abspath(os.path.dirname(__file__)), ".."))
SCHEMA_PATH = os.path.join(ROOT_DIR, "amazonorders_mcp", "schema.json")
SCHEMA_COMMENT = ("AUTO-GENERATED — DO NOT EDIT. Generated from the installed amazon-orders' docstrings and field "
                  "docs, via: scripts/generate-schema.py (make schema)")

ENTITIES: List[Type[Parsable]] = [Order, Shipment, Item, Seller, Recipient, Tracking, Transaction, GiftCardActivity,
                                  FamilyMember, InvoicePdf, PrimePayment, OrderSearchResult]

PRIMITIVE_SCHEMAS = {
    "bool": {"type": "boolean"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "str": {"type": "string"},
    "date": {"type": "string", "format": "date"},
}

SPHINX_ROLE_REGEX = re.compile(r":[a-z]+:`~?([^`]+)`")
SPHINX_LITERAL_REGEX = re.compile(r"``([^`]+)``")
OPTIONAL_REGEX = re.compile(r"^Optional\[(.+)\]$")
LIST_REGEX = re.compile(r"^List\[(.+)\]$")


class Tool(NamedTuple):
    """
    A tool the server wraps a core call with, and where its text and output come from.
    """

    #: The tool's name.
    name: str
    #: The core method the tool wraps, whose docstring gives the tool's text.
    call: Callable[..., Any]
    #: The core method's parameters the tool takes.
    parameters: List[str]
    #: The entity the tool returns.
    entity: Type[Parsable]
    #: ``"page"`` for a :class:`~amazonorders.paging.PagedResults` of ``entity``, ``"list"`` for a list of them, or
    #: ``"entity"`` for one.
    output: str
    #: Parameters whose text comes from another core method, where ``call``'s defers to it.
    parameter_calls: Optional[Dict[str, Callable[..., Any]]] = None


TOOLS = [
    Tool("get_order_history", AmazonOrders.get_order_history_page,
         ["year", "time_filter", "order_filter", "family_member_id", "start_index", "full_details", "limit", "cursor"],
         Order, "page",
         parameter_calls={"time_filter": AmazonOrders.get_order_history,
                          "order_filter": AmazonOrders.get_order_history,
                          "family_member_id": AmazonOrders.get_order_history}),
    Tool("get_order", AmazonOrders.get_order, ["order_id"], Order, "entity"),
    Tool("get_tracking", AmazonOrders.get_tracking, ["order_id", "shipment_id"], Tracking, "entity"),
    Tool("search_orders", AmazonOrders.search_orders, ["query", "limit", "cursor"], OrderSearchResult, "page",
         parameter_calls={"limit": AmazonOrders.search_orders_page,
                          "cursor": AmazonOrders.search_orders_page}),
    Tool("get_invoice_pdfs", AmazonOrders.get_invoice_pdfs, ["order_id"], InvoicePdf, "list"),
    Tool("get_transactions", AmazonTransactions.get_transactions_page, ["days", "order_id", "limit", "cursor"],
         Transaction, "page"),
    Tool("get_gift_card_activity", AmazonGiftCards.get_gift_card_activity, ["days"], GiftCardActivity, "list"),
    Tool("get_prime_payments", AmazonPrime.get_prime_payments, [], PrimePayment, "list"),
    Tool("get_family_members", AmazonOrders.get_family_members, [], FamilyMember, "list"),
]


def _plain_text(docstring: str) -> str:
    """
    Turn a docstring's Sphinx roles and literals into the single-backtick text an MCP client reads, and join its
    wrapped lines.
    """
    text = SPHINX_ROLE_REGEX.sub(lambda match: f"`{match.group(1).split('.')[-1]}`", docstring)
    text = SPHINX_LITERAL_REGEX.sub(r"`\1`", text)

    return " ".join(text.split())


def _first_sentence(text: str) -> str:
    return text.split(". ")[0].rstrip(".") + "."


def _attribute_docs(module_name: str) -> Dict[str, Dict[str, Any]]:
    analyzer = ModuleAnalyzer.for_module(module_name)
    analyzer.analyze()

    attributes: Dict[str, Dict[str, Any]] = {}
    for (class_name, attribute), doc_lines in analyzer.attr_docs.items():
        attributes.setdefault(class_name, {})[attribute] = {
            "description": _plain_text(" ".join(doc_lines)),
            "annotation": analyzer.annotations.get((class_name, attribute)),
        }

    return attributes


def _annotation_schema(annotation: str) -> Dict[str, Any]:
    optional_match = OPTIONAL_REGEX.match(annotation)
    if optional_match:
        return _annotation_schema(optional_match.group(1))

    list_match = LIST_REGEX.match(annotation)
    if list_match:
        return {"type": "array", "items": _annotation_schema(list_match.group(1))}

    if annotation in PRIMITIVE_SCHEMAS:
        return dict(PRIMITIVE_SCHEMAS[annotation])

    for entity in ENTITIES:
        if annotation == entity.__name__:
            return _entity_schema(entity)

    raise ValueError(f"No JSON Schema for the annotation \"{annotation}\", add it to generate-schema.py.")


def _entity_schema(entity: Type[Parsable]) -> Dict[str, Any]:
    """
    Build an entity's schema from its ``#:`` field docs. Nested entities are inlined, not referenced, since the SDK
    can't publish a schema with ``$ref``. Every field is optional and others are allowed, so a field added in a
    later ``amazon-orders`` never makes a result invalid.
    """
    fields = _attribute_docs(entity.__module__)[entity.__name__]

    properties = {}
    for field, field_doc in fields.items():
        if field_doc["annotation"] is None:
            raise ValueError(f"{entity.__name__}.{field} has no type annotation.")

        properties[field] = {**_annotation_schema(field_doc["annotation"]), "description": field_doc["description"]}

    return {
        "type": "object",
        "description": _plain_text(inspect.getdoc(entity) or ""),
        "properties": properties,
        "additionalProperties": True,
    }


def _output_schema(tool: Tool,
                   docstring: docstring_parser.Docstring) -> Dict[str, Any]:
    description = _plain_text((docstring.returns and docstring.returns.description) or "")

    if tool.output == "entity":
        return {**_entity_schema(tool.entity), "description": description}

    items_schema = {"type": "array", "items": _entity_schema(tool.entity)}
    if tool.output == "list":
        return {
            "type": "object",
            "description": description,
            "properties": {"items": {**items_schema, "description": description}},
            "additionalProperties": True,
        }

    paging_fields = _attribute_docs(PagedResults.__module__)[PagedResults.__name__]
    return {
        "type": "object",
        "description": description,
        "properties": {
            "items": {**items_schema, "description": paging_fields["items"]["description"]},
            "cursor": {"type": ["object", "null"], "description": paging_fields["cursor"]["description"]},
            "has_more": {"type": "boolean",
                         "description": _plain_text(inspect.getdoc(PagedResults.has_more) or "")},
        },
        "additionalProperties": True,
    }


def _parameter_descriptions(tool: Tool) -> Dict[str, str]:
    descriptions = {}
    for parameter in tool.parameters:
        call = (tool.parameter_calls or {}).get(parameter, tool.call)
        docstring_params = {param.arg_name: param for param in docstring_parser.parse(call.__doc__ or "").params}
        if parameter not in docstring_params:
            raise ValueError(f"{call.__qualname__} has no docs for \"{parameter}\".")

        descriptions[parameter] = _plain_text(docstring_params[parameter].description or "")

    return descriptions


def _tool_schema(tool: Tool) -> Dict[str, Any]:
    docstring = docstring_parser.parse(tool.call.__doc__ or "")

    return {
        "description": _first_sentence(_plain_text(docstring.description or "")),
        "parameters": _parameter_descriptions(tool),
        "output_schema": _output_schema(tool, docstring),
    }


def generate_schema() -> None:
    """
    Write ``amazonorders_mcp/schema.json`` from the installed ``amazon-orders``: each tool's description, parameter
    text, and output schema, from the docstrings and ``#:`` field docs of the core calls and entities it returns.
    """
    schema = {
        "$comment": SCHEMA_COMMENT,
        "tools": {tool.name: _tool_schema(tool) for tool in TOOLS},
    }

    with open(SCHEMA_PATH, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(SCHEMA_PATH)


if __name__ == "__main__":
    generate_schema()
