# GitHub Support-ticket, klaar om in te dienen

Stand: de rewrite is uitgevoerd en geforceerd gepusht op 26 september 2026. Dit ticket is de laatste stap.

Indienen via <https://support.github.com/contact> door iemand met adminrechten op de repository. Heeft de organisatie GitHub Enterprise, gebruik dan dat kanaal; dat is sneller.

## Wat er al gedaan is

| Stap | Stand |
|---|---|
| Fork `GliderGeek/RIG-Cluster` verwijderd | Ja, geverifieerd: 404, `forks_count` is 0 |
| Historie herschreven met `git filter-repo` | Ja, alleen de AGE-sleutel vervangen |
| 11 branches geforceerd gepusht | Ja, `main` van `ee6dae21` naar `704450be` |
| 51 tags geforceerd gepusht | Ja |
| Sleutelmateriaal in branches en tags | 0 blobs, gecontroleerd op een verse clone |

## Wat GitHub nog moet doen

Gemeten na de push, op een verse clone:

- **10 blobs met de sleutel zijn nog bereikbaar via `refs/pull/5/head`, `refs/pull/6/head` en `refs/pull/7/head`.** Dat zijn gemergede pull requests uit 2026. Die refs zijn door ons niet te wijzigen of te verwijderen.
- **De oude commits zijn nog op te halen op SHA.** `git fetch --depth 1 origin ee6dae21ff88ebbeed662f5d5eb503da279fa30b` slaagt, en `https://github.com/RijksICTGilde/RIG-Cluster/commit/ee6dae21ff88ebbeed662f5d5eb503da279fa30b` geeft HTTP 200.

## Ticket-tekst

Onderwerp: `Purge unreachable objects and pull request refs after history rewrite (leaked private key)`

> Repository: RijksICTGilde/RIG-Cluster
>
> A private encryption key was committed to this repository and remained in the history of the public repository for about a year. We have rewritten the history with git-filter-repo to remove the key, and force-pushed all 11 branches and all 51 tags. The rewritten history no longer contains the key: we verified this on a fresh clone of the repository.
>
> Two things remain that we cannot fix ourselves, and we would like GitHub to resolve them:
>
> 1. **Pull request refs.** Ten blobs containing the key are still reachable through `refs/pull/5/head`, `refs/pull/6/head` and `refs/pull/7/head`. These are merged pull requests. We cannot push to or delete refs in the `refs/pull/` namespace. Please remove or purge these refs.
>
> 2. **Unreachable objects and cached views.** The pre-rewrite commits are still retrievable by SHA. For example, `git fetch --depth 1 origin ee6dae21ff88ebbeed662f5d5eb503da279fa30b` succeeds, and `https://github.com/RijksICTGilde/RIG-Cluster/commit/ee6dae21ff88ebbeed662f5d5eb503da279fa30b` returns HTTP 200. Please run garbage collection on the repository so these objects are dropped, and purge the cached commit and diff views for them.
>
> The only fork of this repository has been deleted, so nothing else in the network should reference the old objects.
>
> The old history contained 3220 commits. We have the full list of pre-rewrite commit SHAs and can supply it in any format you prefer.
>
> This is a security incident on a Dutch government platform, so we would appreciate it being handled with priority.

## Bijlagen die Support kan opvragen

- De volledige lijst met 3220 oude commit-SHA's staat in de werkkopie van de mirror (`oude-shas.txt`) en de oude ref-standen in `oude-refs.txt`.
- Een volledige backup van de repository zoals die vóór de rewrite op GitHub stond, is bewaard als bare mirror. Bewaar die tot Support het ticket heeft afgerond, zodat terugdraaien mogelijk blijft.
