# Changelog

All notable changes to this SDK. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/) (before 1.0, a minor version may
change the API).

## 0.1.0 (2026-10-29)

First release of the integration layer. Host contract version `1`.

### Added
- `contract`: `Observation`, `Outcome`, `Remedy`, `AttentionNotice`, `SinkOutcome`, the
  `TargetAdapter` and `AttentionSink` protocols, identifier and closed-word rules,
  `utc_timestamp`.
- `compose.ComposeHealthAdapter`: Docker Compose service health through the Docker Engine API.
- `statusfile.read_json_status`: a bounded, link-safe JSON status-file read that refuses a
  file others can write.
- `telegram.TelegramAttentionSink`: at-least-once, de-duplicated attention delivery to Telegram.
- `config`: settings parsing that refuses rather than guesses; owner-only secret files.
- `testing`: `assert_target_adapter` contract checks (repeat calls, the time bound, a forked
  call, synthetic broken looks), `FakeDockerDaemon`, `FakeTelegramApi`.
- `engine`: the run-time bridge to the engine and `read_only_host_wiring` for monitors.
