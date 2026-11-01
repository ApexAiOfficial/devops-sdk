"""ApexAi DevOps SDK: integrate an application with the ApexAi DevOps engine.

The SDK is the integration layer only. It defines the host contract
(:mod:`~apexai_devops_sdk.contract`), reusable adapters and sinks
(:mod:`~apexai_devops_sdk.compose`, :mod:`~apexai_devops_sdk.statusfile`,
:mod:`~apexai_devops_sdk.telegram`), settings helpers
(:mod:`~apexai_devops_sdk.config`), contract checks and fakes for tests
(:mod:`~apexai_devops_sdk.testing`), and the run-time bridge to the engine
(:mod:`~apexai_devops_sdk.engine`). The engine itself is proprietary, runs only
in environments ApexAi operates, and is not part of this package.
"""

from apexai_devops_sdk.contract import (
    CONTRACT_VERSION,
    AttentionNotice,
    AttentionSink,
    ContractError,
    Observation,
    Outcome,
    Remedy,
    SinkOutcome,
    TargetAdapter,
    utc_timestamp,
)

__version__ = "0.1.1"

__all__ = [
    "CONTRACT_VERSION",
    "AttentionNotice",
    "AttentionSink",
    "ContractError",
    "Observation",
    "Outcome",
    "Remedy",
    "SinkOutcome",
    "TargetAdapter",
    "__version__",
    "utc_timestamp",
]
