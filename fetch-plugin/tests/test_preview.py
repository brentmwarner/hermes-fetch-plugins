import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_preview():
    spec = importlib.util.spec_from_file_location(
        "fetch_plugin_preview_test", PLUGIN_DIR / "_preview.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_fenced_card_json_becomes_readable_notification_body():
    preview = _load_preview()
    raw = """
```card
{"title":"World Cup Brief, Fri Jun 26","subtitle":"6 group finales","stats":[{"label":"Matches","value":6}],"items":[{"title":"Group E finales","subtitle":"Starts at 10 AM"}]}
```
"""

    body = preview.notification_body(raw, fallback="Open Fetch to view the update.")

    assert body == "World Cup Brief, Fri Jun 26, 6 group finales, Matches 6, Group E finales, Starts at 10 AM"
    assert "{" not in body
    assert "stats" not in body


def test_cron_wrapped_card_json_summarizes_card_not_header():
    preview = _load_preview()
    raw = """
Cronjob Response: Morning World Cup
(job_id: abc123)

```card
{"title":"World Cup Brief","subtitle":"Today at a glance","blocks":[{"type":"metric","label":"Matches","value":2},{"type":"checklist","steps":[{"title":"Spain vs Japan"},{"title":"Canada vs Morocco"}]}]}
```
"""

    body = preview.notification_body(raw, fallback="Open Fetch to view the update.")

    assert body == "World Cup Brief, Today at a glance, Matches 2, Spain vs Japan, Canada vs Morocco"


def test_plain_text_strips_markdown_but_preserves_message():
    preview = _load_preview()

    assert preview.notification_body("## Done\n- **Report** is `ready`", fallback="fallback") == "Done Report is ready"


def test_unsummarized_json_falls_back():
    preview = _load_preview()

    assert (
        preview.notification_body('{"type":"unknown","payload":{"raw":true}}', fallback="Open Fetch")
        == "Open Fetch"
    )


def test_truncated_json_like_payload_falls_back():
    preview = _load_preview()

    assert preview.notification_body('{"payload":"' + ("x" * 7000), fallback="Open Fetch") == "Open Fetch"


def test_malformed_card_fence_falls_back():
    preview = _load_preview()

    assert preview.notification_body('```card\n{"title":"Daily"', fallback="Open Fetch") == "Open Fetch"
