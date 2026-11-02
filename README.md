# 29 Card Game (دەستە)

Multiplayer trick-taking card game for 4 players over local network/WiFi.

## How to Play

1. **Host**: Open http://localhost:3000, enter your name, click "Create Room"
2. **Share** the 6-character room code with your friends
3. **Join**: Friends open the same URL, enter their name + room code, click "Join Room"
4. **Start**: Once 4 players are in, host clicks "Start Game"
5. **Play**: Take turns clicking cards to play tricks

## The Game

- 32-card deck (7 through Ace in each suit)
- 4 players, 2 teams (partners sit opposite)
- Trump suit is randomly determined
- First team to reach 29 points wins!
- Card values: A=11, 10=10, K=4, Q=3, J=2

## Run

```bash
python3 server.py
```

Open http://localhost:3000 in your browser.
