"""Publication contract: the source must resolve the same package and version."""
import json
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class PluginMarketTests(unittest.TestCase):
    def test_market_identity_version_and_install_repo_match_the_package(self):
        metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8-sig"))
        market = json.loads((ROOT / "plugin-market.json").read_text(encoding="utf-8"))
        self.assertEqual(market["$meta"]["schema_version"], 1)
        self.assertEqual(set(market), {"$meta", metadata["name"]})
        record = market[metadata["name"]]
        for key in ("author", "name", "version", "repo", "desc", "astrbot_version", "support_platforms"):
            self.assertEqual(record[key], metadata[key], key)
        self.assertEqual(record["repo"], "https://github.com/ink2421665373/astrbot-plugin-xxhyjwj")
        self.assertEqual(market["$meta"]["version"], metadata["version"])
        self.assertNotIn("download_url", record)  # Updates follow the repository's current default branch.


if __name__ == "__main__":
    unittest.main()
