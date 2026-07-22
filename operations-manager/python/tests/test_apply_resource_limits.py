"""Tests for apply_resource_limits: memory/CPU limits land in the canonical form.

The API's memory_limit/cpu_limit used to be written as the flat shorthand
(``resources.memory`` / ``resources.cpu``). Manifest generation reads only the
nested ``resources.limits.*`` / ``resources.requests.*``, and the read-time fixup
migrated the shorthand into ``limits`` only when it was ABSENT - so patching an
EXISTING limit was silently dropped. These tests pin the corrected behaviour:
the nested form is written directly, and the memory request never exceeds the
memory limit.
"""

from opi.utils.project_utils import apply_resource_limits


def test_new_component_memory_sets_both_requests_and_limits() -> None:
    resources: dict = {}
    apply_resource_limits(resources, memory_limit="384Mi")
    assert resources == {"limits": {"memory": "384Mi"}, "requests": {"memory": "384Mi"}}


def test_patch_existing_limit_takes_effect() -> None:
    """The core regression: patching an existing limit must not be discarded."""
    resources = {"requests": {"memory": "256Mi"}, "limits": {"memory": "512Mi"}}
    apply_resource_limits(resources, memory_limit="384Mi")
    assert resources["limits"]["memory"] == "384Mi"
    # 256Mi request is still <= 384Mi limit, so it is left untouched.
    assert resources["requests"]["memory"] == "256Mi"


def test_patch_below_existing_request_lowers_the_request() -> None:
    """A request must never exceed the limit; lower it to the new limit."""
    resources = {"requests": {"memory": "256Mi"}, "limits": {"memory": "512Mi"}}
    apply_resource_limits(resources, memory_limit="200Mi")
    assert resources["limits"]["memory"] == "200Mi"
    assert resources["requests"]["memory"] == "200Mi"


def test_cpu_limit_only_sets_limits_cpu() -> None:
    resources: dict = {}
    apply_resource_limits(resources, cpu_limit="500m")
    assert resources["limits"] == {"cpu": "500m"}
    # No memory touched, so no memory request is invented.
    assert resources["requests"] == {}


def test_no_flat_shorthand_is_written() -> None:
    """The flat resources.memory / resources.cpu form must not reappear."""
    resources: dict = {}
    apply_resource_limits(resources, cpu_limit="1", memory_limit="512Mi")
    assert "memory" not in resources
    assert "cpu" not in resources


def test_neither_limit_is_a_noop() -> None:
    resources: dict = {"limits": {"memory": "512Mi"}}
    apply_resource_limits(resources)
    assert resources == {"limits": {"memory": "512Mi"}}
