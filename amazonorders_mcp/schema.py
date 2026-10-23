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


class OrderHistoryPage(ToolOutput):
    tool = "get_order_history"


class FamilyMemberList(ToolOutput):
    tool = "get_family_members"


class OrderDetails(ToolOutput):
    tool = "get_order"


class OrderSearchPage(ToolOutput):
    tool = "search_orders"


class InvoicePdfList(ToolOutput):
    tool = "get_invoice_pdfs"


class TrackingDetails(ToolOutput):
    tool = "get_tracking"


class TransactionsPage(ToolOutput):
    tool = "get_transactions"


class GiftCardActivityList(ToolOutput):
    tool = "get_gift_card_activity"


class PrimePaymentList(ToolOutput):
    tool = "get_prime_payments"
