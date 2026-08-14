# Disclaimer: verouderde oefenmap, gemarkeerd voor verwijdering

Deze map (`sops-sandbox/`) is een SOPS/AGE-oefenmap uit de begindagen van de repo. Ze legde stap voor stap uit hoe je met SOPS en een AGE-sleutel een Kubernetes-secret versleutelt (zie `steps.md`). Ze hangt nergens aan: geen Taskfile, script, CI of test verwijst ernaar, en ze wordt in geen enkele kustomize-build ingelezen.

## Over de sleutel in deze map

In `sops-key.txt` en `sops-secret-for-in-namespace.yaml` staat een AGE-sleutel, publieke helft `age1fdup3p822e4qd3jexcpsqq7nrqsrrmvmu4c22p0s9qlxc93dkplqd0xj0j`. Dit is een wegwerp-testsleutel die ooit tijdens development is gebruikt en verder geen doel meer dient.

De reikwijdte is bewust klein en hier voor de volledigheid opgeschreven, zodat een security review dit niet als een openstaand risico hoeft te behandelen:

- De sleutel ontsleutelt uitsluitend de twee demobestanden in deze map (`secret.sops.yaml` en `secret2.sops.yaml`). Die bevatten geen echte gegevens.
- Het is geen productiesleutel of sandboxsleutel.
- De echte sleutels worden bij het opzetten gegenereerd in het gitignored `security/` (`task generate-age-key`, `Taskfile.yaml:226`) en horen niet in versiebeheer. Deze oefenmap wijkt daarvan af en is daarmee achterhaald.

## Status

Deze map is gemarkeerd voor verwijdering. Ze wordt geschrapt zodra alle informatie die hier nog van waarde is (in de praktijk alleen de werkwijze in `steps.md`) elders is overgenomen en gedocumenteerd. Tot die tijd staat deze disclaimer hier zodat duidelijk is waarom de map er nog is en dat de sleutel geen levende functie heeft.
