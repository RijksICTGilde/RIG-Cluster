"""
Tests for the FormState lifecycle and hooks system.

Tests the state machine, hook collection, ordering, and concrete hooks
(SubdomainRequestHook, StripTransientsHook).
"""

import pytest
from opi.forms.editables.editable import Editable, FormState, WidgetType
from opi.forms.editables.lifecycle import collect_hooks, run_hooks
from opi.forms.visualizers.visualizer import EditableVisualizer

# ---------------------------------------------------------------------------
# FormState tests
# ---------------------------------------------------------------------------


class TestFormState:
    def test_state_ordering(self):
        assert FormState.VALIDATE.value < FormState.PRE_SAVE.value
        assert FormState.PRE_SAVE.value < FormState.SAVE.value
        assert FormState.SAVE.value < FormState.POST_SAVE.value
        assert FormState.POST_SAVE.value < FormState.COMPLETED.value

    def test_get_next_state(self):
        assert FormState.get_next_state(FormState.VALIDATE) == FormState.PRE_SAVE
        assert FormState.get_next_state(FormState.PRE_SAVE) == FormState.SAVE
        assert FormState.get_next_state(FormState.SAVE) == FormState.POST_SAVE
        assert FormState.get_next_state(FormState.POST_SAVE) == FormState.COMPLETED
        assert FormState.get_next_state(FormState.COMPLETED) == FormState.COMPLETED

    def test_is_before_save(self):
        assert FormState.VALIDATE.is_before_save()
        assert FormState.PRE_SAVE.is_before_save()
        assert not FormState.SAVE.is_before_save()
        assert not FormState.POST_SAVE.is_before_save()

    def test_is_save(self):
        assert FormState.SAVE.is_save()
        assert not FormState.PRE_SAVE.is_save()

    def test_is_after_save(self):
        assert FormState.POST_SAVE.is_after_save()
        assert FormState.COMPLETED.is_after_save()
        assert not FormState.SAVE.is_after_save()


# ---------------------------------------------------------------------------
# Hook collection tests
# ---------------------------------------------------------------------------


class _TrackingHook:
    """Test hook that records execution."""

    def __init__(self, name: str, order: int = 0) -> None:
        self.name = name
        self.order = order
        self.executed = False
        self.execution_order: int | None = None

    async def execute(self, yaml_data: dict, context: dict) -> None:
        self.executed = True


class TestCollectHooks:
    def test_collects_hooks_from_editables(self):
        hook = _TrackingHook("test")
        ed = Editable(yaml_path="name", hooks={FormState.PRE_SAVE: hook})
        vis = EditableVisualizer(editable=ed, widget=WidgetType.TEXT, label="Name")

        result = collect_hooks([vis], FormState.PRE_SAVE)
        assert len(result) == 1
        assert result[0] == ("name", hook)

    def test_ignores_editables_without_hooks(self):
        ed = Editable(yaml_path="name")
        vis = EditableVisualizer(editable=ed, widget=WidgetType.TEXT, label="Name")

        result = collect_hooks([vis], FormState.PRE_SAVE)
        assert len(result) == 0

    def test_ignores_hooks_for_other_states(self):
        hook = _TrackingHook("test")
        ed = Editable(yaml_path="name", hooks={FormState.POST_SAVE: hook})
        vis = EditableVisualizer(editable=ed, widget=WidgetType.TEXT, label="Name")

        result = collect_hooks([vis], FormState.PRE_SAVE)
        assert len(result) == 0

    def test_recurses_into_children(self):
        child_hook = _TrackingHook("child")
        child_ed = Editable(yaml_path="child", hooks={FormState.PRE_SAVE: child_hook})
        child_vis = EditableVisualizer(editable=child_ed, widget=WidgetType.TEXT, label="Child")

        parent_ed = Editable(yaml_path="parent", children=[child_ed])
        parent_vis = EditableVisualizer(
            editable=parent_ed, widget=WidgetType.GROUP, label="Parent", children=[child_vis]
        )

        result = collect_hooks([parent_vis], FormState.PRE_SAVE)
        assert len(result) == 1
        assert result[0] == ("child", child_hook)

    def test_sorts_by_order(self):
        hook_last = _TrackingHook("last", order=999)
        hook_first = _TrackingHook("first", order=0)
        hook_mid = _TrackingHook("mid", order=50)

        editables = [
            EditableVisualizer(
                editable=Editable(yaml_path="last", hooks={FormState.PRE_SAVE: hook_last}),
                widget=WidgetType.TEXT,
                label="Last",
            ),
            EditableVisualizer(
                editable=Editable(yaml_path="first", hooks={FormState.PRE_SAVE: hook_first}),
                widget=WidgetType.TEXT,
                label="First",
            ),
            EditableVisualizer(
                editable=Editable(yaml_path="mid", hooks={FormState.PRE_SAVE: hook_mid}),
                widget=WidgetType.TEXT,
                label="Mid",
            ),
        ]

        result = collect_hooks(editables, FormState.PRE_SAVE)
        assert [r[1].name for r in result] == ["first", "mid", "last"]


# ---------------------------------------------------------------------------
# run_hooks tests
# ---------------------------------------------------------------------------


class TestRunHooks:
    @pytest.mark.asyncio
    async def test_executes_hooks(self):
        hook = _TrackingHook("test")
        ed = Editable(yaml_path="name", hooks={FormState.PRE_SAVE: hook})
        vis = EditableVisualizer(editable=ed, widget=WidgetType.TEXT, label="Name")

        yaml_data = {"name": "test"}
        await run_hooks(FormState.PRE_SAVE, [vis], yaml_data)

        assert hook.executed

    @pytest.mark.asyncio
    async def test_hooks_can_mutate_yaml_data(self):
        class _MutatingHook:
            order = 0

            async def execute(self, yaml_data: dict, context: dict) -> None:
                yaml_data["added"] = True

        ed = Editable(yaml_path="x", hooks={FormState.PRE_SAVE: _MutatingHook()})
        vis = EditableVisualizer(editable=ed, widget=WidgetType.TEXT, label="X")

        yaml_data: dict = {}
        await run_hooks(FormState.PRE_SAVE, [vis], yaml_data)

        assert yaml_data["added"] is True

    @pytest.mark.asyncio
    async def test_hooks_execute_in_order(self):
        execution_log: list[str] = []

        class _OrderedHook:
            def __init__(self, name: str, order: int) -> None:
                self.name = name
                self.order = order

            async def execute(self, yaml_data: dict, context: dict) -> None:
                execution_log.append(self.name)

        editables = [
            EditableVisualizer(
                editable=Editable(yaml_path="c", hooks={FormState.PRE_SAVE: _OrderedHook("strip", 999)}),
                widget=WidgetType.TEXT,
                label="C",
            ),
            EditableVisualizer(
                editable=Editable(yaml_path="a", hooks={FormState.PRE_SAVE: _OrderedHook("subdomain", 0)}),
                widget=WidgetType.TEXT,
                label="A",
            ),
        ]

        await run_hooks(FormState.PRE_SAVE, editables, {})
        assert execution_log == ["subdomain", "strip"]


# ---------------------------------------------------------------------------
# SubdomainRequestHook tests
# ---------------------------------------------------------------------------


class TestSubdomainRequestHook:
    @pytest.mark.asyncio
    async def test_creates_domain_entry_when_checkbox_checked(self, monkeypatch):
        from opi.forms.editables.hooks import SubdomainRequestHook

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())

        hook = SubdomainRequestHook()
        yaml_data = {
            "deployments": [
                {
                    "domain-format": "subdomain",
                    "base-domain": "rijks.app",
                    "subdomain": "mijn-app",
                    "_request-subdomain": True,
                }
            ],
        }

        await hook.execute(yaml_data, {})

        domains = yaml_data.get("domains")
        assert domains is not None
        allowed = domains["allowed-subdomains"]
        assert len(allowed) == 1
        assert allowed[0]["domain"] == "rijks.app"
        assert len(allowed[0]["subdomains"]) == 1
        assert allowed[0]["subdomains"][0]["name"] == "mijn-app"
        assert allowed[0]["subdomains"][0]["status"] == "requested"
        assert len(allowed[0]["subdomains"][0]["history"]) == 1

    @pytest.mark.asyncio
    async def test_skips_when_checkbox_not_checked(self, monkeypatch):
        from opi.forms.editables.hooks import SubdomainRequestHook

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())

        hook = SubdomainRequestHook()
        yaml_data = {
            "deployments": [
                {
                    "domain-format": "subdomain",
                    "base-domain": "rijks.app",
                    "subdomain": "mijn-app",
                }
            ],
        }

        await hook.execute(yaml_data, {})
        assert "domains" not in yaml_data

    @pytest.mark.asyncio
    async def test_skips_unrestricted_domain(self, monkeypatch):
        from opi.forms.editables.hooks import SubdomainRequestHook

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "local"})())

        hook = SubdomainRequestHook()
        yaml_data = {
            "deployments": [
                {
                    "domain-format": "subdomain",
                    "base-domain": "unknown.com",
                    "subdomain": "test",
                    "_request-subdomain": True,
                }
            ],
        }

        await hook.execute(yaml_data, {})
        # Domain request IS created (unknown.com needs approval),
        # but no subdomain request (subdomains not restricted on unknown domains)
        assert "domains" in yaml_data
        assert "allowed-domains" in yaml_data["domains"]
        assert "allowed-subdomains" not in yaml_data.get("domains", {})

    @pytest.mark.asyncio
    async def test_skips_already_registered_subdomain(self, monkeypatch):
        from opi.forms.editables.hooks import SubdomainRequestHook

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())

        hook = SubdomainRequestHook()
        yaml_data = {
            "deployments": [
                {
                    "domain-format": "subdomain",
                    "base-domain": "rijks.app",
                    "subdomain": "existing",
                    "_request-subdomain": True,
                }
            ],
            "domains": {
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [{"name": "existing", "status": "approved"}],
                    }
                ]
            },
        }

        await hook.execute(yaml_data, {})
        # Should not add a duplicate
        assert len(yaml_data["domains"]["allowed-subdomains"][0]["subdomains"]) == 1


# ---------------------------------------------------------------------------
# StripTransientsHook tests
# ---------------------------------------------------------------------------


class TestStripTransientsHook:
    @pytest.mark.asyncio
    async def test_removes_transient_fields(self):
        from opi.forms.editables.hooks import StripTransientsHook

        transient_ed = Editable(yaml_path="deployments[0]/_request-subdomain", transient=True)
        transient_vis = EditableVisualizer(editable=transient_ed, widget=WidgetType.CHECKBOX, label="Request")

        normal_ed = Editable(yaml_path="deployments[0]/subdomain")
        normal_vis = EditableVisualizer(editable=normal_ed, widget=WidgetType.TEXT, label="Subdomain")

        hook = StripTransientsHook([transient_vis, normal_vis])
        yaml_data = {
            "deployments": [
                {
                    "subdomain": "test",
                    "_request-subdomain": True,
                }
            ]
        }

        await hook.execute(yaml_data, {})

        assert "_request-subdomain" not in yaml_data["deployments"][0]
        assert yaml_data["deployments"][0]["subdomain"] == "test"


# ---------------------------------------------------------------------------
# Integration: SubdomainRequestHook runs before StripTransientsHook
# ---------------------------------------------------------------------------


class TestHookOrdering:
    @pytest.mark.asyncio
    async def test_subdomain_hook_runs_before_strip(self, monkeypatch):
        """SubdomainRequestHook (order=0) reads transients before StripTransientsHook (order=999) removes them."""
        from opi.forms.editables.hooks import StripTransientsHook, SubdomainRequestHook

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())

        request_ed = Editable(
            yaml_path="deployments[0]/_request-subdomain",
            transient=True,
            hooks={FormState.PRE_SAVE: SubdomainRequestHook()},
        )
        request_vis = EditableVisualizer(editable=request_ed, widget=WidgetType.CHECKBOX, label="Request")

        strip_ed = Editable(
            yaml_path="_system/strip",
            hooks={FormState.PRE_SAVE: StripTransientsHook([request_vis])},
        )
        strip_vis = EditableVisualizer(editable=strip_ed, widget=WidgetType.HIDDEN, label="")

        yaml_data = {
            "deployments": [
                {
                    "domain-format": "subdomain",
                    "base-domain": "rijks.app",
                    "subdomain": "mijn-app",
                    "_request-subdomain": True,
                }
            ],
        }

        await run_hooks(FormState.PRE_SAVE, [request_vis, strip_vis], yaml_data)

        # SubdomainRequestHook created the domains entry (ran first, transient was available)
        assert "domains" in yaml_data
        assert yaml_data["domains"]["allowed-subdomains"][0]["subdomains"][0]["name"] == "mijn-app"

        # StripTransientsHook removed the transient field (ran last)
        assert "_request-subdomain" not in yaml_data["deployments"][0]
