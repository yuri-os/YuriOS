/* Space: the joined graph in three dimensions (SPEC §24.4).
 *
 * Time runs left to right on the same range as every other view; the other two
 * axes carry only relationship (graph/layout.js has the rules). Solid lines are
 * recorded links, dashed amber ones inferred. Loaded on demand — the rest of
 * the page never pays for three.js — and disposed completely when the view
 * goes, context and all.
 */
import * as T from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

import { layout } from "./layout.js";

export function mount(root, api) {
  const esc = api.escape;
  const wrap = document.createElement('div');
  wrap.className = 'network-wrap';
  wrap.innerHTML = `<div class="network-toolbar"><div class="network-title"><span class="network-orbit-icon">◉</span><span>SPACE<small>Time runs left to right; shared work sits together</small></span></div>
    <div class="network-actions"><button type="button" data-net="overview">Overview</button><button type="button" data-net="straight" title="Time left to right with no tilt, like the Timeline">Straight on</button><button type="button" data-net="focus" disabled>Focus chain</button><button type="button" data-net="expand" aria-pressed="false">Expand</button></div></div>
    <div class="network-viewport"><div class="network-axis"></div><div class="network-labels"></div><div class="network-guide">Time runs left → right <span>·</span> Drag to orbit <span>·</span> Scroll to move closer <span>·</span> Right-drag to pan</div>
      <div class="network-corner"><b>3D</b><span>One point · one record</span></div><div class="network-empty" hidden></div>
      <div class="network-controls"><button type="button" data-net="in" aria-label="Move closer">+</button><button type="button" data-net="out" aria-label="Move farther away">−</button></div>
      <div class="network-selection" hidden></div></div>
    <div class="network-bottom"><div class="network-legend"></div><div class="network-status" role="status"></div></div>`;
  root.appendChild(wrap);
  const $ = (selector) => wrap.querySelector(selector);
  const viewport = $('.network-viewport');
  let renderer;
  try { renderer = new T.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' }); }
  catch (_) {
    $('.network-empty').hidden = false;
    $('.network-empty').textContent = 'WebGL 2 is unavailable in this browser. Enable hardware acceleration or use Timeline.';
    return { update() {}, dispose() {} };
  }
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setClearColor(0x050b12, 0);
  const canvas = renderer.domElement;
  canvas.tabIndex = 0;
  canvas.setAttribute('aria-label', '3D relationship map. Drag to orbit. Arrow keys select records; plus and minus move closer or farther. R restores overview.');
  viewport.prepend(canvas);
  const scene = new T.Scene();
  const camera = new T.PerspectiveCamera(48, 1, .5, 12000);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = .12;
  controls.minPolarAngle = .025;
  controls.maxPolarAngle = Math.PI - .025;
  controls.minDistance = 15;
  controls.zoomSpeed = .75;
  controls.rotateSpeed = .65;
  const graph = layout(api.events, api.links, api.range);
  const index = new Map(graph.nodes.map((node, i) => [node.event.id, i]));
  const resources = [];
  const disposable = (resource) => { resources.push(resource); return resource; };
  const points = graph.nodes.map((node) => new T.Vector3(...node.p));
  const bounds = new T.Box3(); points.forEach((p) => bounds.expandByPoint(p));
  const sphere = points.length ? bounds.getBoundingSphere(new T.Sphere()) : new T.Sphere(new T.Vector3(), 180);
  if (points.length) sphere.radius = Math.max(...points.map((p) => p.distanceTo(sphere.center)));
  sphere.radius = Math.max(60, sphere.radius + 30);
  scene.fog = new T.FogExp2(0x071019, .12 / sphere.radius);
  scene.add(new T.HemisphereLight(0xbddcff, 0x16343c, 2.1));
  const key = new T.DirectionalLight(0xffffff, 3); key.position.set(-200, 300, 400); scene.add(key);
  const rim = new T.DirectionalLight(0x62d6ff, 2.5); rim.position.set(300, -100, -200); scene.add(rim);
  const geometry = disposable(new T.IcosahedronGeometry(1, 2));
  const material = disposable(new T.MeshStandardMaterial({ roughness: .28, metalness: .28, emissive: 0x143b48, emissiveIntensity: .45 }));
  const mesh = new T.InstancedMesh(geometry, material, Math.max(1, graph.nodes.length));
  mesh.count = graph.nodes.length;
  scene.add(mesh);
  const dummy = new T.Object3D();
  const baseColors = graph.nodes.map((n) => new T.Color(api.colors[n.event.kind] || '#a5afa6'));
  const visible = new Set();
  let chainOnly = false, selected = null, currentChain = new Set(), raf = 0, disposed = false, tween = null;
  let expanded = false, pointerDown = null, previousSelection = null;

  // Soft halos add depth without obscuring the solid, individually pickable nodes.
  const glowCanvas = document.createElement('canvas'); glowCanvas.width = glowCanvas.height = 64;
  const glowCtx = glowCanvas.getContext('2d');
  const gradient = glowCtx.createRadialGradient(32, 32, 1, 32, 32, 32);
  gradient.addColorStop(0, 'rgba(255,255,255,.55)'); gradient.addColorStop(.25, 'rgba(255,255,255,.18)'); gradient.addColorStop(1, 'rgba(255,255,255,0)');
  glowCtx.fillStyle = gradient; glowCtx.fillRect(0, 0, 64, 64);
  const texture = disposable(new T.CanvasTexture(glowCanvas));
  const glowGeometry = disposable(new T.BufferGeometry());
  glowGeometry.setAttribute('position', new T.Float32BufferAttribute(graph.nodes.flatMap((n) => n.p), 3));
  const glowColors = new Float32Array(graph.nodes.length * 3);
  glowGeometry.setAttribute('color', new T.BufferAttribute(glowColors, 3));
  const glowMaterial = disposable(new T.PointsMaterial({ size: 42, map: texture, vertexColors: true, transparent: true, opacity: .5, depthWrite: false, blending: T.AdditiveBlending, sizeAttenuation: true }));
  const glows = new T.Points(glowGeometry, glowMaterial); scene.add(glows);

  // The time ruler: a floor under the records with one line per grid step,
  // day boundaries brighter, labelled along its front edge.
  let yMin = -60, zMin = -60, zMax = 60;
  if (points.length) {
    yMin = zMin = Infinity; zMax = -Infinity;
    for (const p of points) { yMin = Math.min(yMin, p.y); zMin = Math.min(zMin, p.z); zMax = Math.max(zMax, p.z); }
  }
  const floorY = yMin - 24, zBack = zMin - 20, zFront = zMax + 20;
  const ruler = (color, opacity) => {
    const object = new T.LineSegments(disposable(new T.BufferGeometry()), disposable(new T.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false })));
    scene.add(object);
    return object;
  };
  const hourLines = ruler(0x2c4653, .45), dayLines = ruler(0x5d8797, .6), axisLine = ruler(0x7aa6b6, .7);
  axisLine.geometry.setAttribute('position', new T.Float32BufferAttribute([graph.xOf(graph.t0), floorY, zFront, graph.xOf(graph.t1), floorY, zFront], 3));
  const axisLabels = $('.network-axis');
  let ticks = [], tickWidth = 0;
  function buildTicks(width) {
    ticks = api.timeGrid ? api.timeGrid(graph.t0, graph.t1, width) : [];
    const hours = [], days = [];
    for (const tick of ticks) { const x = graph.xOf(tick.t); (tick.day ? days : hours).push(x, floorY, zBack, x, floorY, zFront); }
    hourLines.geometry.setAttribute('position', new T.Float32BufferAttribute(hours, 3));
    dayLines.geometry.setAttribute('position', new T.Float32BufferAttribute(days, 3));
    hourLines.geometry.computeBoundingSphere(); dayLines.geometry.computeBoundingSphere();
    axisLabels.replaceChildren(...ticks.map((tick) => {
      const label = document.createElement('span');
      label.textContent = tick.label; label.className = tick.day ? 'day' : ''; label.hidden = true;
      return label;
    }));
  }

  function edgeObject(edges, color, opacity, dashed = false) {
    const geometry = disposable(new T.BufferGeometry());
    geometry.setAttribute('position', new T.Float32BufferAttribute(edges.flatMap((edge) => [...graph.nodes[index.get(edge.a)].p, ...graph.nodes[index.get(edge.b)].p]), 3));
    const material = disposable(dashed ? new T.LineDashedMaterial({ color, transparent: true, opacity, dashSize: 5, gapSize: 5, depthWrite: false }) :
      new T.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false }));
    const object = new T.LineSegments(geometry, material);
    if (dashed) object.computeLineDistances();
    scene.add(object);
    return object;
  }
  const baseEdges = edgeObject(graph.edges.filter((l) => l.confidence !== 'inferred'), 0x71aec7, .28);
  const baseInferred = edgeObject(graph.edges.filter((l) => l.confidence === 'inferred'), 0xb59364, .1, true);
  const selectedEdges = edgeObject([], 0xa2efff, .85);
  const inferredEdges = edgeObject([], 0xf1c57a, .65, true);
  const ring = new T.Mesh(disposable(new T.IcosahedronGeometry(1, 1)), disposable(new T.MeshBasicMaterial({ color: 0xffffff, wireframe: true, transparent: true, opacity: .65 })));
  ring.visible = false; scene.add(ring);
  const labelNodes = graph.groups.map((g) => {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'network-group-label';
    const title = g.unlinked ? `Unlinked ${api.labels[g.anchor.kind] || g.anchor.kind}` : g.anchor.title;
    button.innerHTML = `<i style="background:${api.colors[g.anchor.kind]}"></i><span>${esc(title.length > 45 ? title.slice(0, 44) + '…' : title)}<small>${g.events.length} record${g.events.length === 1 ? '' : 's'}${g.unlinked ? ' · no recorded links' : g.events.length > 1 ? ' · shared work' : ' · linked record'}</small></span>`;
    button.title = title;
    button.onclick = () => { if (!g.unlinked) { api.select(g.anchor.id); focusChain(); } else fly(g.events.map((e) => index.get(e.id))); };
    $('.network-labels').appendChild(button);
    return { button, position: new T.Vector3(...g.p), group: g };
  });
  const counts = new Map(); graph.nodes.forEach((n) => counts.set(n.event.kind, (counts.get(n.event.kind) || 0) + 1));
  $('.network-legend').innerHTML = [...counts].map(([kind, n]) => `<span><i style="background:${api.colors[kind]}"></i>${esc(api.labels[kind] || kind)}<small>${n}</small></span>`).join('');
  if (!points.length) { $('.network-empty').hidden = false; $('.network-empty').textContent = 'No records in this range match your filters. Widen the range or enable event types.'; }

  function schedule() { if (!disposed && !raf) raf = requestAnimationFrame(render); }
  function render(now) {
    raf = 0;
    if (disposed) return;
    if (tween) {
      const t = Math.min(1, (now - tween.start) / 500), eased = 1 - (1 - t) ** 3;
      camera.position.lerpVectors(tween.from, tween.to, eased);
      controls.target.lerpVectors(tween.targetFrom, tween.targetTo, eased);
      if (t === 1) tween = null; else schedule();
    }
    controls.update();
    renderer.render(scene, camera);
    projectAxis();
    projectLabels();
  }
  function projectLabels() {
    const width = viewport.clientWidth, height = viewport.clientHeight, used = [];
    const candidates = labelNodes.map((label) => ({ ...label, projected: label.position.clone().project(camera), distance: label.position.distanceTo(camera.position) }))
      .sort((a, b) => {
        const aSelected = a.group.events.some((e) => currentChain.has(e.id)), bSelected = b.group.events.some((e) => currentChain.has(e.id));
        return Number(bSelected) - Number(aSelected) || b.group.events.length - a.group.events.length || a.distance - b.distance;
      });
    for (const label of candidates) {
      const p = label.projected, x = (p.x + 1) * width / 2, y = (1 - p.y) * height / 2;
      const allowed = !chainOnly || label.group.events.some((e) => currentChain.has(e.id));
      const maxLabels = width < 500 ? 3 : 7;
      const labelX = Math.max(10, Math.min(x + 12, width - (width < 500 ? 150 : 215) - 10));
      const show = allowed && used.length < maxLabels && p.z > -1 && p.z < 1 && x > 20 && x < width - 20 && y > 65 && y < height - 135 &&
        !used.some(([ux, uy]) => Math.abs(labelX - ux) < 230 && Math.abs(y - uy) < 64);
      label.button.hidden = !show;
      if (show) { label.button.style.transform = `translate(${labelX}px,${y - 15}px)`; used.push([labelX, y]); }
    }
  }
  function projectAxis() {
    const width = viewport.clientWidth, height = viewport.clientHeight;
    // Choose the grid from how long the whole range is on screen at the
    // point being looked at, so zooming in brings finer steps.
    const a = controls.target.clone().setX(controls.target.x - 10).project(camera);
    const b = controls.target.clone().setX(controls.target.x + 10).project(camera);
    const perUnit = Math.hypot((b.x - a.x) * width / 2, (b.y - a.y) * height / 2) / 20;
    const want = Math.min(8000, Math.max(240, perUnit * (graph.xOf(graph.t1) - graph.xOf(graph.t0))));
    if (Number.isFinite(want) && Math.abs(want - tickWidth) > tickWidth * .3) { tickWidth = want; buildTicks(want); }
    const used = [];
    const order = ticks.map((tick, i) => i).sort((i, j) => Number(ticks[j].day) - Number(ticks[i].day) || i - j);
    for (const i of order) {
      const label = axisLabels.children[i];
      const p = new T.Vector3(graph.xOf(ticks[i].t), floorY, zFront).project(camera);
      const x = (p.x + 1) * width / 2, y = (1 - p.y) * height / 2 + 6;
      const show = p.z > -1 && p.z < 1 && x > 24 && x < width - 24 && y > 40 && y < height - 14 &&
        !used.some(([ux, uy]) => Math.abs(ux - x) < 70 && Math.abs(uy - y) < 18);
      label.hidden = !show;
      if (show) { label.style.transform = `translate(${x}px,${y}px) translateX(-50%)`; used.push([x, y]); }
    }
  }
  function fly(indices, immediate = false, directionOverride = null) {
    const box = new T.Box3(); indices.forEach((i) => box.expandByPoint(points[i]));
    const target = indices.length ? box.getCenter(new T.Vector3()) : sphere.center.clone();
    const direction = (directionOverride || camera.position.clone().sub(controls.target)).normalize();
    if (direction.lengthSq() < .01) direction.set(.2, .34, 1).normalize();
    // Fit the records themselves, not a bounding sphere: the layout is long
    // in time and thin across it, and a sphere would leave the width empty.
    const right = new T.Vector3(0, 1, 0).cross(direction);
    if (right.lengthSq() < 1e-6) right.set(1, 0, 0);
    right.normalize();
    const up = direction.clone().cross(right).normalize();
    const tanV = Math.tan(T.MathUtils.degToRad(camera.fov / 2)) * .84, tanH = tanV / .84 * camera.aspect * .94;
    let distance = indices.length ? 30 : sphere.radius / tanV;
    for (const i of indices) {
      const v = points[i].clone().sub(target), depth = v.dot(direction);
      distance = Math.max(distance, (Math.abs(v.dot(right)) + 14) / tanH + depth, (Math.abs(v.dot(up)) + 14) / tanV + depth);
    }
    const to = target.clone().addScaledVector(direction, distance);
    if (immediate || matchMedia('(prefers-reduced-motion: reduce)').matches) { camera.position.copy(to); controls.target.copy(target); tween = null; }
    else tween = { from: camera.position.clone(), to, targetFrom: controls.target.clone(), targetTo: target.clone(), start: performance.now() };
    controls.maxDistance = Math.max(sphere.radius * 12, distance * 2);
    schedule();
  }
  function overview(immediate = false, direction = new T.Vector3(.2, .34, 1)) {
    chainOnly = false;
    fly(graph.nodes.map((_, i) => i), immediate, direction.normalize());
    update();
  }
  function updateEdges(object, edges) {
    object.geometry.setAttribute('position', new T.Float32BufferAttribute(edges.flatMap((edge) => [...graph.nodes[index.get(edge.a)].p, ...graph.nodes[index.get(edge.b)].p]), 3));
    object.geometry.computeBoundingSphere();
    if (object.material.isLineDashedMaterial) object.computeLineDistances();
  }
  function update() {
    selected = api.selected();
    currentChain = selected ? new Set(api.cluster(selected).keys()) : new Set();
    if (!selected) chainOnly = false;
    visible.clear();
    graph.nodes.forEach((node, i) => {
      const related = currentChain.has(node.event.id), show = !chainOnly || related;
      if (show) visible.add(i);
      dummy.position.copy(points[i]); dummy.scale.setScalar(show ? node.size * (selected === node.event.id ? 1.3 : 1) : 0); dummy.updateMatrix();
      mesh.setMatrixAt(i, dummy.matrix);
      const color = baseColors[i].clone().multiplyScalar(selected && !related ? .16 : 1);
      mesh.setColorAt(i, color);
      color.multiplyScalar(show ? 1 : 0).toArray(glowColors, i * 3);
    });
    mesh.instanceMatrix.needsUpdate = true; if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    mesh.computeBoundingSphere();
    glowGeometry.attributes.color.needsUpdate = true;
    baseEdges.visible = !chainOnly; baseEdges.material.opacity = selected ? .035 : .28;
    baseInferred.visible = !chainOnly && !selected;
    const chainEdges = graph.edges.filter((e) => currentChain.has(e.a) && currentChain.has(e.b));
    updateEdges(selectedEdges, chainEdges.filter((e) => e.confidence !== 'inferred'));
    updateEdges(inferredEdges, chainEdges.filter((e) => e.confidence === 'inferred'));
    const selectedIndex = index.get(selected);
    ring.visible = selectedIndex != null;
    if (ring.visible) { ring.position.copy(points[selectedIndex]); ring.scale.setScalar(graph.nodes[selectedIndex].size * 2.3); }
    $('[data-net="focus"]').disabled = selectedIndex == null;
    const panel = $('.network-selection');
    const scroll = panel.scrollLeft;
    const focusedId = panel.contains(document.activeElement) ? document.activeElement.dataset.id : null;
    panel.hidden = selectedIndex == null;
    if (selectedIndex != null) {
      const event = graph.nodes[selectedIndex].event;
      const neighbors = graph.nodes.filter((n) => currentChain.has(n.event.id) && n.event.id !== selected).sort((a, b) => a.event.t - b.event.t);
      panel.innerHTML = `<div class="network-selected-title"><span><i style="background:${api.colors[event.kind]}"></i>${esc(api.labels[event.kind])} <small>${esc(api.time(event.t))}</small></span>
        <b>${esc(event.title)}</b><p class="network-summary">${esc((event.summary || '').slice(0, 280))}</p><div><button type="button" data-net="isolate" aria-pressed="${chainOnly}">${chainOnly ? 'Show all records' : 'Only this chain'}</button><button type="button" data-net="clear">Clear</button></div></div>
        <div class="network-neighbors">${neighbors.slice(0, 30).map((n) => `<button type="button" data-id="${esc(n.event.id)}" title="${esc(n.event.title)}"><i style="background:${api.colors[n.event.kind]}"></i><span>${esc(n.event.title)}</span></button>`).join('')}${neighbors.length > 30 ? `<span>+${neighbors.length - 30} more in the inspector</span>` : ''}</div>`;
      panel.querySelectorAll('[data-id]').forEach((button) => { button.onclick = () => api.select(button.dataset.id); if (button.dataset.id === focusedId) button.focus({ preventScroll: true }); });
      panel.scrollLeft = scroll;
    } else panel.replaceChildren();
    const outside = selected ? [...currentChain].filter((id) => !index.has(id)).length : 0;
    $('.network-status').textContent = `${visible.size.toLocaleString()} records · ${graph.edges.length.toLocaleString()} links · ${graph.groups.filter((g) => !g.unlinked && g.events.length > 1).length} work groups` +
      (selected ? ` · solid: recorded / dashed amber: inferred${outside ? ` · ${outside} related outside filters` : ''}` : ' · left → right is time; nearness across it is shared work');
    if (selected !== previousSelection) { previousSelection = selected; api.hideTooltip(); }
    schedule();
  }
  const raycaster = new T.Raycaster(), pointer = new T.Vector2();
  function pick(ev) {
    const rect = canvas.getBoundingClientRect();
    pointer.set((ev.clientX - rect.left) / rect.width * 2 - 1, -(ev.clientY - rect.top) / rect.height * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const exact = raycaster.intersectObject(mesh).find((hit) => visible.has(hit.instanceId))?.instanceId;
    if (exact != null) return exact;
    // Forgiving picking for small nodes, especially touch. Screen distance
    // chooses the closest target; depth resolves ties toward the camera.
    let result, best = ev.pointerType === 'touch' ? 17 : 9, nearest = Infinity;
    for (const i of visible) {
      const p = points[i].clone().project(camera);
      if (p.z < -1 || p.z > 1) continue;
      const d = Math.hypot((p.x + 1) * rect.width / 2 - (ev.clientX - rect.left), (1 - p.y) * rect.height / 2 - (ev.clientY - rect.top));
      const depth = points[i].distanceTo(camera.position);
      if (d < best || Math.abs(d - best) < .5 && depth < nearest) { best = d; nearest = depth; result = i; }
    }
    return result;
  }
  canvas.addEventListener('pointerdown', (ev) => { pointerDown = { x: ev.clientX, y: ev.clientY }; tween = null; });
  canvas.addEventListener('pointermove', (ev) => {
    if (ev.buttons) { api.hideTooltip(); return; }
    const i = pick(ev);
    canvas.style.cursor = i == null ? 'grab' : 'pointer';
    if (i != null) api.tooltip(graph.nodes[i].event, ev.clientX, ev.clientY); else api.hideTooltip();
  });
  canvas.addEventListener('pointerup', (ev) => {
    if (!pointerDown) return;
    const moved = Math.hypot(ev.clientX - pointerDown.x, ev.clientY - pointerDown.y) > 5; pointerDown = null;
    if (moved || ev.button !== 0) return;
    const i = pick(ev); if (i != null) api.select(graph.nodes[i].event.id);
  });
  canvas.addEventListener('dblclick', (ev) => { const i = pick(ev); if (i != null) { api.select(graph.nodes[i].event.id); focusChain(); } });
  canvas.addEventListener('pointercancel', () => { pointerDown = null; });
  canvas.addEventListener('pointerleave', () => { api.hideTooltip(); });
  canvas.addEventListener('webglcontextlost', (ev) => { ev.preventDefault(); $('.network-empty').hidden = false; $('.network-empty').textContent = 'The 3D context was interrupted. Switch views and return to Space to reload it.'; });
  function focusChain() { fly([...currentChain].filter((id) => index.has(id)).map((id) => index.get(id))); }
  function zoom(factor) { tween = null; camera.position.sub(controls.target).multiplyScalar(factor).add(controls.target); controls.update(); schedule(); }
  function expand(on) {
    expanded = on; wrap.classList.toggle('network-expanded', on);
    $('[data-net="expand"]').textContent = on ? 'Collapse' : 'Expand';
    $('[data-net="expand"]').setAttribute('aria-pressed', String(on)); resize();
    if (selected) focusChain(); else overview();
  }
  wrap.addEventListener('click', (ev) => {
    switch (ev.target.closest('[data-net]')?.dataset.net) {
      case 'overview': overview(); break;
      case 'straight': overview(false, new T.Vector3(0, .1, 1)); break;
      case 'focus': focusChain(); break;
      case 'expand': expand(!expanded); break;
      case 'in': zoom(.8); break;
      case 'out': zoom(1.25); break;
      case 'clear': api.select(null); break;
      case 'isolate': chainOnly = !chainOnly; update(); if (chainOnly) focusChain(); break;
    }
  });
  canvas.addEventListener('keydown', (ev) => {
    if (ev.key === '+' || ev.key === '=') { ev.preventDefault(); zoom(.8); }
    else if (ev.key === '-') { ev.preventDefault(); zoom(1.25); }
    else if (ev.key.toLowerCase() === 'r') { ev.preventDefault(); overview(); }
  });
  wrap.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && expanded) { ev.stopPropagation(); expand(false); } });
  function resize() {
    const width = Math.max(1, viewport.clientWidth), height = Math.max(1, viewport.clientHeight);
    renderer.setSize(width, height); camera.aspect = width / height; camera.updateProjectionMatrix(); schedule();
  }
  controls.addEventListener('change', schedule);
  controls.addEventListener('start', () => { tween = null; api.hideTooltip(); });
  const observer = new ResizeObserver(resize); observer.observe(viewport);
  resize(); overview(true);
  return { update, dispose() {
    disposed = true; cancelAnimationFrame(raf); observer.disconnect(); controls.dispose();
    resources.forEach((resource) => resource.dispose()); mesh.dispose(); renderer.dispose(); renderer.forceContextLoss();
    api.hideTooltip();
  } };
}
