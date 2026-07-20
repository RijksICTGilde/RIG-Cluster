"""A field that BOTH writers created must not resolve silently to one of them.

The structural merge re-applies our change (base -> ours) onto whatever landed
(theirs) using a bidirectional deepdiff Delta. That delta records the value each
edit started from and refuses to apply when it changed underneath -- but only for
fields that already existed. A key that was absent in ``base`` is represented as
``dictionary_item_added``, which deepdiff applies unconditionally: it has no
previous value to verify against.

So two writers each creating the same previously-absent field -- a config block, a
component alias, a component's first ``resources`` stanza -- both validated, both
structurally fine, resolved silently to whoever pushed second. That is the lost
update the compare-and-swap exists to prevent, arriving through the one door the
delta does not watch.

The pairing that matters is asymmetric and both halves are pinned here: a
*differing* value is a conflict, an *identical* value is agreement and must still
merge. Treating agreement as a collision would make ordinary concurrent edits fail
for no reason.
"""

from __future__ import annotations

from typing import Any

from opi.services.project_store import _apply_our_change_to


def _merge(base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any]) -> dict[str, Any] | None:
    return _apply_our_change_to(base=base, ours=ours, theirs=theirs)


class TestBothSidesAddedTheSameKey:
    def test_differing_values_conflict_instead_of_overwriting(self) -> None:
        """Ours must not win silently; the caller has to re-read and re-apply."""
        merged = _merge(
            base={"config": {}},
            ours={"config": {"x": "mine"}},
            theirs={"config": {"x": "theirs"}},
        )

        assert merged is None

    def test_identical_values_are_agreement_not_conflict(self) -> None:
        """Both writers reaching the same answer is not a collision."""
        merged = _merge(
            base={"config": {}},
            ours={"config": {"x": "same"}},
            theirs={"config": {"x": "same"}},
        )

        assert merged == {"config": {"x": "same"}}

    def test_first_resources_stanza_from_two_tuners_conflicts(self) -> None:
        """The realistic shape: auto-tune and oom_watcher both sizing a component.

        Nothing structural rejects the merged result -- one memory limit is as valid
        as the other -- so without this check the losing value disappears with no
        error anywhere.
        """
        merged = _merge(
            base={"components": [{"name": "c1"}]},
            ours={"components": [{"name": "c1", "resources": {"limits": {"memory": "100Mi"}}}]},
            theirs={"components": [{"name": "c1", "resources": {"limits": {"memory": "900Mi"}}}]},
        )

        assert merged is None

    def test_same_alias_key_with_different_targets_conflicts(self) -> None:
        merged = _merge(
            base={"components": [{"name": "c1", "aliases": {}}]},
            ours={"components": [{"name": "c1", "aliases": {"DB_HOST": "$MINE"}}]},
            theirs={"components": [{"name": "c1", "aliases": {"DB_HOST": "$THEIRS"}}]},
        )

        assert merged is None


class TestUnrelatedEditsStillMerge:
    """The check must not turn ordinary concurrent edits into conflicts."""

    def test_only_we_added_the_key(self) -> None:
        merged = _merge(
            base={"config": {}},
            ours={"config": {"x": "mine"}},
            theirs={"config": {}},
        )

        assert merged == {"config": {"x": "mine"}}

    def test_different_alias_keys_both_survive(self) -> None:
        merged = _merge(
            base={"components": [{"name": "c1", "aliases": {}}]},
            ours={"components": [{"name": "c1", "aliases": {"A": "1"}}]},
            theirs={"components": [{"name": "c1", "aliases": {"B": "2"}}]},
        )

        assert merged is not None
        assert merged["components"][0]["aliases"] == {"A": "1", "B": "2"}

    def test_two_appended_components_both_survive(self) -> None:
        """The motivating case for the structural merge; must not regress."""
        merged = _merge(
            base={"components": [{"name": "a"}]},
            ours={"components": [{"name": "a"}, {"name": "mine"}]},
            theirs={"components": [{"name": "a"}, {"name": "theirs"}]},
        )

        assert merged is not None
        assert [c["name"] for c in merged["components"]] == ["a", "mine", "theirs"]

    def test_editing_an_existing_field_is_still_guarded(self) -> None:
        """Regression guard: the pre-existing values_changed protection stays."""
        merged = _merge(
            base={"description": "old"},
            ours={"description": "mine"},
            theirs={"description": "theirs"},
        )

        assert merged is None
