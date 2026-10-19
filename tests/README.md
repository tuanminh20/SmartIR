# Fan feedback tests

Use Python 3.13 and the declared minimum Home Assistant version:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r tests/requirements.txt
.venv/bin/python -m pytest -q tests
```

Repeat with Python 3.14.2 or newer in a separate environment to test Home
Assistant 2026.10.2. The requirements select the HA version by Python version;
CI runs both environments.

These tests use real Home Assistant states, event listeners, restore-state APIs,
percentage conversion, and (in one test) its timer helper. Most timer callbacks
are controlled for deterministic settling tests. The IR controller is mocked;
every feedback scenario asserts that it receives no commands. A separate test
checks that normal fan control still sends commands.

The HAIR state-sync tests cover all nine speeds and off, invalid observations,
pending feedback cancellation, subsequent sensor events, and normal control.
Platform setup registers the entity service against real Home Assistant service
dispatch, including multiple SmartIR platforms, per-device speed validation,
actual published states, and isolation from fans belonging to other integrations.

The tests load the Panasonic 1221 device file supplied by the user. Watt ranges
remain synthetic fixtures; they do not establish real-world watt thresholds.
The controller is mocked, so hardware IR validation, calibration, and a Home
Assistant deployment are still required before use.
