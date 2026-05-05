const canvas = document.getElementById('game');
const ctx = canvas.getContext('2d');
const statsEl = document.getElementById('stats');
const questEl = document.getElementById('quest');
const logEl = document.getElementById('log');

const keys = {};
const mouse = { x: 0, y: 0, down: false };
const log = [];

const world = {
  time: 8,
  weather: 'clear',
  wanted: 0,
  money: 30,
  seed: 42,
};

const player = {
  x: 600,
  y: 350,
  vx: 0,
  vy: 0,
  hp: 100,
  stamina: 100,
  deadEye: 100,
  ammo: 24,
  speed: 2.3,
  horseMounted: false,
  inventoryOpen: false,
  inventory: { food: 4, medicine: 2, pelts: 0, gold: 0 },
};

const horse = { x: 560, y: 390, hp: 100, speed: 3.8 };
const bullets = [];
const enemies = [];
const npcs = [];
const loot = [];

const quests = [
  { id: 'q1', name: 'Stagecoach Ambush', desc: 'Defeat 4 outlaws near the trail.', goal: 4, progress: 0, reward: 20, done: false },
  { id: 'q2', name: 'Hunter Path', desc: 'Collect 3 pelts from wildlife.', goal: 3, progress: 0, reward: 30, done: false },
];

for (let i = 0; i < 6; i++) npcs.push({ x: 100 + i * 160, y: 100 + (i % 3) * 150, mood: Math.random() });
for (let i = 0; i < 5; i++) enemies.push(spawnEnemy());

function spawnEnemy() {
  return { x: Math.random() * 1100 + 50, y: Math.random() * 600 + 50, hp: 35, cooldown: Math.random() * 80, alive: true };
}

function addLog(text, cls = '') {
  log.unshift(`<span class="${cls}">${text}</span>`);
  if (log.length > 6) log.pop();
}

window.addEventListener('keydown', e => {
  keys[e.key.toLowerCase()] = true;
  if (e.key.toLowerCase() === 'i') player.inventoryOpen = !player.inventoryOpen;
  if (e.key.toLowerCase() === 'h') toggleHorse();
  if (e.key.toLowerCase() === 'c') restAtCamp();
  if (e.key.toLowerCase() === 'e') interact();
});
window.addEventListener('keyup', e => (keys[e.key.toLowerCase()] = false));
canvas.addEventListener('mousemove', e => {
  const r = canvas.getBoundingClientRect();
  mouse.x = (e.clientX - r.left) * (canvas.width / r.width);
  mouse.y = (e.clientY - r.top) * (canvas.height / r.height);
});
canvas.addEventListener('mousedown', () => (mouse.down = true));
window.addEventListener('mouseup', () => (mouse.down = false));

function toggleHorse() {
  const d = Math.hypot(player.x - horse.x, player.y - horse.y);
  if (d < 50) {
    player.horseMounted = !player.horseMounted;
    addLog(player.horseMounted ? 'Mounted horse.' : 'Dismounted horse.', 'good');
  }
}

function restAtCamp() {
  if (player.stamina < 80) {
    player.stamina = Math.min(100, player.stamina + 40);
    player.hp = Math.min(100, player.hp + 20);
    world.time = (world.time + 4) % 24;
    addLog('You set camp and recovered.', 'good');
  }
}

function interact() {
  for (const item of loot) {
    if (!item.taken && Math.hypot(player.x - item.x, player.y - item.y) < 25) {
      item.taken = true;
      if (item.type === 'money') world.money += item.value;
      if (item.type === 'pelt') {
        player.inventory.pelts += item.value;
        progressQuest('q2', item.value);
      }
      addLog(`Picked up ${item.type}.`, 'good');
    }
  }
}

function progressQuest(id, amt) {
  const q = quests.find(q => q.id === id && !q.done);
  if (!q) return;
  q.progress += amt;
  if (q.progress >= q.goal) {
    q.done = true;
    world.money += q.reward;
    addLog(`Quest complete: ${q.name} (+$${q.reward})`, 'good');
  }
}

function shoot() {
  if (!mouse.down || player.ammo <= 0) return;
  if (shoot.cooldown > 0) return;
  shoot.cooldown = 12;
  player.ammo--;
  const angle = Math.atan2(mouse.y - player.y, mouse.x - player.x);
  bullets.push({ x: player.x, y: player.y, vx: Math.cos(angle) * 8, vy: Math.sin(angle) * 8, ttl: 70, fromPlayer: true });
}
shoot.cooldown = 0;

function updatePlayer() {
  const spd = player.horseMounted ? horse.speed : player.speed;
  player.vx = ((keys.d ? 1 : 0) - (keys.a ? 1 : 0)) * spd;
  player.vy = ((keys.s ? 1 : 0) - (keys.w ? 1 : 0)) * spd;
  player.x = Math.max(20, Math.min(canvas.width - 20, player.x + player.vx));
  player.y = Math.max(20, Math.min(canvas.height - 20, player.y + player.vy));
  if (!player.horseMounted) {
    player.stamina = Math.max(0, player.stamina - (Math.abs(player.vx) + Math.abs(player.vy)) * 0.012);
  } else {
    horse.x = player.x - 15;
    horse.y = player.y + 10;
  }
}

function updateEnemies() {
  for (const e of enemies) {
    if (!e.alive) continue;
    const d = Math.hypot(player.x - e.x, player.y - e.y);
    if (d < 260) {
      e.x += ((player.x - e.x) / d) * 1.2;
      e.y += ((player.y - e.y) / d) * 1.2;
      e.cooldown--;
      if (e.cooldown <= 0) {
        e.cooldown = 90;
        const a = Math.atan2(player.y - e.y, player.x - e.x);
        bullets.push({ x: e.x, y: e.y, vx: Math.cos(a) * 4, vy: Math.sin(a) * 4, ttl: 120, fromPlayer: false });
      }
    }
  }
}

function updateBullets() {
  for (const b of bullets) {
    b.x += b.vx; b.y += b.vy; b.ttl--;
    if (b.fromPlayer) {
      for (const e of enemies) {
        if (e.alive && Math.hypot(e.x - b.x, e.y - b.y) < 16) {
          e.hp -= 18; b.ttl = 0;
          if (e.hp <= 0) {
            e.alive = false;
            progressQuest('q1', 1);
            world.wanted += 5;
            loot.push({ x: e.x, y: e.y, type: Math.random() > 0.5 ? 'money' : 'pelt', value: Math.ceil(Math.random() * 2), taken: false });
          }
        }
      }
    } else if (Math.hypot(player.x - b.x, player.y - b.y) < 14) {
      player.hp -= 8;
      b.ttl = 0;
      world.wanted = Math.min(100, world.wanted + 2);
      addLog('You were hit by gunfire.', 'warning');
    }
  }
  for (let i = bullets.length - 1; i >= 0; i--) if (bullets[i].ttl <= 0) bullets.splice(i, 1);
}

function updateWorld() {
  world.time = (world.time + 0.004) % 24;
  if (Math.random() < 0.0008) world.weather = ['clear', 'fog', 'rain'][Math.floor(Math.random() * 3)];
  if (world.wanted > 0) world.wanted -= 0.01;
  shoot.cooldown = Math.max(0, shoot.cooldown - 1);
  if (enemies.filter(e => e.alive).length < 3) enemies.push(spawnEnemy());
}

function drawWorld() {
  const day = Math.max(0.1, Math.sin((world.time / 24) * Math.PI));
  ctx.fillStyle = `rgba(${60 + day * 80}, ${40 + day * 50}, ${25 + day * 35},1)`;
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  // terrain patches
  for (let i = 0; i < 40; i++) {
    const x = (i * 97 + world.seed * 17) % canvas.width;
    const y = (i * 57 + world.seed * 11) % canvas.height;
    ctx.fillStyle = i % 2 ? '#6f5434' : '#574123';
    ctx.fillRect(x, y, 45, 30);
  }
  if (world.weather === 'fog') {
    ctx.fillStyle = 'rgba(200,200,220,0.17)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  if (world.weather === 'rain') {
    ctx.strokeStyle = 'rgba(170,190,255,0.4)';
    for (let i = 0; i < 180; i++) {
      const x = (i * 73 + performance.now() * 0.2) % canvas.width;
      const y = (i * 41 + performance.now() * 0.5) % canvas.height;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - 4, y + 10); ctx.stroke();
    }
  }
}

function circle(x, y, r, c) { ctx.fillStyle = c; ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill(); }

function drawEntities() {
  if (!player.horseMounted) circle(horse.x, horse.y, 14, '#8d6a47');
  for (const n of npcs) circle(n.x + Math.sin(performance.now() * 0.001 + n.mood) * 3, n.y, 10, '#d8bc97');
  for (const e of enemies) if (e.alive) circle(e.x, e.y, 12, '#4f1f1a');
  for (const b of bullets) circle(b.x, b.y, 3, b.fromPlayer ? '#ffe7a2' : '#ff6d66');
  for (const l of loot) if (!l.taken) circle(l.x, l.y, 6, l.type === 'money' ? '#f2d06b' : '#d0d0d0');
  circle(player.x, player.y, player.horseMounted ? 15 : 12, '#68a0d8');
}

function drawHud() {
  const active = quests.filter(q => !q.done).map(q => `${q.name}: ${q.progress}/${q.goal}`).join(' • ') || 'All missions complete';
  statsEl.innerHTML = `HP ${player.hp.toFixed(0)} | STA ${player.stamina.toFixed(0)} | DeadEye ${player.deadEye.toFixed(0)} | Ammo ${player.ammo} | $${world.money} | Wanted ${world.wanted.toFixed(0)}%`;
  questEl.textContent = `Quest: ${active}`;
  logEl.innerHTML = log.join('<br>');

  if (player.inventoryOpen) {
    ctx.fillStyle = 'rgba(0,0,0,0.65)'; ctx.fillRect(430, 180, 340, 220);
    ctx.fillStyle = '#f5e6ce'; ctx.font = '20px sans-serif';
    ctx.fillText('Inventory', 550, 220);
    ctx.font = '16px sans-serif';
    const items = Object.entries(player.inventory);
    items.forEach(([k, v], i) => ctx.fillText(`${k}: ${v}`, 520, 260 + i * 30));
  }
}

function gameLoop() {
  updatePlayer();
  updateEnemies();
  updateBullets();
  updateWorld();
  shoot();
  drawWorld();
  drawEntities();
  drawHud();

  if (player.hp <= 0) {
    addLog('You died. Refresh to restart.', 'warning');
    return;
  }
  requestAnimationFrame(gameLoop);
}

addLog('Welcome to Frontier Legends 2D.', 'good');
addLog('Take missions and survive the frontier.');
gameLoop();
