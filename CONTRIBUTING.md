# Contributing to agent-swap

Thanks for helping. agent-swap handles real login credentials, so the rules below matter more than usual.

## Set up

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/saurabh-awate96/agent-swap.git
cd agent-swap
uv sync                 # installs the package and the dev dependencies from uv.lock
uv run aswap help       # run the CLI from the checkout
```

`uv sync --locked` is what CI runs, so keep `uv.lock` in step with `pyproject.toml` when you change dependencies.

## Test

```bash
uv run pytest           # the whole suite, in parallel (pytest-xdist)
uv run pytest tests/test_codex.py -q   # one file while you work
```

The tests never touch your real logins: they use an in-memory Keychain, temporary home directories and stand-in commands. A guard test (`tests/test_real_store_guard.py`) refuses writes to the real store. If a test you add needs the real `$HOME`, it is wrong; make it use `tmp_path`.

CI also runs the suite on Windows and on macOS (the macOS job exercises the real Keychain code).

## Lint

CI runs these; run them before you push:

```bash
uvx ruff check src --select F401,F841,E9     # unused imports and names, syntax errors in the shipped code
uvx --from shellcheck-py shellcheck install.sh
```

Style is not enforced beyond that. Match the code around your change.

## Make a change

1. Branch from `main`: `git checkout -b short-description origin/main`.
2. Make the change, with a test for new non-trivial logic. A bug fix starts with a test that fails without the fix.
3. Update the docs in the same change. The README is the entry point and the reference lives in `docs/`; edit the existing text rather than appending to it. Add a line to the Unreleased section of `CHANGELOG.md` for anything user-visible.
4. Run the tests and the lint commands above.
5. Commit with a short imperative subject (`Add aswap codex run: ...`, `Stop the panel refreshing idle tokens`), and explain the why in the body when it is not obvious.
6. Push the branch and open a pull request into `main`. Describe what changed and how you checked it. CI must pass before it is merged.

Keep changes small and focused. A refactor and a behaviour change belong in separate pull requests.

## Credentials: what must never be committed

- Never commit a real token, API key, setup token, refresh token, `auth.json`, Keychain export, `.credentials.json`, or an `aswap export` file.
- Test data uses obviously fake values (`sk-ant-oat01-test…`, `user@example.com`). Do not copy a value from your own machine into a fixture, a test name or a log line.
- Redact emails and account identifiers in bug reports and pull requests.
- If you do commit one by mistake, treat it as leaked: revoke it at the provider first, then remove it from history. Report it through [SECURITY.md](SECURITY.md).

## Where things are

- `src/claude_swap/`: the package. `cli.py` is the command line; `switcher.py` and `autoswitch.py` hold Claude switching and the auto-switch engine; `codex.py` and `codex_run.py` hold Codex; `gui/` is the web control panel; `tui/` is the terminal dashboard; `menubar.py` is the macOS menu bar app.
- `tests/`: one file per area, named after it.
- `docs/`: the reference pages. Start from [docs/README.md](docs/README.md).

## Security reports

Do not open a public issue for a vulnerability. Use GitHub's private reporting, as described in [SECURITY.md](SECURITY.md).
