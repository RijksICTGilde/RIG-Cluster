"""De token-bucket limiter en de grenzen die OPI ermee legt.

Twee lagen die elkaar niet vervangen: een strenge faalteller op de sleutelpaden, zodat
het raden van een X-API-Key vastloopt voordat de vergelijking draait, en een grove
bovengrens per client op de API als geheel, zodat een stortvloed OPI niet platlegt.
Beide weigeren meteen met een 429 en een ``Retry-After``: een vertraging zou juist OPI
ophouden, want een opengehouden verzoek kost een slot in de event-loop.

Het is de binnenste laag. Een ingress kan een grove, volumetrische grens per IP leggen
voordat een verzoek Python bereikt, maar kent onze authenticatie niet en kan dus geen
grens per sleutel of project leggen. De tellers staan in het geheugen van dit proces,
wat klopt zolang OPI een proces per cluster is.
"""

import hashlib
import logging
import math
import threading
import time
from collections import defaultdict
from typing import NoReturn

from fastapi import HTTPException
from opi.core.config import settings
from opi.utils.client_ip import client_ip
from opi.utils.logging_redact import fingerprint_for_log
from starlette.requests import Request  # noqa: TC002 -- FastAPI needs Request at runtime

logger = logging.getLogger(__name__)


def _ip_of(client_id: str) -> str:
    """Waar de tweede emmer op telt: het eerste veld van het client-id."""
    return client_id.split("|")[0] if "|" in client_id else client_id


def _wait_for_one(available: float, rate: float) -> float:
    """Hoe lang het duurt tot een emmer met ``available`` tokens er een heeft."""
    if available >= 1:
        return 0.0
    return (1 - available) / rate if rate > 0 else math.inf


def _refill_seconds(burst: int, rate: float) -> float:
    """Hoe lang een lege emmer erover doet om weer op zijn volle burst te staan."""
    return burst / rate if rate > 0 else math.inf


class IPRateLimiter:
    """Twee-lagen limiter: per client-id en per IP, met noodopruiming."""

    # Maximale entries voordat opruiming nodig is
    MAX_TRACKED_CLIENTS = 10000
    # Drempel voor vroege opruiming, als geheugengroeien dreigt
    CLEANUP_THRESHOLD = 8000
    # Minimale leeftijd voor noodopruiming
    EMERGENCY_PURGE_MIN_AGE_SECONDS = 60
    # Minimale tijd tussen twee noodopruimingen: zonder dit draait de scan per verzoek
    EMERGENCY_CLEANUP_INTERVAL_SECONDS = 10.0
    # Maximale nieuwe client-ids per minuut, detectie van identifier-flooding
    MAX_NEW_CLIENTS_PER_MINUTE = 100

    def __init__(
        self,
        requests_per_minute: float = 30,
        burst: int = 10,
        cleanup_interval: float = 60.0,
        per_ip_requests_per_minute: float | None = None,
        max_new_clients_per_minute: int | None = MAX_NEW_CLIENTS_PER_MINUTE,
    ):
        # float, niet int: een grens per UUR is minder dan een verzoek per minuut
        # (de uitnodigingsroutes), en de rekensom hieronder deelt al door 60.0.
        self.rate = requests_per_minute / 60.0  # requests per second
        self.burst = burst
        self._tokens: dict[str, float] = defaultdict(lambda: float(burst))
        self._last_update: dict[str, float] = defaultdict(time.monotonic)
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.monotonic()
        # 0.0 is "lang geleden": de eerste noodopruiming mag meteen.
        self._last_emergency_cleanup = 0.0
        # Thread-veiligheid voor gelijktijdige async-requests.
        self._lock = threading.Lock()

        # Tweede emmer per IP: kan niet omzeild worden door vingerafdruk te wisselen.
        # Default: 3x de normale snelheid, om gebruikers met meerdere sessies toe te laten.
        self._per_ip_rate = (per_ip_requests_per_minute or (requests_per_minute * 3)) / 60.0
        self._per_ip_burst = burst * 3
        self._ip_tokens: dict[str, float] = defaultdict(lambda: float(self._per_ip_burst))
        self._ip_last_update: dict[str, float] = defaultdict(time.monotonic)

        # Een bucket mag niet worden opgeruimd voordat hij zichzelf toch had bijgevuld,
        # anders houdt een per-uur grens geen uur (zie _min_retention_seconds).
        self._min_retention_seconds = max(
            _refill_seconds(burst, self.rate),
            _refill_seconds(self._per_ip_burst, self._per_ip_rate),
        )

        # Behoud van tijd van nieuwe clients: detectie van identifier-flooding.
        self._new_client_timestamps: list[float] = []
        # None zet die teller uit. Voor een grens die voor ELK verzoek geldt is hij een
        # weigering van iedere nieuwe bezoeker zodra iemand client-ids verzint.
        self._max_new_clients_per_minute = max_new_clients_per_minute

    @staticmethod
    def get_client_fingerprint(request: Request) -> str:
        """Hash van User-Agent en headers: een tweede factor naast het IP in het client-id."""
        user_agent = request.headers.get("user-agent", "")
        accept_language = request.headers.get("accept-language", "")
        accept_encoding = request.headers.get("accept-encoding", "")

        fingerprint_data = f"{user_agent}|{accept_language}|{accept_encoding}"
        return hashlib.sha256(fingerprint_data.encode()).hexdigest()[:16]

    @classmethod
    def get_client_identifier(cls, request: Request) -> str:
        """Client-id: IP | fingerprint | sessie-id.

        Het IP komt van ``client_ip``, de ene plek die het bepaalt: een aanroeper die zijn
        ``X-Forwarded-For`` verzint mag geen eigen emmer kunnen kiezen.
        """
        ip = client_ip(request)
        fingerprint = cls.get_client_fingerprint(request)

        session_id = ""
        # AssertionError: request.session buiten SessionMiddleware valt zo.
        try:
            session = request.session
        except AssertionError, AttributeError:
            session = None
        if session:
            session_id = session.get("_session_id", "")[:32]

        # Combineer tot ip|fingerprint|sessie (sessie leeg zonder authenticatie).
        return f"{ip}|{fingerprint}|{session_id}"

    def _check_new_client_rate_unlocked(self) -> bool:
        """Te veel nieuwe client-ids aangemaakt (onder de lock)."""
        if self._max_new_clients_per_minute is None:
            return True

        now = time.monotonic()
        # Verwijder tijdstempels ouder dan 1 minuut.
        self._new_client_timestamps = [ts for ts in self._new_client_timestamps if now - ts < 60]

        if len(self._new_client_timestamps) >= self._max_new_clients_per_minute:
            logger.warning(
                f"New client rate limit exceeded: {len(self._new_client_timestamps)} new clients in last minute"
            )
            return False

        return True

    def _check_per_ip_limit_unlocked(self, ip: str) -> bool:
        """De tweede emmer, per IP (onder de lock)."""
        now = time.monotonic()
        elapsed = now - self._ip_last_update[ip]
        self._ip_last_update[ip] = now

        # Voeg tokens toe op basis van verstreken tijd.
        self._ip_tokens[ip] = min(self._per_ip_burst, self._ip_tokens[ip] + elapsed * self._per_ip_rate)

        if self._ip_tokens[ip] >= 1:
            self._ip_tokens[ip] -= 1
            return True
        return False

    def is_allowed(self, client_id: str) -> bool:
        """Controleert beide emmers en ruimt oude entries op. Thread-safe."""
        with self._lock:
            now = time.monotonic()

            # Pak het IP uit het client-id voor de tweede emmer.
            ip = _ip_of(client_id)

            # Nieuw client: check tegen flooding. Alleen bij nieuwe clients telt mee.
            is_new_client = client_id not in self._last_update
            if is_new_client:
                if not self._check_new_client_rate_unlocked():
                    return False
                # Een nieuwe client heeft zijn volle burst, dus alleen de emmer per IP kan
                # hem nog weigeren. Die eerst LEZEN: een weigering die toch een emmer
                # achterlaat laat de aanvaller bepalen wat de weigering kost.
                if self._ip_available_unlocked(ip, now) < 1:
                    logger.debug(f"Per-IP rate limit exceeded for new client on {ip}")
                    return False
                self._new_client_timestamps.append(now)
                self._evict_overflow_unlocked()

            elapsed = now - self._last_update[client_id]
            self._last_update[client_id] = now

            # Voeg tokens toe op basis van verstreken tijd.
            self._tokens[client_id] = min(self.burst, self._tokens[client_id] + elapsed * self.rate)

            # Periodieke opruiming op basis van tijd.
            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup_old_entries_unlocked()
                self._last_cleanup = now
            # Noodopruiming als geheugen dreigt vol te lopen, hoogstens een keer per
            # interval: per verzoek is het twee O(n)-scans onder de lock, en dan bepaalt
            # een stortvloed wat elk verzoek van iedere client kost.
            elif (
                self._tracked_count_unlocked() > self.CLEANUP_THRESHOLD
                and now - self._last_emergency_cleanup > self.EMERGENCY_CLEANUP_INTERVAL_SECONDS
            ):
                self._last_emergency_cleanup = now
                self._cleanup_old_entries_unlocked(max_age_seconds=300)
                # Nog steeds vol na opruiming: meer agressieve purge.
                if self._tracked_count_unlocked() > self.MAX_TRACKED_CLIENTS:
                    self._gradual_purge_unlocked()

            # Eerste controle: emmer per client-id.
            if self._tokens[client_id] < 1:
                return False

            # Tweede controle: emmer per IP, kan niet omzeild worden via vingerafdruk.
            if not self._check_per_ip_limit_unlocked(ip):
                logger.debug(f"Per-IP rate limit exceeded for {ip}")
                return False

            self._tokens[client_id] -= 1
            return True

    def _tracked_count_unlocked(self) -> int:
        """Het aantal emmers dat de opruiming moet bewaken: beide soorten tellen mee.

        Alleen op ``_tokens`` kijken laat de emmers per IP ongelimiteerd doorgroeien, en
        die maakt elk verzoek aan.
        """
        return max(len(self._tokens), len(self._ip_tokens))

    def has_tokens(self, client_id: str) -> bool:
        """Of deze client nu een verzoek zou mogen doen, zonder er een token voor te nemen.

        Voor een grens die alleen op falen aftelt: de controle mag een geslaagde
        authenticatie niet uit het budget van de volgende mislukte poging halen.
        """
        with self._lock:
            now = time.monotonic()
            ip = _ip_of(client_id)
            return self._available_unlocked(client_id, now) >= 1 and self._ip_available_unlocked(ip, now) >= 1

    def seconds_until_token(self, client_id: str) -> float:
        """Hoe lang het duurt voor deze client weer een token heeft, in beide emmers."""
        with self._lock:
            now = time.monotonic()
            ip = _ip_of(client_id)
            return max(
                _wait_for_one(self._available_unlocked(client_id, now), self.rate),
                _wait_for_one(self._ip_available_unlocked(ip, now), self._per_ip_rate),
            )

    def _available_unlocked(self, client_id: str, now: float) -> float:
        """Het tokensaldo van deze client, zonder de emmer aan te maken of af te tellen.

        Een onbekende client heeft zijn volle burst: alleen aftellen mag een emmer
        aanmaken, anders is lezen een manier om het geheugen te laten groeien.
        """
        if client_id not in self._last_update:
            return float(self.burst)
        return min(self.burst, self._tokens[client_id] + (now - self._last_update[client_id]) * self.rate)

    def _ip_available_unlocked(self, ip: str, now: float) -> float:
        """Het tokensaldo van de tweede emmer, die per IP telt. Leest, telt niet af."""
        if ip not in self._ip_last_update:
            return float(self._per_ip_burst)
        return min(self._per_ip_burst, self._ip_tokens[ip] + (now - self._ip_last_update[ip]) * self._per_ip_rate)

    def _evict_overflow_unlocked(self) -> None:
        """Houdt beide emmerverzamelingen onder ``MAX_TRACKED_CLIENTS``, in O(1) per entry.

        Verdringt op aanmaakvolgorde (een dict bewaart die), zodat de bovengrens hard is
        ook als er niets oud genoeg is om op te ruimen. Verdringen geeft een client zijn
        volle burst terug; de emmer per IP blijft de backstop, en het adres daarin is niet
        te kiezen (``client_ip`` telt hops van rechts).
        """
        while len(self._last_update) >= self.MAX_TRACKED_CLIENTS:
            oudste = next(iter(self._last_update))
            del self._last_update[oudste]
            self._tokens.pop(oudste, None)
        while len(self._ip_last_update) >= self.MAX_TRACKED_CLIENTS:
            oudste_ip = next(iter(self._ip_last_update))
            del self._ip_last_update[oudste_ip]
            self._ip_tokens.pop(oudste_ip, None)

    def cleanup_old_entries(self, max_age_seconds: int = 600) -> None:
        """Verwijdert oude entries, met het vulvenster als ondergrens."""
        with self._lock:
            self._cleanup_old_entries_unlocked(max_age_seconds)

    def _cleanup_old_entries_unlocked(self, max_age_seconds: int = 600) -> None:
        """Ruimt op onder de lock, met vulvenster als ondergrens."""
        now = time.monotonic()
        max_age = max(max_age_seconds, self._min_retention_seconds)
        to_remove = [ip for ip, last in self._last_update.items() if now - last > max_age]
        for ip in to_remove:
            del self._tokens[ip]
            del self._last_update[ip]

        # Opruiming ook voor de emmers per IP.
        ip_to_remove = [ip for ip, last in self._ip_last_update.items() if now - last > max_age]
        for ip in ip_to_remove:
            del self._ip_tokens[ip]
            del self._ip_last_update[ip]

        if to_remove or ip_to_remove:
            logger.debug(
                f"Rate limiter cleanup: removed {len(to_remove)} client entries, {len(ip_to_remove)} IP entries"
            )

    def _gradual_purge_unlocked(self) -> None:
        """Noodopruiming: verwijdert alleen entries ouder dan de minimale leeftijd."""
        if not self._last_update and not self._ip_last_update:
            return

        now = time.monotonic()
        min_age = self.EMERGENCY_PURGE_MIN_AGE_SECONDS

        # Verwijder alleen entries ouder dan de minimale leeftijd.
        to_remove = [client_id for client_id, last_update in self._last_update.items() if now - last_update > min_age]

        for client_id in to_remove:
            del self._tokens[client_id]
            del self._last_update[client_id]

        ip_to_remove = [ip for ip, last_update in self._ip_last_update.items() if now - last_update > min_age]
        for ip in ip_to_remove:
            del self._ip_tokens[ip]
            del self._ip_last_update[ip]

        if to_remove or ip_to_remove:
            logger.warning(
                f"Rate limiter gradual purge: removed {len(to_remove)} stale entries and "
                f"{len(ip_to_remove)} stale IP entries (>{min_age}s old)"
            )

        # Nog steeds vol: echte problemen. Verse entries niet verwijderen, die zijn waarschijnlijk legitiem.
        if self._tracked_count_unlocked() > self.MAX_TRACKED_CLIENTS:
            logger.error(
                f"Rate limiter still over limit ({self._tracked_count_unlocked()} entries) after gradual purge. "
                f"Possible DoS attack or configuration issue."
            )


#: De faalteller per client. De emmersleutel is het client-id zoals ``IPRateLimiter`` het
#: zelf maakt, dus het IP staat vooraan en de tweede emmer van de limiter is de backstop
#: tegen een rader die zijn user-agent verwisselt. Omdat die emmer op het IP alleen telt,
#: weigert deze teller pas na een mislukte vergelijking: zie
#: ``refuse_when_key_failures_exhausted``.
AUTH_FAILURE_PER_CLIENT = IPRateLimiter(
    requests_per_minute=settings.AUTH_FAILURE_LIMIT_PER_MINUTE,
    burst=settings.AUTH_FAILURE_LIMIT_PER_MINUTE,
)

#: De faalteller per aangeboden sleutel, voor een rader die wel van client-id wisselt maar
#: dezelfde sleutel blijft proberen. Per sleutel, dus de foute pogingen van een ander
#: drukken niet op een geldige sleutel: die heeft een andere vingerafdruk, en alleen falen
#: telt af. Twee aparte limiters dus, zoals ``opi/api/invite_routes.py`` het al doet; een
#: emmer die beide in een string stopt verschuift stil waarop de tweede emmer telt.
AUTH_FAILURE_PER_KEY = IPRateLimiter(
    requests_per_minute=settings.AUTH_FAILURE_LIMIT_PER_MINUTE,
    burst=settings.AUTH_FAILURE_LIMIT_PER_MINUTE,
)

#: De grove bovengrens op de API als geheel, tegen een stortvloed van verzoeken die wel
#: goed gevormd zijn. Ruim: bedoeld voor de piek, niet voor normaal gebruik. Zonder de
#: nieuwe-clientgrens, want die zou hier elke nieuwe bezoeker weigeren zodra iemand zijn
#: user-agent verwisselt. In plaats daarvan houdt de limiter zelf de kosten vlak: een
#: geweigerd verzoek maakt geen emmer, ``MAX_TRACKED_CLIENTS`` is een harde bovengrens met
#: verdringing, en de opruimscan staat niet in het hete pad.
API_FLOOD_LIMITER = IPRateLimiter(
    requests_per_minute=settings.API_RATE_LIMIT_PER_MINUTE,
    burst=settings.API_RATE_LIMIT_BURST,
    max_new_clients_per_minute=None,
)


def retry_after_seconds(limiter: IPRateLimiter, client_id: str) -> int:
    """De ``Retry-After`` die bij deze geweigerde client hoort, in hele seconden.

    Afgekapt op ``RATE_LIMIT_RETRY_AFTER_MAX_SECONDS``: een emmer die per uur vult geeft
    anders een wachttijd waarin niemand een client nog ziet terugkomen.
    """
    plafond = settings.RATE_LIMIT_RETRY_AFTER_MAX_SECONDS
    # Afkappen vóór de ceil: een grens op 0 geeft math.inf, en math.ceil(inf) valt met een
    # OverflowError in plaats van de 429 waar deze header bij hoort.
    wachttijd = min(limiter.seconds_until_token(client_id), float(plafond))
    return min(max(math.ceil(wachttijd), 1), plafond)


def retry_after_header(limiter: IPRateLimiter, client_id: str) -> dict[str, str]:
    """De headers bij een 429 van deze limiter."""
    return {"Retry-After": str(retry_after_seconds(limiter, client_id))}


def _refuse_as_exhausted(limiter: IPRateLimiter, bucket: str, presented: str | None) -> NoReturn:
    """De 429 die bij een lege faalemmer hoort."""
    logger.warning(f"Te veel mislukte authenticatiepogingen voor sleutel {fingerprint_for_log(presented or '')}")
    raise HTTPException(
        status_code=429,
        detail="Too many failed authentication attempts",
        headers=retry_after_header(limiter, bucket),
    )


def refuse_when_key_failures_exhausted(presented: str | None) -> None:
    """Weigert met 429 zodra de emmer van DEZE aangeboden sleutel leeg is.

    Alleen deze emmer mag vóór de vergelijking weigeren: een ander kan hem niet leeg
    krijgen, want een foute sleutel heeft een andere vingerafdruk en alleen falen telt af.
    De emmer per client weigert pas na een mislukte vergelijking, zodat de houder van een
    geldige sleutel niet buitengesloten wordt door een rader op hetzelfde IP.
    """
    bucket = fingerprint_for_log(presented or "")
    if not AUTH_FAILURE_PER_KEY.has_tokens(bucket):
        _refuse_as_exhausted(AUTH_FAILURE_PER_KEY, bucket, presented)


def refuse_after_auth_failure(request: Request, presented: str | None) -> None:
    """Telt de mislukte poging af in beide emmers en weigert als de emmer per client leeg is.

    Hoort ná de sleutelvergelijking: een geldige sleutel komt hier nooit langs, dus de
    foute pogingen van een ander kosten de houder niets.
    """
    client = IPRateLimiter.get_client_identifier(request)
    AUTH_FAILURE_PER_KEY.is_allowed(fingerprint_for_log(presented or ""))
    if not AUTH_FAILURE_PER_CLIENT.is_allowed(client):
        _refuse_as_exhausted(AUTH_FAILURE_PER_CLIENT, client, presented)


#: Scope-sleutel: de grove grens heeft dit verzoek al afgeteld. ``RateLimitMiddleware``
#: telt het dan niet opnieuw, want tweemaal tellen halveert de grens van een pijplijn.
FLOOD_COUNTED_IN_SCOPE = "opi.flood_limit_counted"


def refuse_when_flood_limit_reached(request: Request) -> None:
    """Telt het verzoek af in de grove grens en weigert met 429 als zijn emmer leeg is.

    Voor een weg die buiten ``RateLimitMiddleware`` om loopt: de voorcontrole van de
    image-push weigert vóór de body en staat dus buiten die middleware. Wie dit aanroept
    zet ``FLOOD_COUNTED_IN_SCOPE``, zodat het verzoek niet tweemaal wordt geteld.
    """
    client_id = IPRateLimiter.get_client_identifier(request)
    if not API_FLOOD_LIMITER.is_allowed(client_id):
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers=retry_after_header(API_FLOOD_LIMITER, client_id),
        )
