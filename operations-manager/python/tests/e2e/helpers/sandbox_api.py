"""
Sandbox API helpers: geef een project een API-token en drijf de REST-endpoints aan.

De endpoints hieronder legitimeren zich met ``X-API-Key``. Sinds schemaversie 2.12 is dat
geen ``config.api-key`` meer die van de detailpagina te lezen is, maar een TOKEN van het
service account ``project-api``, waarvan alleen de hash in het projectbestand staat. Een
bestaand token is dus niet op te halen: de weg is er een UITGEVEN, en dat doet
:func:`geef_projecttoken` over het tabblad Team met de sessiecookie van de e2e-gebruiker.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
import time
from urllib.parse import urlparse

import httpx
from itsdangerous import TimestampSigner
from tests.e2e.helpers import cluster

logger = logging.getLogger(__name__)


#: De namen uit het portaal die de uitgifte hieronder nodig heeft: het service account dat
#: de API-toegang van een project draagt (``opi/services/schema_migration.py``) en de
#: CSRF-cookie en -header (``opi/utils/csrf.py``). Letterlijk en niet geimporteerd, want dit
#: bestand moet importeerbaar blijven ZONDER de OPI-config: ``scripts/sandbox_project_tool.py``
#: praat tegen een sandbox op afstand en heeft geen platformsleutel, en een import van
#: ``opi.core.config`` beeindigt daar het proces. Dat ze gelijk blijven staat in
#: ``tests/test_e2e_helpers.py``.
PROJECT_API_ACCOUNT = "project-api"
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"

#: De sessiesleutel met het moment van de laatste step-up (``opi/core/step_up.py``). Een
#: token uitgeven vraagt een verse (RC-387), en wie zijn eigen sessie ondertekent zet hem
#: er dus bij: zonder stempel antwoordt het portaal met een 403 in plaats van een token.
STEP_UP_SESSION_KEY = "step_up_at"

#: Hoe een uitgegeven token eruitziet: het voorvoegsel plus de willekeurige waarde.
_TOKEN_VORM = re.compile(r'value="(zad_[A-Za-z0-9_-]+)"')


def make_session_cookie(data: dict, *, secret: str, max_age_seconds: int = 28800) -> str:
    """Maak een server-side sessie op de SANDBOX en geef de ondertekende cookie terug.

    Sinds RC-390 draagt de cookie alleen een ondertekend id; de inhoud woont in de tabel
    ``sessions`` van de rig-db. Deze helper schrijft die rij (via een psql-pod, zoals
    ``cluster.run_psql`` dat doet) en tekent het id met de SECRET_KEY van het cluster.

    Een ``user`` in ``data`` krijgt een vers ``login_at``: de guard weigert een sessie
    zonder inlogtijdstip (opi/core/session_guard.py).
    """
    if gebruiker := data.get("user"):
        data = {**data, "user": {**gebruiker, "login_at": time.time()}}
    session_id = secrets.token_urlsafe(32)
    # SQL-literal-escaping: de data gaat als een letterlijke string de psql-pod in.
    data_sql = json.dumps(data).replace("'", "''")
    password = cluster.secret_values("rig-system", "postgres-admin-credentials")["password"]
    code, uitvoer = cluster.run_psql(
        "INSERT INTO sessions (id, data, created_at, expires_at, last_seen) "
        f"VALUES ('{session_id}', '{data_sql}'::jsonb, now(), "
        f"now() + interval '{max_age_seconds} seconds', now()) "
        "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, expires_at = EXCLUDED.expires_at, "
        "last_seen = now()",
        host="rig-db-rw",
        user="postgres",
        database="operations_manager",
        password=password,
    )
    if code != 0:
        raise AssertionError(f"sessie-rij aanmaken op de sandbox mislukte (exit {code}): {uitvoer[:300]}")
    return TimestampSigner(secret).sign(session_id.encode()).decode()


def geef_projecttoken(
    base_url: str,
    project_name: str,
    *,
    sessie_cookie: str,
    lid: str = PROJECT_API_ACCOUNT,
    tokennaam: str | None = None,
    dagen: int = 1,
    verify_ssl: bool = True,
    timeout: float = 60.0,
) -> str:
    """Geef een nieuw API-token uit voor ``lid`` en geef de waarde terug.

    Zonder browser: het tabblad Team is een formulier, dus een sessiecookie, de
    ``csrf_token``-cookie terug in de header en een ``Origin`` op dezelfde host zijn alles
    wat het nodig heeft (dezelfde drie eisen als ``weesprojecten.start_verwijdering``).

    De uitgegeven waarde staat EEN keer op de pagina waar de 303 naartoe gaat, dus hij
    komt hier uit het antwoord op de POST nadat de omleiding gevolgd is. Wie hem kwijt is
    geeft een nieuwe uit; teruglezen kan niemand, ook de uitgever niet.

    De sessiecookie moet een verse step-up dragen (:data:`STEP_UP_SESSION_KEY`): uitgeven
    vraagt er een, en zonder stempel antwoordt het portaal met een 403.
    """
    basis = base_url.rstrip("/")
    host = urlparse(basis).hostname or ""
    # Met een willekeurig staartje: twee pogingen binnen dezelfde seconde zouden op
    # dezelfde naam botsen, en dan weigert de dienst de tweede.
    tokennaam = tokennaam or f"e2e-{int(time.time())}-{secrets.token_hex(3)}"
    # De client volgt GEEN omleidingen: een sessie die niet aanvaard wordt loopt naar de
    # inlogpagina van Keycloak, en die antwoordt zelf 200. Een gevolgde omleiding zou hier
    # dus als een geslaagd tabblad lezen. De POST volgt zijn 303 wel, want daar staat de
    # waarde.
    with httpx.Client(verify=verify_ssl, timeout=timeout, follow_redirects=False) as client:
        client.cookies.set("session", sessie_cookie, domain=host)
        antwoord = client.get(f"{basis}/projects/{project_name}/team")
        if antwoord.status_code != 200:
            raise AssertionError(
                f"het tabblad Team van '{project_name}' gaf HTTP {antwoord.status_code} "
                f"({antwoord.headers.get('location', 'geen omleiding')}); klopt de "
                f"sessiecookie met de SECRET_KEY van het cluster?"
            )
        csrf = client.cookies.get(CSRF_COOKIE_NAME)
        if not csrf:
            raise AssertionError(f"geen {CSRF_COOKIE_NAME}-cookie na GET van het tabblad Team van '{project_name}'")
        if lid == PROJECT_API_ACCOUNT:
            # Een project dat na RC-322 nieuw is, heeft dit service account niet: alleen de
            # migratie van 2.12 maakte het voor een bestaande projectsleutel. Bestaat het al,
            # dan weigert de dienst het toevoegen en loopt dit gewoon door naar het uitgeven.
            client.post(
                f"{basis}/projects/{project_name}/leden/toevoegen",
                data={"naam": lid, "rol": "admin"},
                headers={CSRF_HEADER_NAME: csrf, "Origin": basis},
            )
        antwoord = client.post(
            f"{basis}/projects/{project_name}/leden/{lid}/tokens",
            data={"tokennaam": tokennaam, "dagen": str(dagen)},
            headers={CSRF_HEADER_NAME: csrf, "Origin": basis},
            follow_redirects=True,
        )
    if antwoord.status_code != 200:
        raise AssertionError(
            f"het uitgeven van een token voor '{lid}' op '{project_name}' gaf HTTP "
            f"{antwoord.status_code}: {antwoord.text[:300]}"
        )
    treffer = _TOKEN_VORM.search(antwoord.text)
    if not treffer:
        # De pagina draagt in dit geval de weigering van de dienst in een alert.
        raise AssertionError(
            f"geen uitgegeven token op het tabblad Team van '{project_name}' na het aanmaken; "
            f"de pagina zei: {_melding(antwoord.text)}"
        )
    return treffer.group(1)


def _melding(html: str) -> str:
    """De weigering die de pagina draagt, voor een leesbare mislukking."""
    treffer = re.search(r'type="error" heading="([^"]+)">\s*<c-paragraph>([^<]*)', html)
    if treffer:
        return f"{treffer.group(1)}: {treffer.group(2).strip()}"
    kop = re.findall(r'heading="([^"]+)"', html)
    return kop[-1] if kop else "niets bruikbaars"


def start_task(
    base_url: str,
    method: str,
    path: str,
    api_key: str,
    body: dict,
    *,
    verify_ssl: bool = True,
) -> str:
    """Fire an async v1/v2 API request and return its task id WITHOUT waiting.

    Used by the real-life suite to start mutations on several projects at once;
    the server processes the tasks concurrently while the test continues (e.g.
    driving UI edits on other projects). Pair with wait_for_task().
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        response = client.request(method, f"{base}{path}", json=body, headers=headers)
        assert response.status_code == 202, (
            f"Expected 202 from {method} {path}, got {response.status_code}: {response.text}"
        )
        location = response.headers.get("Location")
        task_id = location.rsplit("/", 1)[-1] if location else response.json().get("task_id")
        assert task_id, f"No task id returned from {method} {path}: {response.text}"
        return task_id


def wait_for_task(
    base_url: str,
    task_id: str,
    api_key: str,
    *,
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> dict:
    """Poll a task started with start_task() until it completes successfully."""
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        return _wait_for_task(client, base, task_id, headers, timeout=timeout)


def task_outcome(
    base_url: str,
    task_id: str,
    api_key: str,
    *,
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> tuple[str, str | None]:
    """Poll a task and report its outcome without asserting.

    Returns ("completed", None), ("superseded", None), ("failed", reason) or
    ("running", None) when the task has not reached a terminal state before the
    timeout. "superseded" is a benign outcome: a newer task whose scope covers
    this one took over, so this task's ArgoCD wait was abandoned by design. It is
    recorded as a completed task carrying result.status == "superseded".
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    deadline = time.monotonic() + timeout
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        while time.monotonic() < deadline:
            response = client.get(f"{base}/api/tasks/{task_id}", headers=headers)
            if response.status_code == 200:
                task = response.json()
                status = task.get("status")
                if status != "completed":
                    return "failed", f"task status '{status}': {task}"
                if (task.get("result") or {}).get("status") == "superseded":
                    return "superseded", None
                try:
                    _assert_no_subtask_failure(task_id, task)
                except AssertionError as exc:
                    return "failed", str(exc)
                return "completed", None
            if response.status_code != 202:
                return "failed", f"unexpected task poll status {response.status_code}: {response.text}"
            time.sleep(3.0)
    return "running", None


def add_component(
    base_url: str,
    project_name: str,
    api_key: str,
    *,
    component_name: str,
    image: str,
    deployment_names: list[str],
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> dict:
    """Add one component via the v2 async API and wait for the task to finish.

    Returns the terminal task response dict. Raises AssertionError if the task
    does not reach 'completed'.
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    body = {
        "name": component_name,
        "image": image,
        "deployment_names": deployment_names,
    }
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        response = client.post(
            f"{base}/api/v2/projects/{project_name}/components",
            json=body,
            headers=headers,
        )
        assert response.status_code == 202, (
            f"Expected 202 from component-add, got {response.status_code}: {response.text}"
        )
        location = response.headers.get("Location")
        task_id = location.rsplit("/", 1)[-1] if location else response.json().get("task_id")
        assert task_id, f"No task id returned from component-add: {response.text}"

        return _wait_for_task(client, base, task_id, headers, timeout=timeout)


def delete_component(
    base_url: str,
    project_name: str,
    api_key: str,
    *,
    component_name: str,
    confirm_in_use: bool = False,
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> tuple[int, dict]:
    """Delete one component via the v2 async API.

    Returns (status_code, body). On 202 the task is waited out and the terminal task
    response is the body; on any other status the response body is returned unchanged --
    a 409 (still in use) and a 404 (no such component) are answers this endpoint gives on
    purpose, so the caller decides what they mean rather than this helper asserting.
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    params = {"confirm_in_use": "true"} if confirm_in_use else None
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        response = client.delete(
            f"{base}/api/v2/projects/{project_name}/components/{component_name}",
            params=params,
            headers=headers,
        )
        if response.status_code != 202:
            return response.status_code, response.json()

        location = response.headers.get("Location")
        task_id = location.rsplit("/", 1)[-1] if location else response.json().get("task_id")
        assert task_id, f"No task id returned from component-delete: {response.text}"
        return 202, _wait_for_task(client, base, task_id, headers, timeout=timeout)


def delete_project_via_api(
    base_url: str,
    project_name: str,
    api_key: str,
    *,
    force: bool = True,
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> None:
    """Safety-net deletion via the v1 API (DELETE /api/projects/{name}).

    Best-effort: swallows connection errors so it never breaks fixture teardown.
    Waits for the async delete task to finish when a task id is returned.

    Wat het NIET meer doet is zwijgen: een opruiming die niet lukte wordt gelogd.
    Na een groene run bleef er een testproject op de sandbox staan zonder dat
    ergens te zien was dat de teardown was mislukt, en zulke projecten stapelen
    zich over runs op.
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    body = {"confirmDeletion": True, "force": force}
    accepted = False
    with contextlib.suppress(httpx.HTTPError), httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        response = client.request(
            "DELETE",
            f"{base}/api/projects/{project_name}",
            json=body,
            headers=headers,
        )
        accepted = response.is_success
        if response.status_code == 202:
            location = response.headers.get("Location")
            task_id = location.rsplit("/", 1)[-1] if location else None
            if task_id:
                try:
                    _wait_for_task(client, base, task_id, headers, timeout=timeout)
                except AssertionError as fout:
                    logger.warning("Delete task %s for '%s' did not succeed: %s", task_id, project_name, fout)
        elif not accepted:
            logger.warning(
                "Delete of '%s' was refused with HTTP %d; als het project nog bestaat blijft het "
                "op de sandbox staan (401 betekent meestal dat de test het zelf al opruimde)",
                project_name,
                response.status_code,
            )

    # Belt-and-suspenders: the project delete only drops the namespace once the
    # ArgoCD app deletion is confirmed, which frequently times out on a busy
    # sandbox and leaves an empty namespace + dangling app that pile up across
    # runs. Reclaim them directly so test projects never accumulate.
    #
    # ONLY when OPI actually accepted the delete. A 401/403/404 means we never had
    # the authority to remove this project (an empty api_key after a failed setup
    # fixture is the usual cause), and tearing the ArgoCD app and namespace out
    # from under it then leaves the WORST state there is: gone from the cluster,
    # still present in ZAD and in the projects repo. That half-state is not
    # self-healing and the next run inherits it.
    if accepted:
        cluster.force_cleanup_project(project_name)


def _wait_for_task(
    client: httpx.Client,
    base: str,
    task_id: str,
    headers: dict,
    *,
    timeout: float,
    interval: float = 3.0,
) -> dict:
    """Poll GET /api/tasks/{task_id} until terminal (200), then assert real success.

    The task envelope reports status 'completed' even when the underlying operation
    failed - the actual outcome is in `result.status` and in the subtasks. We inspect
    both so a failed operation surfaces its real error instead of a vague downstream
    assertion.
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"{base}/api/tasks/{task_id}", headers=headers)
        # 202 = still running, 200 = terminal (completed/failed/cancelled)
        if resp.status_code == 200:
            last = resp.json()
            status = last.get("status")
            assert status == "completed", f"Task {task_id} ended with status '{status}': {last}"
            _assert_no_subtask_failure(task_id, last)
            return last
        if resp.status_code not in (200, 202):
            raise AssertionError(f"Unexpected task poll status {resp.status_code}: {resp.text}")
        last = resp.json()
        time.sleep(interval)
    raise AssertionError(f"Task {task_id} did not complete within {timeout}s (last: {last})")


def _assert_no_subtask_failure(task_id: str, task: dict) -> None:
    """Raise with the real error if the task's result or any subtask failed."""
    result = task.get("result") or {}
    if result.get("status") == "failed":
        error = task.get("error_message") or result.get("error") or "unknown error"
        raise AssertionError(f"Task {task_id} operation failed: {error} (result={result})")
    for sub in task.get("subtasks") or []:
        if sub.get("status") == "failed":
            raise AssertionError(
                f"Task {task_id} subtask '{sub.get('name')}' failed: {sub.get('error') or 'unknown error'}"
            )
