__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import copy
import json
from importlib import resources
from typing import Any, ClassVar, Dict

from pydantic import BaseModel, ConfigDict, Field, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

#: Each tool's description, parameter text, and output schema, generated from ``amazon-orders`` by
#: ``make schema``.
SCHEMA: Dict[str, Any] = json.loads(resources.files("amazonorders_mcp").joinpath("schema.json").read_text("utf-8"))


def tool_description(tool: str) -> str:
    """
    Get a tool's description, from the docstring of the ``amazon-orders`` call it wraps.

    :param tool: The tool's name.
    :return: The tool's description.
    """
    return SCHEMA["tools"][tool]["description"]


def parameter_field(tool: str,
                    parameter: str) -> Any:
    """
    Build a tool parameter's ``Field``, described from the docstring of the ``amazon-orders`` call it wraps.

    :param tool: The tool's name.
    :param parameter: The parameter's name.
    :return: The parameter's ``Field``.
    """
    return Field(description=SCHEMA["tools"][tool]["parameters"][parameter])


class ToolOutput(BaseModel):
    """
    A tool's result, published with the output schema generated for it, so a client sees every field's type and
    description. Fields are kept as they are, including any a later ``amazon-orders`` adds.
    """

    model_config = ConfigDict(extra="allow")

    #: The tool whose output schema this is.
    tool: ClassVar[str]

    @classmethod
    def __get_pydantic_json_schema__(cls,
                                     core_schema: CoreSchema,
                                     handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return copy.deepcopy(SCHEMA["tools"][cls.tool]["output_schema"])


class OrdersPage(ToolOutput):
    tool = "get_orders"


class OrderDetails(ToolOutput):
    tool = "get_order"


class OrderSearchPage(ToolOutput):
    tool = "search_orders"


class InvoiceList(ToolOutput):
    tool = "get_invoices"


class TrackingDetails(ToolOutput):
    tool = "get_tracking"


class TransactionsPage(ToolOutput):
    tool = "get_transactions"


class GiftCardsPage(ToolOutput):
    tool = "get_gift_cards"


class PrimePaymentList(ToolOutput):
    tool = "get_prime_payments"


class ChargeMatches(BaseModel):
    """
    The ``find_charge`` tool's result, published with an output schema built from the generated one for
    ``get_transactions``, so it follows it when ``amazon-orders`` changes. Each match's Order is the one ``get_order``
    returns, so it's described by reference rather than repeating that tool's schema.
    """

    model_config = ConfigDict(extra="allow")

    @classmethod
    def __get_pydantic_json_schema__(cls,
                                     core_schema: CoreSchema,
                                     handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        transactions = SCHEMA["tools"]["get_transactions"]["output_schema"]["properties"]["items"]
        transaction = copy.deepcopy(transactions["items"])
        transaction["properties"]["order"] = {
            "type": "object",
            "description": "The Transaction's Order, as `get_order` returns it, with its Items. Absent when the "
                           "Transaction has no Order number.",
            "additionalProperties": True,
        }

        return {
            "type": "object",
            "description": "The Transactions that match the charge, each with its Order.",
            "properties": {
                "items": {"type": "array", "description": "The matching Transactions.", "items": transaction},
                "note": {"type": "string", "description": "Why nothing matched, when nothing did."},
            },
            "additionalProperties": True,
        }
