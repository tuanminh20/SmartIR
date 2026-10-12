# Fan feedback tests

Use Python 3.13 and the declared minimum Home Assistant version:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r tests/requirements.txt
.venv/bin/python -m pytest -q tests
```

These tests use real Home Assistant states, event listeners, restore-state APIs,
percentage conversion, and (in one test) its timer helper. Most timer callbacks
are controlled for deterministic settling tests. The IR controller is mocked;
every feedback scenario asserts that it receives no commands. A separate test
checks that normal fan control still sends commands.

The nine-speed device data and watt ranges are synthetic fixtures. They do not
validate Panasonic IR codes or establish real-world watt thresholds. Hardware
calibration and a Home Assistant deployment are still required before use.
