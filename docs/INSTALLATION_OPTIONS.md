# Installation formats

## Release 1: signed portable archive (supported)

This is the primary format for Windows and Linux. It preserves the repository
layout required by schemas and prompts, installs hash-locked dependencies into
an adjacent virtual environment, keeps files on the user's chosen disk, and
does not modify the global Python installation.

## Source checkout (supported for integrators)

Useful for embedding the runtime API or contributing. It uses the same install
scripts but is not the recommended path for non-technical users.

## PyPI wheel / pipx (future)

Do this after the public CLI and Python API are versioned. Before publishing,
move prompts and schemas into package resources, add wheel-install E2E tests,
define compatibility policy, and reserve the package name. Publishing the
current tree as a wheel would omit or mislocate runtime assets.

## Native Windows installer (future)

An MSIX or signed bootstrapper is appropriate once there is a stable desktop
UI/service lifecycle. It should allow selection of a non-system drive and must
not bundle or relocate Docker Desktop silently.

## Controller Docker image (not recommended)

The controller launches sandbox containers. Running it inside Docker would
normally require mounting the host Docker socket, effectively granting broad
host control. Do not present this as the safe default. Reconsider only with a
remote isolated executor API that removes direct socket access.
