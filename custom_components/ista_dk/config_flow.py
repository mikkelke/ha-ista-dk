"""Config flow for ista Denmark."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    UnitOfEnergy,
    UnitOfVolume,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import IstaDkApi, IstaDkAuthError, IstaDkError, LoginInfo
from .const import CONF_ESTIMATE_AFTER, DOMAIN, ESTIMATE_OPTIONS, PriceComponent
from .statements import StatementPrices

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.TEXT,
                autocomplete="username",
            )
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD,
                autocomplete="current-password",
            )
        ),
    }
)


def _price_selector(unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0,
            step="any",
            mode=NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


def _estimate_schema(currency: str) -> vol.Schema:
    per_volume = f"{currency}/{UnitOfVolume.CUBIC_METERS}"
    per_energy = f"{currency}/{UnitOfEnergy.KILO_WATT_HOUR}"
    units = {
        PriceComponent.BRUGSVAND: per_volume,
        PriceComponent.VARMT_VAND: per_volume,
        PriceComponent.VARME: per_energy,
    }
    return vol.Schema(
        {
            vol.Optional(ESTIMATE_OPTIONS[component]): _price_selector(unit)
            for component, unit in units.items()
        }
    )


def _credentials(user_input: Mapping[str, Any]) -> dict[str, str]:
    return {
        CONF_USERNAME: user_input[CONF_USERNAME].strip(),
        CONF_PASSWORD: user_input[CONF_PASSWORD],
    }


class IstaDkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ista Denmark."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> IstaDkOptionsFlow:
        """Return the options flow for estimated prices."""
        return IstaDkOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the consumer number and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = _credentials(user_input)
            if info := await self._async_validate(credentials, errors):
                await self.async_set_unique_id(info.cons_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=info.first_name or f"ista {info.cons_id}",
                    data=credentials,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA,
                {CONF_USERNAME: user_input[CONF_USERNAME]} if user_input else None,
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after ista rejected the stored login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the login again."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            credentials = _credentials(user_input)
            if info := await self._async_validate(credentials, errors):
                await self.async_set_unique_id(info.cons_id)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates=credentials
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA,
                {CONF_USERNAME: (user_input or reauth_entry.data)[CONF_USERNAME]},
            ),
            description_placeholders={CONF_USERNAME: reauth_entry.data[CONF_USERNAME]},
            errors=errors,
        )

    async def _async_validate(
        self, credentials: dict[str, str], errors: dict[str, str]
    ) -> LoginInfo | None:
        """Log in; on failure store the error key in ``errors`` and return None."""
        api = IstaDkApi(
            async_get_clientsession(self.hass),
            credentials[CONF_USERNAME],
            credentials[CONF_PASSWORD],
        )
        try:
            return await api.async_login()
        except IstaDkAuthError:
            errors["base"] = "invalid_auth"
        except IstaDkError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        return None


class IstaDkOptionsFlow(OptionsFlowWithReload):
    """Set estimated prices for the period no statement has settled yet."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the estimated unit prices."""
        if user_input is not None:
            options: dict[str, Any] = {
                key: float(user_input[key])
                for key in ESTIMATE_OPTIONS.values()
                if user_input.get(key) is not None
            }
            if options:
                statements = StatementPrices(self.hass, self.config_entry.entry_id)
                await statements.async_load()
                if newest_end := statements.table().newest_end:
                    options[CONF_ESTIMATE_AFTER] = newest_end.isoformat()
            return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _estimate_schema(self.hass.config.currency),
                self.config_entry.options,
            ),
        )
