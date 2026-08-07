"""Where an attachment is used, and how a reference to it is taken away again (RC-52).

The foundation under deleting an attachment. Before anything may be removed, one question
has to have exactly one answer: which places reference this attachment. It is asked from
three sides -- the wizard's remove button, the delete-confirmation modal, and the API
delete -- and each used to want a different shape of answer, so the walk produces records
and the human-readable labels are derived from those records rather than gathered
separately.

Two properties are measured:

* the walk finds a reference *everywhere* one can sit. A place it misses is a place a
  delete would silently break, so each site shape gets its own case rather than one
  project with all of them at once;
* removing the references leaves nothing behind: no dangling coupling, no empty block, and
  a project file that still validates. That last one is the real check -- the reference
  integrity rule is what would catch a cleanup that missed a spot.
"""

from __future__ import annotations

from opi.handlers.project_file_handler import (
    USAGE_CERTIFICATE,
    USAGE_COUPLING,
    AttachmentUsageSite,
    attachment_is_referenced,
    attachment_usage_sites,
    extract_attachment_usage,
    remove_attachment_references,
    validate_attachment_references,
)

CERT = "server-cert"


def _site(component: str, deployment: str | None, kind: str) -> AttachmentUsageSite:
    return AttachmentUsageSite(component, deployment, kind)


def _catalog(*ids: str) -> list:
    return [
        {
            "attachments": {
                "data": [
                    {"id": i, "filename": f"{i}.pem", "content": "-----BEGIN AGE ENCRYPTED FILE-----\nx\n"} for i in ids
                ]
            }
        }
    ]


def _coupling(reference: str = CERT) -> dict:
    return {"reference": reference, "provide-as": "file", "path": "/etc/ssl/server.pem"}


# ---------------------------------------------------------------------------
# Every place a reference can sit
# ---------------------------------------------------------------------------


class TestTheWalkFindsEverySite:
    def test_a_component_coupling_in_the_record_form(self) -> None:
        project = {
            "services": _catalog(CERT),
            "components": [{"name": "backend", "services": [{"reference": "attachments", "config": [_coupling()]}]}],
        }
        assert attachment_usage_sites(project)[CERT] == [_site("backend", None, USAGE_COUPLING)]

    def test_a_component_coupling_in_the_legacy_name_as_key_form(self) -> None:
        project = {
            "services": _catalog(CERT),
            "components": [{"name": "backend", "services": [{"attachments": {"config": [_coupling()]}}]}],
        }
        assert attachment_usage_sites(project)[CERT] == [_site("backend", None, USAGE_COUPLING)]

    def test_the_pre_rename_use_key_is_normalised_before_anything_reads_it(self) -> None:
        """'use' is what the coupling key was called before the rename.

        The schema migration renames it to 'config' on load, so it never reaches this walk
        -- which is why the walk does not look for it, and why a project carrying 'use'
        must be migrated rather than read as-is. Pinned because the writer below IS
        tolerant of the old key, and that tolerance would look unmotivated otherwise.
        """
        from opi.services.schema_migration import migrate_to_latest

        project = {
            "schema-version": 2,
            "name": "demo",
            "services": _catalog(CERT),
            "components": [{"name": "backend", "services": [{"attachments": {"use": [_coupling()]}}]}],
        }
        # As it arrives: invisible to the walk, because the key is the old one.
        assert attachment_usage_sites(project) == {}

        migrate_to_latest(project)

        assert attachment_usage_sites(project)[CERT] == [_site("backend", None, USAGE_COUPLING)]

    def test_a_deployment_component_override_names_its_deployment(self) -> None:
        project = {
            "services": _catalog(CERT),
            "components": [{"name": "backend"}],
            "deployments": [
                {
                    "name": "staging",
                    "components": [{"reference": "backend", "services": {"attachments": {"config": [_coupling()]}}}],
                }
            ],
        }
        site = attachment_usage_sites(project)[CERT][0]
        assert site == _site("backend", "staging", USAGE_COUPLING)
        assert site.label == "backend (staging)"

    def test_a_component_certificate_is_a_use_of_a_different_kind(self) -> None:
        project = {
            "services": _catalog(CERT),
            "components": [
                {
                    "name": "backend",
                    "services": [{"publish-on-web": {"config": {"tls": "provided", "attachment": CERT}}}],
                }
            ],
        }
        assert attachment_usage_sites(project)[CERT] == [_site("backend", None, USAGE_CERTIFICATE)]

    def test_a_deployment_component_certificate(self) -> None:
        project = {
            "services": _catalog(CERT),
            "components": [{"name": "backend"}],
            "deployments": [
                {
                    "name": "staging",
                    "components": [
                        {
                            "reference": "backend",
                            "services": {"publish-on-web": {"config": {"tls": "provided", "attachment": CERT}}},
                        }
                    ],
                }
            ],
        }
        assert attachment_usage_sites(project)[CERT] == [_site("backend", "staging", USAGE_CERTIFICATE)]

    def test_the_project_wide_certificate_belongs_to_no_component(self) -> None:
        project = {
            "services": [
                *_catalog(CERT),
                {"publish-on-web": {"config": {"tls": "provided", "attachment": CERT}}},
            ],
        }
        site = attachment_usage_sites(project)[CERT][0]
        assert site == _site("", None, USAGE_CERTIFICATE)
        assert site.label == "publicatie (project-breed)"

    def test_an_attachment_nothing_points_at_has_no_sites(self) -> None:
        project = {"services": _catalog(CERT), "components": [{"name": "backend", "services": ["publish-on-web"]}]}
        assert attachment_usage_sites(project) == {}
        assert not attachment_is_referenced(project, CERT)

    def test_a_certificate_not_in_provided_mode_is_not_a_use(self) -> None:
        # tls 'standard' ignores the attachment key entirely, so it holds nothing hostage.
        project = {
            "services": _catalog(CERT),
            "components": [
                {
                    "name": "backend",
                    "services": [{"publish-on-web": {"config": {"tls": "standard", "attachment": CERT}}}],
                }
            ],
        }
        assert attachment_usage_sites(project) == {}


class TestTheLabelsStayWhatThePortalShows:
    def test_labels_are_the_projection_of_the_sites(self) -> None:
        project = {
            "services": _catalog(CERT),
            "components": [
                {"name": "backend", "services": [{"attachments": {"config": [_coupling()]}}]},
                {"name": "frontend", "services": [{"attachments": {"config": [_coupling()]}}]},
            ],
        }
        assert extract_attachment_usage(project) == {CERT: ["backend", "frontend"]}

    def test_one_component_using_it_twice_is_named_once(self) -> None:
        """A component that couples the attachment AND serves it as its certificate is two
        sites but one place; the reader is told once."""
        project = {
            "services": _catalog(CERT),
            "components": [
                {
                    "name": "backend",
                    "services": [
                        {"attachments": {"config": [_coupling()]}},
                        {"publish-on-web": {"config": {"tls": "provided", "attachment": CERT}}},
                    ],
                }
            ],
        }
        assert extract_attachment_usage(project) == {CERT: ["backend"]}
        assert len(attachment_usage_sites(project)[CERT]) == 2


# ---------------------------------------------------------------------------
# Taking the references away
# ---------------------------------------------------------------------------


class TestRemovingTheReferences:
    def test_a_components_only_coupling_takes_the_whole_block_with_it(self) -> None:
        # What clearing a service config does everywhere else: the block goes, the
        # selection stays. An empty coupling list left behind is not a valid config.
        project = {
            "services": _catalog(CERT),
            "components": [
                {"name": "backend", "services": ["publish-on-web", {"attachments": {"config": [_coupling()]}}]}
            ],
        }
        removed = remove_attachment_references(project, CERT)

        assert removed == [_site("backend", None, USAGE_COUPLING)]
        assert project["components"][0]["services"] == ["publish-on-web", "attachments"]

    def test_a_component_with_another_coupling_keeps_its_block(self) -> None:
        project = {
            "services": _catalog(CERT, "ca"),
            "components": [
                {"name": "backend", "services": [{"attachments": {"config": [_coupling(), _coupling("ca")]}}]}
            ],
        }
        remove_attachment_references(project, CERT)

        assert project["components"][0]["services"][0]["attachments"]["config"] == [_coupling("ca")]

    def test_the_pre_rename_use_key_is_written_back_in_its_own_shape(self) -> None:
        project = {
            "services": _catalog(CERT, "ca"),
            "components": [{"name": "backend", "services": [{"attachments": {"use": [_coupling(), _coupling("ca")]}}]}],
        }
        remove_attachment_references(project, CERT)

        assert project["components"][0]["services"][0]["attachments"]["use"] == [_coupling("ca")]

    def test_the_record_form_is_written_back_in_its_own_shape(self) -> None:
        project = {
            "services": _catalog(CERT, "ca"),
            "components": [
                {
                    "name": "backend",
                    "services": [{"reference": "attachments", "config": [_coupling(), _coupling("ca")]}],
                }
            ],
        }
        remove_attachment_references(project, CERT)

        assert project["components"][0]["services"][0]["config"] == [_coupling("ca")]

    def test_a_deployment_component_loses_its_attachments_key(self) -> None:
        project = {
            "services": _catalog(CERT),
            "components": [{"name": "backend"}],
            "deployments": [
                {
                    "name": "staging",
                    "components": [{"reference": "backend", "services": {"attachments": {"config": [_coupling()]}}}],
                }
            ],
        }
        removed = remove_attachment_references(project, CERT)

        assert removed == [_site("backend", "staging", USAGE_COUPLING)]
        # The services map held nothing else, so it goes too rather than sitting empty.
        assert "services" not in project["deployments"][0]["components"][0]

    def test_a_deployment_component_keeps_its_other_services(self) -> None:
        project = {
            "services": _catalog(CERT),
            "components": [{"name": "backend"}],
            "deployments": [
                {
                    "name": "staging",
                    "components": [
                        {
                            "reference": "backend",
                            "services": {"attachments": {"config": [_coupling()]}, "persistent-storage": [{"g": 1}]},
                        }
                    ],
                }
            ],
        }
        remove_attachment_references(project, CERT)

        assert project["deployments"][0]["components"][0]["services"] == {"persistent-storage": [{"g": 1}]}

    def test_a_certificate_is_never_touched(self) -> None:
        """Removing it would mean deciding how the site is served instead. That refusal
        lives in ProjectManager.remove_attachment; here it must simply not be cleaned up."""
        project = {
            "services": _catalog(CERT),
            "components": [
                {
                    "name": "backend",
                    "services": [{"publish-on-web": {"config": {"tls": "provided", "attachment": CERT}}}],
                }
            ],
        }
        assert remove_attachment_references(project, CERT) == []
        assert project["components"][0]["services"][0]["publish-on-web"]["config"]["attachment"] == CERT

    def test_an_attachment_nobody_uses_changes_nothing(self) -> None:
        project = {
            "services": _catalog(CERT),
            "components": [{"name": "backend", "services": [{"attachments": {"config": [_coupling("ca")]}}]}],
        }
        assert remove_attachment_references(project, CERT) == []
        assert project["components"][0]["services"][0]["attachments"]["config"] == [_coupling("ca")]


class TestNothingIsLeftPointingAtNothing:
    """The check the plan asks for: after the cleanup the file still validates.

    ``validate_attachment_references`` is the rule that catches a reference to an id that
    is no longer in the catalog, and it runs on every save. If a cleanup missed a site,
    this is where it shows up -- which is why it is asserted against the *emptied* catalog
    rather than against the project as it was.
    """

    def _project_using_it_everywhere(self) -> dict:
        return {
            "services": _catalog(CERT),
            "components": [
                {"name": "backend", "services": [{"attachments": {"config": [_coupling()]}}]},
                {"name": "frontend", "services": [{"reference": "attachments", "config": [_coupling()]}]},
            ],
            "deployments": [
                {
                    "name": "staging",
                    "components": [{"reference": "backend", "services": {"attachments": {"config": [_coupling()]}}}],
                }
            ],
        }

    def test_every_coupling_site_is_found_and_then_removed(self) -> None:
        project = self._project_using_it_everywhere()

        assert len(attachment_usage_sites(project)[CERT]) == 3
        removed = remove_attachment_references(project, CERT)

        assert len(removed) == 3
        assert attachment_usage_sites(project) == {}

    def test_the_emptied_catalog_leaves_no_dangling_reference(self) -> None:
        project = self._project_using_it_everywhere()
        remove_attachment_references(project, CERT)
        # Now empty the catalog, as the delete does in the same change.
        project["services"] = [{"attachments": {"data": []}}]

        assert validate_attachment_references(project) == []

    def test_the_check_would_have_caught_a_missed_site(self) -> None:
        """The guard has teeth: leave one coupling in place and the same check fails."""
        project = self._project_using_it_everywhere()
        remove_attachment_references(project, CERT)
        project["components"][0]["services"] = [{"attachments": {"config": [_coupling()]}}]
        project["services"] = [{"attachments": {"data": []}}]

        errors = validate_attachment_references(project)
        assert len(errors) == 1
        assert CERT in errors[0]
