import argparse

from tests.support import load_fetcher

okta = load_fetcher("okta")


def test_openapi_and_help_conversion_with_path_mapping():
    spec = {
        "components": {
            "schemas": {
                "User": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string", "description": "User ID"},
                        "status": {"type": "string", "enum": ["ACTIVE", "SUSPENDED"]},
                    },
                }
            }
        }
    }
    schema = okta.schema_to_markdown({"$ref": "#/components/schemas/User"}, spec)
    title, markdown = okta.html_to_markdown(
        '<html><title>Reset Password</title><body><main id="mc-main-content"><h1>Reset</h1>'
        "<p>Select <strong>Reset</strong>.</p></main></body></html>"
    )

    assert "`id` (string) **required**: User ID" in schema
    assert "enum: `ACTIVE`, `SUSPENDED`" in schema
    assert title == "Reset Password"
    assert "# Reset" in markdown
    assert "Select **Reset**." in markdown
    assert okta.sanitize_help_path("/content/topics/security/reset.htm") == "security/reset"


def test_help_discovery_failure_preserves_existing_cache(monkeypatch):
    cache = {"help:security/reset.md": {"sha256": "old"}, "api:other": {"sha256": "api"}}
    new_cache = {}
    monkeypatch.setattr(okta, "fetch_all_help_pages", lambda: (_ for _ in ()).throw(RuntimeError("offline")))

    result = okta.sync_help(cache, new_cache, argparse.Namespace(dry_run=False, verbose=False))

    assert result == (0, 0, 1, 0)
    assert new_cache == {"help:security/reset.md": {"sha256": "old"}}
