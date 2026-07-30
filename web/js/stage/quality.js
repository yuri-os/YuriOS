/* How hard to push the room (SPEC §6.2) — one decision, made once at boot, read
 * from everywhere else. Nothing in the scene is allowed to sniff the device on
 * its own; it asks here.
 *
 * Cost is a first-class constraint: the GPU drawing this room is usually also
 * holding her model (SPEC §3). On a phone it is holding the room, the model, the
 * browser's compositor and an audio graph, on a battery, behind a fill rate a
 * desktop card would call a rounding error. Two tiers were not enough for that —
 * `low` was written for a small *window*, and it still asked a handset for full
 * resolution, full-res bloom, nine lights and a 20 Hz canvas upload. Hence three:
 *
 *   full   a desktop GPU: the wet floor, the shadow map, the area lights, post-AA
 *   low    a narrow window or a large touch display: those four come off
 *   phone  a handset: fewer pixels, half-res bloom, one less full-screen pass,
 *          four lights instead of nine, smaller canvases, fewer of everything
 *          that moves
 *
 * `?fx=full|low|phone` forces a tier — the escape hatch in both directions, and
 * the only way to see the phone tier from a desk. Everything else is measured,
 * and measurement is never the last word: whatever tier is chosen, VrmStage
 * watches the frame clock and moves the render scale between `minScale` and
 * `maxScale` around it, because "a phone" spans an order of magnitude and a
 * table of constants cannot tell which one this is.
 */

const TIERS = {
  full: {
    pixelRatio: 1.5,          // cap on devicePixelRatio
    pixelBudget: Infinity,    // …and on the drawn pixels, whatever the ratio
    minScale: 0.7,            // how far the adaptive scaler may fall below that
    maxScale: 1,              // …and above it, on a device with frames to spare
    anisotropy: 16,
    bloomScale: 1,            // fraction of the frame the bloom mips are built at
    mergeGrade: false,        // fold tone-map + grade into one pass (→ Post.js)
    surface: 512,             // the room's canvas maps (floor, walls, cloth)
    city: 1,                  // fraction of full size the Sprawl is baked at
    drops: 1100,              // rain outside the glass
    glassLayers: 2,           // drip layers in the pane shader
    dust: 380,
    farRain: 200,             // strokes in the city's overlay
    flyers: 8,                // hover traffic between the near towers
    overlayHz: 20,            // city overlay redraws + uploads per second
    terminalHz: 3.6,          // her terminal's feed
    accents: true,            // the three small coloured point lights
    fixtureLights: true,      // the dying tube, the holo's glow
  },
  low: {
    pixelRatio: 1.25,
    pixelBudget: 2.4e6,
    minScale: 0.65,
    maxScale: 1.15,
    anisotropy: 4,
    bloomScale: 0.7,
    mergeGrade: false,
    surface: 512,
    city: 1,
    drops: 700,
    glassLayers: 2,
    dust: 200,
    farRain: 110,
    flyers: 4,
    overlayHz: 12,
    terminalHz: 2,
    accents: true,
    fixtureLights: true,
  },
  phone: {
    pixelRatio: 1,
    pixelBudget: 1.1e6,
    minScale: 0.55,
    maxScale: 1.35,           // a flagship handset can hold more than the cap
    anisotropy: 2,            // not 1: the floor is seen edge-on and shimmers
    bloomScale: 0.5,
    mergeGrade: true,
    surface: 384,             // 256 turns the deck's roughness into soft blotches
    city: 0.5,                // a phone shows the district in ~700 px of screen
    drops: 240,
    glassLayers: 1,
    dust: 100,
    farRain: 70,
    flyers: 3,
    overlayHz: 7,
    terminalHz: 1.2,
    accents: false,           // their emitters still bloom — only the wash goes
    fixtureLights: false,
  },
};

/** A coarse pointer on a small screen is a handset. `screen`, not `innerWidth`:
 *  the window is what changes when a mobile address bar slides away, and a phone
 *  in landscape is still a phone. A thin core count is the other tell — coarse
 *  pointers also arrive on kiosk panels and TVs, which render like handsets. */
function detect() {
  // Desktop-pet mode (SPEC §6.5) builds no room at all — just her body in a small
  // frameless window — so the narrow-window rule below, which is a statement about
  // what the room costs, has nothing to say about it.
  if (new URLSearchParams(location.search).has('desktop')) return 'full';
  const coarse = matchMedia('(pointer: coarse)').matches;
  const short = Math.min(screen.width, screen.height);
  const thin = (navigator.hardwareConcurrency || 8) <= 6;
  if (coarse && (short <= 900 || thin)) return 'phone';
  if (coarse || innerWidth < 900) return 'low';
  return 'full';
}

function resolve() {
  const fx = new URLSearchParams(location.search).get('fx');
  const name = TIERS[fx] ? fx : detect();
  return Object.freeze({
    tier: name,
    low: name !== 'full',     // what the room has always called the reduced tier
    phone: name === 'phone',
    ...TIERS[name],
  });
}

export const QUALITY = resolve();
