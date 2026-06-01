# Contributing to RedGW-lite

Thanks for your interest. Please read how this repository is maintained before contributing.

## About this repository — a one-way public mirror

**RedGW-lite is a one-way public mirror of a private upstream (RedGW).**

- All development happens in the private upstream. This public repository is **regenerated** from that
  upstream by extracting only the common (open-source) features.
- As a result, changes pushed directly to this repository (or merged PRs) **will be overwritten on the
  next regeneration.**
- The flow is **upstream → public mirror, one direction only.** External changes are not automatically
  merged back into the upstream.

## How contributions are incorporated

1. **Issues & proposals**: Please file bugs and improvement ideas as GitHub Issues. A maintainer will review them.
2. **Pull requests (patches)**: PRs are accepted as **patches/proposals rather than direct merges**. When
   adopted, a maintainer applies the change to the upstream, and it lands here on the next release
   regeneration.
   - This means a PR may be **closed (as incorporated) instead of "merged"** — that is expected.
3. The origin of adopted changes is credited in release notes / commit messages.

## Scope

- This repository contains only the **common gateway features** (Redis REST + WebSocket Pub/Sub,
  authentication & authorization, audit logging, monitoring).
- Commercial database-integration features are full-version-only and out of scope here. PRs that add such
  features will be declined as a matter of project identity.

## License

By contributing, you agree that your contributions are distributed under this repository's license
([Apache License 2.0](LICENSE)).
