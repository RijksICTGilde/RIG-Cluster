"""
Interprets raw Kubernetes and ArgoCD events into user-friendly messages.

Sits between raw event fetching and display: translates cryptic K8s/ArgoCD
messages, deduplicates repeated events, and classifies severity.
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum

from opi.handlers.project_file_handler import is_transient_registry_error
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)


class EventSeverity(Enum):
    """How actionable an event is for the user."""

    ACTIONABLE = "actionable"  # User can fix this
    INFORMATIONAL = "informational"  # Awareness only
    NOISE = "noise"  # Skip entirely


@dataclass
class InterpretedEvent:
    """A user-friendly interpreted event."""

    resource: str
    title: str
    description: str
    severity: EventSeverity
    suggestion: str = ""
    age: str = ""
    timestamp: str = ""
    count: int = 1


# --- Translation table for K8s event reasons ---

# De melding voor een container die uit zichzelf stopt en opnieuw wordt gestart. Een
# constante, omdat de probe-oorzaak hieronder hem gericht moet kunnen vervangen: de
# kubelet meldt namelijk hetzelfde als hij een container kilt op een falende probe.
_CRASH_TITLE = "Applicatie crasht herhaaldelijk"
#: De crashmelding voor het geval waarin er NOG iets draait: een mislukte uitrol naast een
#: applicatie die het gewoon doet. Bewust een eigen titel en geen extra zin achter de oude:
#: de titel is wat er in de opsomming staat, en "crasht herhaaldelijk" is precies de zin
#: die de lezer op het verkeerde been zet.
_CRASH_WITH_SERVING_TITLE = "Nieuwe versie start niet op"

_EVENT_TRANSLATIONS: dict[str, tuple[str, str, EventSeverity]] = {
    # (title, suggestion, severity)
    #
    # Image problems (ErrImagePull / ImagePullBackOff get a dynamic suggestion,
    # see _image_pull_suggestion; only the static ones remain here)
    "InvalidImageName": (
        "Ongeldige image-naam",
        "De opgegeven image-naam is ongeldig. Controleer de naam op typefouten.",
        EventSeverity.ACTIONABLE,
    ),
    # Crash / restart
    "BackOff": (
        _CRASH_TITLE,
        "De container start steeds opnieuw op en crasht. Bekijk de logs voor de oorzaak.",
        EventSeverity.ACTIONABLE,
    ),
    "CrashLoopBackOff": (
        _CRASH_TITLE,
        "De container start steeds opnieuw op en crasht. Bekijk de logs voor de oorzaak.",
        EventSeverity.ACTIONABLE,
    ),
    # OOM
    "OOMKilled": (
        "Applicatie gestopt wegens geheugengebrek",
        "De container heeft meer geheugen nodig dan toegestaan. Verhoog de geheugenlimiet.",
        EventSeverity.ACTIONABLE,
    ),
    "OOMKilling": (
        "Applicatie gestopt wegens geheugengebrek",
        "De container heeft meer geheugen nodig dan toegestaan. Verhoog de geheugenlimiet.",
        EventSeverity.ACTIONABLE,
    ),
    # Scheduling
    "FailedScheduling": (
        "Pod kan niet worden ingepland",
        "Er zijn onvoldoende resources beschikbaar op het cluster. Dit lost zich meestal vanzelf op.",
        EventSeverity.INFORMATIONAL,
    ),
    # Volume / mount
    "FailedMount": (
        "Volume kan niet worden gekoppeld",
        "Controleer of de storage-configuratie correct is.",
        EventSeverity.ACTIONABLE,
    ),
    "FailedAttachVolume": (
        "Volume kan niet worden gekoppeld",
        "Controleer of de storage-configuratie correct is.",
        EventSeverity.ACTIONABLE,
    ),
    # Health probes. Alleen de READINESS-probe komt hier terecht: een falende liveness-
    # of startup-probe is een eigen oorzaak en wordt eerder afgevangen (zie
    # _LIVENESS_PROBE_RE en _probe_kill_translation).
    "Unhealthy": (
        "Health-check gefaald",
        "De applicatie reageert niet op health-checks. Controleer of de applicatie correct opstart en luistert op de juiste poort.",
        EventSeverity.ACTIONABLE,
    ),
    # Security
    "FailedCreate": (
        "Container kon niet worden aangemaakt",
        "Controleer de security-instellingen en container-configuratie.",
        EventSeverity.ACTIONABLE,
    ),
    # Progress deadline
    "ProgressDeadlineExceeded": (
        "Deployment duurt te lang",
        "De deployment is niet binnen de verwachte tijd gereed. Bekijk de logs voor mogelijke oorzaken.",
        EventSeverity.ACTIONABLE,
    ),
}

# Reasons to always skip (normal lifecycle, not useful to users)
_NOISE_REASONS: set[str] = {
    "Pulling",
    "Pulled",
    "Created",
    "Started",
    "Scheduled",
    "ScalingReplicaSet",
    "SuccessfulCreate",
    "Killing",
    "NoPods",
    "SuccessfulDelete",
    "LeaderElection",
}

# A pod waiting on a still-provisioning volume: not a resource shortage, so this
# must win from both the FailedScheduling reason and the "0/N nodes" pattern
# (the scheduler message contains both).
_UNBOUND_PVC_RE = re.compile(r"unbound (?:immediate )?PersistentVolumeClaims?", re.IGNORECASE)
_UNBOUND_PVC_TRANSLATION = (
    "Wacht op opslagvolume",
    "Het opslagvolume wordt nog aangemaakt; de pod start zodra het klaar is. "
    "Dit lost zich meestal binnen enkele minuten vanzelf op.",
    EventSeverity.INFORMATIONAL,
)

# --- Pattern-based translations for ArgoCD health messages ---

_MESSAGE_PATTERNS: list[tuple[re.Pattern[str], str, str, EventSeverity]] = [
    # (pattern, title, suggestion, severity)
    # (image-pull failures are handled dynamically before _MESSAGE_PATTERNS, see
    # _image_pull_suggestion / _IMAGE_PULL_RE)
    (_UNBOUND_PVC_RE, *_UNBOUND_PVC_TRANSLATION),
    (
        re.compile(r"runAsNonRoot", re.IGNORECASE),
        "Container draait als root",
        "Het container image draait als root, maar dat is niet toegestaan. Gebruik een image dat als non-root gebruiker draait.",
        EventSeverity.ACTIONABLE,
    ),
    (
        re.compile(r"exec format error"),
        "Verkeerd platform voor container image",
        "Het image is gebouwd voor een ander CPU-platform (bijv. ARM in plaats van AMD64). Bouw het image voor het juiste platform.",
        EventSeverity.ACTIONABLE,
    ),
    (
        re.compile(r"permission denied"),
        "Geen toegang tot bestand of directory",
        "De applicatie heeft geen rechten om bestanden te lezen of schrijven. Controleer de bestandspermissies in het image.",
        EventSeverity.ACTIONABLE,
    ),
    (
        re.compile(r"port \d+ is already in use|address already in use", re.IGNORECASE),
        "Poort is al in gebruik",
        "De applicatie probeert een poort te gebruiken die al bezet is. Controleer de poort-configuratie.",
        EventSeverity.ACTIONABLE,
    ),
    (
        re.compile(r"exceeded its progress deadline"),
        "Deployment duurt te lang",
        "De deployment is niet binnen de verwachte tijd gereed. Bekijk de logs voor mogelijke oorzaken.",
        EventSeverity.ACTIONABLE,
    ),
    (
        re.compile(r"back-off.*restarting failed container", re.IGNORECASE),
        _CRASH_TITLE,
        "De container start steeds opnieuw op en crasht. Bekijk de logs voor de oorzaak.",
        EventSeverity.ACTIONABLE,
    ),
    (
        re.compile(r"0/\d+ nodes are available", re.IGNORECASE),
        "Geen beschikbare nodes",
        "Er zijn geen cluster-nodes beschikbaar die aan de eisen voldoen. Dit lost zich meestal vanzelf op.",
        EventSeverity.INFORMATIONAL,
    ),
    (
        re.compile(r"Insufficient (cpu|memory)", re.IGNORECASE),
        "Onvoldoende cluster-resources",
        "Er is niet genoeg CPU of geheugen beschikbaar op het cluster. Dit lost zich meestal vanzelf op.",
        EventSeverity.INFORMATIONAL,
    ),
    (
        re.compile(r"OOMKill", re.IGNORECASE),
        "Applicatie gestopt wegens geheugengebrek",
        "De container heeft meer geheugen nodig dan toegestaan. Verhoog de geheugenlimiet.",
        EventSeverity.ACTIONABLE,
    ),
]


_IMAGE_PULL_REASONS = {"ErrImagePull", "ImagePullBackOff"}
_IMAGE_PULL_RE = re.compile(r"ErrImagePull|ImagePullBackOff|back-off.*pulling image|failed to pull", re.IGNORECASE)
_IMAGE_IN_MSG_RE = re.compile(r'image "([^"]+)"')


def _source_image(rewritten: str) -> str:
    """Show the source-registry image, not the rcr.rijksapps.nl proxy rewrite."""
    from opi.core.config import settings
    from opi.extensions.pipeline import get_registry_rewrite_mappings
    from opi.extensions.registry_rewrite import original_image

    return original_image(rewritten, get_registry_rewrite_mappings(settings.CLUSTER_MANAGER))


def _image_pull_suggestion(message: str) -> str:
    """Short suggestion for an image-pull failure, naming the source-registry image.

    Only two outcomes are distinguished, and only on wording the registry is explicit
    about (see ``is_transient_registry_error``): a 5xx or a rate limit means the
    registry could not answer, anything else is treated as a problem with the image
    reference. Deliberately no finer than that -- the exact status is unreliable, an
    auth problem can surface as more than one code -- so the second branch stays one
    check of the common causes plus an anonymous ``docker pull`` to test public access.
    """
    match = _IMAGE_IN_MSG_RE.search(message)
    if is_transient_registry_error(message):
        subject = f"De image {_source_image(match.group(1))}" if match else "De image"
        return (
            f"{subject} kon niet worden opgehaald omdat de registry zelf geen antwoord gaf. Dat zegt niets "
            "over de image: die kan prima bestaan. Hier is niets voor je te doen, het ophalen wordt vanzelf "
            "opnieuw geprobeerd en herstelt zodra de registry weer werkt. Houdt het uren aan, meld het dan "
            "bij het platformteam."
        )
    if not match:
        return "Controleer of de image publiek toegankelijk is en of de naam en tag kloppen."
    image = _source_image(match.group(1))
    return (
        f"De image {image} kon niet worden opgehaald. Controleer of de image publiek toegankelijk is "
        f"en of de naam en tag kloppen. Test zonder credentials met: DOCKER_CONFIG=$(mktemp -d) docker pull {image} *\n"
        "* DOCKER_CONFIG=$(mktemp -d) haalt de image op zonder je config (dus publiek/anoniem) en wijzigt "
        "niets aan je eigen Docker-configuratie."
    )


def _image_pull_translation(message: str) -> tuple[str, str, EventSeverity]:
    """Title, suggestion and severity for an image-pull failure.

    A registry that could not answer is INFORMATIONAL: the user cannot fix it and the
    pull retries by itself, so presenting it as something to act on sends them looking
    for a broken image that is fine. A missing or unreachable image stays ACTIONABLE.
    """
    if is_transient_registry_error(message):
        return (
            "Registry kon de container image niet leveren",
            _image_pull_suggestion(message),
            EventSeverity.INFORMATIONAL,
        )
    return "Container image kan niet worden opgehaald", _image_pull_suggestion(message), EventSeverity.ACTIONABLE


# --- Een container die de kubelet kilt omdat de probe faalt -------------------------
#
# Dit is GEEN crash, en het onderscheid is voor de gebruiker het hele verschil: bij
# "crasht" ga je je applicatie debuggen, bij "de probe komt er niet doorheen" pas je je
# health-instelling aan.
#
# Gemeten op de sandbox met twee pods naast elkaar - een die draait maar een liveness-
# probe op een dichte poort heeft, en een die echt met exit 1 stopt:
#
#   probefail   Running, ready=true   lastState.terminated: reason=Error exitCode=137
#               events: [Unhealthy] Liveness probe failed: dial tcp 10.244.0.85:9999: ...
#                       [Killing]   Container app failed liveness probe, will be restarted
#   echtcrash   CrashLoopBackOff      lastState.terminated: reason=Error exitCode=1
#               events: [BackOff]    Back-off restarting failed container app in pod ...
#
# lastState.terminated.reason is dus in BEIDE gevallen "Error" en draagt het onderscheid
# niet. Blijft de probe lang genoeg falen, dan gaat de kubelet ook op de probe-kill
# backoffen en meldt hij daar hetzelfde "Back-off restarting failed container" - en dat
# is precies de tekst die hieronder als "Applicatie crasht herhaaldelijk" vertaald wordt.
# Vandaar de verkeerde melding.
#
# Wat het onderscheid WEL draagt is het Unhealthy-event: de kubelet schrijft "Liveness
# probe failed" alleen als hij een DRAAIENDE container aan het killen is. Een container
# die uit zichzelf stopt haalt dat event niet.
_LIVENESS_PROBE_RE = re.compile(r"\b(liveness|startup) probe failed", re.IGNORECASE)
_PROBE_KILL_TITLE = "Health-check faalt, de container wordt herstart"

# De poort uit de probe-foutmelding, zodat de melding zegt WAAR het misgaat. Twee vormen,
# beide gemeten: een tcp-probe meldt "dial tcp <ip>:<poort>", een http(s)-probe meldt
# 'Get "http://<ip>:<poort>/<pad>"'.
_PROBE_TCP_PORT_RE = re.compile(r"dial tcp \S*?:(\d{1,5})\b")
_PROBE_HTTP_PORT_RE = re.compile(r"https?://[^/\s\"]*:(\d{1,5})")


def _probe_port(message: str) -> str | None:
    """De poort waarop de probe strandde, of ``None`` als de melding hem niet noemt."""
    for pattern in (_PROBE_TCP_PORT_RE, _PROBE_HTTP_PORT_RE):
        match = pattern.search(message)
        if match:
            return match.group(1)
    return None


def _probe_kill_translation(message: str) -> tuple[str, str, EventSeverity]:
    """Vertaling voor een container die op een falende liveness-/startup-probe wordt gekild."""
    port = _probe_port(message)
    waar = f"De health-check op poort {port} krijgt geen antwoord" if port else "De health-check krijgt geen antwoord"
    return (
        _PROBE_KILL_TITLE,
        f"{waar}, daarom herstart Kubernetes de container. De applicatie zelf draait wel. "
        "Controleer of de health-check op de poort en het pad staat waar je component echt "
        "luistert - niet of je applicatie crasht.",
        EventSeverity.ACTIONABLE,
    )


def condense_render_error(message: str) -> str:
    """Reduce a verbose ArgoCD generation-error message to its meaningful tail.

    Measured against the sandbox: ArgoCD echoes the whole failed ``/bin/bash -c "<script>"``
    command, so a CMP render failure produces a multi-kB message dominated by the plugin
    script source, with the real error (the plugin stderr) at the very end after
    ``exit status N:``. This extracts that tail so the user sees the actual cause instead of
    the 14 kB script dump. Falls back to the cached-generation marker (for errors without an
    exec wrapper, e.g. "app path does not exist"), then to the original message (length
    capped so a giant message never floods logs or the UI).
    """
    if not message:
        return message
    # Real plugin stderr sits after the last "exit status N:"; fall back to the whole message.
    exec_matches = list(re.finditer(r"exit status \d+:\s*", message))
    tail = message[exec_matches[-1].end() :].strip() if exec_matches else message

    # The stderr still leads with the CMP script's DEBUG noise; the real error is the last
    # explicit error line - kustomize prints "Error:", the CMP script prints "ERROR:".
    # (Measured on the sandbox for a duplicate-identity and a missing-namespace failure.)
    best = max(tail.rfind("Error:"), tail.rfind("ERROR:"))
    if best != -1:
        return tail[best:].strip()

    if exec_matches and tail:
        return tail
    # No exec wrapper: take what follows the cached-generation marker.
    marker = "Manifest generation error (cached):"
    idx = message.rfind(marker)
    if idx != -1:
        cached_tail = message[idx + len(marker) :].strip()
        if cached_tail:
            return cached_tail
    return message if len(message) <= 800 else message[:800] + " ...(truncated)"


def _interpret_by_reason(reason: str, message: str) -> tuple[str, str, EventSeverity] | None:
    """Look up translation by event reason, then fall back to message patterns."""
    if reason in _NOISE_REASONS:
        return None

    # Image-pull failures get a dynamic, solution-oriented suggestion.
    if reason in _IMAGE_PULL_REASONS or _IMAGE_PULL_RE.search(message):
        return _image_pull_translation(message)

    # Voor de reason-tabel: het Unhealthy-event zou anders als het algemene
    # "Health-check gefaald" landen, en dat is een SYMPTOOM dat verderop wordt
    # weggefilterd zodra er een crashmelding naast staat - precies de melding die deze
    # oorzaak moet vervangen. Alleen liveness en startup: die killen de container, een
    # falende readiness-probe doet dat niet en blijft dus wel een symptoom.
    if _LIVENESS_PROBE_RE.search(message):
        return _probe_kill_translation(message)

    # Checked before the reason table: a FailedScheduling on an unbound PVC would
    # otherwise be mistranslated as a cluster resource shortage.
    if _UNBOUND_PVC_RE.search(message):
        return _UNBOUND_PVC_TRANSLATION

    if reason in _EVENT_TRANSLATIONS:
        return _EVENT_TRANSLATIONS[reason]

    # Fall back to message pattern matching
    for pattern, title, suggestion, severity in _MESSAGE_PATTERNS:
        if pattern.search(message):
            return title, suggestion, severity

    return None


def _extract_base_name(name: str) -> str:
    """Extract base deployment name, stripping pod/replicaset hash suffixes."""
    # Strip pod hash suffixes (e.g., "myapp-54887cbf98-m65r2" -> "myapp")
    result = re.sub(r"-[a-f0-9]{8,10}-[a-z0-9]{5}$", "", name)
    # Also strip replicaset hashes
    result = re.sub(r"-[a-f0-9]{8,10}$", "", result)
    return result


# Titles that are symptoms — suppressed when a root cause exists for the same component.
_SYMPTOM_TITLES: set[str] = {
    "Deployment duurt te lang",
    "Health-check gefaald",
}

# Titles considered root causes that suppress symptoms.
_ROOT_CAUSE_TITLES: set[str] = {
    _CRASH_TITLE,
    _PROBE_KILL_TITLE,
    "Container image kan niet worden opgehaald",
    "Ongeldige image-naam",
    "Applicatie gestopt wegens geheugengebrek",
    "Container draait als root",
    "Verkeerd platform voor container image",
    "Geen toegang tot bestand of directory",
    "Volume kan niet worden gekoppeld",
    "Container kon niet worden aangemaakt",
}


def _resource_base_name(resource: str) -> str:
    """Extract base component name from a resource string, stripping Kind/ prefix and hashes."""
    name = resource.split("/", 1)[-1] if "/" in resource else resource
    return _extract_base_name(name)


def _suppress_symptoms(
    errors: list[dict[str, str]], serving_components: set[str] | None = None
) -> list[dict[str, str]]:
    """Remove symptom errors when a root cause exists for the same component.

    Bovenop de symptoomregel geldt er een tussen twee OORZAKEN: staat er voor hetzelfde
    component een probe-kill, dan is de crashmelding onwaar en verdwijnt hij. Beide
    komen namelijk van dezelfde kubelet-backoff, maar alleen de probe-kill weet WAAROM
    de container omging. Andersom - een component dat echt crasht - is er geen
    probe-kill, en dan blijft de crashmelding gewoon staan.

    En er is een derde geval, dat geen melding wegneemt maar hem BIJSTELT. Draait er voor
    hetzelfde component nog een pod die verkeer afhandelt (``serving_components``, de
    unieke namen), dan is "de applicatie crasht herhaaldelijk" niet onwaar maar wel
    misleidend: de vorige versie bedient de gebruikers gewoon door en alleen de NIEUWE pod
    komt niet omhoog. Dat is een mislukte uitrol en geen storing, en dat verschil bepaalt
    of iemand 's nachts opstaat. Zelfde soort correctie als de probe-kill hierboven: een
    preciezere waarneming verdringt een minder precieze, op dezelfde plek en niet in een
    tweede mechanisme ernaast.
    """
    root_cause_components: set[str] = set()
    probe_kill_components: set[str] = set()
    for error in errors:
        title = error.get("message", "")
        if title in _ROOT_CAUSE_TITLES:
            root_cause_components.add(_resource_base_name(error.get("resource", "")))
        if title == _PROBE_KILL_TITLE:
            probe_kill_components.add(_resource_base_name(error.get("resource", "")))

    if not root_cause_components:
        return errors

    result: list[dict[str, str]] = []
    for error in errors:
        title = error.get("message", "")
        component = _resource_base_name(error.get("resource", ""))
        if title in _SYMPTOM_TITLES and component in root_cause_components:
            continue
        if title == _CRASH_TITLE and component in probe_kill_components:
            continue
        result.append(error)
    return _restate_crash_with_serving_pod(result, serving_components)


def _restate_crash_with_serving_pod(
    errors: list[dict[str, str]], serving_components: set[str] | None
) -> list[dict[str, str]]:
    """Reword the crash message for a component whose previous version keeps serving.

    Applied AFTER the suppression above so it cannot disturb it: that pass reads titles to
    decide what is a root cause, and a crash entry only stops being ``_CRASH_TITLE`` once
    every one of those decisions has been made.
    """
    if not serving_components:
        return errors
    for error in errors:
        if error.get("message") != _CRASH_TITLE:
            continue
        if _resource_base_name(error.get("resource", "")) not in serving_components:
            continue
        error["message"] = _CRASH_WITH_SERVING_TITLE
        error["suggestion"] = (
            "De vorige versie draait door, dus je applicatie blijft bereikbaar. Alleen de nieuwe pod "
            "komt niet omhoog; de oorzaak staat in de logs van die pod. Kies hem in het logpaneel, en "
            "zet 'vorige poging' aan als hij tussen twee pogingen in niets laat zien."
        )
    return errors


def _dedupe_cross_source(errors: list[dict[str, str]]) -> list[dict[str, str]]:
    """Deduplicate errors with the same translated message for the same component.

    The same issue often appears from both ArgoCD resource tree (Pod/x) and
    K8s events (x) — keep only the first occurrence.
    """
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for error in errors:
        base = _resource_base_name(error.get("resource", ""))
        title = error.get("message", "")
        key = f"{base}:{title}"
        if key in seen:
            continue
        seen.add(key)
        result.append(error)
    return result


def _dedupe_key(event: dict[str, str]) -> str:
    """Generate a key for deduplication: same reason + same object base name."""
    reason = event.get("reason", "")
    obj = event.get("object", event.get("resource", ""))
    return f"{reason}:{_extract_base_name(obj)}"


def interpret_events(raw_events: list[dict[str, str]]) -> list[InterpretedEvent]:
    """
    Interpret raw K8s events into user-friendly messages.

    Filters noise, translates known patterns, and deduplicates.
    """
    seen: dict[str, InterpretedEvent] = {}

    for event in raw_events:
        reason = event.get("reason", "")
        message = event.get("message", "")
        obj = event.get("object", "unknown")

        translation = _interpret_by_reason(reason, message)
        if translation is None:
            continue

        title, suggestion, severity = translation
        if severity == EventSeverity.NOISE:
            continue

        key = _dedupe_key(event)
        if key in seen:
            seen[key].count += 1
            continue

        interpreted = InterpretedEvent(
            resource=obj,
            title=title,
            description=message,
            severity=severity,
            suggestion=suggestion,
            timestamp=event.get("time", ""),
        )
        seen[key] = interpreted

    return list(seen.values())


def _friendly_resource_name(resource: str, deployment_name: str) -> str:
    """Convert a K8s resource name to a user-friendly component name.

    Strips Kind/ prefix, deployment name prefix, and pod/replicaset hashes.
    E.g., "Pod/productie-typesense-5c75cf5664-wkrkf" with deployment "productie"
    becomes "typesense".
    """
    # Strip Kind/ prefix
    name = resource.split("/", 1)[-1] if "/" in resource else resource
    # Strip deployment name prefix
    if deployment_name and name.startswith(f"{deployment_name}-"):
        name = name[len(deployment_name) + 1 :]
    # Strip pod hash suffixes (replicaset-hash-podhash)
    name = re.sub(r"-[a-f0-9]{8,10}-[a-z0-9]{5}$", "", name)
    # Strip replicaset hash
    name = re.sub(r"-[a-f0-9]{8,10}$", "", name)
    return name


def _is_orphaned_resource(resource: str, deployment_name: str, valid_unique_names: set[str]) -> bool:
    """True if a component-scoped resource has no matching current component.

    Component resources are named ``{deployment_name}-{component}`` (pods and
    replicasets carry that prefix plus a hash). Resources that are not scoped to
    a component (app-level conditions, SyncOperation, shared resources) never
    start with the deployment prefix and are never treated as orphaned.

    The trailing-dash match keeps ``magazijn`` from matching ``magazijna``.
    """
    name = resource.split("/", 1)[-1] if "/" in resource else resource
    if not name.startswith(f"{deployment_name}-"):
        return False
    return not any(name == unique or name.startswith(f"{unique}-") for unique in valid_unique_names)


def interpret_argocd_errors(
    errors: list[dict[str, str]],
    deployment_name: str = "",
    component_names: list[str] | None = None,
    serving_components: set[str] | None = None,
) -> list[dict[str, str]]:
    """
    Interpret a mixed list of ArgoCD errors and K8s events.

    K8s events (resource starts with "Event/") are translated and deduplicated.
    ArgoCD errors are passed through but enriched with pattern matching.
    Resource names are simplified to component names when deployment_name is provided.

    ``serving_components`` names the component REFERENCES for which a pod is currently
    serving traffic (see ``summarize_component_pods``). Only the crash message uses it, and
    only to say what is really the case: the previous version is still serving and the new
    one is what fails.

    When ``component_names`` is a non-empty list, entries whose resource belongs
    to a component no longer present in the deployment are flagged with
    ``orphaned=True`` (a leftover whose cluster cleanup has not finished). The UI
    renders these de-emphasised instead of as live deployment errors.

    Returns a list of error dicts compatible with the template.
    """
    k8s_events: list[dict[str, str]] = []
    other_errors: list[dict[str, str]] = []

    for error in errors:
        resource = error.get("resource", "")
        if resource.startswith("Event/"):
            # Convert back to raw event format for interpretation
            message = error.get("message", "")
            reason = ""
            msg_body = message
            # Parse "[Reason] message" format
            reason_match = re.match(r"^\[(\w+)]\s*(.*)$", message)
            if reason_match:
                reason = reason_match.group(1)
                msg_body = reason_match.group(2)

            k8s_events.append(
                {
                    "reason": reason,
                    "message": msg_body,
                    "object": resource.removeprefix("Event/"),
                    "time": error.get("timestamp", ""),
                }
            )
        else:
            other_errors.append(_enrich_argocd_error(error))

    # Interpret and convert K8s events back to error dict format
    interpreted = interpret_events(k8s_events)
    result: list[dict[str, str]] = []

    for event in interpreted:
        entry: dict[str, str] = {
            "resource": event.resource,
            "message": event.title,
            "suggestion": event.suggestion,
            "severity": event.severity.value,
        }
        if event.count > 1:
            entry["count"] = str(event.count)
        if event.timestamp:
            entry["timestamp"] = event.timestamp
        if event.age:
            entry["age"] = event.age
        result.append(entry)

    # Other errors first, then interpreted events — then suppress symptoms and dedupe
    serving_unique_names = (
        {generate_unique_name(deployment_name, ref) for ref in serving_components if ref}
        if deployment_name and serving_components
        else None
    )
    combined = _suppress_symptoms(other_errors + result, serving_unique_names)
    deduped = _dedupe_cross_source(combined)

    # Flag leftovers from components no longer in the deployment (must run on the
    # full resource name, before it is simplified to a bare component name).
    if deployment_name and component_names:
        valid_unique_names = {f"{deployment_name}-{ref}" for ref in component_names if ref}
        for error in deduped:
            if _is_orphaned_resource(error["resource"], deployment_name, valid_unique_names):
                error["orphaned"] = "true"

    # Simplify resource names to component names
    if deployment_name:
        for error in deduped:
            error["resource"] = _friendly_resource_name(error["resource"], deployment_name)

    return deduped


# Een ComparisonError betekent alleen "ArgoCD kon de gewenste toestand niet bepalen". Dat
# gebeurt in twee heel verschillende fases: bij het OPHALEN van de repository, of bij het
# RENDEREN van de manifesten. De uitleg wees altijd naar het tweede, dus wie een fetch-
# timeout kreeg ging zijn manifesten zitten uitpluizen terwijl daar niets mis mee was
# (amtbz-2m9/productie, 2026-08-18). Deze tabel splitst de repository-fases af; wat er niet
# op matcht is een echte renderfout en houdt de oude uitleg.
_COMPARISON_ERROR_PATTERNS: list[tuple[re.Pattern[str], str, str, EventSeverity]] = [
    (
        # Bewust op git-context matchen en niet op een kaal "failed to fetch": dat staat ook
        # in een Helm-chart die zijn dependency niet kan ophalen, en dat is wel degelijk
        # een renderfout.
        re.compile(
            r"git fetch|unable to checkout git repo|failed to initialize repository resources",
            re.IGNORECASE,
        ),
        "Repository niet bereikbaar",
        "ArgoCD kon de Git-repository niet ophalen; aan je manifesten ligt het dus niet. "
        "Dit gebeurt vooral vlak na een herstart van de repository-server, die dan alle "
        "repositories opnieuw moet binnenhalen. Meestal lost het zichzelf op bij de volgende "
        "sync. Blijft het staan, meld het dan bij het platformteam.",
        EventSeverity.INFORMATIONAL,
    ),
    (
        # Geen ".*" tussen "path" en "does not exist": in een lange renderfout kan dat over
        # een halve melding heen matchen.
        re.compile(r"app path does not exist|path \S* ?does not exist", re.IGNORECASE),
        "Pad bestaat niet in de repository",
        "De ArgoCD-applicatie wijst naar een map die niet (meer) in de repository staat. "
        "Meld dit bij het platformteam: waarschijnlijk is de deployment verwijderd terwijl "
        "de applicatie is blijven staan.",
        EventSeverity.ACTIONABLE,
    ),
    (
        re.compile(r"authentication required|could not read Username|invalid credentials", re.IGNORECASE),
        "Geen toegang tot de repository",
        "ArgoCD mag de Git-repository niet lezen. Controleer of het toegangstoken van het project nog geldig is.",
        EventSeverity.ACTIONABLE,
    ),
    (
        re.compile(r"unknown revision|couldn't find remote ref|revision .* not found", re.IGNORECASE),
        "Revisie niet gevonden",
        "De branch of commit waar de deployment naar wijst bestaat niet in de repository. "
        "Controleer de branchnaam in je projectbestand.",
        EventSeverity.ACTIONABLE,
    ),
]

_COMPARISON_ERROR_DEFAULT = (
    "Configuratiefout (kustomize CMP)",
    "De manifesten konden niet worden gegenereerd of vergeleken. Vaak staan er twee "
    "resources met dezelfde naam in de deployment, of is een manifest ongeldig.",
    EventSeverity.ACTIONABLE,
)


def _comparison_error_translation(message: str) -> tuple[str, str, EventSeverity]:
    """Kies label, uitleg en ernst voor een ComparisonError op basis van de ruwe tekst.

    Returns:
        (resource-label, suggestie, ernst)
    """
    for pattern, label, suggestion, severity in _COMPARISON_ERROR_PATTERNS:
        if pattern.search(message):
            return label, suggestion, severity
    return _COMPARISON_ERROR_DEFAULT


def _enrich_argocd_error(error: dict[str, str]) -> dict[str, str]:
    """Enrich an ArgoCD error with pattern-matched translation if possible."""
    message = error.get("message", "")
    # A ComparisonError is ArgoCD saying it could not generate or compare the manifests -
    # the render/CMP failure this whole feature is about. Give it a readable heading with the
    # raw kustomize/CMP message underneath, so it does not read as a cryptic condition name.
    if error.get("resource") == "ComparisonError":
        enriched = dict(error)
        label, suggestion, severity = _comparison_error_translation(message)
        # No "/" in the label: interpret_argocd_errors later strips a Kind/ prefix on "/".
        enriched["resource"] = label
        # ArgoCD dumps the whole failed CMP command (kBs of script source) into the message;
        # show only the meaningful tail, keep the raw under "Origineel bericht".
        condensed = condense_render_error(message)
        enriched["message"] = condensed
        if condensed != message:
            enriched["original_message"] = message
        enriched["suggestion"] = suggestion
        enriched["severity"] = severity.value
        return enriched
    if _IMAGE_PULL_RE.search(message):
        title, suggestion, severity = _image_pull_translation(message)
        enriched = dict(error)
        enriched["message"] = title
        enriched["suggestion"] = suggestion
        enriched["severity"] = severity.value
        enriched["original_message"] = message
        return enriched
    for pattern, title, suggestion, severity in _MESSAGE_PATTERNS:
        if pattern.search(message):
            enriched = dict(error)
            enriched["message"] = title
            enriched["suggestion"] = suggestion
            enriched["severity"] = severity.value
            enriched["original_message"] = message
            return enriched
    return error


# --- Componentfouten groeperen voor de voortgangspagina --------------------------------
#
# Zestien kapotte componenten leverden zestien losse meldingen op, elk met dezelfde
# suggestie van ruim vierhonderd tekens eronder. Dat leest niet als zes problemen maar als
# een muur, en juist het verband -- welke componenten hebben HETZELFDE probleem -- was
# nergens te zien.
#
# Groeperen gebeurt op de vertaalde titel, niet op de rauwe message: twee componenten die
# allebei hun image niet kunnen ophalen zijn voor de lezer één ding, ook al verschilt de
# registry-tekst per image. De suggestie is per titel praktisch identiek (dezelfde raad,
# een ander voorbeeld-image), dus daarvan blijft er één staan.


def group_component_failures(failures: list[dict] | None) -> list[dict]:
    """Vat een platte lijst componentfouten samen tot één groep per soort probleem.

    Elke groep draagt de vertaalde titel, de ernst, de betrokken componenten (per
    deployment) en één suggestie. De volgorde van binnenkomst blijft behouden, zodat de
    lijst niet van run tot run wisselt.

    Returns:
        Een lijst groepen met ``title``, ``severity``, ``failure_type``, ``suggestion``,
        ``suggestion_is_example`` (waar als de leden verschillende suggesties hadden),
        ``component_count``, ``deployments`` (lijst van ``{"name", "components"}``) en
        ``members`` (de oorspronkelijke fouten, voor de logboeklinks per component).
    """
    if not failures:
        return []

    groups: dict[tuple[str, str], dict] = {}
    for failure in failures:
        title = failure.get("title") or failure.get("message") or "Onbekend probleem"
        failure_type = failure.get("failure_type") or ""
        key = (title, failure_type)
        group = groups.get(key)
        if group is None:
            group = {
                "title": title,
                "failure_type": failure_type,
                # Een groep is zo ernstig als zijn ernstigste lid: één component waar de
                # gebruiker iets aan moet doen maakt de hele groep actionable.
                "severity": failure.get("severity") or EventSeverity.ACTIONABLE.value,
                "suggestion": failure.get("suggestion") or "",
                # De suggesties binnen een groep verschillen als ze een image noemen: elk
                # component heeft zijn eigen image. Er wordt er EEN getoond -- twaalf keer
                # dezelfde raad met een ander voorbeeld erin is de muur die we juist
                # weghalen -- maar dan moet er wel bij staan dat het een voorbeeld is.
                "suggestions_seen": set(),
                "deployments": {},
                "members": [],
            }
            groups[key] = group
        elif failure.get("severity") == EventSeverity.ACTIONABLE.value:
            group["severity"] = EventSeverity.ACTIONABLE.value

        if failure.get("suggestion"):
            group["suggestions_seen"].add(failure["suggestion"])

        deployment = failure.get("deployment") or ""
        component = failure.get("component") or ""
        components = group["deployments"].setdefault(deployment, [])
        if component and component not in components:
            components.append(component)
        group["members"].append(failure)

    result = []
    for group in groups.values():
        deployments = [{"name": name, "components": comps} for name, comps in group["deployments"].items()]
        result.append(
            {
                "title": group["title"],
                "failure_type": group["failure_type"],
                "severity": group["severity"],
                "suggestion": group["suggestion"],
                "suggestion_is_example": len(group["suggestions_seen"]) > 1,
                "deployments": deployments,
                "component_count": sum(len(d["components"]) for d in deployments),
                "members": group["members"],
            }
        )
    return result
