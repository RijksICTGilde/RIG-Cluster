"""Two event lists, one way to hook in (RC-39).

Before this, a service hooked in by overriding a method the base class had grown for it,
and generic code found it by scanning the registry for exactly that override. Every new
extension point cost a method on the shared base class plus a scan somewhere else -- and
six of those methods had a single inhabitant, which is bespoke code on a shared class
rather than a contract.

These tests hold the mechanism that replaced it, and above all the two things that are
easy to lose again:

* the two FAMILIES stay apart -- an action changes state and never commits, a UI event
  returns something to show and mutates nothing;
* participation stays DERIVED from the declaration, so a listener list can never name a
  service that does not implement the event, nor miss one that does.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from opi.services import registry
from opi.services.catalog.base import (
    DeploymentPageContext,
    DeploymentStateContext,
    DetailPageSection,
    ProjectPageContext,
    RedeployContext,
    Service,
)
from opi.services.catalog.events import EVENT_MARKER, collect_event_handlers, on
from opi.services.registry import SERVICES, listeners
from opi.services.services_enums import ActionEvent, HookLevel, ServiceType, UIEvent

ALL_EVENTS = [*ActionEvent, *UIEvent]


def _handlers(service: Service) -> list[tuple[Any, Any]]:
    """(event, function) for every handler a service declares."""
    return [
        (event, getattr(type(service), name))
        for event, entries in service.event_handlers.items()
        for name, _order in entries
    ]


class TestTheTwoFamiliesStayApart:
    """The reason there are two enums and not one."""

    def test_an_action_handler_is_async_and_a_ui_handler_is_not(self) -> None:
        """The split is enforced, not merely described. Acting on the world is the side
        that may await; a page render that could await would fan out into connector calls
        behind a template."""
        checked = 0
        for service in SERVICES.values():
            for event, func in _handlers(service):
                checked += 1
                is_async = inspect.iscoroutinefunction(func)
                if isinstance(event, ActionEvent):
                    assert is_async, f"{func.__qualname__} handles {event} and must be async"
                else:
                    assert not is_async, f"{func.__qualname__} handles {event} and must be synchronous"
        assert checked >= len(ALL_EVENTS), "the catalog's handlers were not actually inspected"

    def test_no_action_handler_commits(self) -> None:
        """The one contract of the ``ActionEvent`` family: a handler mutates
        ``project_data`` and the CALLER commits, once, for every outcome together. Two
        services that each commit give two commits and a lost update -- the bug this
        platform has already had (RC-17).

        Measured on the source of every action handler in the catalog, because the
        contract is exactly the kind of thing that survives in a docstring while quietly
        dying in the code."""
        checked = 0
        for service in SERVICES.values():
            for event, func in _handlers(service):
                if not isinstance(event, ActionEvent):
                    continue
                checked += 1
                source = inspect.getsource(func)
                assert "save_and_commit_project" not in source, (
                    f"{func.__qualname__} handles {event} and commits itself; the caller commits once"
                )
        assert checked >= 3, "the catalog's action handlers were not actually inspected"

    def test_every_event_belongs_to_exactly_one_family(self) -> None:
        assert not set(ActionEvent) & set(UIEvent)
        assert len(ALL_EVENTS) == len({event.value for event in ALL_EVENTS})

    def test_every_event_names_what_it_iterates(self) -> None:
        """A level per event, so it is written down what generic code walks when it
        fires."""
        for event in ALL_EVENTS:
            assert isinstance(event.level, HookLevel)


class TestParticipationIsDerived:
    def test_a_listener_list_only_holds_services_that_declare_a_handler(self) -> None:
        for event in ALL_EVENTS:
            for service in listeners(event):
                assert service.listens_to(event)
                assert service.event_handlers.get(event)

    def test_every_declared_handler_puts_its_service_in_the_index(self) -> None:
        """The other direction: a service cannot implement an event and be left out of the
        scan, which is how a hook silently stops firing."""
        for service in SERVICES.values():
            for event in service.event_handlers:
                assert service in listeners(event), f"{service.service_type} declares {event} but is not indexed"

    def test_the_inhabitants_are_the_ones_the_plan_names(self) -> None:
        """A regression net over the migration itself: these five events have the
        inhabitants the hooks they replaced had."""
        assert {s.service_type for s in listeners(ActionEvent.AFTER_SYNC)} == {ServiceType.RESOURCE_TUNING}
        assert {s.service_type for s in listeners(ActionEvent.REDEPLOY)} == {
            ServiceType.SLEEP_MODE,
            ServiceType.DEPLOYMENT_HEALTH,
        }
        assert {s.service_type for s in listeners(UIEvent.DEPLOYMENT_STATE)} == {
            ServiceType.SLEEP_MODE,
            ServiceType.DEPLOYMENT_HEALTH,
        }
        assert {s.service_type for s in listeners(UIEvent.PROJECT_SECTIONS)} == {
            ServiceType.KEYCLOAK,
            ServiceType.ATTACHMENTS,
            ServiceType.INVITE,
        }
        # Alleen het metrics-blok. De backupable diensten stonden hier ook, tot RC-100
        # het backupblok een eigen tabblad gaf: de mixin declareert de haak sindsdien niet
        # meer en het blok wordt bij naam opgevraagd (collect_backups_sections). Zie de
        # module-docstring van opi/services/catalog/shared/backups.py voor de afweging.
        assert {s.service_type for s in listeners(UIEvent.DEPLOYMENT_SECTIONS)} == {
            ServiceType.METRICS_SCRAPER,
        }

    def test_a_project_narrows_the_listeners_to_the_services_it_uses(self) -> None:
        """What a page wants: a block only belongs on a project that has the service."""
        project = {"name": "p", "services": ["keycloak"], "components": []}

        narrowed = {s.service_type for s in listeners(UIEvent.PROJECT_SECTIONS, project)}

        assert narrowed == {ServiceType.KEYCLOAK}

    def test_asking_everyone_is_the_default(self) -> None:
        """State events ask every listener: a service's record in the project file has to
        be dealt with even when the project no longer lists the service today."""
        project = {"name": "p", "services": [], "components": []}

        assert listeners(UIEvent.DEPLOYMENT_STATE) != listeners(UIEvent.DEPLOYMENT_STATE, project)
        assert listeners(UIEvent.DEPLOYMENT_STATE, project) == []


class TestDispatch:
    def test_a_service_that_does_not_listen_answers_with_nothing(self) -> None:
        """A caller never has to ask first whether a service cares."""
        keycloak = SERVICES[ServiceType.KEYCLOAK]

        assert keycloak.listens_to(UIEvent.DEPLOYMENT_STATE) is False
        assert keycloak.handle_ui(UIEvent.DEPLOYMENT_STATE, DeploymentStateContext("p", {}, {})) == []

    def test_every_handler_of_one_event_contributes(self) -> None:
        """Two handlers for one event on one service are concatenated -- which is what
        replaced the cooperative ``super()`` in the page mixins, where a mixin that forgot
        to chain silently swallowed the other's block."""

        class _TwoBlocks(Service):
            @on(UIEvent.PROJECT_SECTIONS, order=20)
            def second(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
                return [DetailPageSection(template="b")]

            @on(UIEvent.PROJECT_SECTIONS, order=10)
            def first(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
                return [DetailPageSection(template="a")]

        sections = _TwoBlocks().handle_ui(UIEvent.PROJECT_SECTIONS, ProjectPageContext({}, "admin"))

        assert [section.template for section in sections] == ["a", "b"]

    def test_a_handler_from_a_mixin_counts_as_the_services_own(self) -> None:
        """The backups block reaches its owners through a mixin; the index walks the MRO,
        so the owner is a listener without repeating the declaration."""

        class _Mixin:
            @on(UIEvent.PROJECT_SECTIONS)
            def mixed_in(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
                return [DetailPageSection(template="from-mixin")]

        class _Owner(_Mixin, Service):
            pass

        sections = _Owner().handle_ui(UIEvent.PROJECT_SECTIONS, ProjectPageContext({}, "admin"))

        assert [section.template for section in sections] == ["from-mixin"]

    @pytest.mark.asyncio
    async def test_an_action_dispatch_awaits_every_handler(self) -> None:
        class _Recorder(Service):
            @on(ActionEvent.REDEPLOY)
            async def clear(self, ctx: RedeployContext) -> list[str]:
                ctx.project_data.setdefault("cleared", []).append(ctx.deployment_name)
                return ["opgeruimd"]

        project: dict[str, Any] = {}
        notices = await _Recorder().handle_action(ActionEvent.REDEPLOY, RedeployContext("p", project, {"name": "prod"}))

        assert notices == ["opgeruimd"]
        assert project["cleared"] == ["prod"]


class TestTheDeclaration:
    def test_a_method_handles_one_event(self) -> None:
        """Two events on one method would make the payload it takes ambiguous, which is
        the whole thing the payload-per-event pairing buys."""
        with pytest.raises(TypeError, match="already handles"):

            class _Confused(Service):
                @on(UIEvent.PROJECT_SECTIONS)
                @on(ActionEvent.REDEPLOY)
                def both(self, ctx: Any) -> list[Any]:
                    return []

    def test_order_decides_the_position_within_an_event(self) -> None:
        class _Ordered(Service):
            @on(UIEvent.PROJECT_SECTIONS, order=50)
            def late(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
                return []

            @on(UIEvent.PROJECT_SECTIONS, order=5)
            def early(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
                return []

        assert _Ordered.event_handlers[UIEvent.PROJECT_SECTIONS] == [("early", 5), ("late", 50)]

    def test_the_default_order_is_the_documented_one_hundred(self) -> None:
        def handler(self, ctx: Any) -> list[Any]:
            return []

        assert getattr(on(UIEvent.PROJECT_SECTIONS)(handler), EVENT_MARKER) == (UIEvent.PROJECT_SECTIONS, 100)

    def test_an_override_replaces_the_handler_it_overrides(self) -> None:
        """Overriding by name means what it means everywhere else: one handler, the
        derived one -- not two contributions."""

        class _Base(Service):
            @on(UIEvent.PROJECT_SECTIONS)
            def block(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
                return [DetailPageSection(template="base")]

        class _Derived(_Base):
            def block(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
                return [DetailPageSection(template="derived")]

        sections = _Derived().handle_ui(UIEvent.PROJECT_SECTIONS, ProjectPageContext({}, "admin"))

        assert [section.template for section in sections] == ["derived"]

    def test_an_overriding_service_still_gets_indexed_as_a_listener(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The same override, but through the index instead of the dispatch. An override
        does not repeat the decorator, so its method carries no marker; the order has to
        come from the index. Reading it back off the bound method raised AttributeError at
        import of ``registry``, which means the first catalog service to override a handler
        would stop the app from starting."""

        class _Base(Service):
            @on(UIEvent.PROJECT_SECTIONS, order=20)
            def block(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
                return [DetailPageSection(template="base")]

        class _Overriding(_Base):
            def block(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
                return [DetailPageSection(template="derived")]

        overriding = _Overriding()
        monkeypatch.setitem(registry.SERVICES, ServiceType.INVITE, overriding)
        monkeypatch.setattr(registry, "_LISTENERS", registry._build_listener_index())

        assert overriding in listeners(UIEvent.PROJECT_SECTIONS)

    def test_a_class_without_handlers_has_an_empty_index(self) -> None:
        class _Quiet(Service):
            pass

        assert collect_event_handlers(_Quiet) == {}
        assert all(not _Quiet().listens_to(event) for event in ALL_EVENTS)


class TestThePayloadIsOneObjectPerEvent:
    """The typed part of the contract. Pyright checks the pairing through the overloads on
    ``handle_ui`` / ``handle_action``; at runtime what can be held is that every event has
    a payload type and that the dispatch really passes it through."""

    def test_the_payload_reaches_the_handler_unchanged(self) -> None:
        seen: list[Any] = []

        class _Nosey(Service):
            @on(UIEvent.DEPLOYMENT_SECTIONS)
            def block(self, ctx: DeploymentPageContext) -> list[DetailPageSection]:
                seen.append(ctx)
                return []

        payload = DeploymentPageContext({}, {"name": "prod"}, "admin", "local")
        _Nosey().handle_ui(UIEvent.DEPLOYMENT_SECTIONS, payload)

        assert seen == [payload]

    def test_the_project_page_payload_carries_what_the_two_arguments_did(self) -> None:
        """``detail_page_sections(project_data, user_role)`` became one object; both inputs
        are still there, now as a type rather than as a call convention."""
        payload = ProjectPageContext(project_data={"name": "p"}, user_role="admin")

        assert payload.project_data["name"] == "p"
        assert payload.user_role == "admin"
