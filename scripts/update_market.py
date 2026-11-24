"""Generate the AstrBot market source from the installable plugin metadata."""
import argparse
import json
from pathlib import Path
import re
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]


def build_market(metadata):
    if not isinstance(metadata, dict):
        raise ValueError("metadata.yaml must contain an object")
    required = ("author", "name", "version", "repo", "desc")
    for key in required:
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"metadata.yaml requires a non-empty string: {key}")
    for key in ("author", "name"):
        value = metadata[key]
        if value != value.strip() or "/" in value or any(ord(char) < 32 for char in value):
            raise ValueError(f"Invalid plugin identity: {key}")
    repo = metadata["repo"].rstrip("/")
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)(?:\.git)?", repo)
    if not match:
        raise ValueError("repo must be an HTTPS GitHub repository URL")
    owner, repository = match.groups()
    record = {key: metadata[key] for key in required}
    record["repo"] = f"https://github.com/{owner}/{repository}"
    for key in ("display_name", "short_desc", "astrbot_version", "support_platforms"):
        if key in metadata:
            record[key] = metadata[key]
    record["logo"] = f"https://raw.githubusercontent.com/{owner}/{repository}/main/logo.png"
    record["tags"] = ["永劫无间", "战绩查询", "AI分析"]
    # The name-key form is explicitly supported by the current specification
    # and remains compatible with older AstrBot market readers.
    return {
        "$meta": {
            "schema_version": 1,
            "name": "永劫无间战绩查询插件源",
            "version": metadata["version"],
            "homepage": record["repo"],
            "repository": record["repo"],
        },
        metadata["name"]: record,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the committed source is out of date")
    args = parser.parse_args()
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8-sig"))
    expected = build_market(metadata)
    target = ROOT / "plugin-market.json"
    if args.check:
        actual = json.loads(target.read_text(encoding="utf-8")) if target.exists() else None
        if actual != expected:
            print("plugin-market.json is out of date; run: python scripts/update_market.py", file=sys.stderr)
            return 1
        print("plugin-market.json matches metadata.yaml")
    else:
        target.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Updated plugin-market.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
