"""The set of sync options ArgoCD reads, shared by the two guards that check against it.

There are two Applications sources in this repo and one guard each: the generated one in
``test_argocd_generated_application_syncoptions.py`` (RC-226) and the handwritten platform
ones in ``test_argocd_handwritten_application_syncoptions.py`` (RC-228). Both ask the same
question of the same ArgoCD, so the answer lives here rather than once per guard: two
copies of this set would drift apart, and the half that drifted would go on passing.

It sits in its own module rather than in either guard so that neither has to import the
other. The generated guard imports ``opi``, the handwritten one walks the overlays on
disk; whichever way round the import went, one guard would go red for the other's reason.
"""

#: Every option ArgoCD v3.5.1 reads from ``spec.syncPolicy.syncOptions``, measured on the
#: ``SyncOptions.HasOption`` and ``SyncOptions.GetOptionValue`` calls in ``controller/``.
#: ``Force=true`` is deliberately not here: the only reader of it is
#: ``HasAnnotationOption`` in gitops-engine, so it works as a resource annotation and does
#: nothing on an Application.
KNOWN_SYNC_OPTIONS = {
    "ApplyOutOfSyncOnly",
    "ClientSideApplyMigration",
    "CreateNamespace",
    "Delete",
    "FailOnSharedResource",
    "Prune",
    "PruneLast",
    "PrunePropagationPolicy",
    "Replace",
    "RespectIgnoreDifferences",
    "ServerSideApply",
    "SkipDryRunOnMissingResource",
    "Validate",
}
