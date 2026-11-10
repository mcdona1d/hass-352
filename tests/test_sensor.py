"""Entity wiring and restart lifecycle with minimal HA interface stubs.

These checks exercise the real integration module without installing HA;
they do not replace acceptance testing in a running Home Assistant instance.
"""

from dataclasses import field, make_dataclass
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def sensor_module(monkeypatch):
    def module(name, **attrs):
        result = ModuleType(name)
        result.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, result)
        return result

    class Entity:
        async def async_added_to_hass(self):
            pass

        def async_write_ha_state(self):
            self.written_state = self.is_on if hasattr(self, 'is_on') else self.native_value

    class CoordinatorEntity(Entity):
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, coordinator):
            self.coordinator = coordinator

        @property
        def available(self):
            return self.coordinator.last_update_success

        async def async_added_to_hass(self):
            await super().async_added_to_hass()
            self.coordinator.listener = self._handle_coordinator_update

        def _handle_coordinator_update(self):
            self.async_write_ha_state()

    class RestoreEntity(Entity):
        async def async_get_last_state(self):
            return getattr(self, "saved_state", None)

    names = ('key', 'name', 'native_unit_of_measurement', 'device_class', 'state_class',
             'icon', 'options', 'entity_registry_enabled_default', 'suggested_display_precision')
    description = make_dataclass('SensorEntityDescription',
        [(name, object, field(default=None)) for name in names], frozen=True, kw_only=True)
    clock = SimpleNamespace(now=datetime(2026, 11, 8, tzinfo=timezone.utc))
    module('homeassistant')
    module('homeassistant.components')
    module('homeassistant.components.sensor', SensorEntity=type('SensorEntity', (Entity,), {}),
           SensorEntityDescription=description,
           SensorDeviceClass=SimpleNamespace(**{key: key for key in (
               'PM25', 'VOLATILE_ORGANIC_COMPOUNDS', 'CO2', 'TEMPERATURE',
               'HUMIDITY', 'ENUM', 'SIGNAL_STRENGTH', 'DURATION')}),
           SensorStateClass=SimpleNamespace(MEASUREMENT='measurement', TOTAL_INCREASING='total_increasing'))
    module('homeassistant.config_entries', ConfigEntry=object)
    module('homeassistant.const', CONCENTRATION_MICROGRAMS_PER_CUBIC_METER='µg/m³',
           CONCENTRATION_PARTS_PER_MILLION='ppm', PERCENTAGE='%',
           UnitOfTemperature=SimpleNamespace(CELSIUS='°C'),
           UnitOfTime=SimpleNamespace(HOURS='h', MINUTES='min'))
    module('homeassistant.core', HomeAssistant=object, callback=lambda fn: fn)
    module('homeassistant.helpers')
    module('homeassistant.helpers.entity_platform', AddEntitiesCallback=object)
    module('homeassistant.helpers.restore_state', RestoreEntity=RestoreEntity)
    module('homeassistant.helpers.update_coordinator', CoordinatorEntity=CoordinatorEntity)
    module('homeassistant.util', dt=SimpleNamespace(utcnow=lambda: clock.now))
    root = Path(__file__).resolve().parents[1] / 'custom_components' / 'air352'
    package = module('air352_sensor_test')
    package.__path__ = [str(root)]
    module('air352_sensor_test.coordinator', Air352Coordinator=object)
    # Keep all test-only imports isolated, including relative imports.
    for name in ('const', 'water_usage', 'sensor'):
        spec = importlib.util.spec_from_file_location(f'{package.__name__}.{name}', root / f'{name}.py')
        loaded = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, loaded)
        spec.loader.exec_module(loaded)
    return loaded, clock, root


def coordinator(category='Humidifier', props=None):
    device = {'iotId': 'test-device', 'categoryKey': category, 'productName': 'Test'}
    return SimpleNamespace(devices=[device], device_infos={}, last_update_success=True,
                           data={'test-device': props or {}}), device


@pytest.mark.asyncio
async def test_setup_creates_timer_only_for_supported_humidifiers(sensor_module):
    sensor, _, root = sensor_module
    for category, props, count in (
        ('Humidifier', {'WaterShortage': {'value': 0}}, 1),
        ('Humidifier', {}, 0),
        ('WaterPurifier', {'WaterShortage': {'value': 0}}, 0),
    ):
        coord, _ = coordinator(category, props)
        entities = []
        await sensor.async_setup_entry(SimpleNamespace(data={'air352': {'entry': coord}}),
                                       SimpleNamespace(entry_id='entry'), entities.extend)
        timers = [entity for entity in entities if isinstance(entity, sensor.Air352WaterUsageSensor)]
        assert len(timers) == count
        if timers:
            assert timers[0]._attr_unique_id == 'test-device_WaterUsageDuration'
            for path in ('strings.json', 'translations/en.json', 'translations/zh-Hans.json'):
                translations = json.loads((root / path).read_text())
                assert timers[0]._attr_translation_key in translations['entity']['sensor']


@pytest.mark.asyncio
async def test_sensor_updates_without_getter_side_effects_and_restores(sensor_module):
    sensor, clock, _ = sensor_module
    coord, device = coordinator(props={'WaterShortage': {'value': 1}})
    entity = sensor.Air352WaterUsageSensor(coord, device)
    await entity.async_added_to_hass()
    assert entity.native_value is None
    clock.now += timedelta(minutes=1)
    coord.data['test-device']['WaterShortage'] = {'value': 0}
    coord.listener()
    assert entity.written_state == 0
    before = entity.extra_state_attributes
    clock.now += timedelta(hours=3)
    coord.listener()
    assert entity.written_state == 3
    assert entity.extra_state_attributes['last_refilled_at'] == before['last_refilled_at']
    attributes = entity.extra_state_attributes
    assert entity.native_value == 3
    assert entity.extra_state_attributes == attributes
    # A normal restart continues from the persisted refill, even while unavailable.
    restored = sensor.Air352WaterUsageSensor(coord, device)
    restored.saved_state = SimpleNamespace(attributes=json.loads(json.dumps(attributes)))
    clock.now += timedelta(hours=2)
    coord.last_update_success = False
    await restored.async_added_to_hass()
    assert not restored.available
    assert restored.native_value == 5
    coord.last_update_success = True
    coord.listener()
    assert restored.available
    assert restored.written_state == 5


@pytest.mark.asyncio
async def test_restart_detects_refill_after_saved_shortage(sensor_module):
    sensor, clock, _ = sensor_module
    coord, device = coordinator(props={'WaterShortage': {'value': 1}})
    old = sensor.Air352WaterUsageSensor(coord, device)
    await old.async_added_to_hass()
    restored = sensor.Air352WaterUsageSensor(coord, device)
    restored.saved_state = SimpleNamespace(attributes=old.extra_state_attributes)
    clock.now += timedelta(hours=2)
    refill = clock.now - timedelta(hours=1)
    coord.data['test-device']['WaterShortage'] = {'value': 0, 'time': refill.timestamp() * 1000}
    await restored.async_added_to_hass()
    assert restored.native_value == 1
    assert restored.extra_state_attributes['last_refilled_at'] == refill.isoformat()


async def setup_sensors(sensor, coord):
    entities = []
    await sensor.async_setup_entry(SimpleNamespace(data={'air352': {'entry': coord}}),
                                   SimpleNamespace(entry_id='entry'), entities.extend)
    return {entity.entity_description.key: entity for entity in entities}


@pytest.mark.asyncio
async def test_humidifier_metrics_values_units_and_translations(sensor_module):
    sensor, _, root = sensor_module
    coord, _ = coordinator(props={
        'SetHumidity': {'value': 47.0}, 'WorkMode': {'value': 2},
        'DeviceUsedTime': {'value': 877079}, 'WiFI_RSSI': {'value': -36},
    })
    entities = await setup_sensors(sensor, coord)
    assert entities['SetHumidity'].native_value == 47
    assert entities['SetHumidity'].entity_description.native_unit_of_measurement == '%'
    assert entities['WorkMode'].native_value == 'moisturizing'
    runtime = entities['DeviceUsedTime']
    assert runtime.native_value == 877079
    assert runtime.entity_description.native_unit_of_measurement == 'min'
    assert runtime.entity_description.state_class == 'total_increasing'
    assert entities['WiFI_RSSI'].native_value == -36
    assert entities['WiFI_RSSI'].entity_description.native_unit_of_measurement == 'dBm'
    assert entities['WiFI_RSSI'].entity_description.entity_registry_enabled_default is False
    for path in ('strings.json', 'translations/en.json', 'translations/zh-Hans.json'):
        translations = json.loads((root / path).read_text())['entity']['sensor']
        for key in ('SetHumidity', 'WorkMode', 'DeviceUsedTime'):
            assert entities[key]._attr_translation_key in translations
        assert set(translations['workmode']['state']) == set(entities['WorkMode'].entity_description.options)
    zh = json.loads((root / 'translations/zh-Hans.json').read_text())['entity']['sensor']
    assert zh['workmode']['state']['moisturizing'] == '保湿'


@pytest.mark.asyncio
async def test_humidifier_metrics_not_created_for_other_types_or_missing_fields(sensor_module):
    sensor, _, _ = sensor_module
    for category in ('AirPurifier', 'WaterPurifier', 'Humidifier'):
        props = {} if category == 'Humidifier' else {
            key: {'value': 1} for key in ('SetHumidity', 'WorkMode', 'DeviceUsedTime')
        }
        coord, _ = coordinator(category, props)
        assert not await setup_sensors(sensor, coord)


@pytest.mark.parametrize(('key', 'value', 'expected'), [
    ('SetHumidity', 0, 0), ('SetHumidity', 100, 100), ('SetHumidity', 47.3, 47.3),
    ('SetHumidity', -1, None), ('SetHumidity', 101, None), ('SetHumidity', 65535, None),
    ('WorkMode', 0, 'standby'), ('WorkMode', 1, 'auto'), ('WorkMode', 3, 'sleep'),
    ('WorkMode', 4, 'humidity'), ('WorkMode', 5, 'wind'), ('WorkMode', 6, None),
    ('WorkMode', 1.5, None),
    ('DeviceUsedTime', 0, 0), ('DeviceUsedTime', 65535, 65535),
    ('DeviceUsedTime', -1, None), ('DeviceUsedTime', 1.5, None),
    ('DeviceUsedTime', 1000000000, None),
    ('WiFI_RSSI', -127, -127), ('WiFI_RSSI', -1, -1),
    ('WiFI_RSSI', 0, None), ('WiFI_RSSI', -128, None), ('WiFI_RSSI', 60, None),
])
def test_metric_ranges_and_mode_mapping(sensor_module, key, value, expected):
    sensor, _, _ = sensor_module
    coord, device = coordinator(props={key: {'value': value}})
    desc = next(desc for desc in sensor.SENSOR_DESCRIPTIONS if desc.key == key)
    entity = sensor.Air352Sensor(coord, device, desc)
    assert entity.native_value == expected
    coord.data['test-device'][key] = value
    assert entity.native_value == expected


@pytest.mark.parametrize('key', ['SetHumidity', 'WorkMode', 'DeviceUsedTime', 'WiFI_RSSI'])
def test_metrics_reject_malformed_values_and_follow_read_availability(sensor_module, key):
    sensor, _, _ = sensor_module
    coord, device = coordinator()
    desc = next(desc for desc in sensor.SENSOR_DESCRIPTIONS if desc.key == key)
    entity = sensor.Air352Sensor(coord, device, desc)
    assert entity.native_value is None
    for value in (None, True, False, '1', {}, [], float('nan'), float('inf'), -float('inf')):
        coord.data['test-device'][key] = {'value': value}
        assert entity.native_value is None
    coord.data['test-device'][key] = {'value': -36 if key == 'WiFI_RSSI' else 1}
    assert entity.available
    coord.last_update_success = False
    assert not entity.available  # A failed refresh can leave old data in the coordinator.
    coord.last_update_success = True
    assert entity.available
    coord.data = {}
    assert not entity.available  # This device can fail while other devices succeed.
    coord.data = None
    assert not entity.available


@pytest.mark.asyncio
async def test_wifi_all_device_types_uses_only_rssi_not_percent(sensor_module):
    sensor, _, _ = sensor_module
    for category in ('AirPurifier', 'WaterPurifier', 'Humidifier'):
        coord, _ = coordinator(category, {'WiFI_RSSI': {'value': -36}, 'WiFI_RSSI1': {'value': 88}})
        entities = await setup_sensors(sensor, coord)
        assert list(entities) == ['WiFI_RSSI']
        assert entities['WiFI_RSSI'].native_value == -36
        coord.data['test-device'] = {'WiFI_RSSI1': {'value': 88}}
        assert entities['WiFI_RSSI'].native_value is None
        assert not await setup_sensors(sensor, coord)


@pytest.fixture
def switch_module(sensor_module, monkeypatch):
    _, _, root = sensor_module
    description = make_dataclass('SwitchEntityDescription',
        [(name, object, field(default=None)) for name in ('key', 'name', 'icon')],
        frozen=True, kw_only=True)
    ha_switch = ModuleType('homeassistant.components.switch')
    ha_switch.SwitchEntity = type('SwitchEntity', (), {})
    ha_switch.SwitchEntityDescription = description
    monkeypatch.setitem(sys.modules, ha_switch.__name__, ha_switch)
    spec = importlib.util.spec_from_file_location('air352_sensor_test.switch', root / 'switch.py')
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, loaded)
    spec.loader.exec_module(loaded)
    return loaded, root


@pytest.mark.asyncio
async def test_air_drying_switch_discovery_state_and_commands(switch_module):
    switch, root = switch_module
    coord, _ = coordinator(props={'AirDringMode': {'value': 1}})
    coord.api = SimpleNamespace(set_device_properties=AsyncMock())
    entities = []
    await switch.async_setup_entry(SimpleNamespace(data={'air352': {'entry': coord}}),
                                  SimpleNamespace(entry_id='entry'), entities.extend)
    assert len(entities) == 1
    entity = entities[0]
    assert entity.is_on is True
    assert entity._attr_unique_id == 'test-device_AirDringMode'
    await entity.async_turn_off()
    coord.api.set_device_properties.assert_awaited_with('test-device', {'AirDringMode': 0})
    assert entity.written_state is False
    await entity.async_turn_on()
    coord.api.set_device_properties.assert_awaited_with('test-device', {'AirDringMode': 1})
    assert entity.written_state is True
    # A failed write must not claim that the setting changed.
    coord.api.set_device_properties.side_effect = RuntimeError('offline')
    with pytest.raises(RuntimeError):
        await entity.async_turn_off()
    assert entity.is_on is True
    for path in ('strings.json', 'translations/en.json', 'translations/zh-Hans.json'):
        translations = json.loads((root / path).read_text())
        assert entity._attr_translation_key in translations['entity']['switch']
    coord.last_update_success = False
    assert not entity.available
    coord.last_update_success = True
    assert entity.available
    for value in (None, '0', '1', 2, {}, [], float('nan')):
        coord.data['test-device']['AirDringMode'] = {'value': value}
        assert entity.is_on is None
    coord.data['test-device']['AirDringMode'] = 0
    assert entity.is_on is False
    coord.data['test-device']['AirDringMode'] = True
    assert entity.is_on is True
    coord.data = None
    assert entity.is_on is None
    assert not entity.available


@pytest.mark.asyncio
async def test_air_drying_switch_only_on_supported_humidifiers(switch_module):
    switch, _ = switch_module
    for category, props in (
        ('Humidifier', {}),
        ('AirPurifier', {'AirDringMode': {'value': 1}}),
        ('WaterPurifier', {'AirDringMode': {'value': 1}}),
    ):
        coord, _ = coordinator(category, props)
        entities = []
        await switch.async_setup_entry(SimpleNamespace(data={'air352': {'entry': coord}}),
                                      SimpleNamespace(entry_id='entry'), entities.extend)
        assert entities == []
