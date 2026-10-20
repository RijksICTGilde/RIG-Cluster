"""De naampatronen die de rand van de API en de dienstconfig delen.

Een patroon hoort op een plek te staan: zodra dezelfde regel op twee plekken als regex
staat, betekent hij vroeg of laat twee dingen. ``DNS1123_LABEL`` stond in het pakket
``cross_domain_access``, dat het als eerste nodig had; het is hierheen verhuisd toen de
terugzet-endpunten hetzelfde patroon nodig bleken te hebben (RC-339).

De patronen zijn geankerd, zodat ze zowel in ``pydantic``-``pattern=`` als in
``re.match`` dezelfde betekenis hebben.
"""

#: Een DNS-1123-label: wat Kubernetes toestaat als naam van een namespace of een label.
DNS1123_LABEL = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"

#: Een DNS-1123-subdomein: wat Kubernetes toestaat als naam van een object (PVC,
#: StorageClass). Een of meer labels, gescheiden door punten.
DNS1123_SUBDOMAIN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"

#: Een Kubernetes-hoeveelheid zoals 500Mi, 1Gi of 262144k. Dezelfde vorm die
#: ``parse_k8s_memory_to_mi`` ontleedt; hier als patroon, omdat de rand een waarde
#: moet kunnen weigeren voordat er iets mee gebeurt.
KUBERNETES_QUANTITY = r"^\d+(\.\d+)?(Ki|Mi|Gi|Ti|Pi|k|M|G|T|P)?$"

#: Een PostgreSQL-identifier, voor een schema- of gebruikersnaam.
#: ``PostgresConnector._validate_identifier`` leest hem hier.
POSTGRES_IDENTIFIER = r"^[a-zA-Z_][a-zA-Z0-9_$]*$"

#: Een S3-bucketnaam volgens de regels van AWS: 3 tot 63 tekens, kleine letters, cijfers,
#: streepjes en punten, beginnend en eindigend op een letter of cijfer.
S3_BUCKET_NAME = r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"

#: Een hostnaam of IP-adres, eventueel met poort. Hoofdletters zijn toegestaan omdat een
#: DNS-naam case-insensitive is; de poort omdat een doelhost zo wordt aangeleverd
#: (``postgresql.elsewhere:5432``), naast het aparte poortveld.
HOST_WITH_OPTIONAL_PORT = (
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*(:\d{1,5})?$"
)

#: Een endpoint als URL of als host:poort, zoals een MinIO-endpoint wordt opgegeven.
ENDPOINT_URL_OR_HOST_PORT = (
    r"^(https?://)?[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?$"
)

#: Een kopia-snapshot-id: een hexadecimale sleutel met een letterprefix (``k1f2a...``).
#: Ruimer gezet dan dat, want de id komt uit kopia en de rand hoort hem niet nauwer te
#: beschrijven dan nodig is om tekst met regeleindes en quotes te weigeren.
KOPIA_SNAPSHOT_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
