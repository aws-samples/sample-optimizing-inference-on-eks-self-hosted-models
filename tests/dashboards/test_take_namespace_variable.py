# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""demo/take.json: the `namespace` variable must NOT depend on the dashboard time range.  RED until the fix lands.

Incident (E7 takes, 2026-10-23): `namespace` was a query `label_values(dynamo_frontend_time_to_first_token_seconds_count, namespace)`
with refresh=2 (on time range change).  The S7 capture opened the dashboard ~240 s before the replay with an absolute from/to in the
future, so the options only held `s-agg`; Grafana replaced `var-namespace=s-dis-hic` with `s-agg` and the recorded video had 'No data'.

Contract of the fix: a custom variable with the four scenario namespaces, default `s-agg`, so any `var-namespace=<ns>` in the URL is
always a valid option whatever the range; and take-configmap.yaml carries byte-for-byte the JSON of take.json.
"""
from __future__ import annotations

import json

import pytest
import yaml

import _lib as L

TAKE = L.DASH / "demo" / "take.json"
CM = L.DASH / "demo" / "take-configmap.yaml"
NAMESPACES = ["s-agg", "s-agg-hic", "s-dis", "s-dis-hic"]


@pytest.fixture(scope="module")
def ns_var():
    d = L.load_json(TAKE)
    v = [x for x in d["templating"]["list"] if x["name"] == "namespace"]
    assert len(v) == 1, "exactly one namespace variable"
    return v[0]


def _values(var):
    return [x.strip() for x in str(var["query"]).split(",") if x.strip()]


def test_namespace_variable_is_custom_not_a_query(ns_var):
    assert ns_var["type"] == "custom", f"got {ns_var['type']!r}: a query variable is re-evaluated against the time range"


def test_namespace_variable_has_no_label_values_query_anywhere(ns_var):
    assert "label_values" not in json.dumps(ns_var)
    assert "definition" not in ns_var or "label_values" not in str(ns_var["definition"])


def test_namespace_variable_does_not_refresh_on_time_range_change(ns_var):
    assert ns_var.get("refresh", 0) != 2, "refresh=2 re-queries on time range change (the incident)"


def test_values_cover_the_four_namespaces_exactly(ns_var):
    assert _values(ns_var) == NAMESPACES, "values in the order s-agg,s-agg-hic,s-dis,s-dis-hic (the S0..S7 cells' namespaces)"


def test_default_is_s_agg(ns_var):
    cur = ns_var["current"]
    assert cur.get("value") == "s-agg" and cur.get("text") == "s-agg"
    assert cur.get("selected", True)


def test_options_if_present_list_all_four_with_s_agg_selected(ns_var):
    opts = ns_var.get("options") or []
    assert [o["value"] for o in opts] == NAMESPACES, "a custom variable stores its options in the JSON (empty options hide the picker)"
    assert [o["value"] for o in opts if o.get("selected")] == ["s-agg"]


def test_single_value_no_all(ns_var):
    assert not ns_var.get("multi") and not ns_var.get("includeAll")


def test_no_dependency_on_the_datasource_variable(ns_var):
    assert "datasource" not in ns_var or ns_var.get("type") == "custom"
    assert "${datasource}" not in json.dumps(ns_var)


def test_datasource_variable_is_untouched():
    d = L.load_json(TAKE)
    v = {x["name"]: x for x in d["templating"]["list"]}
    assert set(v) == {"datasource", "namespace"} and v["datasource"]["type"] == "datasource"


def test_each_namespace_is_a_valid_option_for_the_url(ns_var):
    """var-namespace=<ns> from the capture URL can only survive if <ns> is among the options."""
    for ns in ("s-agg", "s-dis-hic"):
        assert ns in _values(ns_var)


def test_configmap_data_is_byte_identical_to_take_json():
    cm = yaml.safe_load(CM.read_text())
    assert cm["data"]["take.json"] == TAKE.read_text(), "regenerate take-configmap.yaml (dashboards/README.md)"


def test_configmap_carries_the_new_variable():
    cm = yaml.safe_load(CM.read_text())
    d = json.loads(cm["data"]["take.json"])
    v = {x["name"]: x for x in d["templating"]["list"]}
    assert v["namespace"]["type"] == "custom", "ConfigMap is what Grafana loads; it must carry the fix, not only take.json"


def test_configmap_header_and_metadata_unchanged():
    text = CM.read_text()
    assert text.startswith("# Generated from dashboards/demo/take.json (single source of truth); do not edit by hand.\n")
    cm = yaml.safe_load(text)
    assert cm["metadata"]["name"] == "grafana-demo-take-dashboard" and cm["metadata"]["namespace"] == "monitoring"
    assert cm["metadata"]["labels"]["grafana_dashboard"] == "1" and list(cm["data"]) == ["take.json"]
