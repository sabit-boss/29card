import asyncio
import json
import random
import secrets
from pathlib import Path

from aiohttp import web

PORT = 3000
STATIC_DIR = Path(__file__).parent
ROOMS = {}  # room_id -> Game

# Timing knobs (seconds)
DISCONNECT_ANNOUNCE = 3    # delay before telling the others a player went away
WAITING_FREE = 12          # extra delay before an absent player's lobby seat is freed
EMPTY_GAME_DELETE = 30     # delay before deleting a game room where nobody is connected
TRICK_PAUSE = 1.5          # how long a completed trick stays on the table


class Card:
    SUITS = ['hearts', 'diamonds', 'clubs', 'spades']
    RANKS = ['7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    VALUES = {'A': 11, '10': 10, 'K': 4, 'Q': 3, 'J': 2, '9': 0, '8': 0, '7': 0}
    STRENGTH = {'A': 13, 'K': 12, 'Q': 11, 'J': 10, '10': 9, '9': 8, '8': 7, '7': 6}

    def __init__(self, suit, rank):
        self.suit = suit
        self.rank = rank

    def to_dict(self):
        return {'suit': self.suit, 'rank': self.rank}

    def __repr__(self):
        return f'{self.rank} of {self.suit}'

    @classmethod
    def create_deck(cls):
        return [cls(suit, rank) for suit in cls.SUITS for rank in cls.RANKS]


def card_beats(card, other, trump):
    """True if `card` beats `other` under trump rules."""
    if card.suit == other.suit:
        return Card.STRENGTH[card.rank] > Card.STRENGTH[other.rank]
    return card.suit == trump and other.suit != trump


class Game:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = {}   # seat -> {'ws', 'name', 'token', 'connected'}
        self.hands = {}     # seat -> [Card]
        self.state = 'waiting'   # waiting | playing | finished
        self.trump_suit = None
        self.dealer_seat = None
        self.current_player = None
        self.current_trick = []  # [(seat, Card)]
        self.teams = self._fresh_teams()
        self.round_number = 0
        self.round_id = 0        # invalidates pending trick-pause tasks
        self.host_seat = None
        self.game_over = None    # {'winningTeam': str|None, 'teams': {...}}

    @staticmethod
    def _fresh_teams():
        return {'A': {'score': 0, 'cards': 0}, 'B': {'score': 0, 'cards': 0}}

    @property
    def is_full(self):
        return len(self.players) >= 4

    def add_player(self, ws, name):
        if self.is_full:
            return None
        for seat in range(4):
            if seat not in self.players:
                self.players[seat] = {
                    'ws': ws,
                    'name': name,
                    'token': secrets.token_hex(4),
                    'connected': True,
                }
                if self.host_seat is None:
                    self.host_seat = seat
                return seat
        return None

    def free_seat(self, seat):
        if seat not in self.players:
            return
        del self.players[seat]
        if self.host_seat == seat:
            self.host_seat = min(self.players) if self.players else None

    def get_players_info(self):
        return [
            {'seat': s, 'name': p['name'], 'connected': p['connected']}
            for s, p in sorted(self.players.items())
        ]

    def hand_counts(self):
        return {s: len(h) for s, h in self.hands.items()}

    # ------------------------------------------------------------------
    # Round lifecycle
    # ------------------------------------------------------------------
    def start_game(self):
        if len(self.players) != 4:
            return False
        self.state = 'playing'
        self.round_number += 1
        self.round_id += 1
        self.teams = self._fresh_teams()
        self.game_over = None
        self.current_trick = []
        deck = Card.create_deck()
        random.shuffle(deck)
        for seat in range(4):
            self.hands[seat] = deck[seat * 8:(seat + 1) * 8]
        self.dealer_seat = random.choice(list(self.players.keys()))
        self.trump_suit = self.hands[self.dealer_seat][7].suit
        self.current_player = (self.dealer_seat + 1) % 4
        return True

    def cancel_game(self):
        """Host abort: everyone goes back to the lobby."""
        self.state = 'waiting'
        self.hands = {}
        self.current_trick = []
        self.trump_suit = None
        self.dealer_seat = None
        self.current_player = None
        self.teams = self._fresh_teams()
        self.game_over = None
        self.round_id += 1

    # ------------------------------------------------------------------
    # Play
    # ------------------------------------------------------------------
    def play_card(self, seat, card_index):
        if self.state != 'playing':
            return {'error': 'Game is not in progress'}
        if self.current_player is None:
            return {'error': 'Finishing previous trick, please wait...'}
        if self.current_player != seat:
            return {'error': 'Not your turn'}
        hand = self.hands.get(seat) or []
        if not isinstance(card_index, int) or isinstance(card_index, bool) \
                or card_index < 0 or card_index >= len(hand):
            return {'error': 'Invalid card'}
        if self.current_trick:
            led_suit = self.current_trick[0][1].suit
            if any(c.suit == led_suit for c in hand) and hand[card_index].suit != led_suit:
                return {'error': f'You must follow {led_suit} while you still have it'}

        card = hand.pop(card_index)
        self.current_trick.append((seat, card))

        trick_complete = None
        if len(self.current_trick) == 4:
            winner_seat, points = self.complete_trick()
            # Pause until the clients have shown the trick; the next trick
            # starts via begin_next_trick() (scheduled by the handler).
            self.current_player = None
            trick_complete = {'winnerSeat': winner_seat, 'points': points}
            if sum(len(h) for h in self.hands.values()) == 0:
                self._finish_round()
        else:
            self.current_player = (seat + 1) % 4

        return {
            'ok': True,
            'card': card.to_dict(),
            'trick': [{'seat': s, 'card': c.to_dict()} for s, c in self.current_trick],
            'trickComplete': trick_complete,
        }

    def complete_trick(self):
        """Award the current trick. Returns (winner_seat, points)."""
        winner_seat, winner_card = self.current_trick[0]
        for seat, card in self.current_trick[1:]:
            if card_beats(card, winner_card, self.trump_suit):
                winner_seat, winner_card = seat, card

        points = sum(Card.VALUES[c.rank] for _, c in self.current_trick)
        team = 'A' if winner_seat % 2 == 0 else 'B'
        self.teams[team]['score'] += points
        self.teams[team]['cards'] += 4
        return winner_seat, points

    def _finish_round(self):
        ta = self.teams['A']['cards']
        tb = self.teams['B']['cards']
        if ta > tb:
            winner = 'A'
        elif tb > ta:
            winner = 'B'
        else:
            # Same number of tricks: fall back to card points, then a draw.
            sa, sb = self.teams['A']['score'], self.teams['B']['score']
            winner = 'A' if sa > sb else ('B' if sb > sa else None)
        if winner:
            self.teams[winner]['score'] += 29
        self.game_over = {
            'winningTeam': winner,
            'teams': json.loads(json.dumps(self.teams)),
        }
        self.state = 'finished'
        self.current_player = None

    def begin_next_trick(self, winner_seat):
        if self.state != 'playing':
            return False
        self.current_trick = []
        self.current_player = winner_seat
        return True

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    def state_message(self, seat):
        return {
            'type': 'game-state',
            'roomId': self.room_id,
            'yourSeat': seat,
            'hostSeat': self.host_seat,
            'state': self.state,
            'players': {s: p['name'] for s, p in self.players.items()},
            'playerStatus': {s: p['connected'] for s, p in self.players.items()},
            'yourHand': [c.to_dict() for c in self.hands.get(seat, [])],
            'handCounts': self.hand_counts(),
            'currentTrick': [
                {'seat': s, 'card': c.to_dict()} for s, c in self.current_trick
            ],
            'currentPlayer': self.current_player,
            'trumpSuit': self.trump_suit,
            'dealerSeat': self.dealer_seat,
            'teams': self.teams,
            'roundNumber': self.round_number,
            'gameOver': self.game_over,
        }


# ----------------------------------------------------------------------
# Networking helpers
# ----------------------------------------------------------------------
async def send(ws, msg):
    if ws is None:
        return
    try:
        await ws.send_str(json.dumps(msg))
    except Exception:
        pass


async def broadcast(room_id, msg):
    game = ROOMS.get(room_id)
    if not game:
        return
    data = json.dumps(msg)
    for player in game.players.values():
        if player['ws'] is None:
            continue
        try:
            await player['ws'].send_str(data)
        except Exception:
            pass


async def leave_room(ws, session):
    """Explicitly remove this connection's seat from its current room."""
    room_id = session.get('room_id')
    seat = session.get('seat')
    session['room_id'] = None
    session['seat'] = None
    if not room_id or room_id not in ROOMS or seat is None:
        return
    game = ROOMS[room_id]
    player = game.players.get(seat)
    if not player or player['ws'] is not ws:
        return
    game.free_seat(seat)
    if not game.players:
        del ROOMS[room_id]
    else:
        await broadcast(room_id, {
            'type': 'room-updated',
            'players': game.get_players_info(),
            'hostSeat': game.host_seat,
        })


async def trick_pause(room_id, round_id, winner_seat):
    """Keep the completed trick on screen briefly, then start the next one."""
    await asyncio.sleep(TRICK_PAUSE)
    game = ROOMS.get(room_id)
    if not game or game.round_id != round_id:
        return
    if game.begin_next_trick(winner_seat):
        await broadcast(room_id, {
            'type': 'trick-ready',
            'currentPlayer': winner_seat,
            'handCounts': game.hand_counts(),
        })


async def monitor_disconnect(room_id, seat):
    """Announce a gone player, then free their seat / clean up the room."""
    await asyncio.sleep(DISCONNECT_ANNOUNCE)
    game = ROOMS.get(room_id)
    if not game or seat not in game.players:
        return
    player = game.players[seat]
    if player['connected']:
        return  # they came back (page reload / navigation) in time
    await broadcast(room_id, {
        'type': 'player-status',
        'seat': seat,
        'connected': False,
        'players': game.get_players_info(),
    })

    if game.state == 'waiting':
        await asyncio.sleep(WAITING_FREE)
        game = ROOMS.get(room_id)
        if not game or seat not in game.players:
            return
        player = game.players[seat]
        if player['connected']:
            return
        game.free_seat(seat)
        if not game.players:
            del ROOMS[room_id]
        else:
            await broadcast(room_id, {
                'type': 'room-updated',
                'players': game.get_players_info(),
                'hostSeat': game.host_seat,
            })
    else:
        if any(p['connected'] for p in game.players.values()):
            return
        # Nobody is connected to a running game: give them a while to come
        # back, then clean the room up.
        await asyncio.sleep(EMPTY_GAME_DELETE)
        game = ROOMS.get(room_id)
        if not game or any(p['connected'] for p in game.players.values()):
            return
        del ROOMS[room_id]


async def handle_disconnect(ws, session):
    room_id = session.get('room_id')
    seat = session.get('seat')
    session['room_id'] = None
    session['seat'] = None
    if not room_id or room_id not in ROOMS or seat is None:
        return
    game = ROOMS[room_id]
    player = game.players.get(seat)
    if not player or player['ws'] is not ws:
        return  # a newer connection already took over this seat
    player['ws'] = None
    player['connected'] = False
    asyncio.create_task(monitor_disconnect(room_id, seat))


def clean_name(raw):
    name = str(raw or '').strip()[:15]
    return name or 'Player'


# ----------------------------------------------------------------------
# WebSocket endpoint
# ----------------------------------------------------------------------
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    session = {'room_id': None, 'seat': None, 'name': 'Player'}

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue

            mt = data.get('type')
            room_id = session['room_id']
            game = ROOMS.get(room_id) if room_id else None
            seat = session['seat']

            if mt == 'create-room':
                if session['room_id']:
                    await leave_room(ws, session)
                name = clean_name(data.get('playerName'))
                room_id = ''.join(random.choices('ABCDEF0123456789', k=6)).upper()
                while room_id in ROOMS:
                    room_id = ''.join(random.choices('ABCDEF0123456789', k=6)).upper()
                game = Game(room_id)
                seat = game.add_player(ws, name)
                ROOMS[room_id] = game
                session['room_id'] = room_id
                session['seat'] = seat
                session['name'] = name
                await send(ws, {
                    'type': 'room-created',
                    'roomId': room_id,
                    'seat': seat,
                    'token': game.players[seat]['token'],
                    'hostSeat': game.host_seat,
                    'players': game.get_players_info(),
                })

            elif mt == 'join-room':
                wanted = str(data.get('roomId', '')).strip().upper()
                target = ROOMS.get(wanted)
                if target is None:
                    await send(ws, {'type': 'room-error', 'message': 'Room not found'})
                    continue
                if session['room_id']:
                    await leave_room(ws, session)
                name = clean_name(data.get('playerName'))
                seat = target.add_player(ws, name)
                if seat is None:
                    await send(ws, {'type': 'room-error', 'message': 'Room is full'})
                    continue
                session['room_id'] = wanted
                session['seat'] = seat
                session['name'] = name
                await send(ws, {
                    'type': 'room-joined',
                    'roomId': wanted,
                    'seat': seat,
                    'token': target.players[seat]['token'],
                    'hostSeat': target.host_seat,
                    'players': target.get_players_info(),
                })
                await broadcast(wanted, {
                    'type': 'room-updated',
                    'players': target.get_players_info(),
                    'hostSeat': target.host_seat,
                })

            elif mt == 'resume':
                wanted = str(data.get('roomId', ''))
                want_seat = data.get('seat')
                token = str(data.get('token') or '')
                target = ROOMS.get(wanted)
                player = None
                if target is not None and isinstance(want_seat, int) \
                        and not isinstance(want_seat, bool) and want_seat in target.players:
                    player = target.players[want_seat]
                if player is None or not secrets.compare_digest(player['token'], token):
                    await send(ws, {
                        'type': 'resume-failed',
                        'message': 'Room no longer exists or your session expired',
                    })
                    continue
                if session['room_id'] and session['room_id'] != wanted:
                    await leave_room(ws, session)
                was_connected = player['connected']
                player['ws'] = ws
                player['connected'] = True
                session['room_id'] = wanted
                session['seat'] = want_seat
                session['name'] = player['name']
                await send(ws, target.state_message(want_seat))
                if not was_connected:
                    await broadcast(wanted, {
                        'type': 'player-status',
                        'seat': want_seat,
                        'connected': True,
                        'players': target.get_players_info(),
                    })

            elif mt == 'start-game':
                if game is None or seat is None:
                    continue
                if game.host_seat != seat:
                    continue
                if not game.start_game():
                    await send(ws, {'type': 'room-error', 'message': '4 players are required to start'})
                    continue
                await broadcast(room_id, {
                    'type': 'game-started',
                    'roomId': room_id,
                    'roundNumber': game.round_number,
                })

            elif mt == 'cancel-game':
                if game is None or seat is None:
                    continue
                if game.host_seat != seat or game.state == 'waiting':
                    continue
                game.cancel_game()
                # Free seats of players who are already away, so a
                # replacement can join with the room code.
                for absent in [s for s, p in game.players.items() if not p['connected']]:
                    game.free_seat(absent)
                if not game.players:
                    del ROOMS[room_id]
                    session['room_id'] = None
                    session['seat'] = None
                    continue
                await broadcast(room_id, {
                    'type': 'room-updated',
                    'players': game.get_players_info(),
                    'hostSeat': game.host_seat,
                })
                await broadcast(room_id, {'type': 'game-cancelled'})

            elif mt == 'play-card':
                if game is None or seat is None:
                    continue
                result = game.play_card(seat, data.get('cardId'))
                if 'error' in result:
                    await send(ws, {'type': 'play-error', 'message': result['error']})
                    continue
                await send(ws, {
                    'type': 'your-hand',
                    'cards': [c.to_dict() for c in game.hands.get(seat, [])],
                })
                await broadcast(room_id, {
                    'type': 'card-played',
                    'seat': seat,
                    'card': result['card'],
                    'trick': result['trick'],
                    'currentPlayer': game.current_player,
                    'teams': game.teams,
                    'handCounts': game.hand_counts(),
                    'trumpSuit': game.trump_suit,
                    'trickComplete': result['trickComplete'],
                })
                if game.state == 'finished':
                    await broadcast(room_id, {
                        'type': 'game-over',
                        'winningTeam': game.game_over['winningTeam'],
                        'teams': game.teams,
                    })
                elif result['trickComplete']:
                    asyncio.create_task(trick_pause(
                        room_id, game.round_id, result['trickComplete']['winnerSeat']))

            elif mt == 'chat':
                if game is None:
                    continue
                text = str(data.get('message', ''))[:200]
                if not text.strip():
                    continue
                await broadcast(room_id, {
                    'type': 'chat-message',
                    'seat': session['seat'],
                    'playerName': session['name'],
                    'message': text,
                })

    except Exception as exc:  # noqa: BLE001 - keep one bad client from killing the server
        print(f'websocket error: {exc!r}')
    finally:
        await handle_disconnect(ws, session)

    return ws


def create_app():
    app = web.Application()
    app.router.add_get('/ws', websocket_handler)

    async def index(request):
        return web.FileResponse(STATIC_DIR / 'index.html')

    async def game_page(request):
        return web.FileResponse(STATIC_DIR / 'game.html')

    app.router.add_get('/', index)
    app.router.add_get('/game', game_page)
    app.router.add_static('/', STATIC_DIR, name='static')

    return app


async def main():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f'29 Card Game server running at http://localhost:{PORT}')
    print('Share this URL with friends on the same network to play!')
    print('Find your local IP with: hostname -I')
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
