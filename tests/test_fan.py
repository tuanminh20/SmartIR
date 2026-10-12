"""Fan feedback tests using real Home Assistant states and event listeners."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
import voluptuous as vol
from homeassistant.core import HomeAssistant, State
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
