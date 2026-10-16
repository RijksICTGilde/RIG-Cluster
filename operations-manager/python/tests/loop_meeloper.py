"""Meet of een stap de asyncio event loop vasthoudt.

OPI draait FastAPI, de TaskWorker en alle schedulers op EEN loop, dus een synchrone
blokkade raakt webverzoeken, alle lopende taken en de schedulers tegelijk. Een test die
toetst dat ``asyncio.to_thread`` wordt aangeroepen bewijst dat niet: die gaat groen mee
met elke herschrijving die het gedrag stukmaakt. Hier draait een tweede taak MEE die per
tick de klok leest; het grootste gat tussen twee ticks is de tijd dat de loop stilstond.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: Kort genoeg om een blokkade van 200 ms ruim op te merken, lang genoeg om de meting
#: zelf geen merkbare belasting te maken.
TICK = 0.005


@dataclass
class LoopMeting:
    """De gaten tussen twee ticks van de meeloper, in seconden."""

    gaten: list[float] = field(default_factory=list)

    @property
    def grootste_gat(self) -> float:
        return max(self.gaten, default=0.0)

    def eis_onder(self, grens: float, wat: str) -> None:
        assert self.grootste_gat < grens, (
            f"{wat} hield de event loop {self.grootste_gat:.3f}s vast (grens {grens:.3f}s, "
            f"{len(self.gaten)} ticks). Draait het trage werk weer synchroon op de loop?"
        )


@asynccontextmanager
async def meeloper() -> AsyncIterator[LoopMeting]:
    """Tikt op de achtergrond zolang het blok loopt en verzamelt de gaten."""
    meting = LoopMeting()
    bezig = True

    async def tikken() -> None:
        loop = asyncio.get_running_loop()
        vorige = loop.time()
        while bezig:
            await asyncio.sleep(TICK)
            nu = loop.time()
            meting.gaten.append(nu - vorige)
            vorige = nu

    taak = asyncio.create_task(tikken())
    await asyncio.sleep(TICK * 2)
    meting.gaten.clear()
    try:
        yield meting
    finally:
        bezig = False
        await taak
