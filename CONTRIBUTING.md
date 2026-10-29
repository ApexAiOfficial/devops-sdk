# Contributing

Issues and pull requests are welcome.

- Run the checks before a pull request:
  `pip install -e ".[dev]" && ruff check src tests examples && ruff format --check src tests examples && mypy && pytest`
- Keep the SDK an integration layer: contract types, reusable adapters and sinks, helpers and test
  kits. Code specific to one application belongs in that application; the engine's behaviour is
  not part of this repository.
- Keep the standard library as the only runtime dependency.
- A change an existing adapter or sink would notice is a contract change: raise `CONTRACT_VERSION`
  and say what to change in `CHANGELOG.md`.
- Never put a real token, key, chat id or host name in code, tests or examples.
