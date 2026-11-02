const ws = new WebSocket(`ws://${location.host}/ws`);
const suitSymbols = { hearts: '♥', diamonds: '♦', clubs: '♣', spades: '♠' };

// Position containers: you always sit at the bottom, partner opposite.
const positions = { bottom: 0, right: 1, top: 2, left: 3 };

const scoreA = document.getElementById('scoreA');
const scoreB = document.getElementById('scoreB');
const cardsA = document.getElementById('cardsA');
const cardsB = document.getElementById('cardsB');
const teamATitle = document.getElementById('teamATitle');
const teamBTitle = document.getElementById('teamBTitle');
const trumpSuitEl = document.getElementById('trumpSuit');
const trickCardsEl = document.getElementById('trickCards');
const trickBanner = document.getElementById('trickBanner');
const turnIndicator = document.getElementById('turnIndicator');
const resultOverlay = document.getElementById('resultOverlay');
const resultTitle = document.getElementById('resultTitle');
const resultMessage = document.getElementById('resultMessage');
const playAgainBtn = document.getElementById('playAgain');
const playAgainHint = document.getElementById('playAgainHint');
const chatMessages = document.getElementById('chatMessages');
const chatInput = document.getElementById('chatInput');
const chatSend = document.getElementById('chatSend');
const backToLobby = document.getElementById('backToLobby');
const cancelGameBtn = document.getElementById('cancelGame');
const gameToast = document.getElementById('gameToast');
const yourHand = document.getElementById('cards0');

let mySeat = null;
let hostSeat = null;
let players = {};          // seat -> name
let playerStatus = {};     // seat -> connected
let handCounts = {};
let teams = { A: { score: 0, cards: 0 }, B: { score: 0, cards: 0 } };
let trump = null;
let currentPlayer = null;

// ----------------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------------
function send(data) {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(data));
}

function saveSession(d) {
    sessionStorage.setItem('room29', JSON.stringify(d));
}

function loadSession() {
    try { return JSON.parse(sessionStorage.getItem('room29')); }
    catch (e) { return null; }
}

function seatName(seat) {
    if (seat === mySeat) return `${players[seat] || 'You'} (You)`;
    const rel = ((seat - mySeat) + 4) % 4;
    if (rel === 2) return `${players[seat] || 'Player'} (Partner)`;
    return players[seat] || 'Player';
}

function showToast(msg) {
    gameToast.textContent = msg;
    gameToast.classList.add('show');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => gameToast.classList.remove('show'), 2600);
}

// Relative position (0=bottom/me, 1=right, 2=top, 3=left) of a seat.
function relPos(seat) {
    return ((seat - mySeat) + 4) % 4;
}

function posContainer(rel) {
    const name = ['bottom', 'right', 'top', 'left'][rel];
    return document.getElementById(`pos-${name}`);
}

// ----------------------------------------------------------------------
// Rendering
// ----------------------------------------------------------------------
function renderHand() {
    yourHand.innerHTML = currentHand.map((c, i) =>
        `<div class="card ${c.suit}" data-index="${i}" onclick="playCard(${i})">
            <span class="rank">${c.rank}</span><span class="suit">${suitSymbols[c.suit]}</span>
        </div>`
    ).join('');
}

let currentHand = [];

function renderTable() {
    // Opponent stacks + names
    for (let rel = 0; rel <= 3; rel++) {
        const container = posContainer(rel);
        if (!container) continue;
        const seat = mySeat === null ? rel : (mySeat + rel) % 4;
        const nameEl = container.querySelector('.player-name');
        if (nameEl) {
            nameEl.textContent = rel === 0 ? seatName(mySeat) : seatName(seat);
            const status = playerStatus[seat];
            container.classList.toggle('disconnected', status === false);
            container.classList.toggle('current', currentPlayer === seat);
        }
        const stack = container.querySelector('.cards-container');
        if (stack && rel !== 0) {
            const count = handCounts[seat] || 0;
            stack.innerHTML = Array(count).fill(
                '<div class="card back"></div>'
            ).join('');
        }
    }
    updateScoreboard();
    updateTurnIndicator();
}

function renderTrick(trick) {
    const slots = trickCardsEl.querySelectorAll('.trick-slot .trick-card');
    slots.forEach(s => { s.innerHTML = ''; s.className = 'trick-card empty'; });
    trick.forEach(({ seat, card }) => {
        const idx = mySeat === null ? seat : relPos(seat);
        const slot = slots[idx];
        if (!slot) return;
        slot.className = `trick-card card ${card.suit}`;
        slot.innerHTML = `<span class="rank">${card.rank}</span><span class="suit">${suitSymbols[card.suit]}</span>`;
    });
}

function updateScoreboard() {
    scoreA.textContent = teams.A.score;
    scoreB.textContent = teams.B.score;
    cardsA.textContent = `Cards: ${teams.A.cards}`;
    cardsB.textContent = `Cards: ${teams.B.cards}`;
}

function updateTurnIndicator() {
    if (currentPlayer === null) {
        turnIndicator.textContent = 'Finishing trick...';
        return;
    }
    turnIndicator.textContent = currentPlayer === mySeat
        ? 'Your card!'
        : `${players[currentPlayer] || 'Player'}'s turn`;
    yourHand.classList.toggle('not-your-turn', currentPlayer !== mySeat);
}

function setTeamTitles() {
    const myTeam = mySeat % 2 === 0 ? 'A' : 'B';
    teamATitle.textContent = myTeam === 'A' ? 'Team A (You + Partner)' : 'Team A (Opponents)';
    teamBTitle.textContent = myTeam === 'B' ? 'Team B (You + Partner)' : 'Team B (Opponents)';
}

// ----------------------------------------------------------------------
// Game state
// ----------------------------------------------------------------------
function applyState(data) {
    mySeat = data.yourSeat;
    hostSeat = data.hostSeat;
    players = data.players || {};
    playerStatus = data.playerStatus || {};
    handCounts = data.handCounts || {};
    teams = data.teams || teams;
    trump = data.trumpSuit;
    currentPlayer = data.currentPlayer;
    currentHand = data.yourHand || [];

    setTeamTitles();
    renderHand();
    renderTable();
    renderTrick(data.currentTrick || []);
    trumpSuitEl.textContent = trump ? (suitSymbols[trump] || trump) : '?';
    cancelGameBtn.style.display = (mySeat === hostSeat && data.state !== 'waiting') ? 'block' : 'none';

    if (data.gameOver) showGameOver(data.gameOver, true);
}

function showGameOver(over, immediate) {
    teams = over.teams || teams;
    updateScoreboard();
    const show = () => {
        const winner = over.winningTeam;
        if (!winner) {
            resultTitle.textContent = '🤝 It\'s a Draw!';
            resultMessage.textContent = 'Nothing separates the teams this round.';
        } else {
            const myTeam = mySeat % 2 === 0 ? 'A' : 'B';
            const iWon = winner === myTeam;
            resultTitle.textContent = iWon ? '🏆 You Win!' : `Team ${winner} Wins!`;
            resultMessage.textContent = iWon
                ? 'You and your partner take the round!'
                : 'Better luck next time.';
        }
        const iAmHost = mySeat === hostSeat;
        playAgainBtn.style.display = iAmHost ? 'inline-block' : 'none';
        playAgainHint.style.display = iAmHost ? 'none' : 'inline';
        resultOverlay.style.display = 'flex';
    };
    if (immediate) show(); else setTimeout(show, 1200);
}

// ----------------------------------------------------------------------
// Messages
// ----------------------------------------------------------------------
ws.onopen = () => {
    const session = loadSession();
    if (!session) { window.location.href = 'index.html'; return; }
    send({ type: 'resume', ...session });
};

ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    switch (data.type) {
        case 'game-state':
            if (data.state === 'waiting') { window.location.href = 'index.html'; break; }
            applyState(data);
            break;

        case 'resume-failed':
            sessionStorage.removeItem('room29');
            window.location.href = 'index.html';
            break;

        case 'game-started':
            if (!data.roomId || data.roomId === loadSession()?.roomId) window.location.reload();
            break;

        case 'game-cancelled':
            window.location.href = 'index.html';
            break;

        case 'your-hand':
            currentHand = data.cards;
            renderHand();
            break;

        case 'card-played':
            teams = data.teams;
            handCounts = data.handCounts;
            currentPlayer = data.currentPlayer;
            trump = data.trumpSuit ?? trump;
            if (trump) trumpSuitEl.textContent = suitSymbols[trump] || trump;
            renderTrick(data.trick || []);
            renderTable();
            if (data.trickComplete) {
                const winner = data.trickComplete.winnerSeat;
                trickBanner.textContent = `${seatName(winner)} wins the trick!`;
                trickBanner.classList.add('show');
            }
            break;

        case 'trick-ready':
            currentPlayer = data.currentPlayer;
            handCounts = data.handCounts || handCounts;
            renderTrick([]);
            trickBanner.classList.remove('show');
            renderTable();
            break;

        case 'game-over':
            showGameOver(data, false);
            break;

        case 'play-error':
            showToast(data.message);
            break;

        case 'room-error':
            showToast(data.message);
            break;

        case 'player-status':
            if (Array.isArray(data.players)) {
                players = {};
                playerStatus = {};
                data.players.forEach(p => {
                    players[p.seat] = p.name;
                    playerStatus[p.seat] = p.connected;
                });
            }
            renderTable();
            break;

        case 'room-updated':
            if (Array.isArray(data.players)) {
                players = {};
                playerStatus = {};
                data.players.forEach(p => {
                    players[p.seat] = p.name;
                    playerStatus[p.seat] = p.connected;
                });
            }
            if (data.hostSeat !== undefined) hostSeat = data.hostSeat;
            renderTable();
            break;

        case 'chat-message': {
            const el = document.createElement('div');
            el.className = 'chat-message';
            const nameSpan = document.createElement('span');
            nameSpan.className = 'name';
            nameSpan.textContent = `[${players[data.seat] ?? data.playerName}]: `;
            el.appendChild(nameSpan);
            el.appendChild(document.createTextNode(data.message));
            chatMessages.appendChild(el);
            chatMessages.scrollTop = chatMessages.scrollHeight;
            break;
        }
    }
};

// ----------------------------------------------------------------------
// Actions
// ----------------------------------------------------------------------
window.playCard = function (index) {
    send({ type: 'play-card', cardId: index });
};

chatSend.addEventListener('click', () => {
    const msg = chatInput.value.trim();
    if (msg) { send({ type: 'chat', message: msg }); chatInput.value = ''; }
});
chatInput.addEventListener('keypress', (e) => { if (e.key === 'Enter') chatSend.click(); });

playAgainBtn.addEventListener('click', () => {
    resultOverlay.style.display = 'none';
    send({ type: 'start-game' });
});

cancelGameBtn.addEventListener('click', () => {
    if (confirm('Cancel the current game and return everyone to the lobby?')) {
        send({ type: 'cancel-game' });
    }
});

backToLobby.addEventListener('click', () => {
    window.location.href = 'index.html';
});
