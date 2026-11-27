# Fleet spec (`fleet.yaml`)

A fleet spec is a declarative YAML document that describes templates, proxies,
image requirements, instances and capacity. `avdfleet fleet plan` prints a diff;
`avdfleet fleet apply` reconciles the fleet idempotently on a clean host.
The frozen JSON Schema lives at
[contracts/fleet.schema.json](../contracts/fleet.schema.json).

## Example

```yaml
apiVersion: avdfleet/v1
kind: Fleet
name: demo
capacity: 8

settings:
  gpu_mode: swiftshader_indirect

proxies:
  - id: proxy-1
    scheme: http
    host: 127.0.0.1
    port: 8081
    username: fleet
    password_env: AVDFLEET_PROXY_1_PASSWORD   # never the password itself

templates:
  - name: pixel-api35
    image: system-images;android-35;google_apis_playstore;x86_64
    device: pixel_7
    ram_mb: 4096
    cpu_cores: 2
    heap_mb: 256
    gpu_mode: swiftshader_indirect
    headless: true
    proxy: proxy-1
    scripts: []
    tags: [demo]

instances:
  - name: avd-01
    template: pixel-api35
  - name: avd-02
    template: pixel-api35
    proxy: proxy-1
    ram_mb: 4096
    metadata: {role: worker}
```

## Reference

Top level (all fields except `apiVersion`, `kind`, `name` are optional):

| Field | Type | Meaning |
|---|---|---|
| `apiVersion` | string, const `avdfleet/v1` | Schema version. |
| `kind` | string, const `Fleet` | Document kind. |
| `name` | string, 1–63 chars | Fleet name. |
| `capacity` | integer 1–64, default 64 | Capacity cap for this fleet. |
| `settings` | object | Free-form settings (validated by the implementation). |
| `proxies` | list | Proxy definitions (see below). |
| `templates` | list, min 1 | AVD templates (see below). |
| `instances` | list | Instances (see below). |

`proxies[]`: `id` (required, `[A-Za-z0-9._-]+`), `host` (required), `port`
(required, 1–65535), `scheme` (`http`/`https`/`socks5`, default `http`),
`username` (string/null), `password_env` (name of the environment variable that
holds the password — **never a literal password**), `tags`.

`templates[]`: `name` (required, `[A-Za-z0-9._-]+`), `image` (required; image id
or SDK package), `device` (default `pixel_7`), `ram_mb` (512–16384, default
2048; API 37+ images force ≥ 4096), `cpu_cores` (1–16, default 2), `heap_mb`
(16–4096, default 256), `gpu_mode` (default `swiftshader_indirect`; current
emulators deprecate the `*_indirect` modes — see
[compatibility.md](compatibility.md)), `headless` (default true), `proxy`
(proxy id or null), `scripts` (list of ids), `tags`.

`instances[]`: `name` (required, `[A-Za-z0-9._-]{1,63}`), `template` (required;
must name a template), `proxy` (proxy id or null; inherits the template's),
`ram_mb` (integer/null override), `metadata` (free-form object).

Instance `template` references, instance `proxy` references and template `proxy`
references are cross-validated. Unknown keys are rejected (`additionalProperties:
false`) so typos fail loudly.

## Validation, planning and applying

```bash
avdfleet fleet validate -f fleet.yaml
avdfleet fleet plan   -f fleet.yaml            # read-only diff
avdfleet fleet plan   -f fleet.yaml --prune    # include deletions
avdfleet fleet apply  -f fleet.yaml --dry-run  # plan only, change nothing
avdfleet fleet apply  -f fleet.yaml            # reconcile
avdfleet fleet apply  -f fleet.yaml --prune    # also delete resources not in the spec
```

Exit codes: `0` success / applyable plan; `1` plan contains errors or the apply
failed; `2` invalid arguments/document; `3` environment problems (e.g. missing
services); `4` spec file not found; `5` conflict. `--json` prints the
machine-readable plan.

Semantics:

- **Validation** checks the JSON Schema, the pydantic model, template/proxy
  references, name collisions, capacity, and image requirements. A required
  image that is known but not installed produces a warning by default (policy
  `warn`; the reconciler can be configured `fail`/`ignore`); no image is
  downloaded silently.
- **Plan** builds a list of `create|update|delete|noop` changes for templates,
  instances and proxies, with before/after payloads, a summary and
  `applyable`. It is read-only and does not call the mutating services.
- **Apply** re-plans and executes exactly that plan; a second apply is a no-op.
  Proxy passwords are read from `password_env` at apply time and written to the
  vault — they never appear in the spec, the plan, logs or errors.
- **Prune** deletes instances/templates that the fleet owns but the spec no
  longer declares. Without a store that remembers fleet ownership, prune falls
  back to template ownership. Use `--prune` deliberately.
- Updates of running instances take effect in the way the underlying AVD
  service supports (config changes may require a restart); the plan details
  call out what will happen. Destructive operations accept `--dry-run`.

The same capability is available over the API: `POST /api/v1/fleet/plan` and
`POST /api/v1/fleet/apply` take `{"yaml": "...", "spec": {...}, "path": "...",
"prune": false, "dry_run": false}` (see [api.md](api.md)).

## Rebuilding a fleet on a clean host

```bash
# 1. Install and verify the host
./bootstrap/install.sh --accept-android-sdk-licenses --yes
avdfleet doctor

# 2. Install/import the images the spec needs (explicit step; fleet apply does not download)
avdfleet images download "system-images;android-35;google_apis_playstore;x86_64"

# 3. Supply proxy passwords only through the environment
export AVDFLEET_PROXY_1_PASSWORD='…'

# 4. Reconcile
avdfleet fleet validate -f fleet.yaml
avdfleet fleet plan -f fleet.yaml
avdfleet fleet apply -f fleet.yaml

# 5. Start the fleet
avdfleet instances start avd-01   # or use the GUI bulk action
```

Export/import round-trip: `avdfleet avd export -o fleet.yaml` writes the current
templates/instances as a fleet document; `avdfleet avd import fleet.yaml`
imports them without a full reconcile.

## Limitations

- Image downloads are not part of `fleet apply`; the spec carries image
  requirements and validation warns/fails per policy instead of downloading.
- `settings` is intentionally free-form in the schema; only the settings the
  implementation understands are applied.
- Fleet ownership tracking for `--prune` requires the SQLite store; without it
  prune is limited to template-owned instances.
