# Contributing to looptrip

Thank you for your interest in contributing to looptrip! This guide covers setup, testing standards, and code expectations.

## Setup

1. Clone the repository and install in editable mode with development dependencies:

```bash
git clone https://github.com/edkubiak/looptrip.git
cd looptrip
pip install -e ".[dev]"
```

2. Verify the installation:

```bash
looptrip --version
looptrip proof
python -m pytest tests/ -v
```

## Testing

**Testing-led contribution bar:** New detectors must include hermetic fixtures and mutation-sane tests.

Run tests with:

```bash
python -m pytest tests/ -v
```

Test coverage is tracked in CI. New features should maintain or improve the current coverage level (356 passing tests).

### Testing standards for new detectors

1. **Hermetic fixtures** — Events are built directly in test code (no external database required). See `tests/test_detector.py` for examples of the `_dispatch()` helper and the event-building pattern.

2. **Mutation sanity** — Deliberately break a detector (change a threshold, flip a condition) and verify the test suite turns red. This ensures tests are not passing by accident. See [testing.md](docs/testing.md) for the full mutation-testing philosophy.

3. **Coverage** — Edge cases (empty streams, None values, boundary conditions) and error states must be covered.

## Code Style

- **Python version:** 3.10+
- **Dependencies:** The detector core (`src/looptrip/`) is **stdlib-only**. OpenTelemetry is an optional `[otel]` extra, never imported by the core detector code.
- **Imports:** Group by stdlib, third-party, local (stdlib first).
- **Docstrings:** Module-level docstrings explain the high-level design. Function docstrings cover the contract and side effects.
- **Type hints:** Use them on function signatures; be practical about exhaustiveness.
- **Naming:** Frozen dataclasses and immutable state are preferred to avoid bugs. Detector state is always local to a single function call (no globals).

See `src/looptrip/detector.py` and `src/looptrip/normalize.py` for reference implementations.

## PR Expectations

1. **One logical unit per commit** — use clear, imperative commit messages.
2. **Tests pass locally** — run `python -m pytest tests/ -v` before pushing.
3. **Proof still reproduces** — run `looptrip proof` to verify the $792.96 headline is unchanged.
4. **No backward breakage** — Phase 1 callers of `detect()` with no arguments must continue to work (the default `detectors=None` is duplicate-work-only for backward compatibility).
5. **Link to documentation** — if adding a new detector, add a reference to `docs/testing.md` or the appropriate deep-dive doc.

## License

All contributions are licensed under Apache-2.0. By submitting a PR, you agree to this license.

## Code of Conduct

This project adheres to the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). Please report violations to [INSERT CONTACT].

## Questions?

Open an issue on GitHub for questions about the roadmap, design decisions, or how to get started.
