# Example: a Compose service monitor

A complete host distribution in one file (`compose_monitor/wiring.py`): it watches Docker Compose
services and sends attention notices to Telegram, and never acts on anything. The engine loads it
through its `runtime` entry point, in an environment ApexAi operates (the engine is not
distributed); the settings are in the module's docstring.

`tests/test_examples.py` in the SDK checks its settings parsing; the wiring itself needs the engine.
