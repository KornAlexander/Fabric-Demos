# Fabric-Demos

End-to-end Microsoft Fabric demos with one-click installers. Each demo follows the same architecture pattern — **REST API -> Lakehouse -> Direct Lake -> Power BI** — so the code is reusable for any data source.

## Demos

| Demo | Description | Source data | Install |
|---|---|---|---|
| [Hochschul-Insights](./Hochschul-Insights) | German higher-education statistics (Studierende, Personal, Finanzen) with an 8-page Power BI report, Direct Lake semantic model, and natural-language Data Agent. | [DESTATIS GENESIS](https://www-genesis.destatis.de/) REST API | [`Install-Hochschul-Insights.ipynb`](./Hochschul-Insights/Install-Hochschul-Insights.ipynb) (Fabric notebook) · [`Install-Hochschul-Insights.py`](./Hochschul-Insights/Install-Hochschul-Insights.py) (local) |
| [DWD-Wetter-Insights](./DWD-Wetter-Insights) | German weather observations + Mosmix forecasts (1900–today, ~7.5M daily obs, ~1,400 stations, 48M hourly forecasts) with a 4-page IBCS Power BI report, Direct Lake semantic model with PY measures, and a Daily pipeline including Notify-on-failure. | [DWD Climate Data Center](https://opendata.dwd.de/) — open data, **no auth required** | [`Install-DWD-Wetter-Insights.ipynb`](./DWD-Wetter-Insights/Install-DWD-Wetter-Insights.ipynb) (Fabric notebook) · [`Install-DWD-Wetter-Insights.py`](./DWD-Wetter-Insights/Install-DWD-Wetter-Insights.py) (local) |

More demos coming soon.

## How the installers work

Every demo ships two installer flavours:

- **`Install-<demo>.ipynb`** — import into your Fabric workspace and Run All. Authenticates via `notebookutils`, auto-detects the current workspace. No local Python, no `az login`, no env vars.
- **`Install-<demo>.py`** — run from your laptop. Authenticates via `azure-identity` (`DefaultAzureCredential` — picks up `az login`, VS Code, env vars, or interactive browser).

Both share the same `install()` function and produce identical results: create a folder, create the Lakehouse, then patch and POST every dependent item (notebooks, pipeline, semantic model, report, data agent) to the Fabric REST API.

No secrets are stored in this repo — the DESTATIS / API token is supplied at install time and only injected into the payload locally on your machine (or in your Fabric notebook session) before being sent to Fabric.

## Author

Alexander Korn — Solution Engineer Data Platform, Microsoft.
