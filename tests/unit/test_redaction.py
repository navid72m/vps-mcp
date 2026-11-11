"""FR-033 / SC-007: credentials never appear in output, logs or the database (T077)."""

import logging
import sqlite3

from tests.conftest import KEY_PATH, PASSPHRASE, call
from vps_mcp.remote.redact import install_redaction, mask_mapping, mask_text_lines

EVERY_TOOL = [
    ("list_services", {}),
    ("get_service_status", {"service": "api"}),
    ("get_service_logs", {"service": "api"}),
    ("get_service_config", {"service": "api"}),
    ("get_resource_usage", {}),
    ("list_revisions", {"service": "api"}),
    ("start_service", {"service": "worker"}),
    ("stop_service", {"service": "worker"}),
    ("restart_service", {"service": "api"}),
    ("set_boot_start", {"service": "worker", "enabled": True}),
    (
        "deploy_service",
        {
            "name": "cache",
            "kind": "container",
            "confirm": True,
            "container": {"image": "redis:7", "env": {"REDIS_PASSWORD": "pw-123456"}},
        },
    ),
    ("update_service", {"service": "api", "image": "ghcr.io/myorg/api:1.1", "confirm": True}),
    (
        "configure_service",
        {"service": "api", "spec_patch": {"env": {"API_TOKEN": "tok-987654"}}, "confirm": True},
    ),
    ("rollback_service", {"service": "api", "confirm": True}),
    ("remove_service", {"service": "cache", "confirm": True}),
    ("get_audit_log", {"limit": 500}),
]


def test_masking_helpers():
    globs = ["*PASSWORD*", "*TOKEN*"]
    assert mask_mapping({"DB_PASSWORD": "x", "MODE": "y"}, globs) == {
        "DB_PASSWORD": "***",
        "MODE": "y",
    }
    text = "db_password = s3\nexport API_TOKEN=abc\nplain = ok\nauth_token: zzz\n"
    assert mask_text_lines(text, globs) == (
        "db_password = ***\nexport API_TOKEN=***\nplain = ok\nauth_token: ***\n"
    )


def test_log_filter_scrubs():
    logger = logging.getLogger("vps_mcp.test_redaction")
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger.addHandler(H())
    install_redaction(logger, [KEY_PATH, PASSPHRASE])
    logger.warning("loading %s with %s", KEY_PATH, PASSPHRASE)
    assert records == ["loading *** with ***"]


async def test_no_credentials_anywhere(client, vps, app, db_path, caplog):
    # Make the VPS echo the credentials back, as a hostile or broken host might.
    vps.containers["api"].logs.append(
        (vps.containers["api"].started, "stdout", f"leaked {KEY_PATH} {PASSPHRASE}")
    )
    caplog.set_level(logging.DEBUG)
    outputs = []
    for tool, args in EVERY_TOOL:
        result = await client.call_tool(tool, args)
        outputs.append(str(result.structured_content) + "".join(c.text for c in result.content))
    blob = "\n".join(outputs)
    assert KEY_PATH not in blob and PASSPHRASE not in blob
    assert "pw-123456" not in blob and "tok-987654" not in blob  # secret env values masked
    assert KEY_PATH not in caplog.text and PASSPHRASE not in caplog.text
    con = sqlite3.connect(db_path)
    audit = " ".join(str(r) for r in con.execute("SELECT * FROM audit_records"))
    services = " ".join(str(r) for r in con.execute("SELECT * FROM managed_services"))
    assert KEY_PATH not in audit + services and PASSPHRASE not in audit + services
    assert "pw-123456" not in audit and "tok-987654" not in audit
    records = (await call(client, "get_audit_log", limit=500))["records"]
    assert len(records) >= 9
