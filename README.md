# Intent Workflow Runtime

Intent Workflow Runtime turns a user request into a verifiable plan, executes
it in an isolated Docker container, and accepts the result only after
independent checks pass. The model may propose an intent and workflow, but it
cannot expand its own permissions, skip mandatory checks, or declare success
without evidence.

## Features

- OpenAI, Anthropic, OpenRouter, and compatible API support.
- Complete JSON Schemas for intents and workflows.
- Isolated, non-root execution with networking disabled by default.
- Network access restricted to explicitly allowed domains.
- Independent mechanical verification of results.
- Bounded automatic repair of failed steps.
- Transactional result publishing: failed attempts do not modify the workspace.
- API keys stored securely in the operating system credential store.
- Audit trail, interrupted-run recovery, and diagnostic endpoints.

## Requirements

- Windows 10/11 or a current Linux distribution.
- Python 3.11 or 3.12.
- Docker Desktop on Windows or Docker Engine on Linux.
- At least 1 GB of free space on the selected drive.
- An API key for a supported LLM provider.

On Windows systems with limited space on the system drive, extract the release
to another drive, for example `D:\Apps\iw-runtime-v3`. The virtual environment
and runtime files are created in the same directory.

## Install on Windows

1. Download and extract the signed release archive to a permanent directory.
2. Start Docker Desktop.
3. Open PowerShell in the runtime directory and run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

The installer checks Python and Docker, creates a local `.venv`, and installs
dependencies locked with SHA-256 hashes.

## Install on Linux

```bash
chmod +x install.sh
./install.sh
```

## Configure the model

Open `.env` and set the provider, endpoint, and model. Do not put the secret API
key in this file.

Example for OpenRouter:

```dotenv
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=openai/gpt-4o-mini
LLM_STRUCTURED_OUTPUT_MODE=json_schema
IW_SECRET_SOURCE=keyring
```

Store the API key in the Windows credential store:

```powershell
.\.venv\Scripts\python.exe -m runtime.secret_cli set llm_api_key
```

On Linux, use `.venv/bin/python` instead of
`.venv\Scripts\python.exe`.

## Verify the installation

```powershell
.\.venv\Scripts\python.exe -m runtime.cli doctor
```

A working installation returns the `ready` status and successful Python,
Docker, and LLM configuration checks. This command does not call the model or
consume tokens.

## First run

The release includes a safe demonstration task that creates a simple Python
file inside a temporary workspace:

```powershell
.\.venv\Scripts\python.exe examples\example_run.py
```

Docker may download the pinned sandbox image during the first run.

## Monitoring

```powershell
.\.venv\Scripts\python.exe -m runtime.observability_server --port 9090
```

The following endpoints are then available:

- `http://127.0.0.1:9090/health/live`
- `http://127.0.0.1:9090/health/ready`
- `http://127.0.0.1:9090/metrics`

## Upgrade

1. Stop accepting new tasks.
2. Download and verify the new signed release.
3. Extract it into a new directory, not over the active installation.
4. Run the installer and the `doctor` command.
5. Switch to the new directory only after verification succeeds.

Keep the previous installation until the new version is confirmed stable so it
remains available for a fast rollback.

## Uninstall

Remove the runtime directory and, if necessary, delete the stored credential:

```powershell
.\.venv\Scripts\python.exe -m runtime.secret_cli delete llm_api_key
```

Docker images can be removed separately through Docker Desktop. Preserve audit
files and workspace results before uninstalling if they are needed for records.

## Security limitations

A Docker container is a strong practical boundary for ordinary tasks, but it
does not replace a dedicated virtual machine for deliberately hostile code or
highly sensitive data. Manual acceptance criteria still require human judgment.
Use only signed releases and avoid running the runtime as administrator unless
required.

## Support

When reporting a problem, include the output of `runtime.cli doctor`, the
release version, and a description of the task. Never share API keys, operating
system keyring contents, or unredacted audit logs.
