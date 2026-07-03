const teamsEl = document.getElementById("teams");
const detailEl = document.getElementById("team-detail");
const collectBtn = document.getElementById("collect-btn");
const collectStatusEl = document.getElementById("collect-status");

let activeTeamId = null;
let activeTab = "composition";
let pollTimer = null;

function formatMmr(value) {
  return value == null ? "?" : Math.round(value).toLocaleString("ru-RU");
}

function heroIconUrl(slug) {
  return `https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/icons/${slug}.png`;
}

function renderDraftTeamRow(teamName, entries) {
  const cells = entries
    .map((e) => `
      <div class="draft-cell ${e.is_pick ? "cell-pick" : "cell-ban"}">
        <span class="cell-order">${e.order + 1}</span>
        <img src="${heroIconUrl(e.hero_icon)}" alt="${e.hero}" title="${e.hero}">
        <span class="cell-label">${e.is_pick ? "PICK" : "BAN"}</span>
      </div>
    `)
    .join("");
  return `
    <div class="draft-team-row">
      <span class="draft-team-label">${teamName}</span>
      <div class="draft-cells">${cells || '<span class="hint">нет данных</span>'}</div>
    </div>
  `;
}

async function loadTeams() {
  const res = await fetch("/api/teams");
  const teams = await res.json();

  teamsEl.innerHTML = "";
  if (teams.length === 0) {
    teamsEl.innerHTML = '<p class="hint">Нет данных. Нажмите «Обновить матчи».</p>';
    return;
  }

  for (const team of teams) {
    const btn = document.createElement("button");
    btn.className = "team-btn" + (team.team_id === activeTeamId ? " active" : "");
    btn.textContent = `${team.name} (${team.player_count}) · ${formatMmr(team.total_mmr)} MMR`;
    btn.onclick = () => loadTeamDetail(team.team_id);
    teamsEl.appendChild(btn);
  }
}

function renderComposition(team) {
  const container = document.createElement("div");

  const grid = document.createElement("div");
  grid.className = "players-grid";
  for (const player of team.players) {
    const card = document.createElement("div");
    card.className = "player-card";
    const heroItems = player.heroes
      .map((h) => {
        const wr = h.win_rate == null ? "" : `<span class="winrate ${h.win_rate >= 50 ? "wr-good" : "wr-bad"}">${h.win_rate}%</span>`;
        return `<li><span>${h.name}</span><span>${wr}<span class="count">×${h.games}</span></span></li>`;
      })
      .join("");
    card.innerHTML = `<h3>${player.name}</h3><p class="mmr">${formatMmr(player.mmr)} MMR</p><ul>${heroItems}</ul>`;
    grid.appendChild(card);
  }

  const mmrLine = document.createElement("p");
  mmrLine.className = "total-mmr";
  mmrLine.textContent = `Суммарный MMR: ${formatMmr(team.total_mmr)}`;
  container.appendChild(mmrLine);
  container.appendChild(grid);

  if (team.recent_drafts && team.recent_drafts.length > 0) {
    const draftsSection = document.createElement("div");
    draftsSection.className = "drafts-section";
    draftsSection.innerHTML = "<h3>Последние драфты</h3>";
    for (const draft of team.recent_drafts) {
      const match = document.createElement("div");
      match.className = "draft-match";
      match.innerHTML =
        renderDraftTeamRow(team.name, draft.team_entries) +
        renderDraftTeamRow(draft.opponent_name, draft.opponent_entries);
      draftsSection.appendChild(match);
    }
    container.appendChild(draftsSection);
  }

  return container;
}

function heroTagList(items, cssClass) {
  if (!items || items.length === 0) return '<span class="hint">нет данных</span>';
  return items.map((i) => `<span class="tag ${cssClass}">${i.hero} ×${i.count}</span>`).join("");
}

async function renderAnalysisTab(teamId, container) {
  container.innerHTML = '<p class="hint">Считаю аналитику...</p>';
  const res = await fetch(`/api/teams/${teamId}/analysis`);
  if (!res.ok) {
    container.innerHTML = '<p class="hint">Не удалось получить аналитику.</p>';
    return;
  }
  const a = await res.json();

  const signatureHtml = a.signature_heroes.length
    ? a.signature_heroes
        .map((h) => `<span class="tag tag-pick">${h.hero} — ${h.win_rate}% (${h.wins}/${h.games})</span>`)
        .join("")
    : '<span class="hint">нет данных</span>';

  container.innerHTML = `
    <p class="coach-text">${a.text}</p>
    <div class="analysis-grid">
      <div class="analysis-block">
        <h4>Топ пиков</h4>
        <div class="tag-list">${heroTagList(a.top_picks, "tag-neutral")}</div>
      </div>
      <div class="analysis-block">
        <h4>Коронные герои (win rate)</h4>
        <div class="tag-list">${signatureHtml}</div>
      </div>
      <div class="analysis-block">
        <h4>Первый пик</h4>
        <div class="tag-list">${heroTagList(a.first_picks, "tag-neutral")}</div>
      </div>
      <div class="analysis-block">
        <h4>Что банят соперники</h4>
        <div class="tag-list">${heroTagList(a.enemy_bans, "tag-ban")}</div>
      </div>
      <div class="analysis-block">
        <h4>Что банит команда сама</h4>
        <div class="tag-list">${heroTagList(a.own_bans, "tag-ban")}</div>
      </div>
    </div>
  `;
}

async function loadTeamDetail(teamId, tab) {
  activeTeamId = teamId;
  activeTab = tab || "composition";
  for (const btn of teamsEl.querySelectorAll(".team-btn")) {
    btn.classList.remove("active");
  }

  const res = await fetch(`/api/teams/${teamId}`);
  if (!res.ok) {
    detailEl.innerHTML = '<p class="hint">Команда не найдена.</p>';
    return;
  }
  const team = await res.json();

  detailEl.innerHTML = `
    <h2>${team.name}</h2>
    <div class="tabs">
      <button class="tab-btn" data-tab="composition">Состав</button>
      <button class="tab-btn" data-tab="analysis">Аналитика</button>
    </div>
    <div id="tab-content"></div>
  `;

  const tabContent = detailEl.querySelector("#tab-content");
  const tabButtons = detailEl.querySelectorAll(".tab-btn");

  function showTab(tab) {
    activeTab = tab;
    for (const btn of tabButtons) {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    }
    if (tab === "composition") {
      tabContent.innerHTML = "";
      tabContent.appendChild(renderComposition(team));
    } else {
      renderAnalysisTab(teamId, tabContent);
    }
  }

  for (const btn of tabButtons) {
    btn.addEventListener("click", () => showTab(btn.dataset.tab));
  }

  showTab(activeTab);
  loadTeams();
}

async function pollCollectStatus() {
  const res = await fetch("/api/collect/status");
  const status = await res.json();
  const lastLine = status.log[status.log.length - 1] || "";

  if (status.running) {
    collectStatusEl.textContent = `Сбор данных... ${lastLine}`;
  } else if (status.error) {
    collectStatusEl.textContent = `Ошибка: ${status.error}`;
  } else if (status.new_matches != null) {
    collectStatusEl.textContent = `Готово. Новых матчей: ${status.new_matches}`;
  } else {
    collectStatusEl.textContent = "";
  }

  if (!status.running) {
    clearInterval(pollTimer);
    pollTimer = null;
    collectBtn.disabled = false;
    loadTeams();
    if (activeTeamId != null) {
      loadTeamDetail(activeTeamId, activeTab);
    }
  }
}

collectBtn.addEventListener("click", async () => {
  collectBtn.disabled = true;
  const res = await fetch("/api/collect", { method: "POST" });
  if (res.status === 409) {
    collectBtn.disabled = false;
    return;
  }
  pollTimer = setInterval(pollCollectStatus, 1000);
  pollCollectStatus();
});

loadTeams();
