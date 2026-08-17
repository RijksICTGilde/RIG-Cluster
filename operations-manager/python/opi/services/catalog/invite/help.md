# Uitnodiging

Nodig mensen uit voor het Keycloak-realm van je project met een deelbare link. Wie de link opent, maakt een account aan of koppelt zijn rijksaccount, en krijgt meteen de rollen die je aan die uitnodiging hebt gekoppeld.

## Wanneer gebruik je dit?

- Je wilt testers of collega's toegang geven zonder ze per stuk aan te maken
- Je wilt mensen zonder rijksaccount toegang geven tot je applicatie
- Je wilt verschillende groepen verschillende rollen geven, elk met een eigen link

## Wat wordt er ingesteld?

Elke uitnodiging heeft een sleutel die in de link staat (**/invite/&lt;sleutel&gt;**). Laat het sleutelveld leeg en er wordt een veilige willekeurige sleutel gemaakt. Per uitnodiging kies je welke realm-rollen iemand krijgt, of geen rol. Het aanpassen van uitnodigingen verandert niets aan je applicatie en veroorzaakt dus geen nieuwe uitrol.

**Let op:** de link is het enige slot op de deur. Iedereen die hem heeft kan een account aanmaken, dus deel hem bewust en kies geen zelfbedachte, te raden sleutel. Verwijder je een uitnodiging, dan blijven de accounts die er al mee zijn aangemaakt gewoon bestaan.

Deze service vereist **Keycloak Authentication**, dat automatisch wordt meegeselecteerd.
