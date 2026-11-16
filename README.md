# pippa-builders-pipeline

The build that publishes a Pippa hackathon app. Every app repository created from the hackathon template calls it on each push to `main`: it checks the app, publishes it to Pippa and reports the result on the pushed commit.

## Calling it

`.github/workflows/publish.yml` in the app repository:

```yaml
name: Publish to Pippa

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: write
  id-token: write
  statuses: write

jobs:
  publish:
    uses: Pippa-Labs/pippa-builders-pipeline/.github/workflows/reusable-hack-publish.yml@main
```

Input (optional):

| Input | Default | Description |
|-------|---------|-------------|
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARN` or `ERROR` |

Publishing works only from this workflow on `main` and only for app repositories of the hackathon; a copy of it elsewhere is refused.

## What it does

**Check** (runs the app's code)

1. Skips the run when `manifest.yaml` still has the placeholder app id `hack.your_app_name_here`.
2. Refuses, as a code error, an app that:
   - names the Pippa SDK in a requirements file (the build installs it);
   - sets a package index or download location (`-i`, `--index-url`, `--extra-index-url`, `-f`, `--find-links`);
   - installs from a wheel, a local file, a folder, a URL or a VCS link, or includes a requirements file from outside the repository;
   - has a `.whl` file anywhere in the repository.
3. Installs the Pippa SDK at the version the apps run with. The SDK is never stored in the app repository, and no AWS credentials are left in the environment once it is installed. The job can still request a token for the read-only SDK role; that exposure is accepted.
4. Installs `requirements-dev.txt` from PyPI and runs the app's `scripts/check.py` (tests included).
5. After a passed check, runs the app's `scripts/routing_check.py`. Its findings and the warnings of `scripts/check.py` never stop the publish; they are added to the commit comment of a published app.

**Publish** (runs no app code): packages the app (3 MB limit) and sends it to Pippa. Before deploying anything, Pippa checks that the repository is linked to a builder's Pippa account and that `manifest.yaml` has nothing it refuses.

## Result on the commit

| Outcome | Commit status `pippa/publish` | Commit comment |
|---------|-------------------------------|----------------|
| Published | `success` | `Your app <name> (<app_id> v<version>) is live. ...`, plus a `Warnings` section when Pippa, the check or the routing check found something (Pippa's own warnings come first, under `Pippa`) |
| Code error | `failure` | starts with `CODE_ERROR`, names what to fix, ends with `Fix the code and push again.` |
| Refused | `failure` (`Refused: <sentence>`) | starts with `REFUSED: the app was not published:`, then Pippa's sentence. Nothing was deployed, and the cause is not the app's code: the repository is not linked to a Pippa account (a fork or a copy), its owner has no account yet, or `manifest.yaml` has something Pippa does not accept. The publisher marks it `error_kind: "refused"` |
| Platform error | `failure` | starts with `PLATFORM_ERROR` and a reference; the app is not at fault |

## Tests

```bash
python -m pip install --require-hashes -r .github/scripts/hack/requirements.txt
python -m pip install pytest
python -m pytest .github/scripts/hack/tests
```
