# ComfyUI-Credit-Tracker

ComfyUI-Credit-Tracker is a ComfyUI custom node package for estimating and logging ComfyUI Partner/API Node credit usage per workflow, project, user, and partner node.

It is designed for internal company reporting, budget review, and spotting which Partner/API Nodes are driving credit spend, such as Nano Banana, Seedance, Kling, Veo, Runway, or other API-backed nodes.

## Files

```text
comfyui_credit_tracker/
|-- __init__.py
|-- auto_tracker.py
|-- api_node_catalog.json
|-- dashboard.py
|-- nodes.py
|-- pricing_sync.py
|-- report_visual.py
|-- instance_config.json
|-- remote_instances.json
|-- tracker_db.py
|-- pricing_table.json
|-- official_pricing_cache.json
|-- export_report.py
|-- requirements.txt
`-- README.md
```

The node writes usage records to:

```text
usage_log.db
```

The database is created automatically inside this extension folder the first time the node or report script runs.

## Browser Dashboard

After restarting ComfyUI, open:

```text
http://127.0.0.1:8188/credit-tracker
```

The dashboard shows total credits, estimated USD, top partner nodes, top projects, and recent runs. It also exposes debugging fields such as `node_class_type`, `source`, `prompt_id`, and `node_id`.

The dashboard can also sync a local reference copy of the official ComfyUI Partner Nodes pricing page:

```text
http://127.0.0.1:8188/credit-tracker/api/pricing
```

Click `Sync Official Pricing` in the dashboard, or run:

```powershell
..\..\..\python_embeded\python.exe pricing_sync.py
```

The sync writes:

```text
official_pricing_cache.json
```

This cache is a reference table from the official docs. Runtime-price capture is still preferred for actual usage logging because it records the price ComfyUI displayed for the run.

Dashboard CSV exports:

```text
http://127.0.0.1:8188/credit-tracker/export/full.csv
http://127.0.0.1:8188/credit-tracker/export/by-node.csv
http://127.0.0.1:8188/credit-tracker/export/by-project.csv
```

Dashboard JSON API:

```text
http://127.0.0.1:8188/credit-tracker/api/summary
http://127.0.0.1:8188/credit-tracker/api/usage-rows
http://127.0.0.1:8188/credit-tracker/api/ingest-rows
```

## Federated Multi-PC Dashboard Without Central Storage

If you do not have central storage, use federated tracking:

```text
Each ComfyUI machine keeps its own usage_log.db
Your main dashboard reads each machine over HTTP
No database files are copied or centralized
```

Install `ComfyUI-Credit-Tracker` on every ComfyUI machine you want to track.

Copy `instance_config.example.json` to `instance_config.json`, then edit it for the local machine:

```text
ComfyUI/custom_nodes/comfyui_credit_tracker/instance_config.json
```

```json
{
  "name": "Render PC 01",
  "base_url": "http://192.168.1.10:8188"
}
```

Copy `remote_instances.example.json` to `remote_instances.json`, then list the other tracker machines:

```text
ComfyUI/custom_nodes/comfyui_credit_tracker/remote_instances.json
```

```json
[
  {
    "name": "Render PC 02",
    "base_url": "http://192.168.1.11:8188",
    "enabled": true
  }
]
```

On each remote PC, use the opposite setup: its `instance_config.json` should describe itself, and its `remote_instances.json` should point back to the other ComfyUI tracker machines.

Then restart the main ComfyUI and open:

```text
http://127.0.0.1:8188/credit-tracker
```

The dashboard will show:

- local tracker totals
- remote tracker status
- combined network credits
- network spend by partner node
- network recent runs
- deduped network totals if the same SQLite database was copied between machines

The remote machine must have this tracker installed and reachable at:

```text
http://192.168.1.11:8188/credit-tracker/api/summary
http://192.168.1.11:8188/credit-tracker/api/usage-rows
```

If it is not installed or the network blocks the port, the main dashboard shows that instance as offline/error instead of crashing.

For matching totals on both machines, install the same tracker version on both PCs and configure each dashboard to point at the other machine:

```text
Render PC 01: http://192.168.1.10:8188
Render PC 02: http://192.168.1.11:8188
```

If a database was copied by mistake, federated totals use each row's `dedupe_key` so copied historical runs are counted once. Each machine's local card still shows that machine's own local SQLite total; the network total is the shared deduped number.

## Peer SQLite Sync

For local-first syncing without central storage, every tracker can push newly logged rows to the other machines listed in `remote_instances.json`.

How it works:

- when a Partner/API node row is inserted locally, the tracker sends it to each remote `/credit-tracker/api/ingest-rows`
- the receiving machine inserts it into its own SQLite database
- duplicate rows are skipped by `dedupe_key`
- if a remote PC is offline, ComfyUI keeps running and prints a warning only
- click `Sync Peers Now` in the dashboard to pull/backfill old rows from the configured remotes

Optional LAN security:

```powershell
$env:CREDIT_TRACKER_SYNC_TOKEN="choose-a-shared-secret"
```

Set the same token before starting ComfyUI on every tracker machine. When unset, peer sync is open to machines that can reach the ComfyUI port, which may be acceptable only on a trusted internal network.

## Pulling Official Comfy Account Usage

The dashboard includes a `Sync Account Usage` button. This calls the same official account-events API used by ComfyUI's Credits activity panel and stores the results in:

```text
official_usage_events
```

This is account-level usage, so it can show Partner/API node usage from another PC if both machines are logged into the same Comfy account. It cannot recover local workflow metadata from the other PC, such as project name, workflow name, node id, or user name. For that detail, the tracker still needs to be installed on that machine too.

Authentication works in two ways:

1. Open the tracker dashboard from the same browser where you are logged into ComfyUI. The dashboard tries to reuse the browser's Comfy login token and sends it to the local backend in memory only.
2. Run one Partner/API node in the current ComfyUI session. The tracker caches the Comfy account auth token in memory only and can then sync account usage.
3. Or set an environment variable before starting ComfyUI:

   ```powershell
   $env:COMFY_ACCOUNT_AUTH_TOKEN="your-comfy-account-token"
   ```

   If you use a Comfy API key instead, set:

   ```powershell
   $env:COMFY_ACCOUNT_API_KEY="your-comfy-api-key"
   ```

The official account usage table is kept separate from local `credit_usage` rows so local tracked spend is not double-counted. Use it as a reconciliation view against the official Comfy account activity.

## Balance Snapshots and Reconciliation

The dashboard records Comfy account balance snapshots in:

```text
balance_snapshots
```

When the dashboard can read the current Comfy balance, it saves a snapshot if the balance changed or if the latest snapshot is older than a few minutes. The `Balance Reconciliation` panel compares:

- starting balance from the first saved snapshot
- latest/current balance
- real consumed credits from the balance drop
- tracker-estimated network spend
- possible untracked spend

This helps catch API usage that happened outside the local tracker, on another ComfyUI machine, or before peer sync was configured.

Important notes:

- The first snapshot becomes the baseline, so historical spending before that snapshot cannot be reconstructed from balance alone.
- If credits are purchased or added to the account, the balance can increase and the reconciliation window should be interpreted carefully.
- The tracker estimate still comes from `credit_usage`; balance snapshots are a cross-check against the real account balance.

## Health Check and Auto Backup

The dashboard includes a compact `System Health & Backups` section near the top. It summarizes:

- peer sync health
- latest automatic SQLite backup
- latest auto-tracker event
- latest balance snapshot
- official account usage sync status

The tracker creates daily SQLite backups in:

```text
ComfyUI/custom_nodes/comfyui_credit_tracker/backups/
```

Backups are created from the live SQLite database using SQLite's backup API, so ComfyUI can keep running while the backup is made.

## Brick Saver Project Attribution

If `comfyui_brick_tools` / Brick Saver is installed, automatic tracking reads project names directly from the running ComfyUI prompt. The tracker looks for:

```text
SaveArchVizImage      -> Save Brick Image
SaveArchVizSequence   -> Save Brick Sequence
```

When either node has a `project_name` input, Partner/API node usage in the same workflow is logged under that Brick project. For example, a workflow containing:

```text
Save Brick Image project_name = 8140 Riverside Tower
```

will store API usage rows with:

```text
project_name = 8140 Riverside Tower
```

If multiple Brick Saver nodes appear in one workflow, the first unique project is used as the main dashboard project, and the full list is added to the row notes as `brick_saver_projects=...`.

Explicit `credit_tracker` metadata still takes priority. If no Brick Saver project is found, automatic tracking falls back to `General`.

## Installation

1. Copy the `comfyui_credit_tracker` folder into:

   ```text
   ComfyUI/custom_nodes/
   ```

2. Restart ComfyUI.

3. In the ComfyUI node menu, search for:

   ```text
   Credit Tracker Logger
   ```

   or:

   ```text
   Credit Tracker Report Viewer
   ```

4. Add `Credit Tracker Logger` to workflows that use Partner/API Nodes when you want manual tagging.

5. Fill in:

   - `project_name`
   - `user_name`
   - `workflow_name`
   - `partner_node_name`
   - `quantity`
   - `duration_seconds`
   - `resolution`
   - `notes`

6. Run the workflow. The node writes one row to SQLite each time it executes.

## Automatic Tracking Without Adding a Node

This extension also includes automatic tracking. After ComfyUI restarts, it registers a prompt hook and watches execution events. You do not need to add `Credit Tracker Logger` to the workflow if the Partner/API node emits a runtime price, can be matched from `pricing_table.json`, or is discovered in ComfyUI's built-in `comfy_api_nodes` package.

Automatic mode works like this:

1. A workflow is queued.
2. The extension scans the API prompt for node `class_type` values.
3. If a node matches a pricing-table key, an `auto_detect_class_types` alias, or a discovered `comfy_api_nodes` API node id, it is marked for tracking.
4. When ComfyUI reports that node as executed, the extension writes one SQLite row.
5. Cached nodes are not logged unless ComfyUI reports them as executed.

For ComfyUI API nodes that display a real runtime price, the tracker captures that runtime price directly. This is the preferred path for nodes like Nano Banana because it records the actual displayed credit amount rather than only the fallback estimate from `pricing_table.json`. Paid API nodes with a runtime price are still logged even when they are not registered in `pricing_table.json`; their source is recorded as `runtime_price_unmapped`.

Duplicate protection is enabled. Runtime-price rows use a `dedupe_key` based on:

```text
source + prompt_id + node_id + node_class_type + estimated_credits
```

If the same API-node price event is emitted more than once, SQLite keeps only one usage row.

Example pricing entry with automatic class detection:

```json
{
  "Seedance 2.0": {
    "pricing_mode": "per_second",
    "credits_per_second": 20,
    "auto_detect_class_types": [
      "ByteDance2TextToVideoNode",
      "ByteDance2FirstLastFrameNode",
      "ByteDance2ReferenceNode"
    ]
  }
}
```

The tracker also scans:

```text
ComfyUI/comfy_api_nodes/
```

and writes:

```text
api_node_catalog.json
```

This catalog lets the tracker recognize all built-in ComfyUI Partner/API Nodes without manually listing every class name in `pricing_table.json`.

The default pricing table includes aliases for common local ComfyUI API node class names for Nano Banana, Seedance, Kling, Veo, and Runway. Update the aliases if your installed third-party Partner/API nodes use different class names.

Automatic mode cannot know company metadata unless it is provided somewhere. When no tracker node is used, it defaults to:

```text
project_name = General
user_name = Unknown or client_id when available
workflow_name = Auto-detected Workflow
```

API callers can pass metadata in the prompt request:

```json
{
  "prompt": {},
  "credit_tracker": {
    "project_name": "Aramco",
    "user_name": "Momi",
    "workflow_name": "Seedance Campaign Workflow"
  }
}
```

The same `credit_tracker` object may also be placed under `extra_data`.

## Node Inputs

| Input | Type | Default | Purpose |
|---|---:|---|---|
| `project_name` | STRING | `General` | Company project, client, department, or cost center. |
| `user_name` | STRING | `Unknown` | Person or team running the workflow. |
| `workflow_name` | STRING | `Untitled Workflow` | Human-readable workflow name. |
| `partner_node_name` | STRING | `Unknown Partner Node` | Name used to look up pricing in `pricing_table.json`. |
| `quantity` | INT | `1` | Number of runs or outputs to bill. |
| `duration_seconds` | FLOAT | `0` | Duration for per-second pricing. |
| `resolution` | STRING | empty | Optional resolution label, for example `1024x1024` or `1080p`. |
| `notes` | STRING | empty | Optional notes for later review. |

The node returns a readable summary string, for example:

```text
Logged Seedance 2.0 for project Aramco: 600 credits estimated, approx $2.84
```

## Visual Report Node

Search for:

```text
Credit Tracker Report Viewer
```

This node reads `usage_log.db` and outputs:

- `report_image`: a visual chart/table preview for the ComfyUI canvas.
- `report_text`: a plain-text report summary.

Inputs:

| Input | Type | Default | Purpose |
|---|---:|---|---|
| `report_view` | COMBO | `By Partner Node` | Switch between partner-node and project summaries. |
| `top_n` | INT | `10` | Number of rows to show. |
| `project_filter` | STRING | empty | Optional project-name filter. |
| `partner_node_filter` | STRING | empty | Optional partner-node filter. |
| `refresh` | INT | `0` | Change this value manually if you want to force a UI refresh. |
| `trigger_image` | IMAGE | optional | Connect an API node image output here to force the report to render after that node. |

The report viewer does not create usage records. It only reads the database.

## Pricing Table

Edit `pricing_table.json` to match your current ComfyUI Partner/API Node prices.

Official pricing reference:

```text
https://docs.comfy.org/tutorials/partner-nodes/pricing
```

To sync the official docs table into the dashboard cache:

```powershell
python pricing_sync.py
```

or, from the portable Windows install:

```powershell
..\..\..\python_embeded\python.exe pricing_sync.py
```

The official cache helps with review and price lookup, but it does not replace `pricing_table.json` for class-name aliases. The docs table lists products, configurations, credits, and categories; it does not know the local Python `class_type` names installed in your ComfyUI.

For automatic tracking, add `auto_detect_class_types` to the pricing entry. These values should match the ComfyUI node `class_type` names found in workflow API JSON or the Python class names used by the Partner/API node package.

Supported pricing modes:

### fixed_per_run

```json
{
  "Nano Banana": {
    "pricing_mode": "fixed_per_run",
    "credits": 14.7,
    "auto_detect_class_types": [
      "GeminiImageNode",
      "GeminiImage2Node",
      "GeminiNanoBanana2"
    ]
  }
}
```

Calculation:

```text
credits * quantity
```

### per_second

```json
{
  "Seedance 2.0": {
    "pricing_mode": "per_second",
    "credits_per_second": 20
  }
}
```

Calculation:

```text
credits_per_second * duration_seconds * quantity
```

### per_output

```json
{
  "Image API Example": {
    "pricing_mode": "per_output",
    "credits_per_output": 10
  }
}
```

Calculation:

```text
credits_per_output * quantity
```

### manual

```json
{
  "Unknown Node": {
    "pricing_mode": "manual",
    "credits": 0
  }
}
```

Manual pricing logs `0` estimated credits. You can later update the SQLite row or pricing table when the correct billing details are known.

If `pricing_table.json` is missing, the extension creates a default one. If `partner_node_name` is not found, the record is logged with:

```text
pricing_mode = unknown
estimated_credits = 0
```

## Credit Conversion

The current conversion setting is stored once in `tracker_db.py`:

```python
CREDITS_PER_USD = 211.0
```

Estimated USD is calculated as:

```text
estimated_credits / CREDITS_PER_USD
```

Update this one value if the official ComfyUI credit conversion rate changes.

## SQLite Schema

Table name:

```text
credit_usage
```

Columns:

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
timestamp TEXT
project_name TEXT
user_name TEXT
workflow_name TEXT
partner_node_name TEXT
node_class_type TEXT
pricing_mode TEXT
source TEXT
quantity INTEGER
duration_seconds REAL
resolution TEXT
estimated_credits REAL
estimated_usd REAL
prompt_id TEXT
node_id TEXT
dedupe_key TEXT
notes TEXT
```

## Export Reports

Open a terminal in the extension folder:

```text
ComfyUI/custom_nodes/comfyui_credit_tracker/
```

Run:

```bash
python export_report.py
```

If you use the portable Windows build, you can also run it with the bundled Python:

```powershell
..\..\..\python_embeded\python.exe export_report.py
```

The script generates:

```text
credit_usage_full.csv
credit_usage_summary_by_node.csv
credit_usage_summary_by_project.csv
```

It also prints:

- Total credits
- Total USD
- Top 10 most expensive partner nodes
- Top 10 most expensive projects

## Limitations

- This tracker estimates cost based on the local `pricing_table.json`.
- Runtime-price capture logs the displayed ComfyUI API-node price when that event is available.
- Built-in `comfy_api_nodes` are auto-discovered from source code and logged when executed.
- For simple fixed `price_badge` nodes, the tracker can estimate credits from the badge price.
- Complex dynamic `price_badge` formulas need provider-specific fallback code unless the node emits a runtime price.
- The official pricing cache comes from ComfyUI docs, but class aliases still need `pricing_table.json`.
- If ComfyUI changes Partner Node prices, sync the official cache and update any fallback prices in `pricing_table.json`.
- It does not deduct credits from ComfyUI. When the local balance service is available, it can read and snapshot the displayed account balance for reconciliation.
- Automatic mode detects nodes from workflow JSON and execution events. Runtime-price capture depends on the installed API node emitting the price display event.
- Without the manual logger node or API metadata, automatic mode cannot reliably know the project name, user name, or workflow name.
- It is designed for company-level reporting and budget estimation.

## Troubleshooting

If logging fails, the node returns a warning summary instead of crashing ComfyUI. Useful warnings are also printed in the ComfyUI console.

Common checks:

- Confirm the extension folder is inside `ComfyUI/custom_nodes/`.
- Restart ComfyUI after installing or editing Python files.
- Confirm `pricing_table.json` is valid JSON.
- Confirm the ComfyUI process has permission to write inside the extension folder.
