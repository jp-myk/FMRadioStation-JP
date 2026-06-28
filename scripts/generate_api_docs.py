#!/usr/bin/env python3
"""Generate a self-contained Swagger UI HTML from docs/rest_api/openapi.yaml.

Usage:
    python scripts/generate_api_docs.py
    uv run python scripts/generate_api_docs.py
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml  # pyyaml — already in pyproject.toml dependencies

SWAGGER_UI_VERSION = "5.18.2"
_ROOT = Path(__file__).parent.parent
_YAML = _ROOT / "docs" / "rest_api" / "openapi.yaml"
_OUT = _ROOT / "docs" / "rest_api" / "index.html"
_CDN = f"https://unpkg.com/swagger-ui-dist@{SWAGGER_UI_VERSION}"


def main() -> None:
    spec = yaml.safe_load(_YAML.read_text(encoding="utf-8"))
    spec_json = json.dumps(spec, ensure_ascii=False, indent=2)
    title = spec["info"]["title"]

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="stylesheet" href="{_CDN}/swagger-ui.css">
  <style>
    body {{ margin: 0; }}
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="{_CDN}/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({{
      spec: {spec_json},
      dom_id: '#swagger-ui',
      presets: [
        SwaggerUIBundle.presets.apis,
        SwaggerUIBundle.SwaggerUIStandalonePreset,
      ],
      layout: 'BaseLayout',
      deepLinking: true,
      defaultModelsExpandDepth: 1,
      defaultModelExpandDepth: 2,
    }});
  </script>
</body>
</html>"""

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(html, encoding="utf-8")
    print(f"Generated: {_OUT}")


if __name__ == "__main__":
    main()
