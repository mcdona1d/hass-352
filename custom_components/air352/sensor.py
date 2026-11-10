from __future__ import annotations

from dataclasses import dataclass
import math

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MANUFACTURER, DEVICE_TYPE_AIR, DEVICE_TYPE_PURIFIER, DEVICE_TYPE_HUMIDIFIER
from .coordinator import Air352Coordinator
from .water_usage import WaterUsageTracker, water_shortage_value


@dataclass(frozen=True, kw_only=True)
class Air352SensorDescription(SensorEntityDescription):
    category_keys: tuple[str, ...] = ()


HUMIDIFIER_WORK_MODES = {
    0: "standby",
    1: "auto",
    2: "moisturizing",
    3: "sleep",
    4: "humidity",
    5: "wind",
}


SENSOR_DESCRIPTIONS: list[Air352SensorDescription] = [
    # ── Air purifier sensors ──
    Air352SensorDescription(
        key="PM25", name="PM2.5",
        native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        device_class=SensorDeviceClass.PM25, state_class=SensorStateClass.MEASUREMENT,
        category_keys=(DEVICE_TYPE_AIR, DEVICE_TYPE_HUMIDIFIER),
    ),
    Air352SensorDescription(
        key="TVOC", name="TVOC",
        native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        device_class=SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS,
        state_class=SensorStateClass.MEASUREMENT,
        category_keys=(DEVICE_TYPE_AIR,),
    ),
    Air352SensorDescription(
        key="HCHO", name="Formaldehyde",
        native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        state_class=SensorStateClass.MEASUREMENT,
        category_keys=(DEVICE_TYPE_AIR,),
    ),
    Air352SensorDescription(
        key="CO2", name="CO2",
        native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
        device_class=SensorDeviceClass.CO2, state_class=SensorStateClass.MEASUREMENT,
        category_keys=(DEVICE_TYPE_AIR,),
    ),
    Air352SensorDescription(
        key="CurrentTemperature", name="Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT,
        category_keys=(DEVICE_TYPE_AIR, DEVICE_TYPE_HUMIDIFIER),
    ),
    Air352SensorDescription(
        key="RelativeHumidity", name="Humidity",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY, state_class=SensorStateClass.MEASUREMENT,
        category_keys=(DEVICE_TYPE_AIR, DEVICE_TYPE_HUMIDIFIER),
    ),
    # The enum allows localized status labels instead of on/off.
    Air352SensorDescription(
        key="WaterShortage",
        device_class=SensorDeviceClass.ENUM,
        options=["normal", "water_shortage"],
        icon="mdi:water",
        category_keys=(DEVICE_TYPE_HUMIDIFIER,),
    ),
    Air352SensorDescription(
        key="SetHumidity",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
        category_keys=(DEVICE_TYPE_HUMIDIFIER,),
    ),
    Air352SensorDescription(
        key="WorkMode",
        device_class=SensorDeviceClass.ENUM,
        options=list(HUMIDIFIER_WORK_MODES.values()),
        icon="mdi:air-humidifier",
        category_keys=(DEVICE_TYPE_HUMIDIFIER,),
    ),
    Air352SensorDescription(
        key="DeviceUsedTime",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        icon="mdi:timer-outline",
        category_keys=(DEVICE_TYPE_HUMIDIFIER,),
    ),
    # ── Filter life sensors (all device types) ──
    Air352SensorDescription(
        key="FilterLifeTimePercent_1", name="Filter 1 Life",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:air-filter",
        category_keys=(DEVICE_TYPE_AIR, DEVICE_TYPE_PURIFIER, DEVICE_TYPE_HUMIDIFIER),
    ),
    Air352SensorDescription(
        key="FilterLifeTimePercent_2", name="Filter 2 Life",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:air-filter",
        category_keys=(DEVICE_TYPE_AIR, DEVICE_TYPE_PURIFIER),
    ),
    Air352SensorDescription(
        key="FilterLifeTimePercent_3", name="Filter 3 Life",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:air-filter",
        category_keys=(DEVICE_TYPE_AIR,),
    ),
    # ── Water purifier sensors ──
    Air352SensorDescription(
        key="FinishedWaterTDS", name="Output TDS",
        native_unit_of_measurement="ppm", state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-check",
        category_keys=(DEVICE_TYPE_PURIFIER,),
    ),
    Air352SensorDescription(
        key="RawWaterTDS", name="Input TDS",
        native_unit_of_measurement="ppm", state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water",
        category_keys=(DEVICE_TYPE_PURIFIER,),
    ),
    Air352SensorDescription(
        key="WaterTemperature", name="Water Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT,
        category_keys=(DEVICE_TYPE_PURIFIER,),
    ),
    Air352SensorDescription(
        key="TotalPureWater", name="Total Pure Water",
        native_unit_of_measurement="mL", state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:water-plus",
        category_keys=(DEVICE_TYPE_PURIFIER,),
    ),
    # ── WiFi signal ──
    Air352SensorDescription(
        key="WiFI_RSSI", name="WiFi Signal",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH, state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        category_keys=(DEVICE_TYPE_AIR, DEVICE_TYPE_PURIFIER, DEVICE_TYPE_HUMIDIFIER),
    ),
]


WATER_USAGE_DESCRIPTION = Air352SensorDescription(
    key="WaterUsageDuration",
    device_class=SensorDeviceClass.DURATION,
    native_unit_of_measurement=UnitOfTime.HOURS,
    suggested_display_precision=2,
    icon="mdi:timer-outline",
    category_keys=(DEVICE_TYPE_HUMIDIFIER,),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: Air352Coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for device in coordinator.devices:
        category = device.get("categoryKey", "")
        iot_id = device["iotId"]
        props = coordinator.data.get(iot_id, {}) if coordinator.data else {}
        for desc in SENSOR_DESCRIPTIONS:
            if category in desc.category_keys and desc.key in props:
                entities.append(Air352Sensor(coordinator, device, desc))
        if category == DEVICE_TYPE_HUMIDIFIER and "WaterShortage" in props:
            entities.append(Air352WaterUsageSensor(coordinator, device))
    async_add_entities(entities)


class Air352Sensor(CoordinatorEntity[Air352Coordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: Air352SensorDescription

    def __init__(self, coordinator: Air352Coordinator, device: dict, description: Air352SensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._iot_id = device["iotId"]
        self._attr_unique_id = f"{self._iot_id}_{description.key}"
        self._attr_translation_key = description.key.lower()
        info = coordinator.device_infos.get(self._iot_id, {})
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._iot_id)},
            "name": device.get("productName", "352 Device"),
            "manufacturer": MANUFACTURER,
            "model": info.get("firmwareVersion", device.get("productModel", "")),
            "sw_version": info.get("firmwareVersion"),
            "hw_version": info.get("mac"),
        }

    @property
    def native_value(self):
        props = (self.coordinator.data or {}).get(self._iot_id, {})
        prop = props.get(self.entity_description.key)
        if prop is None:
            return None
        val = prop.get("value") if isinstance(prop, dict) else prop
        if self.entity_description.key == "WaterShortage":
            return {0: "normal", 1: "water_shortage"}.get(water_shortage_value(val))
        if self.entity_description.key in (
            "SetHumidity", "WorkMode", "DeviceUsedTime", "WiFI_RSSI"
        ):
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                return None
            if self.entity_description.key == "SetHumidity":
                return val if 0 <= val <= 100 else None
            if self.entity_description.key == "WorkMode":
                return HUMIDIFIER_WORK_MODES.get(val)
            if self.entity_description.key == "DeviceUsedTime":
                # 65535 is a valid minute count, not a warm-up sentinel here.
                return val if 0 <= val <= 999999999 and val == int(val) else None
            return val if -127 <= val <= -1 else None
        if val == 65535:
            return None
        return val

    @property
    def available(self) -> bool:
        return super().available and self._iot_id in (self.coordinator.data or {})


class Air352WaterUsageSensor(Air352Sensor, RestoreEntity):
    """Elapsed time since the most recently observed refill, restored on startup."""

    def __init__(self, coordinator: Air352Coordinator, device: dict) -> None:
        super().__init__(coordinator, device, WATER_USAGE_DESCRIPTION)
        self._tracker = WaterUsageTracker()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (state := await self.async_get_last_state()) is not None:
            self._tracker = WaterUsageTracker.from_attributes(state.attributes, dt_util.utcnow())
        self._observe_water_status()

    @callback
    def _observe_water_status(self) -> None:
        if not self.coordinator.last_update_success:
            return
        props = (self.coordinator.data or {}).get(self._iot_id, {})
        self._tracker.observe(props.get("WaterShortage"), dt_util.utcnow())

    @callback
    def _handle_coordinator_update(self) -> None:
        self._observe_water_status()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float | None:
        return self._tracker.elapsed_hours(dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict:
        return self._tracker.attributes()

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and super().available
