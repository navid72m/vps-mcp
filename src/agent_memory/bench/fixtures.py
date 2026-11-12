"""Deterministic benchmark fixtures with generic content (research A13).

~300 memories over 3 projects, 2 environments and user scope; superseded, expired and
invalidated items; seeded contradictions; 25 tasks with expected and forbidden memories.
"""

from __future__ import annotations

import random
from typing import Any

PROJECTS = ("shop", "blog", "infra")
FILLER_WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november "
    "oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu"
).split()


def M(
    key: str,
    type: str,
    scope: str,
    subject: str,
    title: str,
    content: str,  # noqa: N802
    **kw: Any,
) -> dict[str, Any]:
    return {
        "key": key,
        "type": type,
        "scope": scope,
        "subject": subject,
        "title": title,
        "content": content,
        **kw,
    }


def build() -> dict[str, Any]:
    rnd = random.Random(1306)  # noqa: S311 - deterministic fixtures
    mems: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []

    def task(
        text: str,
        project: str,
        expected: list[str],
        forbidden: list[str] | None = None,
        environment: str | None = None,
    ) -> None:
        tasks.append(
            {
                "text": text,
                "scope": {"project": project, "environment": environment},
                "expected": expected,
                "forbidden": forbidden or [],
            }
        )

    # --- shop project: database upgrade story
    mems += [
        M(
            "pg_old",
            "semantic",
            "project:shop",
            "postgres.version",
            "Production runs PostgreSQL 16",
            "The production database server runs PostgreSQL 16.",
            superseded_by="pg_new",
        ),
        M(
            "pg_new",
            "semantic",
            "project:shop",
            "postgres.version",
            "Production runs PostgreSQL 17",
            "The production database server was upgraded to PostgreSQL 17.",
            importance=4,
        ),
        M(
            "backup_first",
            "procedural",
            "project:shop",
            "migration.backup",
            "Back up before any database migration",
            "Always take a full database backup before running a migration.",
            importance=5,
            pinned=True,
        ),
        M(
            "stop_app_first",
            "procedural",
            "project:shop",
            "deploy.order",
            "Stop the application before database migrations",
            "The deployment fails when the database migration runs before the application "
            "service is stopped; stop the service first.",
            importance=5,
        ),
        M(
            "staged_upgrade",
            "decision",
            "project:shop",
            "upgrade.strategy",
            "Upgrade the database in stages",
            "Upgrade through every major version in stages, rehearsing each one first.",
            alternatives=["jump straight to the target version"],
            importance=4,
        ),
        M(
            "rehearsal_fail",
            "episodic",
            "project:shop",
            "rehearsal.missing.dependency",
            "Rehearsal failed: custom module dependency missing",
            "The last migration rehearsal failed because a custom module dependency was missing.",
        ),
        M(
            "cdn_cache",
            "semantic",
            "project:shop",
            "cdn.cache.ttl",
            "CDN cache TTL is one hour",
            "Static assets are cached by the CDN for one hour.",
        ),
        M(
            "cart_bug",
            "episodic",
            "project:shop",
            "checkout.cart.bug",
            "Checkout cart drops coupons",
            "Coupons disappear from the checkout cart after login.",
            state="open",
        ),
        M(
            "payments_provider",
            "decision",
            "project:shop",
            "payments.provider",
            "Payments go through the hosted gateway",
            "Card payments use the hosted payment gateway so card data never touches our servers.",
            alternatives=["self-hosted card form"],
        ),
        M(
            "old_payments",
            "decision",
            "project:shop",
            "payments.retry",
            "Retry failed payments nightly",
            "Failed card payments are retried every night.",
            status="invalidated",
        ),
        M(
            "promo_expired",
            "semantic",
            "project:shop",
            "promo.banner",
            "Black friday promo banner is live",
            "The black friday promo banner is live.",
            expires="past",
        ),
    ]
    task(
        "upgrade the production database to a new postgres version",
        "shop",
        ["pg_new", "backup_first", "staged_upgrade", "stop_app_first"],
        ["pg_old"],
    )
    task(
        "plan the database migration rehearsal",
        "shop",
        ["backup_first", "rehearsal_fail", "staged_upgrade"],
        ["pg_old"],
    )
    task(
        "deploy the new release with a database migration",
        "shop",
        ["stop_app_first", "backup_first"],
    )
    task("fix the checkout cart coupons problem", "shop", ["cart_bug"])
    task("change how card payments are handled", "shop", ["payments_provider"], ["old_payments"])
    task("retry failed payments", "shop", [], ["old_payments"])
    task("update the promo banner", "shop", [], ["promo_expired"])
    task("tune cdn caching for static assets", "shop", ["cdn_cache"])

    # --- blog project
    mems += [
        M(
            "blog_engine",
            "semantic",
            "project:blog",
            "blog.engine",
            "The blog is a static site",
            "The blog is generated as a static site and served from object storage.",
        ),
        M(
            "blog_deploy",
            "procedural",
            "project:blog",
            "blog.deploy",
            "Publish the blog by pushing to main",
            "Pushing to the main branch rebuilds and publishes the blog.",
        ),
        M(
            "blog_comments",
            "decision",
            "project:blog",
            "blog.comments",
            "No comments on the blog",
            "Comments stay disabled to avoid moderation work.",
            alternatives=["third-party comment widget"],
        ),
        M(
            "blog_backup_pg",
            "procedural",
            "project:blog",
            "migration.backup",
            "Blog database backup",
            "Back up the blog database before migration weekly.",
        ),
        M(
            "blog_rss",
            "episodic",
            "project:blog",
            "blog.rss.broken",
            "RSS feed broke once",
            "The RSS feed broke when a post had an invalid date.",
        ),
    ]
    task("publish a new blog post", "blog", ["blog_deploy", "blog_engine"])
    task("should we add comments to the blog", "blog", ["blog_comments"])
    task("the blog rss feed is broken again", "blog", ["blog_rss"])
    task("back up before the blog database migration", "blog", ["blog_backup_pg"], ["backup_first"])

    # --- infra project + environments
    mems += [
        M(
            "prod_web",
            "semantic",
            "environment:prod",
            "service.web.state",
            "web is running",
            "The web service is active on prod.",
            live_state=True,
            importance=3,
            source_kind="tool_generated",
        ),
        M(
            "prod_disk",
            "semantic",
            "environment:prod",
            "disk.free",
            "prod disk 40 GB free",
            "The prod server had 40 GB free disk.",
            live_state=True,
            stale=True,
            source_kind="tool_generated",
        ),
        M(
            "staging_web",
            "semantic",
            "environment:staging",
            "service.web.state",
            "web is stopped on staging",
            "The web service is stopped on staging.",
            live_state=True,
        ),
        M(
            "infra_ssh",
            "procedural",
            "project:infra",
            "ssh.access",
            "SSH only with keys",
            "Log in over SSH with keys only; password logins are disabled.",
        ),
        M(
            "infra_firewall",
            "decision",
            "project:infra",
            "firewall.policy",
            "Default-deny firewall",
            "The firewall denies all inbound traffic except web and ssh.",
            alternatives=["allow-list per service only"],
        ),
        M(
            "monitor_old",
            "semantic",
            "project:infra",
            "monitoring.tool",
            "Monitoring with the old agent",
            "Servers are monitored with the old agent.",
            superseded_by="monitor_new",
        ),
        M(
            "monitor_new",
            "semantic",
            "project:infra",
            "monitoring.tool",
            "Monitoring with the new exporter",
            "Servers are monitored with the new exporter.",
        ),
        # seeded contradictions (both active, same subject)
        M(
            "tz_a",
            "semantic",
            "project:infra",
            "server.timezone",
            "Servers use UTC",
            "All servers are set to the UTC timezone.",
            conflict_group="tz",
        ),
        M(
            "tz_b",
            "semantic",
            "project:infra",
            "server.timezone",
            "Servers use local time",
            "The servers are set to local time not UTC.",
            conflict_group="tz",
        ),
        M(
            "swap_a",
            "semantic",
            "project:infra",
            "server.swap",
            "Swap is disabled",
            "Swap is disabled on every server.",
            conflict_group="swap",
        ),
        M(
            "swap_b",
            "semantic",
            "project:infra",
            "server.swap",
            "Swap is 2 GB",
            "Every server has a 2 GB swap file enabled.",
            conflict_group="swap",
        ),
    ]
    task(
        "is the web service running on prod",
        "infra",
        ["prod_web"],
        ["staging_web"],
        environment="prod",
    )
    task("how much free disk does prod have", "infra", ["prod_disk"], environment="prod")
    task("how do I log in over ssh", "infra", ["infra_ssh"])
    task("open a new port in the firewall", "infra", ["infra_firewall"])
    task("which monitoring tool watches the servers", "infra", ["monitor_new"], ["monitor_old"])
    task("what timezone are the servers in", "infra", ["tz_a", "tz_b"])
    task("is swap enabled on the servers", "infra", ["swap_a", "swap_b"])

    # --- user scope
    mems += [
        M(
            "pref_short",
            "semantic",
            "user",
            "preference.answers",
            "Prefers short answers",
            "The operator prefers short, direct answers.",
            tags=["preference"],
        ),
        M(
            "pref_night",
            "procedural",
            "user",
            "schedule.heavy.jobs",
            "Run heavy jobs at night",
            "Heavy rehearsals and migrations run at night.",
        ),
    ]
    task("when should the heavy migration rehearsal run", "shop", ["pref_night"])
    task("how should answers be formatted for the operator", "blog", ["pref_short"])
    task("what is the cdn cache ttl on the blog", "blog", [], ["cdn_cache"])

    # --- filler: plausible but unrelated memories
    kinds = ("semantic", "procedural", "episodic", "decision")
    scopes = ["project:shop", "project:blog", "project:infra", "environment:prod", "user"]
    for i in range(260):
        words = rnd.sample(FILLER_WORDS, 6)
        mems.append(
            M(
                f"filler_{i}",
                kinds[i % 4],
                scopes[i % len(scopes)],
                f"misc.{words[0]}.{words[1]}.{i}",
                f"Note {words[2]} {words[3]}",
                f"Unrelated note about {' '.join(words)} number {i}.",
                importance=1 + i % 5,
            )
        )
    return {"memories": mems, "tasks": tasks}
