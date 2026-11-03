"""29 Card Game – WebSocket server (v10 multiplayer).

The server manages rooms, seats, and broadcasts game state between
connected clients.  The game engine itself runs in the browser
(the v10 HTML file).
"""
import asyncio
import json
import random
import secrets
from pathlib import Path

from aiohttp import web

PORT = 3000
STATIC_DIR = Path(__file__).parent

# Timing knobs (seconds)
DISCONNECT_ANNOUNCE = 3
WAITING_FREE = 12
EMPTY_GAME_DELETE = 30


# ── Room / game state ──────────────────────────────────────────────

ROOMS = {}          # room_id -> dict   (see _make_room)

def _make_room(room_id, host_seat, host_ws, host_name):
    return {
        'id': room_id,
        'seats': [None, None, None, None],   # None or {'ws', 'name', 'token'}
        'host_seat': host_seat,
        'game_state': None,                   # None = in lobby; JSON string = running
        'game_id': 0,                          # increments on every deal
    }


def _room_info(room, ws):
    """Return a serialisable snapshot of the room."""
    seats = []
    for i, s in enumerate(room['seats']):
        if s is None:
            seats.append(None)
        else:
            # A seated player is "connected" if their ws is not None
            seated = s.get('ws') is not None
            seats.append({
                'seat': i,
                'name': s['name'],
                'connected': seated,
                'av': s.get('avatar', 0),
            })
    return {
        'roomId': room['id'],
        'hostSeat': room['host_seat'],
        'players': seats,
        'gameState': room['game_state'],
        'gameId': room['game_id'],
    }


async def _send(ws, msg):
    if ws is None:
        return
    try:
        await ws.send_str(json.dumps(msg))
    except Exception:
        pass


async def _broadcast(room, msg, except_ws=None):
    data = json.dumps(msg)
    for s in room['seats']:
        if s is None:
            continue
        target = s['ws']
        if target == except_ws or target is None:
            continue
        try:
            await target.send_str(data)
        except Exception:
            pass


# ── Disconnect / seat management ───────────────────────────────────

async def _monitor_disconnect(room, seat):
    await asyncio.sleep(DISCONNECT_ANNOUNCE)
    s = room['seats'][seat]
    if s is None or s['connected'] or s['ws'] is not None:
        return
    # Player still disconnected after delay
    await _broadcast(room, {
        'type': 'player-status',
        'seat': seat,
        'connected': False,
    })

    if room['game_state'] is not None:
        # Game is running – nobody can leave; just mark disconnected.
        return

    # Waiting in lobby: free the seat
    await asyncio.sleep(WAITING_FREE)
    s = room['seats'][seat]
    if s is None or s['connected'] or s['ws'] is not None:
        return
    room['seats'][seat] = None

    if not any(room['seats']):
        del ROOMS[room['id']]
        return

    host = room['host_seat']
    if room['seats'][host] is None:
        room['host_seat'] = next(i for i in range(4) if room['seats'][i] is not None)

    await _broadcast(room, {
        'type': 'room-updated',
        **_room_info(room, None),
    })


async def _handle_disconnect(room, seat, ws):
    s = room['seats'][seat]
    if s is None or s['ws'] is not ws:
        return
    s['ws'] = None
    s['connected'] = False
    asyncio.create_task(_monitor_disconnect(room, seat))


def _clean_name(raw):
    return str(raw or '').strip()[:16] or None


# ── WebSocket handler ──────────────────────────────────────────────

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
            room_id = session.get('room_id')
            room = ROOMS.get(room_id) if room_id else None
            seat = session.get('seat')

            # ── Create room ───────────────────────────────────────
            if mt == 'create-room':
                if room_id and room:
                    # Leave current room first
                    s = room['seats'].get(seat)
                    if s:
                        await _handle_disconnect(room, seat, ws)
                name = _clean_name(data.get('playerName')) or 'Player'
                rid = ''.join(random.choices('ABCDEF0123456789', k=6)).upper()
                while rid in ROOMS:
                    rid = ''.join(random.choices('ABCDEF0123456789', k=6)).upper()
                new_room = _make_room(rid, 0, ws, name)
                new_room['seats'][0] = {
                    'ws': ws, 'name': name,
                    'token': secrets.token_hex(4), 'connected': True,
                }
                ROOMS[rid] = new_room
                session['room_id'] = rid
                session['seat'] = 0
                session['name'] = name
                await _send(ws, {
                    'type': 'room-created',
                    **_room_info(new_room, ws),
                })

            # ── Join room ─────────────────────────────────────────
            elif mt == 'join-room':
                wanted = str(data.get('roomId', '')).strip().upper()
                target = ROOMS.get(wanted)
                if target is None:
                    # Room doesn't exist — create it (first person is host)
                    name = _clean_name(data.get('playerName')) or 'Player'
                    new_room = _make_room(wanted, 0, ws, name)
                    new_room['seats'][0] = {
                        'ws': ws, 'name': name,
                        'token': secrets.token_hex(4), 'connected': True,
                    }
                    ROOMS[wanted] = new_room
                    session['room_id'] = wanted
                    session['seat'] = 0
                    session['name'] = name
                    await _send(ws, {
                        'type': 'room-joined',
                        **_room_info(new_room, ws),
                    })
                else:
                    if room_id and room_id != wanted and room:
                        await _handle_disconnect(room, seat, ws)
                    name = _clean_name(data.get('playerName')) or 'Player'
                    # Respect team preference (0 = Team A [seats 0,2], 1 = Team B [seats 1,3])
                    team = data.get('team')
                    free_seat = None
                    if isinstance(team, int) and not isinstance(team, bool):
                        # Try preferred team seats first
                        team_seats = [0, 2] if team == 0 else [1, 3]
                        for i in team_seats:
                            if target['seats'][i] is None:
                                free_seat = i
                                break
                    # Fall back to any empty seat
                    if free_seat is None:
                        for i in range(4):
                            if target['seats'][i] is None:
                                free_seat = i
                                break
                    if free_seat is None:
                        await _send(ws, {'type': 'room-error', 'message': 'Room is full'})
                        continue
                    target['seats'][free_seat] = {
                        'ws': ws, 'name': name,
                        'token': secrets.token_hex(4), 'connected': True,
                    }
                    session['room_id'] = wanted
                    session['seat'] = free_seat
                    session['name'] = name
                    await _send(ws, {
                        'type': 'room-joined',
                        **_room_info(target, ws),
                    })
                    await _broadcast(target, {
                        'type': 'room-updated',
                        **_room_info(target, ws),
                    })

            # ── Resume session ────────────────────────────────────
            elif mt == 'resume':
                wanted = str(data.get('roomId', ''))
                want_seat = data.get('seat')
                token = str(data.get('token') or '')
                target = ROOMS.get(wanted)
                player = None
                if (target is not None
                        and isinstance(want_seat, int)
                        and not isinstance(want_seat, bool)
                        and want_seat in target['seats']
                        and target['seats'][want_seat] is not None):
                    player = target['seats'][want_seat]
                if player is None or not secrets.compare_digest(player['token'], token):
                    await _send(ws, {
                        'type': 'resume-failed',
                        'message': 'Room no longer exists or your session expired',
                    })
                    continue
                if room_id and room_id != wanted and room:
                    await _handle_disconnect(room, seat, ws)
                was_connected = player['ws'] is ws and player['connected']
                player['ws'] = ws
                player['connected'] = True
                session['room_id'] = wanted
                session['seat'] = want_seat
                session['name'] = player['name']
                # Send current room state + any running game state
                await _send(ws, {
                    'type': 'room-joined',
                    **_room_info(target, ws),
                })
                if not was_connected:
                    await _broadcast(target, {
                        'type': 'player-status',
                        'seat': want_seat,
                        'connected': True,
                    })

            # ── Host starts game ──────────────────────────────────
            elif mt == 'start-game':
                if room is None or seat is None:
                    continue
                if room['host_seat'] != seat:
                    continue
                if room['game_state'] is not None:
                    await _send(ws, {'type': 'room-error', 'message': 'A round is already in progress'})
                    continue
                if sum(1 for s in room['seats'] if s is not None) < 2:
                    await _send(ws, {'type': 'room-error', 'message': 'At least 2 players are required to start'})
                    continue
                room['game_state'] = ''       # empty string = v10 will initialise
                room['game_id'] += 1
                # Broadcast game-started to everyone (including host)
                msg = {'type': 'game-started', **_room_info(room, ws)}
                await _broadcast(room, msg)
                await _send(ws, msg)

            # ── Game state sync (v10) ─────────────────────────────
            # In v10 multiplayer, the host is the source of truth.
            # The host pushes game state changes; server broadcasts them.
            elif mt == 'game-state':
                if room is None or seat is None:
                    continue
                player = room['seats'][seat]
                if player is None:
                    continue
                # Only accept game state from the host
                if room['host_seat'] != seat:
                    continue
                if room['game_state'] is None:
                    await _send(ws, {'type': 'room-error', 'message': 'No game in progress'})
                    continue
                # Broadcast the full game state to everyone
                state_json = data.get('state', '')
                room['game_state'] = state_json
                room['game_id'] += 1
                await _broadcast(room, {
                    'type': 'game-state-sync',
                    'state': state_json,
                    **_room_info(room, ws),
                })

            # ── Cancel game (host only) ───────────────────────────
            elif mt == 'cancel-game':
                if room is None or seat is None:
                    continue
                if room['host_seat'] != seat:
                    continue
                if room['game_state'] is None:
                    continue
                room['game_state'] = None
                room['game_id'] += 1
                await _broadcast(room, {
                    'type': 'game-cancelled',
                    **_room_info(room, ws),
                })

            # ── Action relay (v10) ────────────────────────────────
            # Non-host players send game actions here.
            # Server forwards them to the host, who processes them.
            elif mt == 'action':
                if room is None or seat is None:
                    continue
                player = room['seats'][seat]
                if player is None:
                    continue
                # Forward to host's connection
                host_ws = room['seats'].get(room['host_seat'], {}).get('ws') if room.get('game_state') else None
                if host_ws:
                    await _send(host_ws, {
                        'type': 'relay-action',
                        'from': seat,
                        'action': data.get('action', ''),
                    })
                if room is None:
                    continue
                text = str(data.get('message', ''))[:200]
                if not text.strip():
                    continue
                await _broadcast(room, {
                    'type': 'chat-message',
                    'seat': seat,
                    'name': room['seats'][seat]['name'],
                    'message': text,
                })

            # ── Sit in lobby (v10 style) ──────────────────────────
            elif mt == 'sit':
                if room is None or seat is None:
                    continue
                target_seat = data.get('seat', seat)
                s = room['seats'][target_seat]
                if s is not None and s['ws'] is not ws:
                    await _send(ws, {'type': 'room-error', 'message': 'Seat taken'})
                    continue
                # If this player already has a seat, free it
                if room['seats'][seat] is not None and seat != target_seat:
                    room['seats'][seat] = None
                # If seat was empty, assign it
                if s is None:
                    room['seats'][target_seat] = {
                        'ws': ws, 'name': session['name'],
                        'token': secrets.token_hex(4), 'connected': True,
                    }
                # Update session to the new seat
                session['seat'] = target_seat
                await _send(ws, {
                    'type': 'room-updated',
                    **_room_info(room, ws),
                })
                await _broadcast(room, {
                    'type': 'room-updated',
                    **_room_info(room, ws),
                })

            # ── Avatar change ─────────────────────────────────────
            elif mt == 'set-avatar':
                if room is None or seat is None:
                    continue
                s = room['seats'][seat]
                if s is None:
                    continue
                s['avatar'] = data.get('avatar', 0)
                await _broadcast(room, {
                    'type': 'room-updated',
                    **_room_info(room, ws),
                })

    except Exception as exc:
        print(f'websocket error: {exc!r}')
    finally:
        if room and seat is not None:
            await _handle_disconnect(room, seat, ws)
        session['room_id'] = None
        session['seat'] = None

    return ws


# ── Middleware: no-cache for HTML ──────────────────────────────────

async def no_cache_middleware(app, handler):
    """Prevent browser caching so back-button always loads fresh page."""
    async def middleware(request):
        response = await handler(request)
        if hasattr(response, 'headers') and 'text/html' in (response.content_type or ''):
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
        return response
    return middleware


# ── Static files ───────────────────────────────────────────────────

async def index(request):
    resp = web.FileResponse(STATIC_DIR / '29_Card_Game_v10.html')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


def create_app():
    app = web.Application()
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/', index)
    app.router.add_static('/', STATIC_DIR, name='static')
    app.middlewares.append(no_cache_middleware)
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
