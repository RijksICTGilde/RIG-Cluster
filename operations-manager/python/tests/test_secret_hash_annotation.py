"""A changed secret must change the pod spec; an unchanged one must not.

The bug this pins: the user secret and the attachment secrets both have a FIXED name, so
replacing their content used to leave the rendered Deployment byte-identical. Kubernetes
rolls nothing on an identical spec, ``envFrom`` is injected only at container start, and a
``subPath`` mount is a one-time copy that is never refreshed -- so a new certificate or a
new password reached the cluster and never reached the running container.

Each test therefore renders the real ``deployment.yaml.jinja`` through the real renderer
with the real hash function, and compares the two pod specs. The two halves that matter
are both here: a replaced value CHANGES the spec (or nothing restarts) and an unchanged
value does NOT (or every processing run churns the GitOps repo).
"""

from __future__ import annotations

from typing import Any

from opi.generation.manifests import render_template
from opi.utils.secret_hash import SECRET_HASH_ANNOTATION, component_secret_hash

USER_SECRET = "prod-web-user"
ATTACHMENT_SECRET = "prod-attch-servercert"


def _pod_spec(
    user_env_vars: dict[str, Any] | None = None,
    attachment_file_secrets: dict[str, dict[str, str]] | None = None,
) -> str:
    """Render the Deployment the way ``create_application_manifests`` does."""
    user_env_vars = user_env_vars or {}
    attachment_file_secrets = attachment_file_secrets or {}
    return render_template(
        "deployment.yaml.jinja",
        {
            "name": "prod-web",
            "deployment_name": "prod",
            "component_name": "web",
            "namespace": "rig-myproject",
            "project": {"name": "myproject"},
            "cluster": "odcn-production",
            "pod_replacement_mode": "RollingUpdate",
            "replicas": 1,
            "imageURL": "ghcr.io/example/web:1.0.0",
            "imagePullPolicy": "IfNotPresent",
            "application_port": 8080,
            "inbound_ports": [8080],
            "probe_scheme": "tcp",
            "probe_port": None,
            "resources_requests_memory": "128Mi",
            "resources_requests_cpu": "100m",
            "resources_limits_memory": "256Mi",
            "resources_limits_cpu": "500m",
            "env_from_secrets": [USER_SECRET] if user_env_vars else [],
            "attachment_secret_mounts": [
                {
                    "name": "attch-servercert",
                    "secret_name": ATTACHMENT_SECRET,
                    "mount_path": "/etc/ssl/certs/server.pem",
                    "sub_path": "servercert",
                }
                for _ in attachment_file_secrets
            ],
            "secret_hash": component_secret_hash(USER_SECRET, user_env_vars, attachment_file_secrets),
            "secret_hash_key": SECRET_HASH_ANNOTATION,
        },
    )


# --- attachments ---------------------------------------------------------------------


def test_replacing_an_attachment_changes_the_pod_spec() -> None:
    before = _pod_spec(attachment_file_secrets={ATTACHMENT_SECRET: {"servercert": "b2xk"}})
    after = _pod_spec(attachment_file_secrets={ATTACHMENT_SECRET: {"servercert": "bmlldXc="}})
    assert SECRET_HASH_ANNOTATION in before
    assert before != after


def test_unchanged_attachment_leaves_the_pod_spec_identical() -> None:
    content = {ATTACHMENT_SECRET: {"servercert": "b2xk"}}
    assert _pod_spec(attachment_file_secrets=content) == _pod_spec(attachment_file_secrets=dict(content))


# --- user env-vars -------------------------------------------------------------------


def test_changing_a_user_env_var_changes_the_pod_spec() -> None:
    before = _pod_spec(user_env_vars={"ADMIN_PASSWORD": "old"})
    after = _pod_spec(user_env_vars={"ADMIN_PASSWORD": "new"})
    assert SECRET_HASH_ANNOTATION in before
    assert before != after


def test_unchanged_user_env_vars_leave_the_pod_spec_identical() -> None:
    values = {"ADMIN_PASSWORD": "secret", "LOG_LEVEL": "info"}
    assert _pod_spec(user_env_vars=values) == _pod_spec(user_env_vars=dict(values))


def test_reordered_user_env_vars_leave_the_pod_spec_identical() -> None:
    # Dict order is not content. A project file read twice may hand the values over in a
    # different order, and a hash that noticed would restart the pod for nothing.
    assert _pod_spec(user_env_vars={"A": "1", "B": "2"}) == _pod_spec(user_env_vars={"B": "2", "A": "1"})


# --- what ends up in the manifest ----------------------------------------------------


def test_component_without_secrets_carries_no_annotation() -> None:
    # Nothing to detect a change in, so no annotation rather than every such pod pinned
    # to the hash of nothing.
    assert SECRET_HASH_ANNOTATION not in _pod_spec()


def test_annotation_holds_a_hash_and_never_the_content() -> None:
    spec = _pod_spec(
        user_env_vars={"ADMIN_PASSWORD": "hunter2"},
        attachment_file_secrets={ATTACHMENT_SECRET: {"servercert": "cHJpdmF0ZS1rZXk="}},
    )
    assert "hunter2" not in spec
    assert "cHJpdmF0ZS1rZXk=" not in spec
    annotation = next(line for line in spec.splitlines() if SECRET_HASH_ANNOTATION in line)
    assert annotation.strip().startswith(f"{SECRET_HASH_ANNOTATION}: ")


def test_the_two_secrets_are_hashed_apart() -> None:
    # The same bytes under another name are another deployment state: an attachment moved
    # to a different id must not hash the same as the one it replaced.
    one = component_secret_hash(USER_SECRET, {}, {ATTACHMENT_SECRET: {"servercert": "aGk="}})
    other = component_secret_hash(USER_SECRET, {}, {ATTACHMENT_SECRET: {"otherref": "aGk="}})
    assert one != other


def test_a_value_moved_between_key_and_name_is_a_different_hash() -> None:
    # Length-prefixed encoding: "ab"="c" must not hash like "a"="bc".
    assert component_secret_hash(USER_SECRET, {"ab": "c"}, {}) != component_secret_hash(USER_SECRET, {"a": "bc"}, {})
