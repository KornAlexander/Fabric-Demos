# Fabric-Demos

End-to-end Microsoft Fabric demos with one-click installers. Each demo follows the same architecture pattern — **REST API -> Lakehouse -> Direct Lake -> Power BI** — so the code is reusable for any data source.

## Demos

| Demo | Description | Source data |
|---|---|---|
| [Hochschul-Insights](./Hochschul-Insights) | German higher-education statistics (Studierende, Personal, Finanzen) with an 8-page Power BI report, Direct Lake semantic model, and natural-language Data Agent. | [DESTATIS GENESIS](https://www-genesis.destatis.de/) REST API |

More demos coming soon.

## How the installers work

Each demo ships an `install.py` that:

1. Authenticates against Microsoft Fabric via `azure-identity` (DefaultAzureCredential).
2. Creates a folder in your target workspace.
3. Creates the Lakehouse, then patches all dependent items with the new IDs (notebooks, pipelines, semantic model, report, data agent) and POSTs them to the Fabric REST API.
4. No secrets are stored in this repo — tokens are passed in at install time.

See each demo's README for the exact one-liner.

## Author

Alexander Korn — Solution Engineer Data Platform, Microsoft.
