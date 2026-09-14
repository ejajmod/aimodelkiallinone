from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_manifest(catalog_path: Path) -> dict[str, object]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    bundles: list[dict[str, object]] = []
    for workflow in catalog.get("workflows", []):
        files: list[dict[str, object]] = []
        for download in workflow.get("downloads", []):
            destination = str(download["destination"]).replace("\\", "/")
            relative = destination.removeprefix("models/")
            digest = str(download["sha256"]).lower()
            files.append(
                {
                    "id": f"model-{digest[:16]}",
                    "filename": Path(relative).name,
                    "size": int(download["size_bytes"]),
                    "sha256": digest,
                    "destination": relative,
                    # R2 upload keeps the same tree as the launcher catalog.
                    "objectKey": destination,
                }
            )
        bundles.append(
            {
                "id": workflow["id"],
                "title": workflow["title"],
                "description": workflow.get("description", ""),
                "files": files,
            }
        )
    return {"version": f"launcher-catalog-{catalog.get('version', 1)}", "bundles": bundles}


def main() -> None:
    parser = argparse.ArgumentParser(description="Eksport katalogu launchera do manifestu Instant Models")
    parser.add_argument("output", type=Path)
    parser.add_argument("--catalog", type=Path, default=Path("catalog/catalog.json"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_manifest(args.catalog), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
