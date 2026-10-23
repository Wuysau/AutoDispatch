# AutoDispatch

AutoDispatch is a Windows desktop interface and DAG-based development-task
scheduler built with Python and React/TypeScript.

This public repository contains a code-only snapshot of the local
`codex/production-baseline` branch at `37d2e6f`.
Design documents, reference images, wallpaper assets, local execution approvals,
credentials, runtime state, and installed dependencies are excluded. The browser
preview uses a plain background in this distribution.

## Install and preview

Requires Python 3.11 or newer, Node.js/npm, and Windows WebView2 for the desktop UI.

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[host]"
npm --prefix ui ci
npm --prefix ui run build
./Start-AutoDispatchDesktop.ps1 -Demo
```

Demo mode uses isolated temporary state and simulated task data. It does not
start development agents or read a production execution approval.

## Configure the scheduler

Copy `config.example.json` to a machine-local configuration and replace its
repository paths and executable paths with your own. The bundled scheduler
targets a separately configured AetherTrade checkout and its pinned task/spec
inputs; that checkout and those inputs are not included here.

Execution requires explicit, configuration-bound authorization. Example
approvals do not authorize agent calls. Opening the desktop monitor does not
start workers. Do not commit local configurations, approvals, or runtime state.

The scheduler supports DAG-bounded worker capacity, durable failure recovery,
manual retry priority, and per-worker model choices discovered through official
Codex and OpenCode model catalog APIs.

## Development checks

```powershell
npm --prefix ui run build
.venv/Scripts/python.exe -m unittest discover -s tests -v
git diff --check
```

Some scheduler tests require the separately configured product checkout and its
pinned inputs. AutoDispatch tests do not establish trading qualification or
authorize live orders.
