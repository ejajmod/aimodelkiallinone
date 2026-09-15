"""Build pinned custom-node archives for R2 and record their SHA-256 in the catalog.

Each archive holds the files of one node at the exact revision pinned in the catalog,
without git history. The launcher installs a node from its archive only when the
catalog carries ``archive_sha256``; without it, the pinned commit comes from GitHub.

Usage:
    python scripts/export-custom-node-archives.py dist/custom-nodes --write-catalog
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path


def git(*arguments: str, cwd: Path) -> bytes:
    return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True).stdout


def build_archive(repository: str, revision: str, output: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="node-archive-") as temporary:
        checkout = Path(temporary)
        git("init", "-q", cwd=checkout)
        git("fetch", "--depth", "1", "--no-tags", repository, revision, cwd=checkout)
        actual = git("rev-parse", "FETCH_HEAD", cwd=checkout).decode("ascii").strip()
        if actual != revision:
            raise RuntimeError(f"{repository}: fetched {actual}, expected {revision}")
        tar = git("archive", "--format=tar", "FETCH_HEAD", cwd=checkout)
    output.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the archive byte-identical for the same revision.
    with output.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as archive:
        archive.write(tar)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path, help="directory for the .tar.gz archives")
    parser.add_argument("--catalog", type=Path, default=Path("catalog/catalog.json"))
    parser.add_argument("--write-catalog", action="store_true", help="store archive_sha256 in the catalog")
    parser.add_argument("--bucket", default="aimodelki-instant-models")
    parser.add_argument("--prefix", default="custom-nodes")
    args = parser.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    digests: dict[str, str] = {}
    for workflow in catalog["workflows"]:
        for node in workflow.get("custom_nodes", []):
            directory = node["directory"]
            if directory in digests:
                continue
            name = Path(directory).name
            archive = args.output / f"{name}-{node['revision']}.tar.gz"
            digests[directory] = build_archive(node["repository"], node["revision"], archive)
            print(f"{archive.name}  {digests[directory]}")

    if args.write_catalog:
        for workflow in catalog["workflows"]:
            for node in workflow.get("custom_nodes", []):
                node["archive_sha256"] = digests[node["directory"]]
        args.catalog.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Updated {args.catalog}")

    print("\nUpload the archives before publishing a catalog that references them:")
    print(
        f'aws s3 cp "{args.output}" s3://{args.bucket}/{args.prefix}/ --recursive '
        '--exclude "*" --include "*.tar.gz" --endpoint-url $R2_ENDPOINT --profile aimodelki-r2-upload'
    )


if __name__ == "__main__":
    main()
