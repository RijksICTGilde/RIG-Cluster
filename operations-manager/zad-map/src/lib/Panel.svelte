<script>
  let { info, onclose } = $props();
</script>

{#if info}
  <aside class="panel">
    <button class="close" onclick={onclose} aria-label="Sluiten">×</button>
    {#if info.eyebrow}<p class="eyebrow">{info.eyebrow}</p>{/if}
    <h2>{info.title}</h2>
    {#if info.subtitle}<p class="mono">{info.subtitle}</p>{/if}
    {#each info.sections ?? [] as section}
      {#if section.heading}<h3>{section.heading}</h3>{/if}
      {#if section.html}
        <!-- Alleen markdown uit de repo, door de server gerenderd; tekst uit een
             projectbestand komt altijd als `text` binnen en wordt ge-escaped. -->
        <div class="help">{@html section.html}</div>
      {:else}
        <ul class="list">
          {#each section.items ?? [] as item}
            <li class:mono={item.mono}>
              {#if item.href}
                <a href={item.href} target="_blank" rel="noopener">{item.text}</a>
              {:else}
                {item.text}
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    {/each}
  </aside>
{/if}

<style>
  .panel {
    position: absolute; top: 16px; right: 16px; bottom: 16px; width: min(440px, calc(100% - 32px));
    overflow: auto; padding: 22px 26px 28px; z-index: 20;
    background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
    box-shadow: 0 8px 24px color-mix(in srgb, var(--ink) 12%, transparent);
    animation: slide 0.28s ease-out;
  }
  @keyframes slide { from { transform: translateX(24px); opacity: 0; } }
  .close { position: absolute; top: 10px; right: 14px; border: 0; background: none; font-size: 26px; cursor: pointer; color: var(--ink-soft); }
  h2 { margin: 2px 0 6px; font-size: 24px; line-height: 1.15; font-weight: 720; }
  h3 { margin: 18px 0 6px; font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-soft); }
  p { line-height: 1.5; margin: 8px 0; }
  .eyebrow { margin: 0; font-size: 12px; font-weight: 700; letter-spacing: 0.07em; text-transform: uppercase; color: var(--ink-soft); }
  .mono { font-family: var(--mono); font-size: 12.5px; color: var(--ink-soft); word-break: break-all; }
  .list { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; line-height: 1.45; }
  .list a { font-weight: 650; color: var(--lintblauw); word-break: break-all; }
  .help { line-height: 1.55; }
  .help :global(h2), .help :global(h3) { font-size: 16px; margin: 20px 0 4px; }
  .help :global(ul) { padding-left: 20px; }
  .help :global(code) { font-family: var(--mono); font-size: 12.5px; background: var(--paper-2); padding: 0 4px; border-radius: 4px; }
</style>
