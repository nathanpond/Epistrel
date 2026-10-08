# Epistrel Engine

A viewpoint-aware memory engine for character/actor GenAI: it tracks what each character knows and believes, and how characters stand in relation to one another and the truth. This repository is the service (Engine Core + Orchestrator) and has no UI. The human-facing test harness lives in [Epistrel-Console](https://github.com/nathanpond/Epistrel-Console).

**Status:** early scaffold. The design is in [`docs/`](docs/): whitepaper, architecture, requirements, testing plan, ADR log.

## Build and test

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync                      # create .venv and install dependencies
uv run epistrel serve        # run locally on http://127.0.0.1:8000 (settings: see .env.example)
uv run pytest                # tests
uv run ruff check && uv run ruff format --check   # lint and format
uv run mypy                  # type check (strict)
```

## License

Apache-2.0. See [LICENSE](LICENSE).
