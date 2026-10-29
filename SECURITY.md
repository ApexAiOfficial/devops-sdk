# Security

## Reporting a vulnerability

Please report security problems privately, through GitHub's
[private vulnerability reporting](https://github.com/ApexAiOfficial/devops-sdk/security/advisories/new)
for this repository. Do not open a public issue for a vulnerability.

Include what you found, how to reproduce it, and the version. We aim to acknowledge reports within
a few days.

## What the SDK promises

- **No secret is echoed.** Adapters copy only closed words and identifiers into observations; the
  Telegram sink never logs or returns its token; settings errors name the setting, never its value.
- **Secrets come from owner-only files**, read when needed: a regular file, not a link, owned by
  the running user, with no group or other permission bits.
- **Bounded I/O.** Every network read is capped in size and time; every file read is capped and
  refuses symbolic links.
- **No runtime dependencies** beyond the Python standard library.

## Scope

This repository covers the SDK only. The ApexAI DevOps engine is a separate, proprietary product.
