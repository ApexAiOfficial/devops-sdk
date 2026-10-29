# Example: a Compose service monitor

A complete host distribution in one file (`compose_monitor/wiring.py`): it watches Docker Compose
services and sends attention notices to Telegram, and never acts on anything. Install it beside the
engine and point the engine's runtime at it; the settings are in the module's docstring.

`tests/test_examples.py` in the SDK checks its settings parsing; the wiring itself needs the engine.
