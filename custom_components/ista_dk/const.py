"""Constants for the ista Denmark integration."""

from datetime import timedelta
from enum import StrEnum

DOMAIN = "ista_dk"

AUTH_URL = "https://prod.istaonlinebeta.dk/token"
METERS_URL = "https://prod.istaonlinebeta.dk/api/Meters"
USAGE_URL = "https://graphs.istaonlinebeta.dk/Overview/Usage_{series}_Data"
STATEMENTS_URL = "https://prod.istaonlinebeta.dk/api/PdfContentList"
STATEMENT_URL = (
    "https://prod.istaonlinebeta.dk/api/Download/"
    "{cons_id}/{document_id}/{works_id}/{inst_id}"
)

STATEMENT_ACCOUNT_ID = "CONSUMER_DIVISION_STATEMENT"
STATEMENT_TYPES = frozenset({"Brugsvandsregnskab", "Varmeregnskab"})

UPDATE_INTERVAL = timedelta(hours=6)


class MeterKind(StrEnum):
    """The meter kinds ista reports."""

    COLD_WATER = "cold_water"
    HOT_WATER = "hot_water"
    HEAT = "heat"


class PriceComponent(StrEnum):
    """The unit prices printed on the settlement statements."""

    BRUGSVAND = "brugsvand"
    VARMT_VAND = "varmt_vand"
    VARME = "varme"


METER_TYPE_TO_KIND: dict[str, MeterKind] = {
    "CW": MeterKind.COLD_WATER,
    "HW": MeterKind.HOT_WATER,
    "ENERGY": MeterKind.HEAT,
}

USAGE_SERIES: dict[MeterKind, str] = {
    MeterKind.COLD_WATER: "WaterCold",
    MeterKind.HOT_WATER: "WaterHot",
    MeterKind.HEAT: "Energy",
}

CONF_ESTIMATE_AFTER = "estimate_after"
ESTIMATE_OPTIONS: dict[PriceComponent, str] = {
    PriceComponent.BRUGSVAND: "estimate_brugsvand",
    PriceComponent.VARMT_VAND: "estimate_varmt_vand",
    PriceComponent.VARME: "estimate_varme",
}

ATTR_READING_DATE = "reading_date"
ATTR_PERIOD_START = "period_start"
ATTR_PERIOD_END = "period_end"
ATTR_SOURCE = "source"

SOURCE_SETTLED = "settled"
SOURCE_ESTIMATE = "estimate"
