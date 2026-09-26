"""The set of sync options ArgoCD reads, shared by the two guards that check against it.

The generated Application and the handwritten platform ones have a guard each, and both
ask this of the same ArgoCD. So the answer lives here rather than once per guard: two
copies would drift apart, and the half that drifted would go on passing.

It sits in its own module rather than in either guard. The generated guard imports
``opi``, the handwritten one walks the overlays on disk; whichever way round an import
between them went, one guard would go red for the other's reason.
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
