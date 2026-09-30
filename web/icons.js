// 24x24 stroke icons. Inline so the app works on an offline LAN.
const P = {
  trackpad: '<rect x="3" y="4" width="18" height="13" rx="3"/><path d="M12 17v3M8 20h8"/><path d="M12 8.5v4" opacity=".55"/>',
  terminal: '<rect x="2.5" y="4" width="19" height="16" rx="3.5"/><path d="m7 9.5 3 2.5-3 2.5M12.5 15h4.5"/>',
  controls: '<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="3.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="3.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/>',
  keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2.5"/><path d="M6.5 10h.01M10 10h.01M13.5 10h.01M17 10h.01M6.5 13.8h.01M17 13.8h.01M9.5 14h5"/>',
  command: '<path d="M9 9V6.5A2.5 2.5 0 1 0 6.5 9H9Zm0 0h6m-6 0v6m6-6V6.5A2.5 2.5 0 1 1 17.5 9H15Zm0 0v6m0 0h-6m6 0v2.5a2.5 2.5 0 1 0 2.5-2.5H15Zm-6 0v2.5A2.5 2.5 0 1 1 6.5 15H9Z"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
  moon: '<path d="M20.5 14.2A8.5 8.5 0 1 1 9.8 3.5a6.8 6.8 0 0 0 10.7 10.7Z"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="10.5" rx="2.5"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
  display: '<rect x="2.5" y="4" width="19" height="12.5" rx="2.5"/><path d="M8.5 20.5h7M12 16.5v4"/><path d="m9.5 8 5 5M14.5 8l-5 5" opacity=".8"/>',
  camera: '<path d="M4 8.5A2.5 2.5 0 0 1 6.5 6h1.7l1.5-2h4.6l1.5 2h1.7A2.5 2.5 0 0 1 20 8.5v8a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 16.5Z"/><circle cx="12" cy="12.5" r="3.5"/>',
  playpause: '<path d="M4 6v12l8-6-8-6Z" fill="currentColor"/><path d="M15.5 6v12M20 6v12"/>',
  next: '<path d="m4.5 6 7 6-7 6V6Zm7.5 0 7 6-7 6V6Z" fill="currentColor"/><path d="M20.5 6v12"/>',
  prev: '<path d="m19.5 6-7 6 7 6V6ZM12 6l-7 6 7 6V6Z" fill="currentColor"/><path d="M3.5 6v12"/>',
  volume: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4Z" fill="currentColor"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
  mute: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4Z" fill="currentColor"/><path d="m16 9.5 5 5m0-5-5 5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
  wifi: '<path d="M2.5 9a14 14 0 0 1 19 0M5.5 12.2a9.5 9.5 0 0 1 13 0M8.6 15.4a5 5 0 0 1 6.8 0"/><circle cx="12" cy="18.6" r="1" fill="currentColor"/>',
  bluetooth: '<path d="m7 7 10 10-5 4V3l5 4L7 17"/>',
  rocket: '<path d="M14 4.5c3-1.5 5.5-1 5.5-1s.5 2.5-1 5.5l-6.5 6.5-4.5-4.5L14 4.5Z"/><path d="M8.5 10.5 5 10l-2 2 4.5 1.5M13.5 15.5l.5 3.5-2 2-1.5-4.5M6 18c-1 1-1.5 2.5-1.5 2.5S6 20 7 19"/><circle cx="15.5" cy="8.5" r="1.3"/>',
  link: '<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 1 0-6.4-6.4l-1 1"/><path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 1 0 6.4 6.4l1-1"/>',
  upload: '<path d="M12 15.5V4M7.5 8.5 12 4l4.5 4.5"/><path d="M4.5 14v3.5A2.5 2.5 0 0 0 7 20h10a2.5 2.5 0 0 0 2.5-2.5V14"/>',
  download: '<path d="M12 4v11.5M7.5 11 12 15.5l4.5-4.5"/><path d="M4.5 14v3.5A2.5 2.5 0 0 0 7 20h10a2.5 2.5 0 0 0 2.5-2.5V14"/>',
  folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.7l2 2h7.3A2.5 2.5 0 0 1 21 9.5v7a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5Z"/>',
  file: '<path d="M6.5 3h7l5 5v11a2 2 0 0 1-2 2h-10a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z"/><path d="M13.5 3v5h5"/>',
  clipboard: '<rect x="5" y="4.5" width="14" height="16.5" rx="2.5"/><path d="M9 4.5a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 4.5V6H9Z"/><path d="M9 11h6M9 15h4"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2.5"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9.5 3v3M14.5 3v3M9.5 18v3M14.5 18v3M3 9.5h3M3 14.5h3M18 9.5h3M18 14.5h3"/>',
  power: '<path d="M12 3v8.5"/><path d="M6.8 6.3a8 8 0 1 0 10.4 0"/>',
  restart: '<path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v4.5h-4.5"/>',
  snow: '<path d="M12 2.5v19M4.2 7l15.6 10M4.2 17 19.8 7"/><path d="m9.5 4 2.5 2 2.5-2M9.5 20l2.5-2 2.5 2"/>',
  logout: '<path d="M14.5 4.5h3a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-3"/><path d="M10 16.5 5.5 12 10 7.5M5.5 12H15"/>',
  x: '<path d="m6 6 12 12M18 6 6 18"/>',
  chev: '<path d="m9 5.5 6.5 6.5L9 18.5"/>',
  back: '<path d="M15 5.5 8.5 12l6.5 6.5"/>',
  check: '<path d="m4.5 12.5 5 5 10-11"/>',
  more: '<circle cx="5.5" cy="12" r="1.2" fill="currentColor"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/><circle cx="18.5" cy="12" r="1.2" fill="currentColor"/>',
  textsm: '<path d="M4 17.5 8 7l4 10.5M5.5 14h5"/><path d="M15 12.5h5" opacity=".8"/>',
  textlg: '<path d="M3 19 8.5 5 14 19M5 15h7"/><path d="M16 12h5M18.5 9.5v5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6h.01"/>',
  pulse: '<path d="M3 12h4l2.5-6 5 12 2.5-6h4"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/>',
  shield: '<path d="M12 3 4.5 6v5.5c0 4.6 3.1 8 7.5 9.5 4.4-1.5 7.5-4.9 7.5-9.5V6Z"/><path d="m8.8 12 2.2 2.2 4.2-4.4"/>',
  screen: '<rect x="2.5" y="4" width="19" height="12.5" rx="2.5"/><path d="M8.5 20.5h7M12 16.5v4"/>',
  hand: '<path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V11m0-1.5V4a1.5 1.5 0 0 1 3 0v6.5m0-4a1.5 1.5 0 0 1 3 0v6m0-3.5a1.5 1.5 0 0 1 3 0V15a7 7 0 0 1-7 7h-1a7 7 0 0 1-5.6-2.8L3.2 16a1.6 1.6 0 0 1 2.5-2L8 16"/>',
  bolt: '<path d="M13 2.5 4.5 13.5h6.5l-1 8 8.5-11h-6.5Z"/>',
};
const cache = new Map();
export function icon(name) {
  if (!cache.has(name)) cache.set(name, `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${P[name] || P.info}</svg>`);
  return cache.get(name);
}
export function hydrateIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach(el => { if (!el.firstChild) el.innerHTML = icon(el.dataset.icon); });
}
