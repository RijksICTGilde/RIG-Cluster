"""Tests for opi.utils.yaml_util module."""

import os
import tempfile
from pathlib import Path

import pytest
from opi.core.config import settings
from opi.utils.yaml_util import (
    YAMLError,
    _create_yaml_writer,
    dump_yaml_documents_to_string,
    dump_yaml_double_quoted_scalar,
    dump_yaml_for_go,
    dump_yaml_for_go_unfolded,
    dump_yaml_to_string,
    find_value_by_jsonpath,
    load_yaml_documents_from_string,
    load_yaml_documents_safe,
    load_yaml_from_string,
    load_yaml_round_trip,
    load_yaml_safe,
    save_yaml_to_path,
    update_value_by_jsonpath,
)


class TestLoadYamlFromString:
    """Tests for load_yaml_from_string."""

    def test_valid_yaml(self):
        yaml_string = "name: test\nvalue: 42\n"
        result = load_yaml_from_string(yaml_string)
        assert result is not None
        assert result["name"] == "test"
        assert result["value"] == 42

    def test_nested_yaml(self):
        yaml_string = "metadata:\n  name: my-app\n  namespace: default\n"
        result = load_yaml_from_string(yaml_string)
        assert result is not None
        assert result["metadata"]["name"] == "my-app"
        assert result["metadata"]["namespace"] == "default"

    def test_invalid_yaml_returns_none(self):
        yaml_string = ":\n  - :\n  invalid: [unbalanced"
        result = load_yaml_from_string(yaml_string)
        assert result is None

    def test_empty_string_returns_none(self):
        result = load_yaml_from_string("")
        assert result is None

    def test_yaml_with_list(self):
        yaml_string = "items:\n  - one\n  - two\n  - three\n"
        result = load_yaml_from_string(yaml_string)
        assert result is not None
        assert result["items"] == ["one", "two", "three"]


class TestDumpYamlToString:
    """Tests for dump_yaml_to_string."""

    def test_simple_dict(self):
        data = {"name": "test", "value": 42}
        result = dump_yaml_to_string(data)
        assert "name: test" in result
        assert "value: 42" in result

    def test_nested_dict(self):
        data = {"metadata": {"name": "my-app", "namespace": "default"}}
        result = dump_yaml_to_string(data)
        assert "metadata:" in result
        assert "name: my-app" in result
        assert "namespace: default" in result

    def test_empty_dict(self):
        result = dump_yaml_to_string({})
        assert isinstance(result, str)

    def test_roundtrip(self):
        original = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "test"}}
        yaml_str = dump_yaml_to_string(original)
        loaded = load_yaml_from_string(yaml_str)
        assert loaded["apiVersion"] == "v1"
        assert loaded["kind"] == "ConfigMap"
        assert loaded["metadata"]["name"] == "test"


class TestFindValueByJsonpath:
    """Tests for find_value_by_jsonpath."""

    def test_simple_key(self):
        data = {"name": "test", "value": 42}
        result = find_value_by_jsonpath(data, "$.name")
        assert result == "test"

    def test_nested_key(self):
        data = {"metadata": {"name": "my-app", "namespace": "default"}}
        result = find_value_by_jsonpath(data, "$.metadata.name")
        assert result == "my-app"

    def test_missing_key_returns_default(self):
        data = {"name": "test"}
        result = find_value_by_jsonpath(data, "$.missing")
        assert result is None

    def test_missing_key_returns_custom_default(self):
        data = {"name": "test"}
        result = find_value_by_jsonpath(data, "$.missing", default="fallback")
        assert result == "fallback"

    def test_empty_data_returns_default(self):
        result = find_value_by_jsonpath({}, "$.name")
        assert result is None

    def test_none_data_returns_default(self):
        result = find_value_by_jsonpath(None, "$.name")
        assert result is None

    def test_deeply_nested_key(self):
        data = {"a": {"b": {"c": "deep"}}}
        result = find_value_by_jsonpath(data, "$.a.b.c")
        assert result == "deep"


class TestUpdateValueByJsonpath:
    """Tests for update_value_by_jsonpath."""

    def test_update_existing_key(self):
        data = {"name": "old", "value": 1}
        result = update_value_by_jsonpath(data, "$.name", "new")
        assert result is True
        assert data["name"] == "new"

    def test_update_nested_key(self):
        data = {"metadata": {"name": "old-app"}}
        result = update_value_by_jsonpath(data, "$.metadata.name", "new-app")
        assert result is True
        assert data["metadata"]["name"] == "new-app"

    def test_missing_key_returns_false(self):
        data = {"name": "test"}
        result = update_value_by_jsonpath(data, "$.nonexistent", "value")
        assert result is False

    def test_empty_data_returns_false(self):
        result = update_value_by_jsonpath({}, "$.name", "value")
        assert result is False

    def test_none_data_returns_false(self):
        result = update_value_by_jsonpath(None, "$.name", "value")
        assert result is False

    def test_update_preserves_other_keys(self):
        data = {"name": "old", "keep": "this"}
        update_value_by_jsonpath(data, "$.name", "new")
        assert data["keep"] == "this"


class TestSaveYamlToPath:
    """Tests for save_yaml_to_path."""

    def test_bare_filename_without_directory(self):
        """save_yaml_to_path should succeed when file_path has no directory component."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.yaml")
            result = save_yaml_to_path(file_path, {"key": "value"})
            assert result is True
            assert os.path.exists(file_path)

    def test_bare_filename_in_cwd(self):
        """save_yaml_to_path with a bare filename (no dir) should not fail due to empty dirname."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                result = save_yaml_to_path("bare_file.yaml", {"key": "value"})
                assert result is True, "save_yaml_to_path should succeed with bare filename in CWD"
                assert os.path.exists(os.path.join(tmpdir, "bare_file.yaml"))
            finally:
                os.chdir(original_cwd)


AGE_BLOCK = (
    "-----BEGIN AGE ENCRYPTED FILE-----\n"
    "YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSBQd3RVbm85emtqV1ZJRnJ0\n"
    "ZklzVEpmbmFvWTRFbzhCQ1FOUEVBak1Rd1h3CkFKd0xxa0IyUFBwUmFpKzZRV0ho\n"
    "-----END AGE ENCRYPTED FILE-----"
)


class TestMultilineScalarStyle:
    """Multi-line values must be written as literal blocks by the canonical writer.

    Regression: a keycloak realm password went through the modal-edit wizard, whose
    session round-trips the project dict through JSON. That strips ruamel's
    LiteralScalarString, and the value landed in the project file as one quoted line
    full of ``\\n`` escapes.
    """

    def test_plain_multiline_string_becomes_literal_block(self):
        output = dump_yaml_to_string({"password": AGE_BLOCK})
        assert "password: |-" in output
        assert "\\n" not in output

    def test_survives_a_json_round_trip(self):
        import json

        from ruamel.yaml.scalarstring import LiteralScalarString

        data = {"password": LiteralScalarString(AGE_BLOCK)}
        output = dump_yaml_to_string(json.loads(json.dumps(data)))
        assert "password: |-" in output
        assert load_yaml_from_string(output)["password"] == AGE_BLOCK

    def test_committed_quoted_block_is_repaired_on_rewrite(self):
        escaped = AGE_BLOCK.replace("\n", "\\n")
        broken = load_yaml_from_string(f'password: "{escaped}"\n')
        output = dump_yaml_to_string(broken)
        assert "password: |-" in output
        assert load_yaml_from_string(output)["password"] == AGE_BLOCK

    def test_single_line_quoting_is_still_preserved(self):
        source = "status: 'requested'\nname: plain\n"
        assert dump_yaml_to_string(load_yaml_from_string(source)) == source

    def test_carriage_returns_stay_quoted_so_the_value_survives(self):
        # A block scalar writes the \r out raw and YAML normalizes line breaks on
        # read, which would silently turn "a\r\nb" into "a\nb".
        output = dump_yaml_to_string({"text": "a\r\nb"})
        assert load_yaml_from_string(output)["text"] == "a\r\nb"


class TestMultiDocumentRoundTrip:
    """A multi-document file must come back out as kubectl can read it."""

    def test_single_document_keeps_its_shape(self):
        source = "kind: Pod\nmetadata:\n  name: a\n"
        assert dump_yaml_documents_to_string(load_yaml_documents_from_string(source)) == source

    def test_two_documents_stay_two_documents(self):
        source = "kind: Pod\n---\nkind: Service\n"
        output = dump_yaml_documents_to_string(load_yaml_documents_from_string(source))
        assert [d["kind"] for d in load_yaml_documents_from_string(output)] == ["Pod", "Service"]

    def test_trailing_separator_does_not_become_a_null_document(self):
        # The loader turns the trailing ``---`` into a None document. Written back as
        # ``--- null`` kubectl rejects the whole file with "Object 'Kind' is missing".
        output = dump_yaml_documents_to_string(load_yaml_documents_from_string("kind: Pod\nspec: {}\n---\n"))
        assert output == "kind: Pod\nspec: {}\n"
        assert "null" not in output


_GO_FIXTURE = {
    "image": "ghcr.io/example/app:1.2.3",
    "replicas": 1,
    "truthy": "yes",
    "switch": "on",
    "falsy": "no",
    "mode": "0755",
    "version": "1.10",
    "stamp": "2026-11-09",
    "unicode": "café €",
    "long": "x" * 120,
    "spaced": "a" + " " * 30 + "b" + " " * 40 + "c",
    "sentence": " ".join(["token"] * 30),
    "env": {"KEY": "value", "A_LONG_SECRET": "s" * 100},
    "list": ["one", "two", {"nested": True}],
    "empty": None,
}

_GO_HEAD = (
    "image: ghcr.io/example/app:1.2.3\n"
    "replicas: 1\n"
    "truthy: yes\n"
    "switch: on\n"
    "falsy: no\n"
    "mode: '0755'\n"
    "version: '1.10'\n"
    "stamp: '2026-11-09'\n"
    "unicode: café €\n"
)
_GO_TAIL = "list:\n- one\n- two\n- nested: true\nempty:\n"


class TestGoProfileIsByteExact:
    """The bytes of a file a Go consumer reads must not move: a changed byte in a
    to-sops file re-encrypts across the whole fleet and changes what Helm sees."""

    def test_folding_writer_matches_the_emitter_default(self):
        assert dump_yaml_for_go(_GO_FIXTURE) == (
            _GO_HEAD + "long: \n  " + "x" * 120 + "\n"
            "spaced: a" + " " * 30 + "b" + " " * 40 + "\n  c\n"
            "sentence: token token token token token token token token token token token \n"
            "  token token token token token token token token token token token token token \n"
            "  token token token token token token\n"
            "env:\n  KEY: value\n  A_LONG_SECRET: \n    " + "s" * 100 + "\n" + _GO_TAIL
        )

    def test_unfolded_writer_keeps_every_scalar_on_one_line(self):
        assert dump_yaml_for_go_unfolded(_GO_FIXTURE) == (
            _GO_HEAD + "long: " + "x" * 120 + "\n"
            "spaced: a" + " " * 30 + "b" + " " * 40 + "c\n"
            "sentence: " + " ".join(["token"] * 30) + "\n"
            "env:\n  KEY: value\n  A_LONG_SECRET: " + "s" * 100 + "\n" + _GO_TAIL
        )

    def test_folding_loses_whitespace_that_the_unfolded_writer_keeps(self):
        # Known gap, kept on purpose: the helm-values and Chart.yaml writers fold, so a
        # value with runs of spaces comes back changed. Only the CMP-env writer is unfolded.
        folded = load_yaml_safe(dump_yaml_for_go(_GO_FIXTURE))
        unfolded = load_yaml_safe(dump_yaml_for_go_unfolded(_GO_FIXTURE))
        assert folded["spaced"] != _GO_FIXTURE["spaced"]
        assert unfolded["spaced"] == _GO_FIXTURE["spaced"]

    def test_a_string_a_go_parser_reads_as_a_bool_stays_unquoted(self):
        # Known gap, kept on purpose: ruamel emits YAML 1.2, Helm and kubectl parse 1.1.
        assert dump_yaml_for_go({"switch": "on"}) == "switch: on\n"


class TestGoProfileRoundTrip:
    """A file that is read, changed and written back keeps its comments and styles."""

    def test_a_literal_block_stays_a_literal_block(self):
        # decrypt-sops.yaml.jinja carries the ksops exec config as a | block; emitted as a
        # quoted one-liner kustomize no longer reads it.
        source = "annotations:\n  config.kubernetes.io/function: |\n    exec:\n      path: ksops\nfiles: []\n"
        data = load_yaml_round_trip(source)
        data["files"] = ["a.sops.yaml"]
        assert dump_yaml_for_go(data) == source.replace("files: []", "files:\n- a.sops.yaml")

    def test_a_comment_survives_a_rewrite(self):
        source = "# keep me\nname: chart\ndependencies:\n- name: common\n  repository: file://../common\n"
        data = load_yaml_round_trip(source)
        data["dependencies"][0]["repository"] = "file://../renamed"
        assert dump_yaml_for_go(data) == source.replace("file://../common", "file://../renamed")

    def test_the_real_kustomization_template_emits_the_committed_bytes(self):
        data = load_yaml_round_trip((Path(settings.MANIFESTS_PATH) / "kustomization.yaml.jinja").read_text())
        data["namespace"] = "rig-demo"
        data["resources"] = ["deployment.yaml"]
        assert dump_yaml_for_go(data) == (
            "apiVersion: kustomize.config.k8s.io/v1beta1\n"
            "generators:\n"
            "- decrypt-sops.yaml\n"
            "kind: Kustomization\n"
            "namespace: rig-demo\n"
            "resources:\n"
            "- deployment.yaml\n"
        )

    def test_the_real_decrypt_sops_template_emits_the_committed_bytes(self):
        data = load_yaml_round_trip((Path(settings.MANIFESTS_PATH) / "decrypt-sops.yaml.jinja").read_text())
        data["files"] = ["db.sops.yaml"]
        assert dump_yaml_for_go(data) == (
            "apiVersion: viaduct.ai/v1\n"
            "kind: ksops\n"
            "metadata:\n"
            "  name: secret-generator\n"
            "  annotations:\n"
            "    config.kubernetes.io/function: |\n"
            "      exec:\n"
            "        path: ksops\n"
            "files:\n"
            "- db.sops.yaml\n"
        )


class TestProjectFileWriterLeaksIntoEveryEmitter:
    """Known gap, pinned on purpose: ruamel's ``add_representer`` is a classmethod, so the
    project-file writer rewires every round-trip emitter in the process. Whether a
    generated manifest writes a multi-line value as a | block therefore depends on whether
    a project file was written earlier in the same process."""

    def test_building_the_project_file_writer_changes_the_go_writer(self):
        _create_yaml_writer()
        assert dump_yaml_for_go({"text": "a\nb\n"}) == "text: |\n  a\n  b\n"


class TestLoadYamlSafe:
    """The read-only loader: plain types, and a parse error the caller can act on."""

    def test_it_returns_plain_types(self):
        data = load_yaml_safe("a:\n  b: [1, 2]\n")
        assert data == {"a": {"b": [1, 2]}}
        assert type(data) is dict

    def test_it_resolves_scalars_as_yaml_1_2(self):
        # PyYAML reads 1.1, where these are a bool and an octal int.
        assert load_yaml_safe("a: yes\nb: 0755\nc: on\n") == {"a": "yes", "b": 755, "c": "on"}

    def test_it_raises_on_broken_yaml(self):
        with pytest.raises(YAMLError):
            load_yaml_safe("a: [1\nb: 2\n")

    def test_empty_text_is_none(self):
        assert load_yaml_safe("") is None

    def test_documents_are_returned_in_order(self):
        assert load_yaml_documents_safe("kind: Pod\n---\nkind: Service\n") == [
            {"kind": "Pod"},
            {"kind": "Service"},
        ]

    def test_documents_raise_on_broken_yaml(self):
        with pytest.raises(YAMLError):
            load_yaml_documents_safe("kind: Pod\n---\na: [1\n")


class TestDumpYamlDoubleQuotedScalar:
    """The Jinja filter's emitter: one double-quoted scalar, nothing else."""

    def test_a_plain_value_is_quoted(self):
        assert dump_yaml_double_quoted_scalar("plain") == '"plain"'

    def test_a_quote_and_a_backslash_are_escaped(self):
        assert dump_yaml_double_quoted_scalar('he said "hi" \\ ok') == '"he said \\"hi\\" \\\\ ok"'

    def test_unicode_is_not_escaped(self):
        assert dump_yaml_double_quoted_scalar("café") == '"café"'

    def test_a_long_value_stays_on_one_line(self):
        value = "a" + " " * 40 + "b" * 200
        assert dump_yaml_double_quoted_scalar(value) == f'"{value}"'


class TestRoundTripLoaderKeepsTheSourceForm:
    """A value that is parsed and emitted again comes out as it went in.

    The pair loader+writer is what makes a rewrite byte-identical, and a to-sops file
    re-encrypts on any changed byte. It also decides what Helm sees: go-yaml parses 1.1,
    where ``0755`` is 493 and ``755`` is 755."""

    SOURCE = "defaultMode: 0755\ntag: 1.10\nreplicas: 1\nname: chart\n"

    def test_a_numeric_scalar_keeps_the_form_it_had(self):
        assert dump_yaml_for_go(load_yaml_round_trip(self.SOURCE)) == self.SOURCE

    def test_the_safe_loader_is_not_a_substitute(self):
        # Pinned so the loader cannot be swapped back: the safe loader resolves a scalar
        # to a Python number and the writer then emits its repr.
        assert dump_yaml_for_go(load_yaml_safe(self.SOURCE)) == (
            "defaultMode: 755\ntag: 1.1\nreplicas: 1\nname: chart\n"
        )
