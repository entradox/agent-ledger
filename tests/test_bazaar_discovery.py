"""Bazaar discovery declaration on the x402 mint route.

CDP's Bazaar only catalogs a resource when the settled PaymentPayload carries
the bazaar extension — a plain settle indexes nothing (confirmed live
2026-10-04: 4 settlements, discovery/search stayed at 0 hits). The declaration
is advertised in the 402's `extensions` block; clients echo it back at pay
time and the facilitator extracts it during settle.

These tests validate the declaration against the x402 SDK's OWN parser — the
same shape validation the facilitator applies — without needing facilitator
credentials to build the resource server.
"""
from x402.extensions.bazaar.types import parse_discovery_extension

import x402_verify


def test_bazaar_declaration_parses_with_sdk():
    parsed = parse_discovery_extension(x402_verify._BAZAAR_DISCOVERY)
    inp = parsed.info.input
    assert inp.type == "http"
    assert inp.method == "POST"
    # BodyInput uses the aliased bodyType field.
    assert getattr(inp, "body_type", getattr(inp, "bodyType", None)) == "json"


def test_bazaar_declaration_describes_the_mint_output():
    out = x402_verify._BAZAAR_DISCOVERY["info"]["output"]
    example = out["example"]
    assert "workspace_id" in example and "workspace_key" in example


def test_bazaar_extension_survives_route_config_construction():
    """The declaration must reach the wire: RouteConfig.extensions is what the
    SDK serializes into the 402's extensions block."""
    from x402.http import RouteConfig, PaymentOption

    rc = RouteConfig(
        accepts=PaymentOption(
            scheme="exact",
            pay_to="0x363c520492EDbA89057bCe696B74263B3295a72A",
            price="$0.01",
            network="eip155:8453",
        ),
        extensions={"bazaar": dict(x402_verify._BAZAAR_DISCOVERY)},
    )
    assert "bazaar" in rc.extensions
    assert rc.extensions["bazaar"]["info"]["input"]["method"] == "POST"
