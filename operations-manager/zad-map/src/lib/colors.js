// De categorienamen uit het contract naar Rijkshuisstijl-kleuren. Het contract noemt
// kleuren bij naam, zodat de palet-keuze hier staat en niet in de Python-kant.
export const COLORS = {
  component: '#007bc7',
  deployment: '#777c00',
  team: '#ffb612',
  project: '#154273',
  web: '#01689b',
  identity: '#42145f',
  data: '#e17000',
  ops: '#39870c',
  link: '#ca005d',
  gitops: '#777c00',
  network: '#01689b',
  platform: '#154273',
  external: '#94710a',
};

export const colorOf = (name) => COLORS[name] ?? '#9aa2ad';
