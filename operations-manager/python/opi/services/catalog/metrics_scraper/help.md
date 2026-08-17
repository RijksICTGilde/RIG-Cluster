# Prometheus Metrics Scraper

Laat Prometheus de metrics van je component ophalen. Zonder deze service wordt je component niet uitgelezen, ook niet als het wel een metrics-endpoint heeft.

## Wanneer gebruik je dit?

- Je applicatie biedt metrics aan, meestal op /metrics
- Je wilt grafieken of alerts op je eigen cijfers
- Je wilt zien hoe je applicatie zich gedraagt onder belasting

## Wat wordt er ingesteld?

Je pod krijgt de annotaties waarmee Prometheus hem vindt. Standaard wordt **/metrics** op de eerste poort van je component uitgelezen; je kunt een andere poort en een ander pad instellen.

Je component krijgt de variabele **METRICS_AUTH_TOKEN**. Prometheus stuurt die mee als bearer-token bij het ophalen. Controleer hem in je applicatie, dan is je metrics-endpoint niet voor iedereen open.
