# Architecture

## Three layers, three owners

| Layer | Where it lives | Licence | What it holds |
|---|---|---|---|
| **The engine** | ApexAI's private repository | proprietary, licensed separately | Detection, decisions, authority, budgets, durable state, recovery, the runtime that hosts adapters |
| **This SDK** | `ApexAiOfficial/devops-sdk` (public) | Apache 2.0 | The host contract, reusable adapters and sinks, settings helpers, contract checks, the run-time bridge |
| **Your integration** | your application's repository | yours | What *your* system's states mean: your targets, your predicates, your status files, your naming, your deployment |

The rule that decides where code goes:

- **Specific to one application's architecture or business** (its state files' vocabulary, its
  container naming, its alert routing, its deployment) belongs in **that application**.
- **Reusable across applications, and integration-only** (talking to Docker, delivering to
  Telegram, reading a status file safely, the contract types, test fakes) belongs in **this SDK**.
- **How the engine decides and acts** stays in **the engine**. The SDK never contains, wraps or
  reimplements it.

## One public SDK, not two

The SDK serves ApexAI's own products and outside developers with the same code. The integration
surface (observe a target, classify a condition, offer a remedy, deliver a notice) is the same
whether the engine runs beside your services or is operated for you. What differs between the two
audiences is *how the engine is obtained*, not *how an application integrates with it*.

A separate private SDK was considered and not created: it would duplicate the contract, drift from
it, and need its own versioning and tests, while everything an internal integration needs that is
not generic already has a better home: the application's own repository.

## The boundary with the engine

The SDK depends on nothing but the Python standard library. Its contract types are its own and
engine-neutral, so adapters, sinks and their tests run without the engine.

At run time `apexai_devops_sdk.engine` converts SDK values into the engine's types, through the
engine's documented host interface, importing the engine only when called. It is the only module
that names the engine at all, and it contains only that conversion.

## Versioning

- The package follows semantic versioning. Before `1.0`, a minor version may change the API.
- The host contract has its own version (`CONTRACT_VERSION`). A change an existing adapter or sink
  would notice raises it, and the changelog says what to change.
- `test_engine_live.py` is the agreement check between an SDK version and an engine version: run
  it wherever you install both.
