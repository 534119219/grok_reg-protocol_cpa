# Headless Register Design

Date: 2026-07-12

## Goal

Add an optional headless registration mode for the existing DrissionPage/Chromium
registration flow. The default remains headed browser mode to preserve current
success rate with Cloudflare Turnstile.

## Scope

In scope:

- Add a `register_headless` config option, defaulting to `false`.
- Add CLI overrides for registration browser mode:
  - `--headless-register`
  - `--headed-register`
- Apply the resolved mode in the existing registration browser options factory.
- Keep the current proxy, slim flags, and `turnstilePatch` extension behavior.
- Add minimal diagnostics when Turnstile token acquisition fails.
- Update project documentation and example config.

Out of scope:

- Automatic fallback from headless to headed mode.
- Replacing DrissionPage with Playwright/Selenium.
- Changing CPA protocol mint behavior.
- Changing email provider behavior.

## Current Architecture

The main registration flow uses `grok_register_ttk.py` with DrissionPage
Chromium automation. `create_browser_options()` creates browser options,
applies slim Chromium flags, loads `turnstilePatch`, and sets the configured
proxy. `register_cli.py` wraps that factory and adds Linux Chromium path
detection.

CPA browser fallback already supports a separate `cpa_headless` option in
`cpa_xai/browser_confirm.py`. This design only adds headless support for the
registration browser.

## Configuration and Precedence

Add to `DEFAULT_CONFIG` and `config.example.json`:

```json
"register_headless": false
```

CLI precedence:

```text
--headless-register / --headed-register > config.json register_headless > false
```

The CLI should update the in-memory `reg.config` value before browser options
are initialized. GUI mode uses `config.json`.

## Browser Options Behavior

When `register_headless` is false:

- Preserve the existing headed behavior.
- Do not add headless-only flags.

When `register_headless` is true:

- Enable headless mode with `options.headless(True)` when available.
- Fall back to `--headless=new` if the DrissionPage API does not expose
  `headless()`.
- Add a stable viewport with `--window-size=1280,900`.
- Log that registration headless mode is enabled.

The existing extension, proxy, `auto_port`, and timeout behavior remain
unchanged.

## Turnstile Diagnostics

Headless mode can reduce Turnstile success rate. On `getTurnstileToken()`
failure, save a small debug bundle:

- Screenshot if the page API supports it.
- Current URL and title.
- A short visible text snapshot.

The diagnostic should be best-effort and must not mask the original failure.

## Error Handling

The implementation does not automatically retry in headed mode. If headless
fails, the user can rerun with `--headed-register` or set
`register_headless=false`.

This keeps the first change small and avoids mixing browser-mode policy with
the existing registration retry logic.

## Testing

Static and CLI validation:

```bash
uv run python -m py_compile grok_register_ttk.py register_cli.py
uv run python -u register_cli.py --help
```

Runtime smoke test, when credentials and network are available:

```bash
uv run python -u register_cli.py --extra 1 --threads 1 --headless-register
```

Expected signs:

- Logs show registration headless mode is enabled.
- Browser options still include proxy and `turnstilePatch`.
- On Turnstile failure, debug artifacts are written without hiding the original
  exception.
