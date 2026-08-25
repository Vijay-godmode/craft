"""Deterministic QA Lab data and validation helpers.

The lab deliberately uses only synthetic catalog/order data owned by the
signed-in user.  It gives a QA learner a real, safe API and database workflow
to test without sending load to job boards or third-party services.
"""

from __future__ import annotations

from typing import Any


LAB_SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "slug": "smoke-workspace",
        "title": "Smoke: account to saved job",
        "area": "Smoke testing",
        "difficulty": "Foundation",
        "objective": "Prove the core signed-in path works after a release.",
        "checks": [
            "Sign in and load the dashboard.",
            "Save a factual profile change.",
            "Create a manual QA job and confirm it appears in New.",
            "Approve the job and confirm it appears in Applications.",
        ],
        "targets": ["/api/auth/me", "/api/profile", "/api/jobs", "/api/applications"],
    },
    {
        "slug": "sanity-state-transition",
        "title": "Sanity: job status transition",
        "area": "Sanity testing",
        "difficulty": "Foundation",
        "objective": "Check that one changed workflow still carries state correctly.",
        "checks": [
            "Create a QA job.",
            "Approve it and verify one application record is created.",
            "Close it and verify the active application is removed.",
            "Restore it and verify it returns to New.",
        ],
        "targets": ["/api/jobs/<id>/decision", "/api/jobs/<id>/close", "/api/jobs/<id>/reopen"],
    },
    {
        "slug": "integration-discovery",
        "title": "Integration: discovery to inbox",
        "area": "Integration testing",
        "difficulty": "Intermediate",
        "objective": "Test source parsing, search-run persistence, filtering, and queue rendering together.",
        "checks": [
            "Use a deterministic test fixture in automated tests.",
            "Refresh the feed and inspect Latest search results.",
            "Confirm a previously approved result still shows its state.",
            "Confirm a new result is visible in the New queue.",
        ],
        "targets": ["/api/jobs/discover", "/api/job-search-runs/<id>/results", "/jobs"],
    },
    {
        "slug": "api-contract-orders",
        "title": "API: catalog and orders contract",
        "area": "API testing",
        "difficulty": "Intermediate",
        "objective": "Use a real protected API to practise happy-path, negative, and contract tests.",
        "checks": [
            "Fetch catalog pagination with a valid session.",
            "Create a valid order with an idempotency key.",
            "Repeat the request and verify the same order is returned.",
            "Send missing/invalid quantities and assert a 400 error shape.",
        ],
        "targets": ["/api/lab/catalog", "/api/lab/orders", "/openapi.json"],
    },
    {
        "slug": "data-integrity",
        "title": "Data: integrity and duplicate handling",
        "area": "Database testing",
        "difficulty": "Intermediate",
        "objective": "Check foreign keys, stock validation, ownership, and idempotent writes.",
        "checks": [
            "Attempt an order for an unknown product.",
            "Attempt an order above available stock.",
            "Verify failed orders do not lower stock.",
            "Verify a second account cannot read the first account's orders.",
        ],
        "targets": ["lab_catalog_items", "lab_orders", "lab_order_items"],
    },
    {
        "slug": "ui-accessibility",
        "title": "UI: keyboard and accessibility",
        "area": "UI/UX and accessibility",
        "difficulty": "Intermediate",
        "objective": "Validate that core forms work with keyboard and assistive technology expectations.",
        "checks": [
            "Navigate profile and job forms without a mouse.",
            "Check visible focus and form labels.",
            "Verify loading, empty, and error messages announce changes.",
            "Run an axe scan in a Playwright test.",
        ],
        "targets": ["/profile", "/jobs", "[aria-live]", "data-testid attributes"],
    },
    {
        "slug": "compatibility-responsive",
        "title": "Compatibility: browser and viewport matrix",
        "area": "Compatibility testing",
        "difficulty": "Intermediate",
        "objective": "Verify the high-value workflows across current browser engines and viewports.",
        "checks": [
            "Check Chromium, Firefox, and WebKit.",
            "Check 360px, tablet, laptop, and wide desktop widths.",
            "Verify no horizontal scroll on forms and job cards.",
        ],
        "targets": ["/jobs", "/builder", "/applications"],
    },
    {
        "slug": "performance-resilience",
        "title": "Performance: local fixture resilience",
        "area": "Performance testing",
        "difficulty": "Advanced",
        "objective": "Measure predictable endpoints and ensure provider failures remain understandable.",
        "checks": [
            "Measure catalog, dashboard, and job-list response time with seeded data.",
            "Simulate an unavailable job provider in automated tests.",
            "Verify cached results remain visible after provider failure.",
            "Never load-test public job sources.",
        ],
        "targets": ["/api/dashboard", "/api/lab/catalog", "/api/jobs/discover"],
    },
    {
        "slug": "security-session",
        "title": "Security: session and authorization",
        "area": "Security testing",
        "difficulty": "Advanced",
        "objective": "Confirm a signed-in user owns their data and unsafe requests are rejected.",
        "checks": [
            "Call a protected API without a session and expect 401.",
            "Send a mutation without the CSRF token and expect 403.",
            "Use two accounts and assert IDOR attempts return 404.",
            "Verify logout makes protected APIs unavailable.",
        ],
        "targets": ["/api/csrf", "/api/auth/logout", "/api/jobs/<id>", "/api/lab/orders"],
    },
    {
        "slug": "cicd-release-gate",
        "title": "CI/CD: release quality gate",
        "area": "CI/CD testing",
        "difficulty": "Advanced",
        "objective": "Turn repeatable checks into a deployment gate.",
        "checks": [
            "Run Python compilation and unit/integration tests.",
            "Run JavaScript syntax validation.",
            "Run contract and browser smoke tests with fixtures.",
            "Build the Docker image before deployment.",
        ],
        "targets": ["test_careercraft.py", "static/app.js", "Dockerfile"],
    },
)


SYNTHETIC_CATALOG: tuple[dict[str, Any], ...] = (
    {"sku": "QA-API-101", "name": "API Testing Workbook", "category": "Learning", "price_paise": 49900, "stock": 24},
    {"sku": "QA-UI-201", "name": "UI Automation Practice Kit", "category": "Learning", "price_paise": 79900, "stock": 15},
    {"sku": "QA-DATA-301", "name": "Database Validation Cards", "category": "Learning", "price_paise": 59900, "stock": 18},
    {"sku": "QA-PERF-401", "name": "Performance Test Checklist", "category": "Toolkit", "price_paise": 29900, "stock": 40},
    {"sku": "QA-A11Y-501", "name": "Accessibility Review Pack", "category": "Toolkit", "price_paise": 34900, "stock": 32},
)


def scenario_by_slug(slug: str) -> dict[str, Any] | None:
    return next((item for item in LAB_SCENARIOS if item["slug"] == slug), None)


def public_scenarios() -> list[dict[str, Any]]:
    return [dict(item) for item in LAB_SCENARIOS]


def normalise_order_payload(payload: Any) -> tuple[str, list[dict[str, int]], str]:
    """Validate the public order contract before database work begins."""

    if not isinstance(payload, dict):
        raise ValueError("Send an order JSON object.")
    customer_name = " ".join(str(payload.get("customer_name") or "").split())[:120]
    idempotency_key = " ".join(str(payload.get("idempotency_key") or "").split())[:120]
    raw_items = payload.get("items")
    if len(customer_name) < 2:
        raise ValueError("customer_name must contain at least two characters.")
    if len(idempotency_key) < 8:
        raise ValueError("idempotency_key must contain at least eight characters.")
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 10:
        raise ValueError("items must contain between one and ten order lines.")
    items: list[dict[str, int]] = []
    seen: set[int] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError("Every order item must be an object.")
        try:
            product_id = int(raw_item.get("product_id"))
            quantity = int(raw_item.get("quantity"))
        except (TypeError, ValueError) as exc:
            raise ValueError("product_id and quantity must be whole numbers.") from exc
        if product_id < 1 or quantity < 1 or quantity > 20:
            raise ValueError("Each product_id must be positive and quantity must be between 1 and 20.")
        if product_id in seen:
            raise ValueError("Combine duplicate product lines before submitting the order.")
        seen.add(product_id)
        items.append({"product_id": product_id, "quantity": quantity})
    return customer_name, items, idempotency_key


def money_from_paise(value: int) -> float:
    return round(int(value) / 100, 2)


def openapi_document(base_url: str = "") -> dict[str, Any]:
    """Return a concise, valid-enough OpenAPI document for testing tools."""

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "CareerCraft QA Lab API",
            "version": "1.0.0",
            "description": "A signed-in, synthetic catalog and order API for safe QA practice.",
        },
        "servers": [{"url": base_url or "/"}],
        "paths": {
            "/api/lab/catalog": {
                "get": {
                    "summary": "List synthetic catalog items",
                    "responses": {"200": {"description": "Catalog page"}, "401": {"description": "Sign in required"}},
                }
            },
            "/api/lab/orders": {
                "get": {"summary": "List the signed-in user's synthetic orders", "responses": {"200": {"description": "Order list"}}},
                "post": {
                    "summary": "Create an idempotent synthetic order",
                    "parameters": [{"name": "X-CSRF-Token", "in": "header", "required": True, "schema": {"type": "string"}}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["customer_name", "idempotency_key", "items"],
                                    "properties": {
                                        "customer_name": {"type": "string"},
                                        "idempotency_key": {"type": "string", "minLength": 8},
                                        "items": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "required": ["product_id", "quantity"],
                                                "properties": {"product_id": {"type": "integer"}, "quantity": {"type": "integer", "minimum": 1}},
                                            },
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "Order created"}, "200": {"description": "Prior idempotent order"}, "400": {"description": "Validation error"}},
                },
            },
        },
    }
