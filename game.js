const ws = new WebSocket(`ws://${location.host}/ws`);
const suitSymbols = { hearts: '♥', diamonds: '♦', clubs: '♣', spades: '♠' };
const suitColors = { hearts: '#e94560', diamonds: '#e94560', clubs: '#eee', spades: '#eee' };

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

// Bidding elements
const biddingPanel = document.getElementById('biddingPanel');
const biddingStatus = document.getElementById('biddingStatus');
const bidSelector = document.getElementById('bidSelector');
const bidHistoryEl = document.getElementById('bidHistory');
const trumpSelectPanel = document.getElementById('trumpSelectPanel');
const trumpSelectHint = document.getElementById('trumpSelectHint');

let mySeat = null;
let hostSeat = null;
let players = {};          // seat -> name
let playerStatus = {};     // seat -> connected
let handCounts = {};
let teams = { A: { match: 0, points: 0, tricks: 0 }, B: { match: 0, points: 0, tricks: 0 } };
let trump = null;
let currentPlayer = null;
let matchTarget = 6;          // first team to this many match points wins
let gameState = 'waiting';  // waiting | bidding | dealing2 | playing | finished

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
    // `match` is the score (first to +6 wins); `points`/`tricks` are this deal.
    scoreA.textContent = teams.A.match;
    scoreB.textContent = teams.B.match;
    cardsA.textContent = `Points: ${teams.A.points} · Tricks: ${teams.A.tricks}`;
    cardsB.textContent = `Points: ${teams.B.points} · Tricks: ${teams.B.tricks}`;
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
// Bidding UI
// ----------------------------------------------------------------------
function showBidSelector() {
    bidSelector.style.display = bidSelector.style.display === 'none' ? 'flex' : 'none';
}

function confirmBid(val) {
    // The bid is a number of points; the winner declares trump afterwards.
    send({ type: 'bid', bidValue: val });
    bidSelector.style.display = 'none';
}

function updateBiddingUI(data) {
    biddingPanel.style.display = 'block';
    trumpSelectPanel.style.display = 'none';
    bidSelector.style.display = 'none';

    const name = seatName(data.nextBidder ?? mySeat);
    biddingStatus.textContent = data.nextBidder === mySeat
        ? 'Your turn to bid!'
        : `${players[data.nextBidder] || 'Player'} is bidding...`;

    // Show/hide controls based on whose turn it is
    const bidBtns = biddingPanel.querySelector('.bid-buttons');
    if (data.nextBidder === mySeat) {
        bidBtns.style.display = 'flex';
    } else {
        bidBtns.style.display = 'none';
    }

    // Show highest bid
    const currentHighest = data.highestBid ?? 0;
    if (currentHighest > 0) {
        biddingStatus.textContent += ` (Highest: ${currentHighest} pts)`;
    }

    // Only offer bids that beat the current highest
    bidSelector.querySelectorAll('.bid-opt').forEach(btn => {
        btn.disabled = Number(btn.textContent.trim()) <= currentHighest;
    });

    // Show bid history
    if (data.bids) {
        bidHistoryEl.innerHTML = Object.entries(data.bids).map(([seat, bid]) => {
            const s = Number(seat);   // JSON object keys arrive as strings
            const bidText = bid ? `${seatName(s)} bid ${bid} pts` : `${seatName(s)} passed`;
            return `<div>${bidText}</div>`;
        }).join('');
    }
}

function selectTrump(suit) {
    send({ type: 'select-trump', trumpSuit: suit });
    trumpSelectPanel.style.display = 'none';
}

function showTrumpSelection(data) {
    trumpSelectPanel.style.display = 'block';
    biddingPanel.style.display = 'none';
    trumpSelectHint.textContent = `You won with a bid of ${data.winningBid.bid_value}. Choose your trump suit.`;

    // Highlight suits the player holds
    const btns = trumpSelectPanel.querySelectorAll('.trump-opt');
    btns.forEach(btn => {
        const suit = btn.getAttribute('onclick').match(/'([^']+)'/)[1];
        const hasSuit = currentHand.some(c => c.suit === suit);
        btn.style.opacity = hasSuit ? '1' : '0.4';
        btn.disabled = !hasSuit;
    });
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
    gameState = data.state;
    if (data.matchTarget) matchTarget = data.matchTarget;

    // In bidding phase, only show first 4 cards
    // In other phases, show full hand
    const displayHand = data.state === 'bidding' ? (data.yourHand || []) : (data.fullHand || data.yourHand || []);
    currentHand = displayHand;

    setTeamTitles();
    renderHand();
    renderTable();
    renderTrick(data.currentTrick || []);

    if (trump) {
        trumpSuitEl.textContent = suitSymbols[trump] || trump;
        trumpSuitEl.style.color = suitColors[trump] || '#eee';
    } else {
        trumpSuitEl.textContent = '?';
        trumpSuitEl.style.color = '#888';
    }

    cancelGameBtn.style.display = (mySeat === hostSeat && data.state !== 'waiting') ? 'block' : 'none';

    // Handle bidding UI
    if (data.state === 'bidding') {
        updateBiddingUI(data);
    } else {
        biddingPanel.style.display = 'none';
    }

    // Handle trump selection
    if (data.state === 'dealing2') {
        showTrumpSelection(data);
    } else {
        trumpSelectPanel.style.display = 'none';
    }

    if (data.gameOver) showGameOver(data.gameOver, true);
}

function showGameOver(over, immediate) {
    teams = over.teams || teams;
    updateScoreboard();
    const show = () => {
        const myTeam = mySeat % 2 === 0 ? 'A' : 'B';
        if (over.matchOver && over.winningTeam) {
            const iWon = over.winningTeam === myTeam;
            resultTitle.textContent = iWon ? '🏆 You win the match!'
                                           : `Team ${over.winningTeam} wins the match!`;
            resultMessage.textContent = `Final score ${teams.A.match} – ${teams.B.match}`;
        } else {
            // Round over, match continues: did the bidding team make its contract?
            const made = over.bidMade;
            const bidderOnMyTeam = over.bidderTeam === myTeam;
            if (made) {
                resultTitle.textContent = bidderOnMyTeam ? '✅ Contract made!'
                                                        : '❌ They made the contract';
            } else {
                resultTitle.textContent = bidderOnMyTeam ? '❌ Contract missed'
                                                        : '✅ You stopped them!';
            }
            const bidderName = players[over.bidderSeat] || 'The bidder';
            resultMessage.textContent =
                `${bidderName} bid ${over.bid} and took ${over.bidderPoints} points. ` +
                `Score ${teams.A.match} – ${teams.B.match} (first to ${matchTarget}).`;
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
            // Full hand update (after bidding completes or card played)
            currentHand = data.cards;
            renderHand();
            break;

        case 'bid-placed':
            // Update bidding UI for all players (always, to keep live history).
            // updateBiddingUI() already hides the bid buttons when it isn't
            // your turn, so the panel stays up with status + history.
            updateBiddingUI(data);
            if (mySeat === data.seat) {
                showToast(data.bid > 0 ? `You bid ${data.bid} pts` : 'You passed');
            }
            break;

        case 'bidding-complete':
            // Bidding is done — the winner now declares trump
            if (data.winningBid) {
                biddingPanel.style.display = 'none';
                trumpSelectPanel.style.display = 'none';
                showToast(`🎉 ${seatName(data.winningBid.bidder)} won the auction at ${data.winningBid.bid_value} pts`);
            }
            break;

        case 'redeal':
            showToast(data.message || 'All four passed — redeal');
            biddingPanel.style.display = 'none';
            trumpSelectPanel.style.display = 'none';
            bidHistoryEl.innerHTML = '';
            break;

        case 'trump-selected':
            trump = data.trumpSuit;
            trumpSuitEl.textContent = suitSymbols[trump] || trump;
            trumpSuitEl.style.color = suitColors[trump] || '#eee';
            showToast(`Trump is ${suitSymbols[trump] || trump}`);
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
            teams = data.teams;
            updateScoreboard();
            renderTrick([]);
            trickBanner.classList.remove('show');
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

// Expose bid functions globally (used by the inline onclick handlers)
window.placeBid = (val) => send({ type: 'bid', bidValue: val });
window.confirmBid = confirmBid;
window.selectTrump = selectTrump;
