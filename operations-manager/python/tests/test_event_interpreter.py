from opi.services.event_interpreter import (
    EventSeverity,
    interpret_argocd_errors,
    interpret_events,
)


class TestInterpretEvents:
    def test_translates_image_pull_error(self):
        events = [
            {
                "reason": "ErrImagePull",
                "message": "rpc error: image not found",
                "object": "myapp-abc123-xyz",
                "time": "",
            }
        ]
        result = interpret_events(events)
        assert len(result) == 1
        assert result[0].title == "Container image kan niet worden opgehaald"
        assert result[0].severity == EventSeverity.ACTIONABLE
        assert result[0].suggestion != ""

    def test_translates_crash_loop_backoff(self):
        events = [
            {"reason": "BackOff", "message": "back-off 5m0s restarting failed container", "object": "pod-1", "time": ""}
        ]
        result = interpret_events(events)
        assert len(result) == 1
        assert "crasht" in result[0].title

    def test_translates_oom_killed(self):
        events = [{"reason": "OOMKilled", "message": "container killed", "object": "pod-1", "time": ""}]
        result = interpret_events(events)
        assert len(result) == 1
        assert "geheugen" in result[0].title.lower()

    def test_filters_noise_events(self):
        events = [
            {"reason": "Pulling", "message": "Pulling image foo", "object": "pod-1", "time": ""},
            {"reason": "Pulled", "message": "Successfully pulled", "object": "pod-1", "time": ""},
            {"reason": "Created", "message": "Created container", "object": "pod-1", "time": ""},
            {"reason": "Started", "message": "Started container", "object": "pod-1", "time": ""},
            {"reason": "Scheduled", "message": "Successfully assigned", "object": "pod-1", "time": ""},
        ]
        result = interpret_events(events)
        assert len(result) == 0

    def test_deduplicates_same_reason_same_base_pod(self):
        events = [
            {
                "reason": "BackOff",
                "message": "back-off restarting failed container",
                "object": "myapp-54887cbf98-m65r2",
                "time": "",
            },
            {
                "reason": "BackOff",
                "message": "back-off restarting failed container",
                "object": "myapp-54887cbf98-x9k1a",
                "time": "",
            },
        ]
        result = interpret_events(events)
        assert len(result) == 1
        assert result[0].count == 2

    def test_does_not_dedupe_different_reasons(self):
        events = [
            {"reason": "BackOff", "message": "back-off restarting", "object": "myapp-abc-xyz", "time": ""},
            {"reason": "ErrImagePull", "message": "image not found", "object": "myapp-abc-xyz", "time": ""},
        ]
        result = interpret_events(events)
        assert len(result) == 2

    def test_does_not_dedupe_different_base_names(self):
        events = [
            {"reason": "BackOff", "message": "back-off restarting", "object": "app-a-abc12345-xyz12", "time": ""},
            {"reason": "BackOff", "message": "back-off restarting", "object": "app-b-abc12345-xyz12", "time": ""},
        ]
        result = interpret_events(events)
        assert len(result) == 2

    def test_failed_scheduling_unbound_pvc_is_storage_wait_not_resource_shortage(self):
        events = [
            {
                "reason": "FailedScheduling",
                "message": (
                    "0/12 nodes are available: pod has unbound immediate PersistentVolumeClaims. "
                    "preemption: 0/12 nodes are available: 12 Preemption is not helpful for scheduling."
                ),
                "object": "pr-483-frontend-74d86df447-jgs9n",
                "time": "",
            }
        ]
        result = interpret_events(events)
        assert len(result) == 1
        assert result[0].title == "Wacht op opslagvolume"
        assert result[0].severity == EventSeverity.INFORMATIONAL
        assert "onvoldoende" not in result[0].suggestion.lower()

    def test_failed_scheduling_without_pvc_keeps_generic_translation(self):
        events = [
            {
                "reason": "FailedScheduling",
                "message": "0/12 nodes are available: 12 Insufficient cpu.",
                "object": "myapp-abc123-xyz12",
                "time": "",
            }
        ]
        result = interpret_events(events)
        assert len(result) == 1
        assert result[0].title == "Pod kan niet worden ingepland"

    def test_falls_back_to_message_pattern(self):
        events = [
            {
                "reason": "UnknownReason",
                "message": "container has runAsNonRoot and image will run as root",
                "object": "pod-1",
                "time": "",
            }
        ]
        result = interpret_events(events)
        assert len(result) == 1
        assert "root" in result[0].title.lower()

    def test_unrecognized_event_is_dropped(self):
        events = [{"reason": "SomeRandomReason", "message": "something random happened", "object": "pod-1", "time": ""}]
        result = interpret_events(events)
        assert len(result) == 0

    def test_progress_deadline_exceeded(self):
        events = [
            {
                "reason": "ProgressDeadlineExceeded",
                "message": 'Deployment "x" exceeded its progress deadline',
                "object": "x",
                "time": "",
            }
        ]
        result = interpret_events(events)
        assert len(result) == 1
        assert "te lang" in result[0].title

    def test_unhealthy_probe(self):
        events = [
            {
                "reason": "Unhealthy",
                "message": "Liveness probe failed: HTTP probe failed",
                "object": "pod-1",
                "time": "",
            }
        ]
        result = interpret_events(events)
        assert len(result) == 1
        assert "health-check" in result[0].title.lower()


class TestInterpretArgocdErrors:
    def test_passes_through_non_event_errors(self):
        errors = [{"resource": "Deployment/myapp", "message": "Some ArgoCD error"}]
        result = interpret_argocd_errors(errors)
        assert len(result) == 1
        assert result[0]["resource"] == "Deployment/myapp"
        assert result[0]["message"] == "Some ArgoCD error"

    def test_translates_event_errors(self):
        errors = [
            {"resource": "Event/myapp-pod", "message": "[ErrImagePull] failed to pull image foo:latest"},
        ]
        result = interpret_argocd_errors(errors)
        assert len(result) == 1
        assert result[0]["resource"] == "myapp-pod"
        assert "image" in result[0]["message"].lower()
        assert "suggestion" in result[0]

    def test_filters_noise_events_from_mixed_list(self):
        errors = [
            {"resource": "Deployment/myapp", "message": "Deployment not healthy"},
            {"resource": "Event/pod-1", "message": "[Pulling] Pulling image foo"},
            {"resource": "Event/pod-1", "message": "[BackOff] back-off restarting failed container"},
        ]
        result = interpret_argocd_errors(errors)
        # Should keep ArgoCD error + interpreted BackOff, drop Pulling
        assert len(result) == 2
        assert result[0]["resource"] == "Deployment/myapp"
        assert "crasht" in result[1]["message"]

    def test_deduplicates_events_in_mixed_list(self):
        errors = [
            {"resource": "Event/myapp-abc12345-12345", "message": "[BackOff] back-off restarting"},
            {"resource": "Event/myapp-abc12345-67890", "message": "[BackOff] back-off restarting"},
        ]
        result = interpret_argocd_errors(errors)
        assert len(result) == 1
        assert result[0].get("count") == "2"

    def test_enriches_argocd_error_with_pattern(self):
        errors = [
            {"resource": "Pod/myapp-xyz", "message": "container has runAsNonRoot and image will run as root"},
        ]
        result = interpret_argocd_errors(errors)
        assert len(result) == 1
        assert "root" in result[0]["message"].lower()
        assert result[0].get("suggestion")
        assert result[0].get("original_message")

    def test_argocd_error_unbound_pvc_wins_from_no_nodes_pattern(self):
        errors = [
            {
                "resource": "Pod/prod-frontend-74d86df447-jgs9n",
                "message": "0/12 nodes are available: pod has unbound immediate PersistentVolumeClaims.",
            }
        ]
        result = interpret_argocd_errors(errors)
        assert len(result) == 1
        assert result[0]["message"] == "Wacht op opslagvolume"
        assert result[0]["severity"] == "informational"

    def test_preserves_timestamps(self):
        errors = [
            {"resource": "Event/pod-1", "message": "[OOMKilled] container killed", "timestamp": "2025-01-01T12:00:00Z"},
        ]
        result = interpret_argocd_errors(errors)
        assert len(result) == 1
        assert result[0]["timestamp"] == "2025-01-01T12:00:00Z"

    def test_comparison_error_gets_readable_heading(self):
        raw = (
            "Failed to load target state: failed to generate manifests in 'x': exit status 1: "
            "may not add resource with an already registered id: PersistentVolumeClaim.v1.[noGrp]/web-data.ns"
        )
        errors = [{"resource": "ComparisonError", "message": raw}]
        result = interpret_argocd_errors(errors, deployment_name="prod", component_names=["web"])
        assert len(result) == 1
        # Friendly, slash-free heading (the deployment-name simplifier splits on "/").
        assert result[0]["resource"] == "Configuratiefout (kustomize CMP)"
        # The raw kustomize/CMP message is kept as the description, with a suggestion.
        assert "already registered id" in result[0]["message"]
        assert result[0].get("suggestion")
        assert result[0]["severity"] == "actionable"
        assert result[0].get("orphaned") is None

    def test_empty_list(self):
        assert interpret_argocd_errors([]) == []

    def test_event_without_reason_bracket_format(self):
        errors = [
            {"resource": "Event/pod-1", "message": "some message without brackets"},
        ]
        result = interpret_argocd_errors(errors)
        # No reason match, no pattern match -> dropped
        assert len(result) == 0

    def test_preserves_age_on_non_event_errors(self):
        errors = [
            {"resource": "Deployment/myapp", "message": "Something wrong", "age": "5 min geleden"},
        ]
        result = interpret_argocd_errors(errors)
        assert result[0].get("age") == "5 min geleden"


class TestSymptomSuppression:
    def test_suppresses_progress_deadline_when_crashloop_exists(self):
        """The exact scenario from the user report: Deployment deadline + Pod crashloop."""
        errors = [
            # ArgoCD health message on the Deployment
            {
                "resource": "Deployment/productie-typesense",
                "message": "Deployment duurt te lang",
                "suggestion": "some suggestion",
                "severity": "actionable",
            },
            # Interpreted K8s event on the Pod
            {
                "resource": "productie-typesense-54887cbf98-m65r2",
                "message": "Applicatie crasht herhaaldelijk",
                "suggestion": "Bekijk de logs",
                "severity": "actionable",
            },
        ]
        result = interpret_argocd_errors(errors)
        assert len(result) == 1
        assert "crasht" in result[0]["message"]

    def test_suppresses_progress_deadline_via_full_pipeline(self):
        """End-to-end: ArgoCD enriched error + K8s event both about same component."""
        errors = [
            # ArgoCD resource health says "exceeded its progress deadline"
            {
                "resource": "Deployment/productie-typesense",
                "message": 'Deployment "productie-typesense" exceeded its progress deadline',
            },
            # K8s event: back-off restarting
            {
                "resource": "Event/productie-typesense-54887cbf98-m65r2",
                "message": "[BackOff] back-off 5m0s restarting failed container=app pod=productie-typesense-54887cbf98-m65r2",
            },
        ]
        result = interpret_argocd_errors(errors)
        # Only the crash loop root cause should remain
        assert len(result) == 1
        assert "crasht" in result[0]["message"]

    def test_keeps_progress_deadline_when_no_root_cause(self):
        """Progress deadline without any root cause should still be shown."""
        errors = [
            {"resource": "Deployment/myapp", "message": "Deployment duurt te lang", "severity": "actionable"},
        ]
        result = interpret_argocd_errors(errors)
        assert len(result) == 1

    def test_keeps_errors_for_different_components(self):
        """Root cause on component A should not suppress symptoms on component B."""
        errors = [
            {"resource": "Deployment/app-b", "message": "Deployment duurt te lang", "severity": "actionable"},
            {
                "resource": "app-a-abc12345-xyz12",
                "message": "Applicatie crasht herhaaldelijk",
                "severity": "actionable",
            },
        ]
        result = interpret_argocd_errors(errors)
        assert len(result) == 2

    def test_suppresses_health_check_when_crashloop_exists(self):
        errors = [
            {"resource": "myapp-54887cbf98-m65r2", "message": "Health-check gefaald", "severity": "actionable"},
            {
                "resource": "myapp-54887cbf98-x9k1a",
                "message": "Applicatie crasht herhaaldelijk",
                "severity": "actionable",
            },
        ]
        result = interpret_argocd_errors(errors)
        assert len(result) == 1
        assert "crasht" in result[0]["message"]

    def test_suppresses_image_pull_derived_progress_deadline(self):
        errors = [
            {"resource": "Deployment/myapp", "message": "Deployment duurt te lang", "severity": "actionable"},
            {
                "resource": "myapp-54887cbf98-m65r2",
                "message": "Container image kan niet worden opgehaald",
                "severity": "actionable",
            },
        ]
        result = interpret_argocd_errors(errors)
        assert len(result) == 1
        assert "image" in result[0]["message"].lower()

    def test_argocd_raw_image_pull_enriched_and_suppresses_deadline(self):
        """Real scenario: ArgoCD resource tree reports raw ErrImagePull on Pod,
        plus progress deadline on Deployment. Should show only the image error."""
        errors = [
            {
                "resource": "Deployment/productie-typesense",
                "message": 'Deployment "productie-typesense" exceeded its progress deadline',
            },
            {
                "resource": "Pod/productie-typesense-5c75cf5664-wkrkf",
                "message": 'Back-off pulling image "typesense/typesense:26.066675": ErrImagePull: rpc error: not found',
            },
        ]
        result = interpret_argocd_errors(errors)
        assert len(result) == 1
        assert "image" in result[0]["message"].lower()
        assert result[0].get("suggestion")

    def test_dedupes_same_error_from_argocd_and_k8s_events(self):
        """Same pod reported by ArgoCD resource tree (Pod/x) and K8s event (x)."""
        errors = [
            # From ArgoCD resource tree
            {
                "resource": "Pod/productie-typesense-5c75cf5664-wkrkf",
                "message": 'Back-off pulling image "typesense/typesense:26.066675": ErrImagePull: not found',
            },
            # From K8s events (same pod, same issue)
            {
                "resource": "Event/productie-typesense-5c75cf5664-wkrkf",
                "message": "[ImagePullBackOff] Back-off pulling image",
                "timestamp": "2025-01-01T12:00:00Z",
            },
        ]
        result = interpret_argocd_errors(errors)
        assert len(result) == 1
        assert "image" in result[0]["message"].lower()


class TestFriendlyResourceNames:
    def test_strips_deployment_prefix_and_pod_hashes(self):
        errors = [
            {
                "resource": "Pod/productie-typesense-5c75cf5664-wkrkf",
                "message": 'Back-off pulling image "typesense/typesense:26.066675": ErrImagePull: not found',
            },
        ]
        result = interpret_argocd_errors(errors, deployment_name="productie")
        assert result[0]["resource"] == "typesense"

    def test_strips_deployment_prefix_from_deployment_resource(self):
        errors = [
            {"resource": "Deployment/productie-typesense", "message": "Some error"},
        ]
        result = interpret_argocd_errors(errors, deployment_name="productie")
        assert result[0]["resource"] == "typesense"

    def test_strips_deployment_prefix_from_k8s_events(self):
        errors = [
            {
                "resource": "Event/productie-echo-54887cbf98-m65r2",
                "message": "[BackOff] back-off restarting failed container",
            },
        ]
        result = interpret_argocd_errors(errors, deployment_name="productie")
        assert result[0]["resource"] == "echo"

    def test_no_deployment_name_preserves_full_resource(self):
        errors = [
            {"resource": "Deployment/productie-typesense", "message": "Some error"},
        ]
        result = interpret_argocd_errors(errors)
        assert result[0]["resource"] == "Deployment/productie-typesense"

    def test_full_scenario_shows_component_name(self):
        """Full realistic scenario: image pull error should show just 'typesense'."""
        errors = [
            {
                "resource": "Deployment/productie-typesense",
                "message": 'Deployment "productie-typesense" exceeded its progress deadline',
            },
            {
                "resource": "Pod/productie-typesense-5c75cf5664-wkrkf",
                "message": 'Back-off pulling image "typesense/typesense:26.066675": ErrImagePull: not found',
            },
        ]
        result = interpret_argocd_errors(errors, deployment_name="productie")
        assert len(result) == 1
        assert result[0]["resource"] == "typesense"
        assert "image" in result[0]["message"].lower()


class TestOrphanedComponentFlagging:
    """Errors for components no longer in the deployment are flagged, not hidden."""

    _CRASH = "[BackOff] back-off restarting failed container"

    def test_flags_resource_for_removed_component(self):
        errors = [{"resource": "Pod/productie-magazijna-5c75cf5664-wkrkf", "message": self._CRASH}]
        result = interpret_argocd_errors(errors, deployment_name="productie", component_names=["profiel"])
        assert len(result) == 1
        assert result[0].get("orphaned") == "true"
        assert result[0]["resource"] == "magazijna"

    def test_does_not_flag_current_component(self):
        errors = [{"resource": "Pod/productie-profiel-5c75cf5664-wkrkf", "message": self._CRASH}]
        result = interpret_argocd_errors(errors, deployment_name="productie", component_names=["profiel"])
        assert "orphaned" not in result[0]

    def test_app_level_resource_never_flagged(self):
        errors = [{"resource": "SyncOperation", "message": "one or more sync tasks failed"}]
        result = interpret_argocd_errors(errors, deployment_name="productie", component_names=["profiel"])
        assert "orphaned" not in result[0]

    def test_prefix_does_not_confuse_similar_names(self):
        # 'magazijn' is current; 'magazijna' was removed and must not match 'magazijn'.
        errors = [{"resource": "Pod/productie-magazijna-5c75cf5664-wkrkf", "message": self._CRASH}]
        result = interpret_argocd_errors(errors, deployment_name="productie", component_names=["magazijn"])
        assert result[0].get("orphaned") == "true"

    def test_no_component_names_does_not_flag(self):
        errors = [{"resource": "Pod/productie-magazijna-5c75cf5664-wkrkf", "message": self._CRASH}]
        result = interpret_argocd_errors(errors, deployment_name="productie")
        assert "orphaned" not in result[0]

    def test_empty_component_names_does_not_flag(self):
        errors = [{"resource": "Pod/productie-magazijna-5c75cf5664-wkrkf", "message": self._CRASH}]
        result = interpret_argocd_errors(errors, deployment_name="productie", component_names=[])
        assert "orphaned" not in result[0]
