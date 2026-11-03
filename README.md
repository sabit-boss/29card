# 29 Card Game (دەستە)

Multiplayer trick-taking card game for 4 players over local network/WiFi.

## How to Play

1. **Host**: Open http://localhost:3000, enter your name, click "Create Room"
2. **Share** the 6-character room code with your friends
3. **Join**: Friends open the same URL, enter their name + room code, click "Join Room"
4. **Start**: Once 4 players are in, host clicks "Start Game"
5. **Bidding**: Each player bids in **points (16–29)** with their first 4 cards, or passes. The highest bidder wins the auction.
6. **Trump Selection**: The winning bidder declares a trump suit from their hand.
7. **Play**: Take turns clicking cards to play tricks. You must follow the led suit if you can.

## The Game

- 32-card deck (7 through Ace in each suit), ranked **J > 9 > A > 10 > K > Q > 8 > 7**
- 4 players, 2 teams (partners sit opposite)
- **Bidding phase**: Deal 4 cards → each player bids points (16–29) or passes → remaining 4 cards dealt → winner declares trump
- If all four pass, the cards are thrown in and redealt (the deal rotates to the next seat)
- **Card points**: J=3, 9=2, A=1, 10=1, K/Q/8/7=0 → **28 points** in the deck
- The team taking the **last trick** earns +1 point → **29 points** in play (which is where the name comes from)
- **Contract**: the bidding team must take at least the points it bid
  - Made → bidding team scores **+1 match point**
  - Missed → defending team scores **+1 match point**
- **First team to +6 match points wins the match**

## Run

```bash
python3 server.py
```

Open http://localhost:3000 in your browser.
