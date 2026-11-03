"""Unit tests for the 29 game logic (trick winner, follow-suit, rounds, bidding)."""
import unittest

from server import Card, Game, card_beats


def make_game(trump='spades'):
    game = Game('TEST01')
    for seat in range(4):
        game.players[seat] = {
            'ws': None, 'name': f'P{seat}', 'token': 'tok', 'connected': True,
        }
    game.host_seat = 0
    game.state = 'playing'
    game.trump_suit = trump
    return game


def card(suit, rank):
    return Card(suit, rank)


class CardBeatsTests(unittest.TestCase):
    def test_higher_same_suit_wins(self):
        self.assertTrue(card_beats(card('hearts', 'A'), card('hearts', 'K'), 'spades'))
        self.assertFalse(card_beats(card('hearts', 'K'), card('hearts', 'A'), 'spades'))

    def test_trump_beats_led_suit(self):
        self.assertTrue(card_beats(card('spades', '7'), card('hearts', 'A'), 'spades'))

    def test_led_suit_never_beats_trump(self):
        self.assertFalse(card_beats(card('hearts', 'A'), card('spades', '7'), 'spades'))

    def test_off_suit_never_beats_led_suit(self):
        self.assertFalse(card_beats(card('diamonds', 'A'), card('hearts', '7'), 'spades'))


class TrickWinnerTests(unittest.TestCase):
    def resolve(self, plays, trump='spades'):
        game = make_game(trump)
        game.current_trick = [(seat, card(suit, rank)) for seat, suit, rank in plays]
        winner, points = game.complete_trick()
        return winner, points

    def test_trump_beats_later_led_suit_card(self):
        # Regression: a higher led-suit card played after a trump must NOT
        # overtake the trump.
        winner, _ = self.resolve([
            (0, 'hearts', 'A'),
            (1, 'spades', 'J'),
            (2, 'hearts', 'K'),
        ])
        self.assertEqual(winner, 1)

    def test_highest_led_suit_wins_without_trump(self):
        # Ranking is J > 9 > A > 10 > K > Q > 8 > 7 — the 9 beats the King.
        winner, _ = self.resolve([
            (0, 'hearts', '7'),
            (1, 'hearts', 'K'),
            (2, 'hearts', '9'),
            (3, 'clubs', 'A'),
        ])
        self.assertEqual(winner, 2)

    def test_trump_vs_trump_higher_wins(self):
        # Jack is top, then the 9 (J > 9 > A > 10 > K > Q > 8 > 7).
        winner, _ = self.resolve([
            (0, 'spades', '9'),
            (1, 'spades', 'J'),
            (2, 'spades', 'A'),
            (3, 'diamonds', 'K'),
        ])
        self.assertEqual(winner, 1)

    def test_points_are_summed(self):
        _, points = self.resolve([
            (0, 'hearts', 'A'),     # 1
            (1, 'clubs', 'K'),      # 0
            (2, 'diamonds', '10'),  # 1
            (3, 'spades', 'J'),     # 3  (trump, wins)
        ])
        self.assertEqual(points, 5)   # 28 card points in the whole deck

    def test_trick_points_go_to_winner_team(self):
        game = make_game()
        game.current_trick = [
            (0, card('hearts', 'A')),     # 1
            (1, card('clubs', 'K')),      # 0
            (2, card('diamonds', '10')),  # 1
            (3, card('spades', 'J')),     # 3, trump -> seat 3 -> team B
        ]
        winner, _ = game.complete_trick()
        self.assertEqual(winner, 3)
        self.assertEqual(game.teams['B']['points'], 5)
        self.assertEqual(game.teams['B']['tricks'], 1)
        self.assertEqual(game.teams['A']['points'], 0)


class PlayCardTests(unittest.TestCase):
    def test_not_your_turn(self):
        game = make_game()
        game.hands = {s: [card('hearts', str(r)) for r in ('7', '8', '9')] for s in range(4)}
        game.current_player = 2
        result = game.play_card(1, 0)
        self.assertIn('error', result)

    def test_follow_suit_enforced(self):
        game = make_game()
        game.hands = {
            0: [card('spades', 'A'), card('hearts', '7')],
            1: [card('clubs', 'K')],
            2: [card('diamonds', 'K')],
            3: [card('clubs', 'Q')],
        }
        game.current_player = 0
        game.current_trick = [(1, card('hearts', 'K'))]  # hearts was led

        # Holding a heart, playing spades is illegal.
        result = game.play_card(0, 0)
        self.assertIn('error', result)

        # Playing the heart is fine.
        result = game.play_card(0, 1)
        self.assertNotIn('error', result)
        self.assertEqual(result['card']['suit'], 'hearts')

    def test_may_dump_when_void(self):
        game = make_game()
        game.hands = {
            0: [card('spades', 'A')],
            1: [card('clubs', 'K')],
            2: [card('diamonds', 'K')],
            3: [card('clubs', 'Q')],
        }
        game.current_player = 0
        game.current_trick = [(1, card('hearts', 'K'))]
        result = game.play_card(0, 0)
        self.assertNotIn('error', result)

    def test_trick_completes_and_pauses(self):
        game = make_game()
        game.hands = {
            0: [card('hearts', '7'), card('clubs', '9')],
            1: [card('hearts', '8'), card('clubs', '10')],
            2: [card('hearts', '9'), card('diamonds', '7')],
            3: [card('spades', '7'), card('diamonds', '8')],
        }
        game.current_player = 0
        for seat in range(4):
            result = game.play_card(seat, 0)
            self.assertNotIn('error', result, msg=str(result))
        self.assertIsNotNone(result['trickComplete'])
        self.assertEqual(result['trickComplete']['winnerSeat'], 3)
        self.assertEqual(game.state, 'playing')  # cards left, round continues
        self.assertIsNone(game.current_player)  # paused between tricks
        self.assertTrue(game.begin_next_trick(3))
        self.assertEqual(game.current_player, 3)
        self.assertEqual(game.current_trick, [])

    def test_invalid_index(self):
        game = make_game()
        game.hands = {s: [card('hearts', '7')] for s in range(4)}
        game.current_player = 0
        self.assertIn('error', game.play_card(0, 5))
        self.assertIn('error', game.play_card(0, -1))
        self.assertIn('error', game.play_card(0, 'x'))


class BiddingTests(unittest.TestCase):
    def test_start_game_enters_bidding(self):
        game = Game('TEST02')
        for seat in range(4):
            game.add_player(None, f'P{seat}')
        self.assertTrue(game.start_game())
        self.assertEqual(game.state, 'bidding')
        self.assertEqual(len(game.hands[0]), 8)  # 8 cards dealt
        self.assertIsNotNone(game.dealer_seat)
        self.assertIsNotNone(game.current_player)  # first bidder

    def test_bidding_order_is_left_of_dealer(self):
        game = Game('TEST03')
        for seat in range(4):
            game.add_player(None, f'P{seat}')
        game.start_game()
        dealer = game.dealer_seat
        first_bidder = game.current_player
        expected = (dealer + 1) % 4
        self.assertEqual(first_bidder, expected)

    def test_place_bid(self):
        game = Game('TEST04')
        for seat in range(4):
            game.add_player(None, f'P{seat}')
        game.start_game()
        # Player 1 (first bidder) bids 20 points
        result = game.place_bid(1, 20)
        self.assertIn('ok', result)
        self.assertEqual(result['bid'], 20)
        self.assertEqual(game.highest_bid, (20, 1))

    def test_bid_must_be_higher(self):
        game = Game('TEST05')
        for seat in range(4):
            game.add_player(None, f'P{seat}')
        game.start_game()
        game.place_bid(1, 20)
        # Same player can't bid again, next player tries same bid
        result = game.place_bid(2, 20)  # wrong player
        self.assertIn('error', result)
        # Correct next player tries equal bid
        result = game.place_bid(2, 20)
        self.assertIn('error', result)
        # Higher bid works
        result = game.place_bid(2, 21)
        self.assertIn('ok', result)

    def test_pass_bid(self):
        game = Game('TEST06')
        for seat in range(4):
            game.add_player(None, f'P{seat}')
        game.start_game()
        result = game.place_bid(1, 0)  # pass
        self.assertIn('ok', result)
        self.assertIsNone(game.bids[1])

    def test_bid_must_be_in_range(self):
        game = Game('TEST11')
        for seat in range(4):
            game.add_player(None, f'P{seat}')
        game.start_game()
        bidder = game.current_player
        # Below the 16-point floor
        result = game.place_bid(bidder, 15)
        self.assertEqual(result.get('error'),
                         'Bid must be between 16 and 29 points')
        # Above the 29-point ceiling
        result = game.place_bid(bidder, 30)
        self.assertEqual(result.get('error'),
                         'Bid must be between 16 and 29 points')
        # A rejected bid doesn't advance the turn, so the floor works
        result = game.place_bid(bidder, 16)
        self.assertIn('ok', result)
        # and the ceiling is legal too on a fresh auction
        game2 = Game('TEST11b')
        for seat in range(4):
            game2.add_player(None, f'P{seat}')
        game2.start_game()
        self.assertIn('ok', game2.place_bid(game2.current_player, 29))

    def test_all_pass_redeals(self):
        game = Game('TEST07')
        for seat in range(4):
            game.add_player(None, f'P{seat}')
        game.start_game()
        dealer_before = game.dealer_seat
        # All four pass
        for _ in range(4):
            game.place_bid(game.current_player, 0)

        # Four passes: the cards are thrown in and dealt again,
        # and the deal rotates to the next seat.
        self.assertEqual(game.state, 'bidding')
        self.assertIsNone(game.highest_bid)
        self.assertEqual(game.dealer_seat, (dealer_before + 1) % 4)
        self.assertEqual(sum(len(h) for h in game.hands.values()), 32)

    def test_winning_bidder_selects_trump(self):
        game = Game('TEST08')
        for seat in range(4):
            game.add_player(None, f'P{seat}')
        game.start_game()
        # Advance through bidding: seat 1 bids 20, rest pass.
        game.place_bid(1, 20)
        while game.state == 'bidding':
            game.place_bid(game.current_player, 0)

        # State should be dealing2 (bidding complete, not all-pass)
        self.assertEqual(game.state, 'dealing2')
        self.assertEqual(game.winning_bid['bidder'], 1)
        self.assertIsNone(game.trump_suit)   # not declared yet

        # Winning bidder declares trump — must be a suit they hold
        suit = next(iter({c.suit for c in game.hands[1]}))
        result = game.set_trump_from_winners_bid(1, suit)
        self.assertIn('ok', result)
        self.assertEqual(game.state, 'playing')
        self.assertEqual(game.trump_suit, suit)

    def test_trump_must_be_in_hand(self):
        game = Game('TEST09')
        for seat in range(4):
            game.add_player(None, f'P{seat}')
        game.start_game()
        # Player 1 wins the auction
        game.place_bid(1, 20)
        # Try a trump suit they don't hold
        hand_suits = {c.suit for c in game.hands[1]}
        for s in Card.SUITS:
            if s not in hand_suits:
                result = game.set_trump_from_winners_bid(1, s)
                self.assertIn('error', result)
                break

    def test_state_message_during_bidding(self):
        game = Game('TEST10')
        for seat in range(4):
            game.add_player(None, f'P{seat}')
        game.start_game()
        msg = game.state_message(0)
        # fullHand should be None during bidding
        self.assertIsNone(msg['fullHand'])
        # yourHand should only have 4 cards
        self.assertEqual(len(msg['yourHand']), 4)
        # handCounts should report 4 per seat during bidding
        for s, c in msg['handCounts'].items():
            self.assertEqual(c, 4)
        # bids should be present
        self.assertIsNotNone(msg['bids'])

class RoundLifecycleTests(unittest.TestCase):
    def test_start_game_resets_scores(self):
        game = make_game()
        game.teams['A']['match'] = 5
        game.teams['A']['points'] = 24
        game.teams['A']['tricks'] = 7
        game.state = 'finished'
        game.game_over = {'winningTeam': 'A', 'teams': game.teams}

        self.assertTrue(game.start_game())
        self.assertEqual(game.state, 'bidding')  # Now enters bidding first
        # Match points carry over between rounds; the deal tallies reset.
        self.assertEqual(game.teams['A']['match'], 5)
        self.assertEqual(game.teams['A']['points'], 0)
        self.assertEqual(game.teams['A']['tricks'], 0)
        self.assertIsNone(game.game_over)
        self.assertEqual(sum(len(h) for h in game.hands.values()), 32)
        self.assertIsNone(game.trump_suit)  # Not set until after bidding
        self.assertIsNotNone(game.current_player)

    def test_start_game_needs_four_players(self):
        game = Game('TEST02')
        game.add_player(None, 'P0')
        self.assertFalse(game.start_game())

    def test_start_game_guard_mid_round(self):
        game = make_game()  # state == 'playing'
        self.assertFalse(game.start_game())  # blocked by guard

    def test_final_trick_ends_round_and_settles_contract(self):
        game = make_game()
        # Last trick of the deal; team A (seats 0,2) takes it with the Ace.
        game.hands = {
            0: [card('hearts', 'A')],
            1: [card('hearts', '7')],
            2: [card('hearts', '8')],
            3: [card('clubs', '9')],
        }
        game.current_player = 0
        game.highest_bid = (20, 0)          # team A bid 20 points
        game.winning_bid = {'bidder': 0, 'bid_value': 20, 'trump': 'spades'}
        for seat in range(4):
            result = game.play_card(seat, 0)
            self.assertNotIn('error', result, msg=str(result))

        self.assertEqual(game.state, 'finished')
        self.assertIsNotNone(game.game_over)
        # The Ace (1) + the 9 (2) = 3 card points, +1 for the last trick = 4.
        self.assertEqual(game.teams['A']['tricks'], 1)
        self.assertEqual(game.teams['A']['points'], 4)
        # 4 points is short of the 20-point contract, so the defenders score.
        self.assertEqual(game.game_over['bidMade'], False)
        self.assertEqual(game.game_over['bid'], 20)
        self.assertEqual(game.game_over['bidderPoints'], 4)
        self.assertEqual(game.teams['B']['match'], 1)
        self.assertEqual(game.teams['A']['match'], 0)
        # Match not over yet (first to 6), so no match winner.
        self.assertIsNone(game.game_over['winningTeam'])
        self.assertFalse(game.game_over['matchOver'])
        self.assertIsNone(game.current_player)
        # A finished game rejects further plays.
        self.assertIn('error', game.play_card(0, 0))

    def test_making_the_contract_scores_the_bidders(self):
        game = make_game()
        game.hands = {
            0: [card('spades', 'J')],   # trump, wins the trick
            1: [card('hearts', '7')],
            2: [card('hearts', '8')],
            3: [card('clubs', '9')],
        }
        game.current_player = 0
        game.highest_bid = (6, 0)
        game.winning_bid = {'bidder': 0, 'bid_value': 6, 'trump': 'spades'}
        for seat in range(4):
            self.assertNotIn('error', game.play_card(seat, 0), msg='play failed')

        # J=3 + 7=0 + 8=0 + 9=2 = 5 card points, +1 for the last trick = 6.
        self.assertEqual(game.teams['A']['points'], 6)
        self.assertEqual(game.teams['A']['tricks'], 1)
        # 6 >= the bid of 6, so the bidders score the match point.
        self.assertEqual(game.game_over['bidMade'], True)
        self.assertEqual(game.game_over['bidderPoints'], 6)
        self.assertEqual(game.teams['A']['match'], 1)
        self.assertEqual(game.teams['B']['match'], 0)

    def test_cancel_returns_to_waiting(self):
        game = make_game()
        game.state = 'waiting'  # allow start_game through the guard
        game.start_game()
        game.cancel_game()
        self.assertEqual(game.state, 'waiting')
        self.assertEqual(game.hands, {})
        self.assertIsNone(game.trump_suit)
        self.assertIsNone(game.game_over)
        self.assertEqual(game.teams['A']['match'], 0)
        self.assertEqual(game.teams['A']['points'], 0)


if __name__ == '__main__':
    unittest.main()
