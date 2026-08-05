/* First-run model selection. The process stays intentionally offline until a
 * choice is made; a restart activates the saved model after any GGUF download. */
const apiPath = (path) => window.YuriOSRuntime?.apiPath(path) || path;

const panel = document.getElementById('model-setup');
const recommendations = document.getElementById('model-recommendations');
const custom = document.getElementById('model-custom');
const save = document.getElementById('model-custom-save');
const status = document.getElementById('model-setup-status');

function setStatus(text, bad = false) {
  status.textContent = text;
  status.classList.toggle('error', bad);
}

async function choose(model) {
  setStatus('Saving model choice…');
  try {
    const response = await fetch(apiPath('/api/onboarding'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model}),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || 'Could not save that model');
    setStatus(`${body.detail} Restart YuriOS to activate it.`, false);
  } catch (error) {
    setStatus(error.message || 'Could not save that model', true);
  }
}

async function load() {
  try {
    const response = await fetch(apiPath('/api/onboarding'), {cache: 'no-store'});
    if (!response.ok) return;
    const setup = await response.json();
    if (setup.configured) return;
    panel.hidden = false;
    for (const item of setup.recommendations || []) {
      const button = document.createElement('button');
      button.className = 'model-recommendation';
      button.innerHTML = `<b>${item.label}</b><small>${item.hardware} · ${item.quant}</small>`;
      button.addEventListener('click', () => choose(item.id));
      recommendations.append(button);
    }
    if (setup.download?.state === 'downloading') setStatus(setup.download.detail);
  } catch {
    // The boot poller already handles a server that has not reached the route yet.
  }
}

save.addEventListener('click', () => choose(custom.value.trim()));
custom.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') choose(custom.value.trim());
});
load();
