const ws = new WebSocket(`ws://${location.host}/ws`);
const playerNameInput = document.getElementById('playerName');
const createBtn = document.getElementById('createRoom');
const joinBtn = document.getElementById('joinRoom');
const roomCodeInput = document.getElementById('roomCode');
const roomDisplay = document.getElementById('roomDisplay');
const displayRoomCode = document.getElementById('displayRoomCode');
const playersList = document.getElementById('playersList');
const startGameBtn = document.getElementById('startGame');
const copyBtn = document.getElementById('copyCode');
const errorMsg = document.getElementById('errorMsg');
const lobbyHint = document.getElementById('lobbyHint');

let mySeat = null;
let hostSeat = null;
let playerArray = [];

function saveSession(d) {
    sessionStorage.setItem('room29', JSON.stringify(d));
}

function loadSession() {
    try { return JSON.parse(sessionStorage.getItem('room29')); }
    catch (e) { return null; }
}

function send(data) {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(data));
}

createBtn.addEventListener('click', () => {
    const name = playerNameInput.value.trim() || 'Player 1';
    send({ type: 'create-room', playerName: name });
});

joinBtn.addEventListener('click', () => {
    const name = playerNameInput.value.trim() || 'Player 2';
    const code = roomCodeInput.value.trim().toUpperCase();
    if (!code) { showError('Please enter a room code'); return; }
    send({ type: 'join-room', roomId: code, playerName: name });
});

startGameBtn.addEventListener('click', () => { send({ type: 'start-game' }); });

copyBtn.addEventListener('click', () => {
    navigator.clipboard.writeText(displayRoomCode.textContent);
    copyBtn.textContent = '✅ Copied!';
    setTimeout(() => { copyBtn.textContent = '📋 Copy'; }, 2000);
});

ws.onopen = () => {
    console.log('Connected to server');
    const session = loadSession();
    if (session) send({ type: 'resume', ...session });
};

ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    switch (data.type) {
        case 'room-created':
        case 'room-joined':
            saveSession({ roomId: data.roomId, seat: data.seat, token: data.token });
            mySeat = data.seat;
            hostSeat = data.hostSeat;
            playerArray = data.players;
            displayRoomCode.textContent = data.roomId;
            roomDisplay.style.display = 'block';
            errorMsg.textContent = '';
            refreshLobby();
            break;

        case 'room-updated':
            playerArray = data.players || playerArray;
            if (data.hostSeat !== undefined) hostSeat = data.hostSeat;
            refreshLobby();
            break;

        case 'room-error':
            showError(data.message);
            break;

        case 'resume-failed':
            sessionStorage.removeItem('room29');
            if (data.message) showError(data.message);
            break;

        case 'game-state':
            // We were restored into a room that is already running.
            if (data.state === 'playing') {
                window.location.href = 'game.html';
            } else if (data.state === 'waiting') {
                mySeat = data.yourSeat;
                hostSeat = data.hostSeat;
                playerArray = Object.entries(data.players).map(([seat, name]) => ({
                    seat: Number(seat),
                    name: name,
                    connected: data.playerStatus ? data.playerStatus[seat] !== false : true,
                }));
                displayRoomCode.textContent = data.roomId;
                roomDisplay.style.display = 'block';
                refreshLobby();
            } else {
                // Round finished: stay in the lobby and wait for the host
                // to start the next one (or create/join another room).
                mySeat = data.yourSeat;
                hostSeat = data.hostSeat;
                playerArray = Object.entries(data.players).map(([seat, name]) => ({
                    seat: Number(seat),
                    name: name,
                    connected: data.playerStatus ? data.playerStatus[seat] !== false : true,
                }));
                displayRoomCode.textContent = data.roomId;
                roomDisplay.style.display = 'block';
                refreshLobby('Round finished — the host can start the next one.');
            }
            break;

        case 'game-started':
            if (!data.roomId || data.roomId === (loadSession() || {}).roomId) {
                window.location.href = 'game.html';
            }
            break;

        case 'player-status':
            if (Array.isArray(data.players)) {
                playerArray = data.players;
                refreshLobby();
            }
            break;
    }
};

function refreshLobby(statusText) {
    playersList.innerHTML = '<h3>Players</h3>';
    playerArray.forEach(p => {
        const item = document.createElement('div');
        item.className = 'player-item';
        const seat = document.createElement('span');
        seat.className = 'seat-num';
        seat.textContent = `Seat ${p.seat}:`;
        const name = document.createElement('span');
        name.textContent = p.name
            + (p.seat === hostSeat ? ' 👑' : '')
            + (p.connected === false ? ' (away)' : '');
        item.appendChild(seat);
        item.appendChild(name);
        playersList.appendChild(item);
    });

    const isHost = mySeat === hostSeat;
    const full = playerArray.length >= 4;
    startGameBtn.style.display = (isHost && full) ? 'block' : 'none';
    if (lobbyHint) {
        if (statusText) {
            lobbyHint.textContent = statusText;
        } else if (!isHost) {
            lobbyHint.textContent = full ? 'Waiting for the host to start the game…'
                                         : 'Waiting for more players…';
        } else {
            lobbyHint.textContent = full ? 'You are the host — press Start Game!'
                                         : `Waiting for players (${playerArray.length}/4)…`;
        }
    }
}

function showError(msg) {
    errorMsg.textContent = msg;
    setTimeout(() => { errorMsg.textContent = ''; }, 3000);
}

roomCodeInput.addEventListener('keypress', (e) => { if (e.key === 'Enter') joinBtn.click(); });
