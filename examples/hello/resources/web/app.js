// Use the HTTP API directly: receive state events at /api/events and send
// commands to /api/cmd.

const count = document.getElementById('count');
const message = document.getElementById('message');

new EventSource('/api/events').addEventListener('state', (e) => {
  count.textContent = JSON.parse(e.data).count;
});

async function call(command) {
  const r = await fetch('/api/cmd', { method: 'POST', body: JSON.stringify(command) });
  const reply = await r.json().catch(() => ({}));
  // Classify failures with reply.code: invalid, unavailable, failed or timeout.
  if (!r.ok) throw new Error(`${reply.error ?? `HTTP ${r.status}`} (${reply.code ?? 'failed'})`);
  return reply.result;
}

function add(amount) {
  call({ cmd: 'add', amount }).then(
    (result) => (message.textContent = `add ${amount}: ${result}`),
    (e) => (message.textContent = `add ${amount} refused: ${e.message}`),
  );
}

// Remote input uses DOM arrow keys, Enter for Select and Escape for Back.
document.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowUp') add(1);
  else if (e.key === 'ArrowDown') add(-1);
  else if (e.key === 'Enter') add(0.5);
  else if (e.key === 'Escape' || e.key === 'Backspace') call({ cmd: 'exit' });
  else message.textContent = `key ${e.key}`;
});

// Report the first rendered frame so the add-on can forward page keys.
requestAnimationFrame(() => requestAnimationFrame(() => call({ cmd: 'ready' })));
