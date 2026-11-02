<script>
  import { useSvelteFlow } from '@xyflow/svelte';

  // Geeft de viewport-API naar buiten: alleen een kind van SvelteFlow kan erbij.
  let { onready } = $props();
  onready(useSvelteFlow());
</script>
