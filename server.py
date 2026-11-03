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
    # Card points: J=3, 9=2, A=1, 10=1, K/Q/8/7=0 → 28 total
    # (+1 for the last trick makes 29, which is where the name comes from)
    VALUES = {'J': 3, '9': 2, 'A': 1, '10': 1, 'K': 0, 'Q': 0, '8': 0, '7': 0}
    # Strength high → low: J, 9, A, 10, K, Q, 8, 7 (the Jack is the top card)
    STRENGTH = {'J': 13, '9': 12, 'A': 11, '10': 10, 'K': 9, 'Q': 8, '8': 7, '7': 6}

    # Bidding is in points, not tricks
    MIN_BID = 16
    MAX_BID = 29
    # First team to this many match points wins the match
    MATCH_TARGET = 6

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
        self.state = 'waiting'   # waiting | dealing | bidding | dealing2 | playing | finished
        self.trump_suit = None
        self.dealer_seat = None
        self.current_player = None
        self.current_trick = []  # [(seat, Card)]
        self._last_trick_winner = None
        self.teams = {'A': self._fresh_round(), 'B': self._fresh_round()}
        self.round_number = 0
        self.round_id = 0        # invalidates pending trick-pause tasks
        self.host_seat = None
        self.game_over = None    # {'winningTeam': str|None, 'teams': {...}}
        # Bidding state
        self.bids = {}           # seat -> int (or None if passed)
        self.bid_order = []      # list of seat ids in bidding order
        self.highest_bid = None  # (bid_value, seat)
        self.winning_bid = None  # {'bidder': seat, 'bid_value': int, 'trump': suit}

    @staticmethod
    def _fresh_round():
        """Per-round tallies. `match` persists across rounds (it is the score)."""
        return {'match': 0, 'points': 0, 'tricks': 0}

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
                    'name': name or f'Player {seat + 1}',
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
    def _deal_round(self):
        """Shuffle, deal all 8 cards each, and open the bidding."""
        self.round_id += 1
        self.state = 'bidding'
        self.bids = {}
        self.highest_bid = None
        self.current_bidder = 0
        self.winning_bid = None
        self.trump_suit = None
        self.current_trick = []
        self._last_trick_winner = None
        self.game_over = None
        # Deal all 32 cards at once (first 4 revealed for bidding, last 4 later)
        deck = Card.create_deck()
        random.shuffle(deck)
        for seat in range(4):
            if seat in self.players:
                self.hands[seat] = deck[seat * 8:(seat + 1) * 8]
        # Bidding order: left of dealer goes first
        self.bid_order = [
            (self.dealer_seat + i) % 4
            for i in range(1, 5)
        ]
        self.current_player = self.bid_order[0]

    def _rotate_dealer(self):
        if self.dealer_seat is None:
            self.dealer_seat = 0          # first round is deterministic
        else:
            self.dealer_seat = (self.dealer_seat + 1) % 4   # rotate

    def _reset_round_tallies(self):
        # Match points carry over between rounds; round tallies reset.
        for team in self.teams.values():
            team['points'] = 0
            team['tricks'] = 0

    def start_game(self):
        """Start a new round: deal 4 cards to each player, then enter bidding."""
        if len(self.players) != 4:
            return False
        if self.state not in ('waiting', 'finished'):
            return False
        self.round_number += 1
        self._reset_round_tallies()
        self._rotate_dealer()
        self._deal_round()
        return True

    def redeal(self):
        """All four passed: throw the cards in and deal again (dealer advances)."""
        self.round_number += 1
        self._reset_round_tallies()
        self._rotate_dealer()
        self._deal_round()

    def cancel_game(self):
        """Host abort: everyone goes back to the lobby."""
        self.state = 'waiting'
        self.hands = {}
        self.current_trick = []
        self.trump_suit = None
        # dealer_seat is deliberately kept so rotation continues next round
        self.current_player = None
        # Cancelled match: reset the score entirely
        self.teams = {'A': self._fresh_round(), 'B': self._fresh_round()}
        self.game_over = None
        self.bids = {}
        self.highest_bid = None
        self.winning_bid = None
        self.current_bidder = 0
        self.round_id += 1

    def place_bid(self, seat, bid_value):
        """
        Place a bid during the bidding phase.

        A bid is a number of card points (Card.MIN_BID..Card.MAX_BID) that the
        bidder's team promises to capture. 0 means pass. The winner declares
        the trump suit afterwards, in the dealing2 phase.
        """
        if self.state != 'bidding':
            return {'error': 'Not in bidding phase'}
        if seat != self.current_player:
            return {'error': 'Not your turn'}

        try:
            bid_value = int(bid_value)
        except (TypeError, ValueError):
            return {'error': 'Invalid bid'}

        if bid_value == 0:
            self.bids[seat] = None  # passed
        else:
            if bid_value < Card.MIN_BID or bid_value > Card.MAX_BID:
                return {
                    'error': f'Bid must be between {Card.MIN_BID} and '
                             f'{Card.MAX_BID} points'
                }
            if self.highest_bid is not None:
                current_bid, _ = self.highest_bid
                if bid_value <= current_bid:
                    return {'error': f'Bid must be higher than {current_bid}'}
            self.bids[seat] = bid_value
            self.highest_bid = (bid_value, seat)

        # Move to next bidder
        self.current_bidder += 1
        if self.current_bidder >= len(self.bid_order):
            # Bidding round complete
            return self._finalize_bidding()

        self.current_player = self.bid_order[self.current_bidder]

        return {
            'ok': True,
            'seat': seat,
            'bid': bid_value,
            'highestBid': self.highest_bid[0] if self.highest_bid else None,
            'nextBidder': self.current_player,
        }

    def _finalize_bidding(self):
        """All players have bid: pick a winner, or redeal if everybody passed."""
        if self.highest_bid is None:
            # Standard rule: four passes means the cards are thrown in.
            # Do it here so the Game is always left in a consistent state,
            # whoever is driving (handler, test, ...).
            self.redeal()
            return {'redeal': True}

        winner = self.highest_bid[1]
        # trump stays None until the winner declares it in dealing2.
        self.winning_bid = {
            'bidder': winner,
            'bid_value': self.highest_bid[0],
            'trump': None,
        }

        # State: dealing2 → winning bidder picks trump from their hand
        self.state = 'dealing2'
        self.current_player = winner

        return {'roundComplete': True, 'winningBid': self.winning_bid}

    def set_trump_from_winners_bid(self, seat, trump_suit):
        """
        The winning bidder selects their trump suit from their hand.
        Called after trump selection phase begins.
        """
        if self.state != 'dealing2':
            return {'error': "Not in trump selection phase"}
        if seat != self.current_player:
            return {'error': "Not your turn"}
        if trump_suit not in Card.SUITS:
            return {'error': 'Invalid trump suit'}
        hand_suits = {c.suit for c in self.hands[seat]}
        if trump_suit not in hand_suits:
            return {'error': 'You must choose a suit you hold'}

        self.trump_suit = trump_suit
        self.state = 'playing'
        self.current_player = seat  # Winner leads first
        return {'ok': True, 'trumpSuit': trump_suit}

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
                return {'error': f'You must follow {led_suit}'}

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
        self.teams[team]['points'] += points
        self.teams[team]['tricks'] += 1
        self._last_trick_winner = winner_seat
        return winner_seat, points

    def _finish_round(self):
        """Count the deal up, settle the contract, and award match points."""
        # The last trick is worth an extra point: 28 card points + 1 = 29,
        # which is where the name of the game comes from.
        if self._last_trick_winner is not None:
            last_team = 'A' if self._last_trick_winner % 2 == 0 else 'B'
            self.teams[last_team]['points'] += 1

        bidder = self.winning_bid['bidder'] if self.winning_bid else None
        bidder_team = 'A' if (bidder or 0) % 2 == 0 else 'B'
        defender_team = 'B' if bidder_team == 'A' else 'A'
        bid = self.highest_bid[0] if self.highest_bid else 0
        bidder_points = self.teams[bidder_team]['points']
        made = bidder_points >= bid

        # Made the contract -> bidders score. Missed it -> defenders score.
        if made:
            self.teams[bidder_team]['match'] += 1
        else:
            self.teams[defender_team]['match'] += 1

        match_over = any(t['match'] >= Card.MATCH_TARGET for t in self.teams.values())
        winner = None
        if match_over:
            winner = 'A' if self.teams['A']['match'] >= Card.MATCH_TARGET else 'B'

        self.game_over = {
            'winningTeam': winner,       # match winner (None while match continues)
            'matchOver': match_over,
            'bid': bid,
            'bidderSeat': bidder,
            'bidderTeam': bidder_team,
            'bidMade': made,
            'bidderPoints': bidder_points,
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
        your_hands = self.hands.get(seat, [])
        # In bidding phase, players see their first 4 cards; opponents see card counts
        first_half = [c.to_dict() for c in your_hands[:4]] if self.state == 'bidding' else your_hands
        counts = {s: min(4, len(h)) if self.state == 'bidding' else len(h)
                  for s, h in self.hands.items()}
        return {
            'type': 'game-state',
            'roomId': self.room_id,
            'yourSeat': seat,
            'hostSeat': self.host_seat,
            'state': self.state,
            'players': {s: p['name'] for s, p in self.players.items()},
            'playerStatus': {s: p['connected'] for s, p in self.players.items()},
            'yourHand': first_half,
            'fullHand': None if self.state == 'bidding' else [c.to_dict() for c in your_hands],
            'handCounts': counts,
            'currentTrick': [
                {'seat': s, 'card': c.to_dict()} for s, c in self.current_trick
            ],
            'currentPlayer': self.current_player,
            'trumpSuit': self.trump_suit,
            'dealerSeat': self.dealer_seat,
            'teams': self.teams,
            'roundNumber': self.round_number,
            'gameOver': self.game_over,
            # Bidding info
            'bids': self.bids if self.state == 'bidding' else None,
            'highestBid': self.highest_bid[0] if self.highest_bid else None,
            'nextBidder': self.current_player if self.state == 'bidding' else None,
            'winningBid': self.winning_bid if self.state in ('dealing2', 'playing') else None,
            'minBid': Card.MIN_BID,
            'maxBid': Card.MAX_BID,
            'matchTarget': Card.MATCH_TARGET,
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


async def send_full_state(room_id, game):
    """Send a fresh game-state to every player in the room."""
    for s in game.players:
        await send(game.players[s]['ws'], game.state_message(s))


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
    game = ROOMS.get(room_id)
    # If the round is already finished (last trick completed), skip the pause.
    # The client already received the game-over message.
    if game and game.state != 'playing':
        return
    await asyncio.sleep(TRICK_PAUSE)
    game = ROOMS.get(room_id)
    if not game or game.round_id != round_id or game.state != 'playing':
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
    """Trim and limit a player-supplied name. May return '' — add_player
    then assigns a unique 'Player N' so nobody is nameless."""
    return str(raw or '').strip()[:15]


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
                session['name'] = game.players[seat]['name']
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
                session['name'] = target.players[seat]['name']
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
                if game.state not in ('waiting', 'finished'):
                    await send(ws, {'type': 'room-error', 'message': 'A round is already in progress'})
                    continue
                if not game.start_game():
                    await send(ws, {'type': 'room-error', 'message': '4 players are required to start'})
                    continue
                await broadcast(room_id, {
                    'type': 'game-started',
                    'roomId': room_id,
                    'roundNumber': game.round_number,
                })

            elif mt == 'bid':
                # Player places a bid (in card points) during the bidding phase
                if game is None or seat is None:
                    continue
                bid_val = data.get('bidValue', 0)
                result = game.place_bid(seat, bid_val)
                if 'error' in result:
                    await send(ws, {'type': 'play-error', 'message': result['error']})
                    continue
                if result.get('redeal'):
                    # Four passes: throw the cards in and deal again.
                    await broadcast(room_id, {
                        'type': 'redeal',
                        'message': 'All four passed — redeal',
                    })
                    await send_full_state(room_id, game)
                elif result.get('roundComplete'):
                    # Bidding is done — deal remaining cards to all players
                    await broadcast(room_id, {
                        'type': 'bidding-complete',
                        'winningBid': result['winningBid'],
                        'players': game.get_players_info(),
                    })
                    # Each player gets their full 8 cards
                    for s in game.players:
                        await send(game.players[s]['ws'], {
                            'type': 'your-hand',
                            'cards': [c.to_dict() for c in game.hands.get(s, [])],
                        })
                    await send_full_state(room_id, game)
                else:
                    await broadcast(room_id, {
                        'type': 'bid-placed',
                        'seat': result['seat'],
                        'bid': result['bid'],
                        'highestBid': result['highestBid'],
                        'nextBidder': result['nextBidder'],
                        'bids': game.bids,
                    })

            elif mt == 'select-trump':
                # Winning bidder selects their trump suit
                if game is None or seat is None:
                    continue
                trump = data.get('trumpSuit')
                result = game.set_trump_from_winners_bid(seat, trump)
                if 'error' in result:
                    await send(ws, {'type': 'play-error', 'message': result['error']})
                    continue
                await broadcast(room_id, {
                    'type': 'trump-selected',
                    'trumpSuit': result['trumpSuit'],
                    'currentPlayer': game.current_player,
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
                        **game.game_over,
                    })
                elif result['trickComplete']:
                    # Only schedule trick_pause if the round isn't finished.
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
