from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse
import unittest

from blackdog.codex_link import build_codex_workspace_url


class CodexLinkTests(unittest.TestCase):
    def test_workspace_url_round_trips_encoded_path_and_prompt(self) -> None:
        workspace_path = Path("/tmp/Blackdog link/µ & task")
        prompt = "Continue A+B & follow next_action exactly."

        url = build_codex_workspace_url(workspace_path=workspace_path, prompt=prompt)

        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, "codex")
        self.assertEqual(parsed.netloc, "threads")
        self.assertEqual(parsed.path, "/new")
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "path": [str(workspace_path.resolve())],
                "prompt": [prompt],
            },
        )
        self.assertIn("%20", url)
        self.assertIn("%26", url)
        self.assertIn("%C2%B5", url)
        self.assertNotIn("+", parsed.query)


if __name__ == "__main__":
    unittest.main()
