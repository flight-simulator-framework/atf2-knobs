# atf2-knobs

ATF2 IPBSM optimization, beam tuning, linac optimization and synthetic controllers.

## Environment

Use Python >=3.11 and Poetry >=2.0. From this repository, run:

```bash
poetry install
```

Each repository has its own `pyproject.toml`, generated `poetry.lock` and `poetry.toml`. Poetry creates `.venv/` locally on installation; virtual environments are not copied or committed.

Required source repositories: `machine-interfaces`.
