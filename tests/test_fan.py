"""Fan feedback tests using real Home Assistant states and event listeners."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from homeassistant.core import HomeAssistant, State
import voluptuous as vol
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.smartir import fan as fan_module

POWER = 'binary_sensor.living_room_quat_tran_s60_plug_quat_tran_running'
WATTS = 'sensor.sonoff_1002a3d31c_power'
SPEEDS = [str(i) for i in range(1, 10)]
# Synthetic ranges for tests only, not Panasonic calibration data.
RANGES = {speed: [10 * i, 10 * (i + 1)] for i, speed in enumerate(SPEEDS)}
DEVICE = json.loads((Path(__file__).resolve().parents[1] / 'codes/fan/1221.json').read_text())
BASE_CONFIG = {
    'platform': 'smartir', 'device_code': 1221,
    'controller_data': 'remote.test', 'power_sensor': POWER,
}


@pytest_asyncio.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    yield instance
    await instance.async_stop()


@pytest.fixture
def timers(monkeypatch):
    scheduled = []

    def schedule(hass, delay, action):
        timer = Mock(delay=delay, action=action, cancelled=False)
        def cancel():
            timer.cancelled = True
        scheduled.append(timer)
        return cancel

    monkeypatch.setattr(fan_module, 'async_call_later', schedule)
    return scheduled


async def create_fan(hass, monkeypatch, *, feedback=True, restored=None):
    config = dict(BASE_CONFIG)
    if feedback:
        config.update(power_speed_sensor=WATTS, power_speed_map=RANGES,
                      power_speed_delay=10)
    controller = Mock(send=AsyncMock())
    monkeypatch.setattr(fan_module, 'get_controller', lambda *args: controller)
    monkeypatch.setattr(RestoreEntity, 'async_added_to_hass', AsyncMock())
    monkeypatch.setattr(RestoreEntity, 'async_get_last_state',
                        AsyncMock(return_value=restored))
    entity = fan_module.SmartIRFan(hass, fan_module.PLATFORM_SCHEMA(config), DEVICE)
    entity.entity_id = 'fan.test'
    entity.async_write_ha_state = Mock()
    await entity.async_added_to_hass()
    return entity


async def set_state(hass, entity_id, value):
    if value is None:
        hass.states.async_remove(entity_id)
    else:
        hass.states.async_set(entity_id, value)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_startup_all_nine_speeds_without_ir(hass, monkeypatch, timers):
    hass.states.async_set(POWER, 'on')
    hass.states.async_set(WATTS, '5')
    entity = await create_fan(hass, monkeypatch)
    assert entity.state == 'on'
    assert entity.percentage is None
    assert timers[0].delay == 10
    for i, speed in enumerate(SPEEDS):
        await set_state(hass, WATTS, str(10 * i + 5))
        timers[-1].action(None)
        assert entity._speed == speed
        assert entity.percentage == (i + 1) * 100 // 9
        assert entity.last_on_speed == speed
    entity._controller.send.assert_not_called()


@pytest.mark.asyncio
async def test_jitter_same_range_does_not_restart_delay(hass, monkeypatch, timers):
    hass.states.async_set(POWER, 'on')
    hass.states.async_set(WATTS, '35')
    entity = await create_fan(hass, monkeypatch)
    first = timers[-1]
    await set_state(hass, WATTS, '36')
    assert timers[-1] is first
    await set_state(hass, WATTS, '41')
    assert first.cancelled
    second = timers[-1]
    await set_state(hass, WATTS, '39')
    assert second.cancelled
    assert entity.percentage is None
    timers[-1].action(None)
    assert entity._speed == '4'
    # Crossing back to the already confirmed range cancels a new candidate.
    await set_state(hass, WATTS, '41')
    candidate = timers[-1]
    await set_state(hass, WATTS, '38')
    assert candidate.cancelled
    assert entity._speed == '4'
    entity._controller.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['unknown', 'unavailable', 'NaN', 'inf', '-1', '90', 'bad', None])
async def test_invalid_watts_cancel_and_preserve_speed(hass, monkeypatch, timers, invalid):
    hass.states.async_set(POWER, 'on')
    hass.states.async_set(WATTS, '35')
    entity = await create_fan(hass, monkeypatch)
    timers[-1].action(None)
    await set_state(hass, WATTS, '45')
    pending = timers[-1]
    await set_state(hass, WATTS, invalid)
    assert pending.cancelled
    assert entity._speed == '4'
    assert entity.state == 'on'
    await set_state(hass, WATTS, '46')
    assert timers[-1] is not pending
    timers[-1].action(None)
    assert entity._speed == '5'
    entity._controller.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('running', ['off', 'unknown', 'unavailable', None])
async def test_binary_feedback_gates_timer(hass, monkeypatch, timers, running):
    hass.states.async_set(POWER, 'on')
    hass.states.async_set(WATTS, '35')
    entity = await create_fan(hass, monkeypatch)
    timers[-1].action(None)
    await set_state(hass, WATTS, '45')
    pending = timers[-1]
    await set_state(hass, POWER, running)
    assert pending.cancelled
    assert entity.state == ('off' if running == 'off' else 'on')
    assert entity.percentage == (0 if running == 'off' else 44)
    # Even a late callback must recheck current feedback.
    entity._pending_speed = '5'
    entity._apply_power_speed(None)
    assert entity._speed == ('off' if running == 'off' else '4')
    await set_state(hass, POWER, 'on')
    timers[-1].action(None)
    assert entity._speed == '5'
    entity._controller.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('running', ['on', 'off', 'unknown'])
async def test_restart_and_current_sensor_state(hass, monkeypatch, timers, running):
    hass.states.async_set(POWER, running)
    hass.states.async_set(WATTS, '55')
    restored = State('fan.test', 'on', {'percentage': 44, 'last_on_speed': '4'})
    entity = await create_fan(hass, monkeypatch, restored=restored)
    if running == 'on':
        assert entity.percentage == 44
        timers[-1].action(None)
        assert entity._speed == '6'
    elif running == 'off':
        assert entity.state == 'off'
        assert entity.percentage == 0
        assert not timers
    else:
        assert entity.percentage == 44
        assert not timers
    entity._controller.send.assert_not_called()


@pytest.mark.asyncio
async def test_initial_missing_sensors_and_legacy_binary_only(hass, monkeypatch, timers):
    entity = await create_fan(hass, monkeypatch, feedback=False)
    assert entity.state == 'off'
    await set_state(hass, POWER, 'on')  # old_state is None
    assert entity.state == 'on'
    assert entity.percentage is None
    await set_state(hass, POWER, 'off')
    assert entity.percentage == 0
    assert not timers
    entity._controller.send.assert_not_called()


@pytest.mark.asyncio
async def test_manual_commands_still_send_and_cancel_pending(hass, monkeypatch, timers):
    hass.states.async_set(POWER, 'on')
    hass.states.async_set(WATTS, '35')
    entity = await create_fan(hass, monkeypatch)
    pending = timers[-1]
    await entity.async_set_percentage(100)
    assert pending.cancelled
    assert entity._speed == '9'
    entity._controller.send.assert_awaited_once_with(DEVICE['commands']['default']['9'])
    await entity.async_turn_off()
    assert entity.percentage == 0
    await entity.async_turn_on()
    assert entity._speed == '9'
    assert entity._controller.send.await_count == 3


@pytest.mark.asyncio
async def test_removal_cancels_timer_and_listeners(hass, monkeypatch, timers):
    hass.states.async_set(POWER, 'on')
    hass.states.async_set(WATTS, '35')
    entity = await create_fan(hass, monkeypatch)
    pending = timers[-1]
    entity._call_on_remove_callbacks()
    assert pending.cancelled
    await set_state(hass, POWER, 'off')
    await set_state(hass, WATTS, '45')
    assert len(timers) == 1
    assert entity.state == 'on'
    entity._controller.send.assert_not_called()


@pytest.mark.asyncio
async def test_real_ha_timer_updates_without_another_event(hass, monkeypatch):
    hass.states.async_set(POWER, 'on')
    hass.states.async_set(WATTS, '35')
    entity = await create_fan(hass, monkeypatch)
    # Reschedule with a short delay through the actual HA timer helper.
    entity._cancel_pending_speed()
    entity._power_speed_delay = 0.01
    entity._schedule_speed_sync()
    import asyncio
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()
    assert entity.percentage == 44
    entity._controller.send.assert_not_called()
    entity._call_on_remove_callbacks()


def test_schema_and_device_map_validation():
    valid = dict(BASE_CONFIG, power_speed_sensor=WATTS, power_speed_map=RANGES)
    assert fan_module.PLATFORM_SCHEMA(valid)['power_speed_delay'] == 10
    for key in ('power_sensor', 'power_speed_sensor', 'power_speed_map'):
        invalid = dict(valid)
        del invalid[key]
        with pytest.raises(vol.Invalid):
            fan_module.PLATFORM_SCHEMA(invalid)
    for ranges in ({}, {'1': [4, 4]}, {'1': [4, 3]}, {'1': [-1, 3]},
                   {'1': [0, float('inf')]}, {'1': [0, float('nan')]},
                   {'1': [0, 10], '2': [9, 20]}, {'1': [0]},
                   {1: [0, 10]}):
        with pytest.raises(vol.Invalid):
            fan_module.PLATFORM_SCHEMA(dict(valid, power_speed_map=ranges))
    for delay in (-1, float('inf'), float('nan')):
        with pytest.raises(vol.Invalid):
            fan_module.PLATFORM_SCHEMA(dict(valid, power_speed_delay=delay))
    with pytest.raises(vol.Invalid, match='device speed strings'):
        fan_module.SmartIRFan(Mock(), dict(valid, power_speed_map={'wrong': [0, 10]}), DEVICE)


@pytest.mark.asyncio
async def test_half_open_ranges_and_gaps(hass, monkeypatch, timers):
    entity = await create_fan(hass, monkeypatch)
    hass.states.async_set(POWER, 'on')
    for watts, speed in [('0', '1'), ('9.99', '1'), ('10', '2'), ('89.99', '9'), ('90', None)]:
        hass.states.async_set(WATTS, watts)
        assert entity._speed_from_power() == speed
    entity._power_speed_map = {'1': [0, 5], '2': [10, 15]}
    hass.states.async_set(WATTS, '7')
    assert entity._speed_from_power() is None
    entity._call_on_remove_callbacks()


@pytest.mark.asyncio
@pytest.mark.parametrize('speed', ['off', *SPEEDS])
async def test_sync_observed_speed_without_ir(hass, monkeypatch, timers, speed):
    hass.states.async_set(POWER, 'on')
    hass.states.async_set(WATTS, '35')
    entity = await create_fan(hass, monkeypatch)
    pending = timers[-1]
    entity._last_on_speed = '6'
    entity._direction = 'forward'
    entity._oscillating = True
    await entity.async_sync_fan_state(speed)
    assert pending.cancelled
    assert entity._pending_speed is None
    assert entity._cancel_speed_sync is None
    assert entity._speed == speed
    assert entity.state == ('off' if speed == 'off' else 'on')
    assert entity.percentage == (0 if speed == 'off' else int(speed) * 100 // 9)
    assert entity.last_on_speed == ('6' if speed == 'off' else speed)
    assert entity._on_by_remote == (speed != 'off')
    assert entity.current_direction == 'forward'
    assert entity.oscillating is True
    entity.async_write_ha_state.assert_called_once_with()
    # A cancelled feedback callback cannot overwrite the observation.
    pending.action(None)
    assert entity._speed == speed
    entity._controller.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('speed', ['10', '', 'ON', 'Off', ' 3', 3, None, True, ['3']])
async def test_invalid_sync_has_no_side_effects(hass, monkeypatch, timers, speed):
    from homeassistant.exceptions import ServiceValidationError

    hass.states.async_set(POWER, 'on')
    hass.states.async_set(WATTS, '35')
    entity = await create_fan(hass, monkeypatch)
    before = (entity._speed, entity._on_by_remote, entity.last_on_speed)
    with pytest.raises(ServiceValidationError, match='Invalid speed'):
        await entity.async_sync_fan_state(speed)
    assert (entity._speed, entity._on_by_remote, entity.last_on_speed) == before
    assert not timers[-1].cancelled
    entity.async_write_ha_state.assert_not_called()
    entity._controller.send.assert_not_called()


@pytest.mark.asyncio
async def test_sensors_and_controls_after_sync(hass, monkeypatch, timers):
    entity = await create_fan(hass, monkeypatch)
    await entity.async_sync_fan_state('7')
    await set_state(hass, POWER, 'off')
    assert entity.state == 'off'
    assert entity.last_on_speed == '7'
    await set_state(hass, POWER, 'on')
    assert entity.percentage is None
    await set_state(hass, WATTS, '45')
    timers[-1].action(None)
    assert entity._speed == '5'
    entity._controller.send.assert_not_called()
    await entity.async_sync_fan_state('8')
    await entity.async_sync_fan_state('off')
    await entity.async_turn_on()
    assert entity._speed == '8'
    entity._controller.send.assert_awaited_once_with(DEVICE['commands']['default']['8'])


@pytest_asyncio.fixture
async def service_fans(hass, monkeypatch, tmp_path):
    """Exercise platform setup and actual HA service dispatch, not a mock registry."""
    import logging
    from datetime import timedelta
    from homeassistant.helpers.entity_platform import EntityPlatform, current_platform

    device_dir = tmp_path / 'codes' / 'fan'
    device_dir.mkdir(parents=True)
    (device_dir / '1221.json').write_text(json.dumps(DEVICE))
    (device_dir / '1000.json').write_text(json.dumps(dict(DEVICE, speed=['low', 'high'])))
    monkeypatch.setattr(fan_module, 'COMPONENT_ABS_DIR', str(tmp_path))
    monkeypatch.setattr(fan_module, 'get_controller', lambda *args: Mock(send=AsyncMock()))
    monkeypatch.setattr(RestoreEntity, 'async_added_to_hass', AsyncMock())
    monkeypatch.setattr(RestoreEntity, 'async_get_last_state', AsyncMock(return_value=None))
    entities = []
    platforms = []
    for code in (1221, 1000):
        platform = EntityPlatform(
            hass=hass, logger=logging.getLogger(__name__), domain='fan',
            platform_name='smartir', platform=fan_module,
            scan_interval=timedelta(seconds=30), entity_namespace=None,
        )
        platforms.append(platform)
        token = current_platform.set(platform)
        try:
            await fan_module.async_setup_platform(
                hass, fan_module.PLATFORM_SCHEMA(dict(BASE_CONFIG, device_code=code)),
                entities.extend,
            )
        finally:
            current_platform.reset(token)
        entity = entities[-1]
        entity.entity_id = f'fan.test_{code}'
        entity.add_to_platform_start(hass, platform, None)
        platform.entities[entity.entity_id] = entity
        platform.domain_entities[entity.entity_id] = entity
        platform.domain_platform_entities[entity.entity_id] = entity
        await entity.add_to_platform_finish()
    yield entities
    for entity in entities:
        entity._call_on_remove_callbacks()


@pytest.mark.asyncio
async def test_service_dispatch_and_platform_isolation(hass, service_fans):
    from homeassistant.components.fan import FanEntity
    from homeassistant.helpers.entity_platform import DATA_DOMAIN_ENTITIES

    assert hass.services.has_service('smartir', 'sync_fan_state')
    assert not hass.services.has_service('fan', 'sync_fan_state')
    other = FanEntity()
    other.entity_id = 'fan.other'
    other.async_sync_fan_state = AsyncMock()
    hass.data[DATA_DOMAIN_ENTITIES]['fan'][other.entity_id] = other
    hass.states.async_set(other.entity_id, 'off')
    # Only SmartIR entities are dispatched, even in mixed targets.
    await hass.services.async_call('smartir', 'sync_fan_state', {
        'entity_id': [service_fans[0].entity_id, other.entity_id], 'speed': '3',
    }, blocking=True)
    assert hass.states.get(service_fans[0].entity_id).attributes['percentage'] == 33
    assert hass.states.get(service_fans[0].entity_id).state == 'on'
    assert hass.states.get(service_fans[1].entity_id).state == 'off'
    other.async_sync_fan_state.assert_not_called()
    assert hass.states.get(other.entity_id).state == 'off'
    await hass.services.async_call('smartir', 'sync_fan_state', {
        'entity_id': service_fans[1].entity_id, 'speed': 'high',
    }, blocking=True)
    assert hass.states.get(service_fans[1].entity_id).attributes['percentage'] == 100
    # Repeated setup must keep dispatch to all SmartIR fan platforms.
    await hass.services.async_call('smartir', 'sync_fan_state', {
        'entity_id': 'all', 'speed': 'off',
    }, blocking=True)
    for entity in service_fans:
        assert hass.states.get(entity.entity_id).state == 'off'
        entity._controller.send.assert_not_called()
    other.async_sync_fan_state.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('data', [{}, {'speed': 3}, {'speed': None}, {'speed': True},
                                  {'speed': ['3']}, {'speed': '10'}, {'speed': 'high'}])
async def test_service_rejects_invalid_speed(hass, service_fans, data):
    from homeassistant.exceptions import ServiceValidationError

    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await hass.services.async_call('smartir', 'sync_fan_state',
                                      dict(data, entity_id=service_fans[0].entity_id),
                                      blocking=True)
    assert hass.states.get(service_fans[0].entity_id).state == 'off'
    service_fans[0]._controller.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('speed', ['7', 'off'])
async def test_synced_state_survives_restore(hass, monkeypatch, service_fans, speed):
    entity = service_fans[0]
    for observed in ('7', speed, speed):
        await hass.services.async_call('smartir', 'sync_fan_state', {
            'entity_id': entity.entity_id, 'speed': observed,
        }, blocking=True)
    state = hass.states.get(entity.entity_id)
    restored = await create_fan(hass, monkeypatch, feedback=False, restored=state)
    assert restored.state == state.state
    assert restored.percentage == state.attributes['percentage']
    assert restored.last_on_speed == '7'
    entity._controller.send.assert_not_called()
    restored._controller.send.assert_not_called()
    restored._call_on_remove_callbacks()


@pytest.mark.asyncio
async def test_sync_requires_a_target(hass, service_fans):
    with pytest.raises(vol.Invalid):
        await hass.services.async_call('smartir', 'sync_fan_state',
                                      {'speed': 'off'}, blocking=True)
    for entity in service_fans:
        entity._controller.send.assert_not_called()
